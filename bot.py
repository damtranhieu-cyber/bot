import os
import sqlite3
import re
import unicodedata
import asyncio
import html
import traceback
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
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

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
    keyword TEXT PRIMARY KEY,
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
""")
db.commit()

# =========================
# HELPERS
# =========================
def is_owner(update: Update):
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

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

def get_topic_link(keyword):
    keyword = clean_name(keyword)
    cur.execute("SELECT link FROM topic_links WHERE keyword=?", (keyword,))
    row = cur.fetchone()
    return row[0] if row else None

# =========================
# GROUP / SOURCE SYSTEM
# =========================
def add_allowed_group(gid):
    cur.execute("INSERT OR IGNORE INTO allowed_groups(group_id) VALUES (?)", (gid,))
    db.commit()

def remove_allowed_group(gid):
    cur.execute("DELETE FROM allowed_groups WHERE group_id=?", (gid,))
    db.commit()

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

def remove_source(gid, tid=None):
    cur.execute(
        "DELETE FROM sources WHERE group_id=? AND (topic_id=? OR (topic_id IS NULL AND ? IS NULL))",
        (gid, tid, tid),
    )
    db.commit()

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
    return True

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

    return jsonify({
        "songs": songs,
        "playlists": playlists,
        "searches": searches,
        "sources": sources,
        "groups": groups,
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
page = 0
limit = 5

rows = search_songs(query, limit)

text = [f"🎵 <b>Kết quả:</b> {query}\n📊 Tổng: {total} bài\n"]

keyboard = []

for i, (sid, name, mid, user) in enumerate(rows, 1):
    text.append(f"🎶 {i}. {name}")
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
    parse_mode="HTML"
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
    rows = search_songs(search_query, limit)

    text = [f"🎵 <b>Kết quả:</b> {search_query}\n📊 Trang {page+1}"]

    keyboard = []

    for i, (sid, name, mid, user) in enumerate(rows, 1):
        text.append(f"🎶 {i}. {name}")
        keyboard.append(
            InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}")
        )

    nav_buttons = build_pagination_buttons(page, total, search_query, limit)

    final_keyboard = []

    if keyboard:
        final_keyboard.append(keyboard)

    if nav_buttons:
        final_keyboard.extend(nav_buttons)

    await q.edit_message_text(
        "\n".join(text),
        reply_markup=InlineKeyboardMarkup(final_keyboard),
        parse_mode="HTML"
    )

    await q.answer()
    return

# =========================
# OWNER COMMANDS
# =========================
async def getdb(update, context):
    if not is_owner(update):
        return
    try:
        await update.message.reply_document(document=open(DB_PATH, "rb"))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

async def settopic(update, context):
    if not is_owner(update):
        return
    text = update.message.text.replace("/settopic ", "", 1).strip()
    if "|" not in text:
        return await update.message.reply_text("ℹ️ Cú pháp: <code>/settopic &lt;từ khóa&gt; | &lt;link&gt;</code>", parse_mode="HTML")
    keyword, link = text.split("|", 1)
    add_topic_link(keyword.strip(), link.strip())
    await update.message.reply_text(f"✅ Đã gán <b>{pretty_text(keyword.strip())}</b> vào topic.", parse_mode="HTML")

async def deltrack(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return await update.message.reply_text("ℹ️ Cú pháp: <code>/deltrack &lt;ID bài hát&gt;</code>", parse_mode="HTML")
    try:
        song_id = int(context.args[0])
        cur.execute("DELETE FROM songs WHERE id=?", (song_id,))
        cur.execute("DELETE FROM aliases WHERE song_id=?", (song_id,))
        cur.execute("DELETE FROM playlists WHERE song_id=?", (song_id,))
        db.commit()
        await update.message.reply_text(f"🗑️ Đã xóa bài hát ID: <b>{song_id}</b>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

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

async def addsource(update, context):
    if not is_owner(update):
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
    if not is_owner(update):
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
    if not is_owner(update):
        return
    rows = get_sources()
    text = f"📦 <b>MAIN SOURCE</b>\n{MAIN_SOURCE_GROUP} / topic={MAIN_SOURCE_TOPIC}\n\n"
    if rows:
        for gid, tid in rows:
            text += f"• group={gid}, topic={tid}\n"
    else:
        text += "📭 Không có source phụ nào."
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

    text = (
        "📊 <b>BOT STATS</b>\n\n"
        f"🎵 Tracks: <b>{songs}</b>\n"
        f"❤️ Playlist items: <b>{playlists}</b>\n"
        f"🔎 Search keywords: <b>{searches}</b>\n"
        f"📦 Sources: <b>{sources}</b>\n"
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
# RUN
# =========================
thread = Thread(target=run_web, daemon=True)
thread.start()

app = ApplicationBuilder().token(TOKEN).build()

# User commands
app.add_handler(CommandHandler("timtrack", timtrack))
app.add_handler(CommandHandler("myplaylist", myplaylist))
app.add_handler(CommandHandler("dashboard", dashboard_cmd))

# Owner commands
app.add_handler(CommandHandler("getdb", getdb))
app.add_handler(CommandHandler("settopic", settopic))
app.add_handler(CommandHandler("deltrack", deltrack))
app.add_handler(CommandHandler("topnhac", topnhac))
app.add_handler(CommandHandler("allowgroup", allowgroup))
app.add_handler(CommandHandler("removegroup", removegroup))
app.add_handler(CommandHandler("listgroups", listgroups))
app.add_handler(CommandHandler("addsource", addsource))
app.add_handler(CommandHandler("removesource", removesource))
app.add_handler(CommandHandler("listsources", listsources))
app.add_handler(CommandHandler("stats", stats))

# Other handlers
app.add_handler(InlineQueryHandler(inline_query_handler))
app.add_handler(CallbackQueryHandler(button_router))
app.add_handler(MessageHandler(filters.ALL, all_messages))
app.add_error_handler(error_handler)

print("BOT RUNNING")
app.run_polling()
