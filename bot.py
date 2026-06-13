import os
import sqlite3
import re
import unicodedata
import traceback

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ==================================================
# CONFIG
# ==================================================
TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

MAIN_SOURCE_GROUP = -1002716112699
MAIN_SOURCE_TOPIC = 5552

# ==================================================
# DATABASE
# ==================================================
db = sqlite3.connect("music.db", check_same_thread=False)
cur = db.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS songs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    message_id INTEGER,
    source_group_id INTEGER,
    source_username TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS aliases(
    song_id INTEGER,
    alias TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS allowed_groups(
    group_id INTEGER PRIMARY KEY
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sources(
    group_id INTEGER,
    topic_id INTEGER
)
""")

db.commit()

# ==================================================
# HELPERS
# ==================================================
def is_owner(update):
    user = update.effective_user
    return user and user.id == OWNER_ID


def remove_accents(text):
    text = text.replace("đ", "d")
    text = text.replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(
        c for c in text
        if unicodedata.category(c) != "Mn"
    )
    return text


def clean_name(name):
    name = name.lower()
    name = remove_accents(name)
    name = name.replace(".mp3", "")
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    name = re.sub(r'[^a-z0-9\s\-]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


# ==================================================
# ALLOWED GROUPS
# ==================================================
def add_allowed_group(group_id):
    cur.execute(
        "INSERT OR IGNORE INTO allowed_groups VALUES (?)",
        (group_id,)
    )
    db.commit()


def remove_allowed_group(group_id):
    cur.execute(
        "DELETE FROM allowed_groups WHERE group_id=?",
        (group_id,)
    )
    db.commit()


def get_allowed_groups():
    cur.execute("SELECT group_id FROM allowed_groups")
    return [x[0] for x in cur.fetchall()]


def is_allowed_group(group_id):
    if group_id == MAIN_SOURCE_GROUP:
        return True

    cur.execute(
        "SELECT 1 FROM allowed_groups WHERE group_id=?",
        (group_id,)
    )
    return cur.fetchone() is not None


# ==================================================
# SOURCES
# ==================================================
def add_source(group_id, topic_id=None):
    cur.execute(
        "INSERT INTO sources(group_id, topic_id) VALUES (?, ?)",
        (group_id, topic_id)
    )
    db.commit()


def remove_source(group_id, topic_id=None):
    cur.execute("""
        DELETE FROM sources
        WHERE group_id=?
        AND (
            topic_id=? OR
            (topic_id IS NULL AND ? IS NULL)
        )
    """, (group_id, topic_id, topic_id))
    db.commit()


def get_sources():
    cur.execute("SELECT group_id, topic_id FROM sources")
    return cur.fetchall()


def is_source(group_id, topic_id):
    if group_id == MAIN_SOURCE_GROUP and topic_id == MAIN_SOURCE_TOPIC:
        return True

    cur.execute("""
        SELECT 1 FROM sources
        WHERE group_id=?
        AND (
            topic_id=? OR
            (topic_id IS NULL AND ? IS NULL)
        )
    """, (group_id, topic_id, topic_id))
    return cur.fetchone() is not None


# ==================================================
# SONGS
# ==================================================
def song_exists(name, source_group_id):
    cur.execute("""
        SELECT 1 FROM songs
        WHERE name=? AND source_group_id=?
        LIMIT 1
    """, (name, source_group_id))
    return cur.fetchone() is not None


def save_song(name, message_id, source_group_id, source_username):
    if song_exists(name, source_group_id):
        print("[SKIP DUPLICATE]", name)
        return False

    cur.execute("""
        INSERT INTO songs(
            name,
            message_id,
            source_group_id,
            source_username
        )
        VALUES (?, ?, ?, ?)
    """, (name, message_id, source_group_id, source_username))
    db.commit()
    return True


def search_songs(query, limit=5):
    query = clean_name(query)

    cur.execute("""
        SELECT DISTINCT
            s.name,
            s.message_id,
            s.source_username
        FROM songs s
        LEFT JOIN aliases a ON s.id = a.song_id
        WHERE
            s.name LIKE ?
            OR a.alias LIKE ?
        ORDER BY s.id DESC
        LIMIT ?
    """, (f"%{query}%", f"%{query}%", limit))

    return cur.fetchall()


# ==================================================
# ALIAS
# ==================================================
def add_alias(song_name, alias):
    song_name = clean_name(song_name)
    alias = clean_name(alias)

    cur.execute("""
        SELECT id FROM songs
        WHERE name=?
        LIMIT 1
    """, (song_name,))

    row = cur.fetchone()
    if not row:
        return False

    song_id = row[0]

    cur.execute("""
        INSERT INTO aliases(song_id, alias)
        VALUES (?, ?)
    """, (song_id, alias))
    db.commit()
    return True


# ==================================================
# STATS
# ==================================================
def get_stats():
    cur.execute("SELECT COUNT(*) FROM songs")
    songs = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM aliases")
    aliases = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sources")
    sources = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM allowed_groups")
    groups = cur.fetchone()[0]

    return songs, aliases, sources, groups


# ==================================================
# AUTO LEARN MUSIC
# ==================================================
async def all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    topic_id = msg.message_thread_id

    if not is_source(msg.chat.id, topic_id):
        return

    if not msg.audio:
        return

    filename = (
        msg.audio.file_name
        or msg.audio.title
        or "unknown"
    )

    song_name = clean_name(filename)
    username = msg.chat.username

    if not username:
        print("Source group phải public")
        return

    saved = save_song(
        song_name,
        msg.message_id,
        msg.chat.id,
        username
    )

    if saved:
        print("[SAVED]", song_name)


# ==================================================
# USER COMMAND
# ==================================================
async def timtrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not is_allowed_group(msg.chat.id):
        await msg.reply_text("❌ Group chưa được cấp quyền.")
        return

    if not context.args:
        await msg.reply_text("/timtrack <ten bai>")
        return

    query = " ".join(context.args)
    rows = search_songs(query, 5)

    if not rows:
        await msg.reply_text("Không tìm thấy.")
        return

    results = [f"🎵 Kết quả cho: <b>{query}</b>\n"]

    for i, (name, message_id, username) in enumerate(rows, 1):
        link = f"https://t.me/{username}/{message_id}"
        results.append(f'{i}. <a href="{link}">{name}</a>')

    await msg.reply_text(
        "\n".join(results),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ==================================================
# OWNER COMMANDS
# ==================================================
async def allowgroup(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("/allowgroup <group_id>")
        return
    try:
        gid = int(context.args[0])
    except:
        await update.message.reply_text("group_id lỗi")
        return
    add_allowed_group(gid)
    await update.message.reply_text("Done")


async def removegroup(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return
    gid = int(context.args[0])
    remove_allowed_group(gid)
    await update.message.reply_text("Done")


async def listgroups(update, context):
    if not is_owner(update):
        return
    groups = get_allowed_groups()
    await update.message.reply_text("\n".join(map(str, groups)) or "Empty")


async def addsource(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("/addsource <group> [topic]")
        return

    group_id = int(context.args[0])
    topic_id = int(context.args[1]) if len(context.args) >= 2 else None

    add_source(group_id, topic_id)
    await update.message.reply_text("Source added")


async def removesource(update, context):
    if not is_owner(update):
        return
    if not context.args:
        return

    group_id = int(context.args[0])
    topic_id = int(context.args[1]) if len(context.args) >= 2 else None

    if group_id == MAIN_SOURCE_GROUP and topic_id == MAIN_SOURCE_TOPIC:
        await update.message.reply_text("Không thể xóa main source")
        return

    remove_source(group_id, topic_id)
    await update.message.reply_text("Source removed")


async def listsources(update, context):
    if not is_owner(update):
        return

    rows = get_sources()
    text = f"MAIN: {MAIN_SOURCE_GROUP} topic={MAIN_SOURCE_TOPIC}\n\n"

    for gid, tid in rows:
        text += f"group={gid}, topic={tid}\n"

    await update.message.reply_text(text)


async def addalias(update, context):
    if not is_owner(update):
        return

    text = update.message.text.replace("/addalias ", "", 1)

    if "|" not in text:
        await update.message.reply_text("/addalias <song> | <alias>")
        return

    song, alias = text.split("|", 1)

    ok = add_alias(song.strip(), alias.strip())

    if ok:
        await update.message.reply_text("Alias added")
    else:
        await update.message.reply_text("Song not found")


async def stats(update, context):
    if not is_owner(update):
        return

    songs, aliases, sources, groups = get_stats()

    text = (
        "📊 BOT STATS\n\n"
        f"🎵 Tracks: {songs}\n"
        f"🏷 Aliases: {aliases}\n"
        f"📦 Extra Sources: {sources}\n"
        f"👥 Allowed Groups: {groups}"
    )

    await update.message.reply_text(text)


# ==================================================
# ERROR
# ==================================================
async def error_handler(update, context):
    traceback.print_exception(
        type(context.error),
        context.error,
        context.error.__traceback__
    )


# ==================================================
# RUN
# ==================================================
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("timtrack", timtrack))
app.add_handler(CommandHandler("allowgroup", allowgroup))
app.add_handler(CommandHandler("removegroup", removegroup))
app.add_handler(CommandHandler("listgroups", listgroups))
app.add_handler(CommandHandler("addsource", addsource))
app.add_handler(CommandHandler("removesource", removesource))
app.add_handler(CommandHandler("listsources", listsources))
app.add_handler(CommandHandler("addalias", addalias))
app.add_handler(CommandHandler("stats", stats))

app.add_handler(MessageHandler(filters.ALL, all_messages))
app.add_error_handler(error_handler)

print("Bot running...")
app.run_polling()
