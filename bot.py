import os
import sqlite3
import re
import unicodedata
import asyncio
import html
import time
import traceback
import threading
from uuid import uuid4
from threading import Thread

from flask import Flask, jsonify, redirect

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.error import RetryAfter, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

from gdrive_sync import download_db, upload_db, get_db_link

# =========================
# KEEP ALIVE
# =========================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "OK"

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

MAIN_SOURCE_GROUP = -1002716112699
MAIN_SOURCE_TOPIC = 5552

WEB_URL = os.getenv("WEB_URL", "http://localhost:8080")

DB_DIR = "/app/data"
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "music.db")

# Try to restore the latest DB from Google Drive before opening it
download_db(DB_PATH)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = db.cursor()

# =========================
# DATABASE
# =========================
cur.executescript("""
CREATE TABLE IF NOT EXISTS songs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    message_id INTEGER,
    source_group_id INTEGER,
    source_username TEXT
);

CREATE TABLE IF NOT EXISTS topic_links(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT,
    link TEXT
);

CREATE TABLE IF NOT EXISTS aliases(
    song_id INTEGER,
    alias TEXT
);

CREATE TABLE IF NOT EXISTS allowed_groups(
    group_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sources(
    group_id INTEGER,
    topic_id INTEGER,
    UNIQUE(group_id, topic_id)
);

CREATE TABLE IF NOT EXISTS top_searches(
    query TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlists(
    user_id INTEGER,
    song_id INTEGER,
    PRIMARY KEY(user_id, song_id)
);

CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY
);
""")
db.commit()

# Migration: old topic_links had `keyword TEXT PRIMARY KEY` (1 link per keyword).
# New schema allows multiple links per keyword via an `id` autoincrement PK.
cur.execute("PRAGMA table_info(topic_links)")
_cols = cur.fetchall()
_has_id = any(c[1] == "id" for c in _cols)
if _cols and not _has_id:
    cur.executescript("""
        ALTER TABLE topic_links RENAME TO topic_links_old;
        CREATE TABLE topic_links(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT,
            link TEXT
        );
        INSERT INTO topic_links(keyword, link) SELECT keyword, link FROM topic_links_old;
        DROP TABLE topic_links_old;
    """)
    db.commit()
    print("[MIGRATION] topic_links upgraded to multi-link schema")

_sync_flag = threading.Event()
_sync_worker_started = False
_sync_worker_lock = threading.Lock()

def _sync_worker():
    """Single long-lived background worker. Wakes up when sync_db() is called,
    waits briefly to debounce bursts of consecutive calls, then uploads once."""
    while True:
        _sync_flag.wait()
        _sync_flag.clear()
        # Debounce: absorb a burst of consecutive sync_db() calls into one upload
        time.sleep(15)
        _sync_flag.clear()
        try:
            upload_db(DB_PATH)
        except Exception as e:
            print("[GDRIVE] sync_db error:", e)

def sync_db():
    """Request a Drive sync. Non-blocking; debounced and runs on a single shared thread."""
    global _sync_worker_started
    with _sync_worker_lock:
        if not _sync_worker_started:
            Thread(target=_sync_worker, daemon=True).start()
            _sync_worker_started = True
    _sync_flag.set()

# =========================
# HELPERS
# =========================
def is_owner(update: Update):
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

def add_admin(uid):
    cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (uid,))
    db.commit()
    sync_db()

def remove_admin(uid):
    cur.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    deleted = cur.rowcount > 0
    db.commit()
    if deleted:
        sync_db()
    return deleted

def get_admins():
    cur.execute("SELECT user_id FROM admins ORDER BY user_id")
    return [r[0] for r in cur.fetchall()]

def is_admin(update: Update):
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return False
    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,))
    return cur.fetchone() is not None

def is_owner_or_admin(update: Update):
    return is_owner(update) or is_admin(update)

def clean_name(text: str):
    if not text:
        return ""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace(".mp3", "")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

async def delete_later(msg, delay=60):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

def build_stream_link(username, message_id):
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"

def pretty_title(text: str):
    return f"🎵 {text}" if text else "🎵"

def pretty_text(s: str):
    return html.escape(s or "")

def build_track_message(query, rows, total, page=0):
    start = page * 5
    lines = [
        f"🎧 <b>KẾT QUẢ CHO:</b> <code>{pretty_text(query)}</code>",
        f"📦 <b>Tổng:</b> {total} bài",
        f"📄 <b>Trang:</b> {page + 1}",
        "",
    ]

    keyboard = []
    for i, (sid, name, mid, user) in enumerate(rows, start + 1):
        link = build_stream_link(user, mid)
        if link:
            lines.append(f"<b>{i}.</b> {pretty_text(name)}\n🔗 <a href=\"{html.escape(link)}\">Nghe bài này</a>")
        else:
            lines.append(f"<b>{i}.</b> {pretty_text(name)}")

        row_buttons = [
            InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}")
        ]
        if link:
            row_buttons.append(InlineKeyboardButton("🎧 Nghe", url=link))
        keyboard.append(row_buttons)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"page|{page-1}|{query}"))
    if start + 5 < total:
        nav.append(InlineKeyboardButton("Sau ➡️", callback_data=f"page|{page+1}|{query}"))
    if nav:
        keyboard.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard) if keyboard else None

