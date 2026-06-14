import os
import sqlite3
import re
import unicodedata
import traceback
import asyncio
import html
from uuid import uuid4

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters
)
from flask import Flask
from threading import Thread

# ==================================================
# KEEP ALIVE WEB SERVER
# ==================================================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "OK"

def web():
    flask_app.run(host="0.0.0.0", port=8080)

Thread(target=web).start()

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
DB_DIR = "/app/data"
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "music.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
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
CREATE TABLE IF NOT EXISTS topic_links(
    keyword TEXT PRIMARY KEY,
    link TEXT
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

cur.execute("""
CREATE TABLE IF NOT EXISTS top_searches(
    query TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS playlists(
    user_id INTEGER,
    song_id INTEGER,
    PRIMARY KEY (user_id, song_id)
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
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
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

async def delete_later(message, delay=60):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass 

# ==================================================
# TOPIC LINKS
# ==================================================
def add_topic_link(keyword, link):
    keyword = clean_name(keyword)
    cur.execute("INSERT OR REPLACE INTO topic_links(keyword, link) VALUES (?, ?)", (keyword, link))
    db.commit()

def get_topic_link(keyword):
    keyword = clean_name(keyword)
    cur.execute("SELECT link FROM topic_links WHERE keyword=?", (keyword,))
    row = cur.fetchone()
    return row[0] if row else None

# ==================================================
# ALLOWED GROUPS & SOURCES
# ==================================================
def add_allowed_group(group_id):
    cur.execute("INSERT OR IGNORE INTO allowed_groups VALUES (?)", (group_id,))
    db.commit()

def remove_allowed_group(group_id):
    cur.execute("DELETE FROM allowed_groups WHERE group_id=?", (group_id,))
    db.commit()

def get_allowed_groups():
    cur.execute("SELECT group_id FROM allowed_groups")
    return [x[0] for x in cur.fetchall()]

def is_allowed_group(group_id):
    if group_id == MAIN_SOURCE_GROUP: return True
    cur.execute("SELECT 1 FROM allowed_groups WHERE group_id=?", (group_id,))
    return cur.fetchone() is not None

def add_source(group_id, topic_id=None):
    cur.execute("INSERT INTO sources(group_id, topic_id) VALUES (?, ?)", (group_id, topic_id))
    db.commit()

def remove_source(group_id, topic_id=None):
    cur.execute("DELETE FROM sources WHERE group_id=? AND (topic_id=? OR (topic_id IS NULL AND ? IS NULL))", (group_id, topic_id, topic_id))
    db.commit()

def get_sources():
    cur.execute("SELECT group_id, topic_id FROM sources")
    return cur.fetchall()

def is_source(group_id, topic_id):
    if group_id == MAIN_SOURCE_GROUP and topic_id == MAIN_SOURCE_TOPIC: return True
    cur.execute("SELECT 1 FROM sources WHERE group_id=? AND (topic_id=? OR (topic_id IS NULL AND ? IS NULL))", (group_id, topic_id, topic_id))
    return cur.fetchone() is not None

# ==================================================
# SONGS & PLAYLIST DB
# ==================================================
def song_exists(name, source_group_id):
    cur.execute("SELECT 1 FROM songs WHERE name=? AND source_group_id=? LIMIT 1", (name, source_group_id))
    return cur.fetchone() is not None

def save_song(name, message_id, source_group_id, source_username):
    if song_exists(name, source_group_id): return False
    cur.execute("INSERT INTO songs(name, message_id, source_group_id, source_username) VALUES (?, ?, ?, ?)", 
                (name, message_id, source_group_id, source_username))
    db.commit()
    return True

def search_songs(query, offset=0, limit=5):
    query = clean_name(query)
    cur.execute("""
        SELECT DISTINCT s.id, s.name, s.message_id, s.source_username
        FROM songs s
        LEFT JOIN aliases a ON s.id = a.song_id
        WHERE s.name LIKE ? OR a.alias LIKE ?
        ORDER BY s.id DESC LIMIT ? OFFSET ?
    """, (f"%{query}%", f"%{query}%", limit, offset))
    return cur.fetchall()

def count_songs(query):
    query = clean_name(query)
    cur.execute("""
        SELECT COUNT(DISTINCT s.id) FROM songs s
        LEFT JOIN aliases a ON s.id = a.song_id
        WHERE s.name LIKE ? OR a.alias LIKE ?
    """, (f"%{query}%", f"%{query}%"))
    return cur.fetchone()[0]

def log_search(query):
    query = clean_name(query)
    cur.execute("INSERT OR IGNORE INTO top_searches (query, count) VALUES (?, 0)", (query,))
    cur.execute("UPDATE top_searches SET count = count + 1 WHERE query = ?", (query,))
    db.commit()

def add_to_playlist(user_id, song_id):
    cur.execute("INSERT OR IGNORE INTO playlists (user_id, song_id) VALUES (?, ?)", (user_id, song_id))
    db.commit()

# ==================================================
# AUTO LEARN MUSIC (ANTI-SPAM)
# ==================================================
async def all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    topic_id = msg.message_thread_id
    if not is_source(msg.chat.id, topic_id): return
    if not msg.audio: return

    # LỌC RÁC: Bỏ qua audio dưới 30 giây hoặc dưới 500KB
    if msg.audio.duration < 30: return
    if msg.audio.file_size and msg.audio.file_size < 500000: return

    filename = msg.audio.file_name or msg.audio.title or "unknown"
    song_name = clean_name(filename)
    username = msg.chat.username

    if not username: return

    saved = save_song(song_name, msg.message_id, msg.chat.id, username)
    if saved: print("[SAVED]", song_name)

# ==================================================
# INLINE QUERY (Tìm kiếm mọi lúc mọi nơi)
# ==================================================
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if not query: return
    
    rows = search_songs(query, offset=0, limit=20)
    results = []
    
    for (sid, name, message_id, username) in rows:
        link = f"https://t.me/{username}/{message_id}"
        text_content = f"🎵 <b>{name}</b>\n👉 <a href='{link}'>Click để nghe bài này</a>"
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=name,
                description="Bấm để gửi link nhạc",
                input_message_content=InputTextMessageContent(
                    message_text=text_content, 
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            )
        )
    await update.inline_query.answer(results, cache_time=10)

# ==================================================
# USER COMMANDS & CALLBACKS
# ==================================================
async def timtrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    if not is_allowed_group(msg.chat.id):
        await msg.reply_text("❌ Group chưa được cấp quyền.")
        return
    if not context.args:
        await msg.reply_text("Vui lòng gõ: /timtrack <tên bài hoặc chủ đề>")
        return

    query = " ".join(context.args)
    log_search(query) 

    topic_url = get_topic_link(query)
    if topic_url:
        sent_msg = await msg.reply_text(f"📁 Chuyên mục <b>{query}</b>:\n👉 <a href='{topic_url}'>{topic_url}</a>", parse_mode="HTML", disable_web_page_preview=True)
        asyncio.create_task(delete_later(msg, 60))
        asyncio.create_task(delete_later(sent_msg, 60))
        return

    total = count_songs(query)
    if total == 0:
        sent_msg = await msg.reply_text("Không tìm thấy kết quả nào.")
        asyncio.create_task(delete_later(msg, 10))
        asyncio.create_task(delete_later(sent_msg, 10))
        return

    rows = search_songs(query, offset=0, limit=5)
    results = [f"🎵 Kết quả cho: <b>{query}</b> (Tổng: {total} bài)\n"]
    
    fav_buttons = []
    for i, (sid, name, message_id, username) in enumerate(rows, 1):
        link = f"https://t.me/{username}/{message_id}"
        results.append(f'{i}. <a href="{link}">{name}</a>')
        # Thêm nút thả tim cho từng bài
        fav_buttons.append(InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}"))

    keyboard = [fav_buttons] # Hàng 1: Các nút thả tim
    
    if total > 5:
        short_query = query[:30] 
        keyboard.append([InlineKeyboardButton("Trang sau ➡️", callback_data=f"page|1|{short_query}")])

    sent_msg = await msg.reply_text(
        "\n".join(results),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    
    asyncio.create_task(delete_later(msg, 60))
    asyncio.create_task(delete_later(sent_msg, 60))

async def myplaylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute("""
        SELECT s.id, s.name, s.message_id, s.source_username
        FROM playlists p
        JOIN songs s ON p.song_id = s.id
        WHERE p.user_id = ?
        ORDER BY p.ROWID DESC LIMIT 20
    """, (user_id,))
    rows = cur.fetchall()
    
    if not rows:
        await update.message.reply_text("Tủ nhạc của bạn đang trống. Dùng /timtrack và bấm nút ❤️ để thêm nhé!")
        return
        
    results = ["🎧 <b>TỦ NHẠC CỦA BẠN (Tối đa 20 bài mới nhất):</b>\n"]
    for i, (sid, name, message_id, username) in enumerate(rows, 1):
        link = f"https://t.me/{username}/{message_id}"
        results.append(f'{i}. <a href="{link}">{name}</a>')
        
    await update.message.reply_text("\n".join(results), parse_mode="HTML", disable_web_page_preview=True)

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split("|")
    
    # 1. Xử lý nút Thả tim
    if data[0] == "fav":
        song_id = int(data[1])
        add_to_playlist(query.from_user.id, song_id)
        await query.answer("❤️ Đã thêm vào /myplaylist", show_alert=False)
        return

    # 2. Xử lý nút Chuyển trang
    if data[0] == "page":
        await query.answer() 
        page = int(data[1])
        search_query = data[2]
        offset = page * 5

        total = count_songs(search_query)
        rows = search_songs(search_query, offset=offset, limit=5)

        results = [f"🎵 Kết quả cho: <b>{search_query}</b> (Trang {page + 1})\n"]
        fav_buttons = []
        for i, (sid, name, message_id, username) in enumerate(rows, offset + 1):
            link = f"https://t.me/{username}/{message_id}"
            results.append(f'{i}. <a href="{link}">{name}</a>')
            fav_buttons.append(InlineKeyboardButton(f"❤️ {i}", callback_data=f"fav|{sid}"))

        keyboard = [fav_buttons]
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"page|{page-1}|{search_query}"))
        if offset + 5 < total:
            nav_buttons.append(InlineKeyboardButton("Sau ➡️", callback_data=f"page|{page+1}|{search_query}"))
        if nav_buttons:
            keyboard.append(nav_buttons)

        await query.edit_message_text(
            "\n".join(results),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

# ==================================================
# OWNER COMMANDS & ERRORS
# ==================================================
async def deltrack(update, context):
    if not is_owner(update): return
    if not context.args:
        await update.message.reply_text("Cú pháp: /deltrack <ID bài hát>")
        return
    try:
        song_id = int(context.args[0])
        cur.execute("DELETE FROM songs WHERE id=?", (song_id,))
        cur.execute("DELETE FROM aliases WHERE song_id=?", (song_id,))
        cur.execute("DELETE FROM playlists WHERE song_id=?", (song_id,)) # Xóa khỏi tủ nhạc mọi người
        db.commit()
        await update.message.reply_text(f"✅ Đã xóa bài hát ID: {song_id}")
    except Exception as e:
        await update.message.reply_text(f"Lỗi: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Lấy thông tin lỗi
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    
    # Cắt ngắn nếu dài quá và gửi về máy Owner
    err_msg = f"⚠️ Lỗi rồi đại vương ơi:\n<pre>{html.escape(tb_string[:3900])}</pre>"
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=err_msg, parse_mode="HTML")
    except Exception as e:
        print(f"Không thể gửi lỗi cho Owner: {e}")
    print(tb_string)

# (Các hàm Owner khác giữ nguyên: getdb, settopic, topnhac, allowgroup, listgroups, addsources, v.v...)
async def getdb(update, context):
    if not is_owner(update): return
    try:
        await update.message.reply_document(document=open(DB_PATH, "rb"))
    except Exception as e: await update.message.reply_text(f"Lỗi: {e}")

async def settopic(update, context):
    if not is_owner(update): return
    text = update.message.text.replace("/settopic ", "", 1)
    if "|" not in text: return await update.message.reply_text("Cú pháp: /settopic <từ khóa> | <link>")
    keyword, link = text.split("|", 1)
    add_topic_link(keyword.strip(), link.strip())
    await update.message.reply_text(f"✅ Đã gán từ khóa '{keyword.strip()}' vào link topic!")

async def topnhac(update, context):
    cur.execute("SELECT query, count FROM top_searches ORDER BY count DESC LIMIT 10")
    rows = cur.fetchall()
    if not rows: return await update.message.reply_text("Chưa có ai tìm kiếm.")
    text = "🔥 <b>TOP 10 TỪ KHÓA TÌM KIẾM</b>\n\n"
    for i, (q, c) in enumerate(rows, 1): text += f"{i}. <b>{q}</b> ({c} lượt)\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def allowgroup(update, context):
    if not is_owner(update): return
    if not context.args: return
    try: gid = int(context.args[0])
    except: return
    add_allowed_group(gid)
    await update.message.reply_text("Done")

async def removegroup(update, context):
    if not is_owner(update): return
    if not context.args: return
    remove_allowed_group(int(context.args[0]))
    await update.message.reply_text("Done")

async def listgroups(update, context):
    if not is_owner(update): return
    groups = get_allowed_groups()
    await update.message.reply_text("\n".join(map(str, groups)) or "Empty")

async def addsource(update, context):
    if not is_owner(update): return
    if not context.args: return
    group_id = int(context.args[0])
    topic_id = int(context.args[1]) if len(context.args) >= 2 else None
    add_source(group_id, topic_id)
    await update.message.reply_text("Source added")

async def removesource(update, context):
    if not is_owner(update): return
    if not context.args: return
    group_id = int(context.args[0])
    topic_id = int(context.args[1]) if len(context.args) >= 2 else None
    if group_id == MAIN_SOURCE_GROUP and topic_id == MAIN_SOURCE_TOPIC: return await update.message.reply_text("Không thể xóa main source")
    remove_source(group_id, topic_id)
    await update.message.reply_text("Source removed")

async def listsources(update, context):
    if not is_owner(update): return
    rows = get_sources()
    text = f"MAIN: {MAIN_SOURCE_GROUP} topic={MAIN_SOURCE_TOPIC}\n\n"
    for gid, tid in rows: text += f"group={gid}, topic={tid}\n"
    await update.message.reply_text(text)

async def stats(update, context):
    if not is_owner(update): return
    cur.execute("SELECT COUNT(*) FROM songs")
    songs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM playlists")
    playlists = cur.fetchone()[0]
    text = f"📊 BOT STATS\n\n🎵 Tracks: {songs}\n❤️ Lượt thêm Playlist: {playlists}"
    await update.message.reply_text(text)

# ==================================================
# RUN
# ==================================================
bot_app = ApplicationBuilder().token(TOKEN).build()

# Lệnh User & Tính năng mở rộng
bot_app.add_handler(CommandHandler("timtrack", timtrack))
bot_app.add_handler(CommandHandler("myplaylist", myplaylist))
bot_app.add_handler(CallbackQueryHandler(button_router))
bot_app.add_handler(InlineQueryHandler(inline_query_handler))

# Lệnh Owner
bot_app.add_handler(CommandHandler("getdb", getdb))
bot_app.add_handler(CommandHandler("settopic", settopic))
bot_app.add_handler(CommandHandler("deltrack", deltrack))
bot_app.add_handler(CommandHandler("topnhac", topnhac))
bot_app.add_handler(CommandHandler("allowgroup", allowgroup))
bot_app.add_handler(CommandHandler("removegroup", removegroup))
bot_app.add_handler(CommandHandler("listgroups", listgroups))
bot_app.add_handler(CommandHandler("addsource", addsource))
bot_app.add_handler(CommandHandler("removesource", removesource))
bot_app.add_handler(CommandHandler("listsources", listsources))
bot_app.add_handler(CommandHandler("stats", stats))

# Lắng nghe file & báo lỗi
bot_app.add_handler(MessageHandler(filters.ALL, all_messages))
bot_app.add_error_handler(error_handler)

print("Bot running...")
bot_app.run_polling()