def build_pagination_buttons(page, total, search_query, limit=5):
    keyboard = []

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Trang trước", callback_data=f"page|{page-1}|{search_query}")
        )

    if (page + 1) * limit < total:
        nav.append(
            InlineKeyboardButton("Trang sau ➡️", callback_data=f"page|{page+1}|{search_query}")
        )

    if nav:
        keyboard.append(nav)

    return keyboard

# =========================
# TOPIC LINKS
# =========================
def add_topic_link(keyword, link):
    keyword = clean_name(keyword)
    cur.execute(
        "INSERT OR REPLACE INTO topic_links(keyword, link) VALUES (?, ?)",
        (keyword, link),
    )
    db.commit()
    sync_db()

def get_topic_link(keyword):
    keyword = clean_name(keyword)
    if not keyword:
        return None

    # Exact match first
    cur.execute("SELECT link FROM topic_links WHERE keyword=?", (keyword,))
    row = cur.fetchone()
    if row:
        return row[0]

    # Fuzzy match: keyword contains a saved topic keyword, or vice versa
    cur.execute("SELECT keyword, link FROM topic_links")
    for kw, link in cur.fetchall():
        if kw and (kw in keyword or keyword in kw):
            return link

    return None

def get_topic_links(keyword):
    """Return all (keyword, link) topic entries matching the search query."""
    keyword = clean_name(keyword)
    if not keyword:
        return []

    cur.execute("SELECT keyword, link FROM topic_links")
    all_rows = cur.fetchall()

    matches = []
    seen_links = set()

    # Exact matches first
    for kw, link in all_rows:
        if kw == keyword and link not in seen_links:
            matches.append((kw, link))
            seen_links.add(link)

    # Then fuzzy matches
    for kw, link in all_rows:
        if kw != keyword and kw and (kw in keyword or keyword in kw) and link not in seen_links:
            matches.append((kw, link))
            seen_links.add(link)

    return matches

# =========================
# GROUP / SOURCE SYSTEM
# =========================
def add_allowed_group(gid):
    cur.execute("INSERT OR IGNORE INTO allowed_groups(group_id) VALUES (?)", (gid,))
    db.commit()
    sync_db()

def remove_allowed_group(gid):
    cur.execute("DELETE FROM allowed_groups WHERE group_id=?", (gid,))
    db.commit()
    sync_db()

def get_allowed_groups():
    cur.execute("SELECT group_id FROM allowed_groups ORDER BY group_id")
    return [x[0] for x in cur.fetchall()]

def is_allowed_group(gid):
    if gid == MAIN_SOURCE_GROUP:
        return True
    cur.execute("SELECT 1 FROM allowed_groups WHERE group_id=?", (gid,))
    return cur.fetchone() is not None

def add_source(gid, tid=None):
    cur.execute(
        "INSERT OR IGNORE INTO sources(group_id, topic_id) VALUES (?, ?)",
        (gid, tid),
    )
    db.commit()
    sync_db()

def remove_source(gid, tid=None):
    cur.execute(
        "DELETE FROM sources WHERE group_id=? AND (topic_id=? OR (topic_id IS NULL AND ? IS NULL))",
        (gid, tid, tid),
    )
    db.commit()
    sync_db()

def is_source(gid, tid):
    if gid == MAIN_SOURCE_GROUP and tid == MAIN_SOURCE_TOPIC:
        return True
    cur.execute(
        "SELECT 1 FROM sources WHERE group_id=? AND (topic_id=? OR (topic_id IS NULL AND ? IS NULL))",
        (gid, tid, tid),
    )
    return cur.fetchone() is not None

def get_sources():
    cur.execute("SELECT group_id, topic_id FROM sources ORDER BY group_id, topic_id")
    return cur.fetchall()

# =========================
# SONG SYSTEM
# =========================
def song_exists(name, gid):
    cur.execute(
        "SELECT 1 FROM songs WHERE name=? AND source_group_id=? LIMIT 1",
        (name, gid),
    )
    return cur.fetchone() is not None

def save_song(name, msg_id, gid, username):
    if song_exists(name, gid):
        return False
    cur.execute(
        "INSERT INTO songs(name, message_id, source_group_id, source_username) VALUES (?,?,?,?)",
        (name, msg_id, gid, username),
    )
    db.commit()
    sync_db()
    return True

def add_alias(song_id, alias):
    alias = clean_name(alias)
    if not alias:
        return False
    cur.execute("SELECT 1 FROM aliases WHERE song_id=? AND alias=?", (song_id, alias))
    if cur.fetchone():
        return False
    cur.execute("INSERT INTO aliases(song_id, alias) VALUES (?, ?)", (song_id, alias))
    db.commit()
    sync_db()
    return True

def remove_alias(song_id, alias):
    alias = clean_name(alias)
    cur.execute("DELETE FROM aliases WHERE song_id=? AND alias=?", (song_id, alias))
    deleted = cur.rowcount > 0
    db.commit()
    if deleted:
        sync_db()
    return deleted

def get_aliases(song_id):
    cur.execute("SELECT alias FROM aliases WHERE song_id=? ORDER BY alias", (song_id,))
    return [r[0] for r in cur.fetchall()]

def get_song_by_id(song_id):
    cur.execute("SELECT id, name, message_id, source_username FROM songs WHERE id=?", (song_id,))
    return cur.fetchone()

def search_songs(query, offset=0, limit=10):
    q = clean_name(query)
    cur.execute("""
        SELECT DISTINCT s.id, s.name, s.message_id, s.source_username
        FROM songs s
        LEFT JOIN aliases a ON s.id = a.song_id
        WHERE s.name LIKE ? OR a.alias LIKE ?
        ORDER BY s.id DESC
        LIMIT ? OFFSET ?
    """, (f"%{q}%", f"%{q}%", limit, offset))
    return cur.fetchall()

def count_songs(query):
    q = clean_name(query)
    cur.execute("""
        SELECT COUNT(DISTINCT s.id)
        FROM songs s
        LEFT JOIN aliases a ON s.id = a.song_id
        WHERE s.name LIKE ? OR a.alias LIKE ?
    """, (f"%{q}%", f"%{q}%"))
    return cur.fetchone()[0]

def log_search(query):
    q = clean_name(query)
    cur.execute("INSERT OR IGNORE INTO top_searches(query, count) VALUES (?, 0)", (q,))
    cur.execute("UPDATE top_searches SET count = count + 1 WHERE query = ?", (q,))
    db.commit()

def add_to_playlist(uid, sid):
    cur.execute("INSERT OR IGNORE INTO playlists(user_id, song_id) VALUES (?, ?)", (uid, sid))
    db.commit()

# =========================
# DASHBOARD
# =========================
@flask_app.route("/api/stats")
def api_stats():
    cur.execute("SELECT COUNT(*) FROM songs")
    songs = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM playlists")
    playlists = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM top_searches")
    searches = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sources")
    sources = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM allowed_groups")
    groups = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM topic_links")
    topics = cur.fetchone()[0]

    return jsonify({
        "songs": songs,
        "playlists": playlists,
        "searches": searches,
        "sources": sources,
        "groups": groups,
        "topics": topics,
    })

@flask_app.route("/api/top")
def api_top():
    cur.execute("SELECT query, count FROM top_searches ORDER BY count DESC LIMIT 10")
    rows = cur.fetchall()
    return jsonify([{"query": q, "count": c} for q, c in rows])

@flask_app.route("/dashboard")
def dashboard():
    return """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Music Bot Dashboard</title>
<style>
    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #0f172a;
        color: #e2e8f0;
    }
    .wrap {
        max-width: 1100px;
        margin: 0 auto;
        padding: 24px;
    }
    h1 {
        margin: 0 0 8px;
        color: #38bdf8;
        font-size: 32px;
    }
    .sub {
        margin-bottom: 24px;
        color: #94a3b8;
    }
    .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin-bottom: 24px;
    }
    .card {
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 18px;
        padding: 18px;
        box-shadow: 0 10px 30px rgba(0,0,0,.22);
    }
    .num {
        font-size: 34px;
        font-weight: 700;
        margin: 0;
        color: #f8fafc;
    }
    .label {
        margin: 6px 0 0;
        color: #94a3b8;
    }
    .panel {
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 18px;
        padding: 18px;
        box-shadow: 0 10px 30px rgba(0,0,0,.22);
    }
    .panel h2 {
        margin: 0 0 12px;
        color: #f8fafc;
        font-size: 20px;
    }
    ul {
        margin: 0;
        padding-left: 20px;
    }
    li {
        margin: 8px 0;
    }
    .small {
        color: #94a3b8;
        font-size: 13px;
        margin-top: 14px;
    }
</style>
</head>
<body>
    <div class="wrap">
        <h1>🎵 Music Bot Dashboard</h1>
        <div class="sub">Cập nhật tự động mỗi 3 giây</div>

        <div class="grid">
            <div class="card"><p class="num" id="songs">0</p><p class="label">Bài hát</p></div>
            <div class="card"><p class="num" id="playlists">0</p><p class="label">Playlist</p></div>
            <div class="card"><p class="num" id="searches">0</p><p class="label">Từ khóa tìm</p></div>
            <div class="card"><p class="num" id="sources">0</p><p class="label">Sources</p></div>
            <div class="card"><p class="num" id="groups">0</p><p class="label">Group được cấp quyền</p></div>
            <div class="card"><p class="num" id="topics">0</p><p class="label">Topics</p></div>
        </div>

        <div class="panel">
            <h2>🔥 Top tìm kiếm</h2>
            <ul id="toplist"></ul>
            <div class="small">Tự refresh theo dữ liệu SQLite của bot.</div>
        </div>
    </div>

<script>
async function loadStats(){
    const s = await fetch('/api/stats').then(r => r.json());
    document.getElementById('songs').innerText = s.songs;
    document.getElementById('playlists').innerText = s.playlists;
    document.getElementById('searches').innerText = s.searches;
    document.getElementById('sources').innerText = s.sources;
    document.getElementById('groups').innerText = s.groups;
    document.getElementById('topics').innerText = s.topics;

    const top = await fetch('/api/top').then(r => r.json());
    const ul = document.getElementById('toplist');
    ul.innerHTML = '';
    top.forEach(item => {
        const li = document.createElement('li');
        li.textContent = `${item.query} (${item.count} lượt)`;
        ul.appendChild(li);
    });
}
loadStats();
setInterval(loadStats, 3000);
</script>
</body>
</html>
"""

@flask_app.route("/stream/<username>/<int:message_id>")
def stream_redirect(username, message_id):
    return redirect(f"https://t.me/{username}/{message_id}", code=302)

def run_web():
    flask_app.run(host="0.0.0.0", port=8080)

# =========================
# AUTO SAVE AUDIO
# =========================
async def all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.audio:
        return

    topic_id = getattr(msg, "message_thread_id", None)
    if not is_source(msg.chat.id, topic_id):
        return

    if msg.audio.duration < 30:
        return
    if msg.audio.file_size and msg.audio.file_size < 500000:
        return

    name = clean_name(msg.audio.file_name or msg.audio.title or "unknown")
    username = msg.chat.username or "unknown"

    if save_song(name, msg.message_id, msg.chat.id, username):
        print("[SAVED]", name)

# =========================
# INLINE SEARCH
# =========================
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query.query
    if not q:
        return

    rows = search_songs(q, limit=20)
    results = []

    for (_, name, mid, user) in rows:
        link = build_stream_link(user, mid) or ""
        text = f"🎵 {name}\n{link}".strip()

        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=name,
                description="🎧 Bấm để gửi bài nhạc",
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
            )
        )

    await update.inline_query.answer(results, cache_time=10)

# =========================
# /timtrack
# =========================
async def timtrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not context.args:
        await msg.reply_text("Dùng: /timtrack <tên>")
        return

    query = " ".join(context.args)

    log_search(query)

    total = count_songs(query)

    page = 0
    limit = 5
    offset = page * limit

    rows = search_songs(query, offset=offset, limit=limit)

    text = [f"🎵 <b>Kết quả:</b> {pretty_text(query)}\n📊 Tổng: {total} bài\n"]

    topic_links = get_topic_links(query)
    if topic_links:
        text.append("━━━━━━━━━━━━━━━")
        text.append("📌 <b>CHỦ ĐỀ LIÊN QUAN</b>")
        for kw, link in topic_links:
            text.append(f"👉 <a href=\"{html.escape(link)}\">{pretty_text(kw)}</a>")
        text.append("━━━━━━━━━━━━━━━\n")

    keyboard = []

    for i, (sid, name, mid, user) in enumerate(rows, 1):
        link = build_stream_link(user, mid)
        if link:
            text.append(f"🎶 <b>{i}.</b> <a href=\"{html.escape(link)}\">{pretty_text(name)}</a>")
        else:
            text.append(f"🎶 <b>{i}.</b> {pretty_text(name)}")
        keyboard.append(
            InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}")
        )

    nav_buttons = build_pagination_buttons(page, total, query, limit)

    final_keyboard = []

    if keyboard:
        final_keyboard.append(keyboard)

    if nav_buttons:
        final_keyboard.extend(nav_buttons)

    await msg.reply_text(
        "\n".join(text),
        reply_markup=InlineKeyboardMarkup(final_keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# =========================
# /myplaylist
# =========================
async def myplaylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    cur.execute("""
        SELECT s.id, s.name, s.message_id, s.source_username
        FROM playlists p
        JOIN songs s ON p.song_id = s.id
        WHERE p.user_id = ?
        ORDER BY p.rowid DESC
        LIMIT 20
    """, (uid,))
    rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("🎧 <b>Playlist của bạn đang trống.</b>", parse_mode="HTML")
        return

    lines = ["🎧 <b>TỦ NHẠC CỦA BẠN</b>", ""]
    keyboard = []

    for i, (sid, name, mid, user) in enumerate(rows, 1):
        link = build_stream_link(user, mid)
        if link:
            lines.append(f"<b>{i}.</b> {pretty_text(name)}\n🔗 <a href=\"{html.escape(link)}\">Nghe bài này</a>")
            keyboard.append([InlineKeyboardButton(f"🎧 {i}", url=link)])
        else:
            lines.append(f"<b>{i}.</b> {pretty_text(name)}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )

# =========================
# CALLBACK
# =========================
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data.split("|")

    if data[0] == "fav":
        add_to_playlist(q.from_user.id, int(data[1]))
        await q.answer("❤️ Đã thêm vào /myplaylist", show_alert=False)
        return

    if data[0] == "page":
        page = int(data[1])
        search_query = data[2]
        limit = 5
        offset = page * limit

        total = count_songs(search_query)
        rows = search_songs(search_query, offset=offset, limit=limit)

        text = [f"🎵 <b>Kết quả:</b> {pretty_text(search_query)}\n📊 Trang {page+1}"]

        keyboard = []

        for i, (sid, name, mid, user) in enumerate(rows, offset + 1):
            link = build_stream_link(user, mid)
            if link:
                text.append(f"🎶 <b>{i}.</b> {pretty_text(name)}\n🔗 <a href=\"{html.escape(link)}\">Nghe bài này</a>")
            else:
                text.append(f"🎶 <b>{i}.</b> {pretty_text(name)}")
            keyboard.append(
                InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}")
            )

        nav_buttons = build_pagination_buttons(page, total, search_query, limit)

        final_keyboard = []

        if keyboard:
            final_keyboard.append(keyboard)

        if nav_buttons:
            final_keyboard.extend(nav_buttons)

        try:
            await q.edit_message_text(
                "\n".join(text),
                reply_markup=InlineKeyboardMarkup(final_keyboard),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except RetryAfter as e:
            await q.answer(f"⏳ Bấm chậm lại ({e.retry_after}s)", show_alert=False)
            return
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

        await q.answer()
        return

# =========================
# OWNER COMMANDS
# =========================
async def getdb(update, context):
    if not is_owner(update):
        return
    try:
        # Make sure Drive has the latest copy before sharing the link
        upload_db(DB_PATH)
        link = get_db_link()
        if link:
            await update.message.reply_text(
                f"📦 <b>Database (Google Drive):</b>\n🔗 {html.escape(link)}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_document(document=open(DB_PATH, "rb"))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

async def restoredb(update, context):
    if not is_owner(update):
        return
    global db, cur
    try:
        await update.message.reply_text("⏳ Đang tải DB mới nhất từ Google Drive...")

        ok = download_db(DB_PATH)
        if not ok:
            return await update.message.reply_text(
                "⚠️ Không tìm thấy DB trên Drive hoặc Drive chưa được cấu hình."
            )

        # Close old connection and reopen with the restored file
        try:
            db.close()
        except Exception:
            pass

        db = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = db.cursor()

        cur.execute("SELECT COUNT(*) FROM songs")
        songs = cur.fetchone()[0]

        await update.message.reply_text(
            f"✅ Đã khôi phục DB từ Google Drive.\n🎵 Tổng số bài hát: <b>{songs}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi khôi phục DB: {e}")

async def forcesync(update, context):
    if not is_owner_or_admin(update):
        return
    try:
        await update.message.reply_text("⏳ Đang đẩy DB lên Google Drive...")
        ok = await asyncio.to_thread(upload_db, DB_PATH)
        if ok:
            await update.message.reply_text("✅ Đã sync DB lên Google Drive.")
        else:
            await update.message.reply_text("⚠️ Sync thất bại hoặc Drive chưa được cấu hình.")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi sync: {e}")

async def settopic(update, context):
    if not is_owner_or_admin(update):
        return
    text = update.message.text.replace("/settopic ", "", 1).strip()

    m = re.match(r'^"([^"]*)"\s*\|\s*"([^"]*)"$', text)
    if not m:
        return await update.message.reply_text(
            'ℹ️ Cú pháp: <code>/settopic "&lt;từ khóa&gt;" | "&lt;link&gt;"</code>',
            parse_mode="HTML",
        )
    keyword, link = m.group(1).strip(), m.group(2).strip()
    add_topic_link(keyword, link)
    await update.message.reply_text(f"✅ Đã gán <b>{pretty_text(keyword)}</b> vào topic.", parse_mode="HTML")

async def deltrack(update, context):
    if not is_owner_or_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Cú pháp: <code>/deltrack &lt;ID bài hát&gt;</code>", parse_mode="HTML")
    try:
        song_id = int(context.args[0])
        cur.execute("DELETE FROM songs WHERE id=?", (song_id,))
        cur.execute("DELETE FROM aliases WHERE song_id=?", (song_id,))
        cur.execute("DELETE FROM playlists WHERE song_id=?", (song_id,))
        db.commit()
        sync_db()
        await update.message.reply_text(f"🗑️ Đã xóa bài hát ID: <b>{song_id}</b>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

async def addalias(update, context):
    if not is_owner_or_admin(update):
        return
    text = update.message.text.replace("/addalias ", "", 1).strip()
    if "|" not in text:
        return await update.message.reply_text(
            "ℹ️ Cú pháp: <code>/addalias &lt;ID bài hát&gt; | &lt;bí danh&gt;</code>",
            parse_mode="HTML",
        )
    id_part, alias_part = text.split("|", 1)
    try:
        song_id = int(id_part.strip())
    except ValueError:
        return await update.message.reply_text("❌ ID bài hát không hợp lệ")

    song = get_song_by_id(song_id)
    if not song:
        return await update.message.reply_text(f"❌ Không tìm thấy bài hát ID: {song_id}")

    alias = alias_part.strip()
    if add_alias(song_id, alias):
        await update.message.reply_text(
            f"✅ Đã thêm bí danh <b>{pretty_text(clean_name(alias))}</b> cho bài <b>{pretty_text(song[1])}</b> (ID {song_id})",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⚠️ Bí danh không hợp lệ hoặc đã tồn tại")

async def delalias(update, context):
    if not is_owner_or_admin(update):
        return
    text = update.message.text.replace("/delalias ", "", 1).strip()
    if "|" not in text:
        return await update.message.reply_text(
            "ℹ️ Cú pháp: <code>/delalias &lt;ID bài hát&gt; | &lt;bí danh&gt;</code>",
            parse_mode="HTML",
        )
    id_part, alias_part = text.split("|", 1)
    try:
        song_id = int(id_part.strip())
    except ValueError:
        return await update.message.reply_text("❌ ID bài hát không hợp lệ")

    if remove_alias(song_id, alias_part.strip()):
        await update.message.reply_text("✅ Đã xóa bí danh", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Không tìm thấy bí danh đó")

async def aliases_cmd(update, context):
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Cú pháp: <code>/aliases &lt;ID bài hát&gt;</code>",
            parse_mode="HTML",
        )
    try:
        song_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID bài hát không hợp lệ")

    song = get_song_by_id(song_id)
    if not song:
        return await update.message.reply_text(f"❌ Không tìm thấy bài hát ID: {song_id}")

    aliases = get_aliases(song_id)
    text = f"🎵 <b>{pretty_text(song[1])}</b> (ID {song_id})\n\n"
    if aliases:
        text += "🏷️ <b>Bí danh:</b>\n" + "\n".join(f"• {pretty_text(a)}" for a in aliases)
    else:
        text += "📭 Chưa có bí danh nào."
    await update.message.reply_text(text, parse_mode="HTML")


async def topnhac(update, context):
    cur.execute("SELECT query, count FROM top_searches ORDER BY count DESC LIMIT 10")
    rows = cur.fetchall()
    if not rows:
        return await update.message.reply_text("😴 Chưa có ai tìm kiếm.", parse_mode="HTML")

    text = "🔥 <b>TOP 10 TỪ KHÓA TÌM KIẾM</b>\n\n"
    for i, (q, c) in enumerate(rows, 1):
        text += f"{i}. {pretty_text(q)} — <b>{c}</b> lượt\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def allowgroup(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/allowgroup &lt;group_id&gt;</code>", parse_mode="HTML")
    try:
        gid = int(context.args[0])
        add_allowed_group(gid)
        await update.message.reply_text(f"✅ Đã cấp quyền group <b>{gid}</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ group_id không hợp lệ")

async def removegroup(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/removegroup &lt;group_id&gt;</code>", parse_mode="HTML")
    try:
        gid = int(context.args[0])
        remove_allowed_group(gid)
        await update.message.reply_text(f"✅ Đã bỏ quyền group <b>{gid}</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ group_id không hợp lệ")

async def listgroups(update, context):
    if not is_owner(update):
        return
    groups = get_allowed_groups()
    if not groups:
        return await update.message.reply_text("📭 Không có group nào.", parse_mode="HTML")
    text = "📋 <b>ALLOWED GROUPS</b>\n\n" + "\n".join(map(str, groups))
    await update.message.reply_text(text, parse_mode="HTML")

async def addadmin(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/addadmin &lt;user_id&gt;</code>", parse_mode="HTML")
    try:
        uid = int(context.args[0])
        add_admin(uid)
        await update.message.reply_text(f"✅ Đã thêm admin <b>{uid}</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ user_id không hợp lệ")

async def removeadmin(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
    try:
        uid = int(context.args[0])
        if remove_admin(uid):
            await update.message.reply_text(f"✅ Đã bỏ admin <b>{uid}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text(f"⚠️ <b>{uid}</b> không phải admin", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ user_id không hợp lệ")

async def listadmins(update, context):
    if not is_owner(update):
        return
    admins = get_admins()
    if not admins:
        return await update.message.reply_text("📭 Chưa có admin nào.", parse_mode="HTML")
    text = "👮 <b>ADMINS</b>\n\n" + "\n".join(map(str, admins))
    await update.message.reply_text(text, parse_mode="HTML")

async def addsource(update, context):
    if not is_owner_or_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/addsource &lt;group_id&gt; [&lt;topic_id&gt;]</code>", parse_mode="HTML")
    try:
        group_id = int(context.args[0])
        topic_id = int(context.args[1]) if len(context.args) >= 2 else None
        add_source(group_id, topic_id)
        await update.message.reply_text("✅ Source added", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ group_id/topic_id không hợp lệ")

async def removesource(update, context):
    if not is_owner_or_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Dùng: <code>/removesource &lt;group_id&gt; [&lt;topic_id&gt;]</code>", parse_mode="HTML")
    try:
        group_id = int(context.args[0])
        topic_id = int(context.args[1]) if len(context.args) >= 2 else None
        if group_id == MAIN_SOURCE_GROUP and topic_id == MAIN_SOURCE_TOPIC:
            return await update.message.reply_text("❌ Không thể xóa main source", parse_mode="HTML")
        remove_source(group_id, topic_id)
        await update.message.reply_text("✅ Source removed", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ group_id/topic_id không hợp lệ")

async def listsources(update, context):
    if not is_owner_or_admin(update):
        return
    rows = get_sources()
    text = f"📦 <b>MAIN SOURCE</b>\n{MAIN_SOURCE_GROUP} / topic={MAIN_SOURCE_TOPIC}\n\n"
    if rows:
        for gid, tid in rows:
            text += f"• group={gid}, topic={tid}\n"
    else:
        text += "📭 Không có source phụ nào."
    await update.message.reply_text(text, parse_mode="HTML")

async def listtopics(update, context):
    if not is_owner_or_admin(update):
        return
    cur.execute("SELECT keyword, link FROM topic_links ORDER BY keyword")
    rows = cur.fetchall()
    if not rows:
        return await update.message.reply_text("📭 Chưa có topic nào được gán.", parse_mode="HTML")

    text = "📌 <b>DANH SÁCH TOPIC</b>\n\n"
    for kw, link in rows:
        text += f"• <b>{pretty_text(kw)}</b> → <a href=\"{html.escape(link)}\">Xem topic</a>\n"
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def broadcast(update, context):
    if not is_owner(update):
        return

    text = update.message.text.replace("/broadcast ", "", 1).strip()
    if not text or text == "/broadcast":
        return await update.message.reply_text(
            "ℹ️ Cú pháp: <code>/broadcast &lt;nội dung&gt;</code>",
            parse_mode="HTML",
        )

    targets = set(get_allowed_groups())
    targets.add(MAIN_SOURCE_GROUP)

    msg_text = f"📢 <b>THÔNG BÁO</b>\n\n{text}"

    sent = 0
    failed = 0
    for gid in targets:
        try:
            await context.bot.send_message(chat_id=gid, text=msg_text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[BROADCAST] Failed for {gid}: {e}")

    await update.message.reply_text(
        f"✅ Đã gửi tới <b>{sent}</b> group" + (f", thất bại <b>{failed}</b>" if failed else ""),
        parse_mode="HTML",
    )

async def ownerhelp(update, context):
    if not is_owner(update):
        return

    text = (
        "🛠️ <b>OWNER COMMANDS</b>\n\n"

        "📦 <b>Database</b>\n"
        "/getdb — Lấy link/file database\n"
        "/restoredb — Khôi phục DB mới nhất từ Google Drive\n"
        "/forcesync — Đẩy DB lên Drive ngay (owner + admin)\n\n"

        "🎵 <b>Bài hát</b> (owner + admin)\n"
        "/deltrack &lt;ID&gt; — Xóa bài hát\n"
        "/addalias &lt;ID&gt; | &lt;bí danh&gt; — Thêm bí danh tìm kiếm\n"
        "/delalias &lt;ID&gt; | &lt;bí danh&gt; — Xóa bí danh\n\n"

        "📁 <b>Topic / Source</b> (owner + admin)\n"
        '/settopic "&lt;từ khóa&gt;" | "&lt;link&gt;" — Gán từ khóa vào topic\n'
        "/addsource &lt;group_id&gt; [topic_id] — Thêm nguồn lấy nhạc\n"
        "/removesource &lt;group_id&gt; [topic_id] — Xóa nguồn\n"
        "/listsources — Danh sách nguồn\n"
        "/listtopics — Danh sách topic đã gán\n\n"

        "🛡️ <b>Group</b>\n"
        "/allowgroup &lt;group_id&gt; — Cấp quyền group\n"
        "/removegroup &lt;group_id&gt; — Bỏ quyền group\n"
        "/listgroups — Danh sách group được cấp quyền\n\n"

        "👮 <b>Admin</b>\n"
        "/addadmin &lt;user_id&gt; — Thêm admin\n"
        "/removeadmin &lt;user_id&gt; — Bỏ admin\n"
        "/listadmins — Danh sách admin\n\n"

        "📊 <b>Thống kê &amp; thông báo</b>\n"
        "/stats — Thống kê bot\n"
        "/topnhac — Top từ khóa tìm kiếm\n"
        "/broadcast &lt;nội dung&gt; — Gửi thông báo tới tất cả group\n\n"

        "ℹ️ /ownerhelp — Hiện danh sách này"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def adminhelp(update, context):
    if not is_owner_or_admin(update):
        return

    text = (
        "🛠️ <b>ADMIN COMMANDS</b>\n\n"

        "🎵 <b>Bài hát</b>\n"
        "/deltrack &lt;ID&gt; — Xóa bài hát\n"
        "/addalias &lt;ID&gt; | &lt;bí danh&gt; — Thêm bí danh tìm kiếm\n"
        "/delalias &lt;ID&gt; | &lt;bí danh&gt; — Xóa bí danh\n\n"

        "📁 <b>Topic / Source</b>\n"
        '/settopic "&lt;từ khóa&gt;" | "&lt;link&gt;" — Gán từ khóa vào topic\n'
        "/addsource &lt;group_id&gt; [topic_id] — Thêm nguồn lấy nhạc\n"
        "/removesource &lt;group_id&gt; [topic_id] — Xóa nguồn\n"
        "/listsources — Danh sách nguồn\n"
        "/listtopics — Danh sách topic đã gán\n\n"

        "📦 <b>Database</b>\n"
        "/forcesync — Đẩy DB lên Google Drive ngay\n\n"

        "ℹ️ /adminhelp — Hiện danh sách này"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def stats(update, context):
    if not is_owner(update):
        return
    cur.execute("SELECT COUNT(*) FROM songs")
    songs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM playlists")
    playlists = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM top_searches")
    searches = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sources")
    sources = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM allowed_groups")
    groups = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM topic_links")
    topics = cur.fetchone()[0]

    text = (
        "📊 <b>BOT STATS</b>\n\n"
        f"🎵 Tracks: <b>{songs}</b>\n"
        f"❤️ Playlist items: <b>{playlists}</b>\n"
        f"🔎 Search keywords: <b>{searches}</b>\n"
        f"📦 Sources: <b>{sources}</b>\n"
        f"📌 Topics: <b>{topics}</b>\n"
        f"🛡️ Allowed groups: <b>{groups}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def dashboard_cmd(update, context):
    await update.message.reply_text(
        f"📊 <b>Dashboard realtime</b>\n🔗 {WEB_URL}/dashboard",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# =========================
# ERROR HANDLER
# =========================
async def error_handler(update, context):
    # Don't try to notify on flood-control errors; that would just trigger more of them
    if isinstance(context.error, RetryAfter):
        print(f"[FLOOD] RetryAfter: retry in {context.error.retry_after}s")
        return

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ <b>Lỗi rồi</b>\n<pre>{html.escape(tb_string[:3500])}</pre>",
            parse_mode="HTML",
        )
    except Exception:
        print(tb_string)

# =========================
# PERIODIC GDRIVE SYNC
# =========================
async def periodic_sync_job(context: ContextTypes.DEFAULT_TYPE):
    sync_db()

# =========================
# RUN
# =========================
thread = Thread(target=run_web, daemon=True)
thread.start()

app = ApplicationBuilder().token(TOKEN).build()

# Periodic sync every 5 minutes
app.job_queue.run_repeating(periodic_sync_job, interval=300, first=300)

# User commands
app.add_handler(CommandHandler("timtrack", timtrack))
app.add_handler(CommandHandler("myplaylist", myplaylist))
app.add_handler(CommandHandler("dashboard", dashboard_cmd))
app.add_handler(CommandHandler("aliases", aliases_cmd))

# Owner commands
app.add_handler(CommandHandler("getdb", getdb))
app.add_handler(CommandHandler("restoredb", restoredb))
app.add_handler(CommandHandler("forcesync", forcesync))
app.add_handler(CommandHandler("settopic", settopic))
app.add_handler(CommandHandler("deltrack", deltrack))
app.add_handler(CommandHandler("addalias", addalias))
app.add_handler(CommandHandler("delalias", delalias))
app.add_handler(CommandHandler("topnhac", topnhac))
app.add_handler(CommandHandler("allowgroup", allowgroup))
app.add_handler(CommandHandler("removegroup", removegroup))
app.add_handler(CommandHandler("listgroups", listgroups))
app.add_handler(CommandHandler("addadmin", addadmin))
app.add_handler(CommandHandler("removeadmin", removeadmin))
app.add_handler(CommandHandler("listadmins", listadmins))
app.add_handler(CommandHandler("addsource", addsource))
app.add_handler(CommandHandler("removesource", removesource))
app.add_handler(CommandHandler("listsources", listsources))
app.add_handler(CommandHandler("listtopics", listtopics))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("broadcast", broadcast))
app.add_handler(CommandHandler("ownerhelp", ownerhelp))
app.add_handler(CommandHandler("adminhelp", adminhelp))

# Other handlers
app.add_handler(InlineQueryHandler(inline_query_handler))
app.add_handler(CallbackQueryHandler(button_router))
app.add_handler(MessageHandler(filters.AUDIO, all_messages))
app.add_error_handler(error_handler)

print("BOT RUNNING")
app.run_polling()
