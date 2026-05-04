import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= WEB =================
web = Flask(__name__)

@web.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# ================= BOT =================
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("Missing TOKEN in environment variables")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot đang chạy ok")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

print("BOT STARTED")

TOKEN = os.getenv("TOKEN")  # 👈 ở đây
ADMIN_ID = 5818758277  # 👈 ID của bạn (dùng /id để lấy)

spamming = False
delay = 1
target_chat_id = None
message_text = "Spam 😈"

# 🔒 check quyền
def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

async def spam_loop(app):
    global spamming, delay, target_chat_id, message_text
    while spamming:
        if target_chat_id:
            try:
                await app.bot.send_message(
                    chat_id=target_chat_id,
                    text=message_text
                )
                await asyncio.sleep(delay)
            except Exception as e:
                print("Lỗi:", e)
                await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Lệnh:\n"
        "/setchat <id>\n"
        "/setmsg <nội dung>\n"
        "/startspam\n"
        "/stopspam\n"
        "/setdelay 1\n"
        "/id"
    )

# /id
async def getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(f"Your ID: {update.effective_user.id}")

# /setchat
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global target_chat_id
    if not is_admin(update):
        return
    try:
        target_chat_id = int(context.args[0])
        await update.message.reply_text(f"Đã set chat: {target_chat_id}")
    except:
        await update.message.reply_text("Dùng: /setchat -100xxxx")

# /setmsg
async def setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global message_text
    if not is_admin(update):
        return
    try:
        message_text = " ".join(context.args)
        await update.message.reply_text(f"Nội dung mới:\n{message_text}")
    except:
        await update.message.reply_text("Dùng: /setmsg nội dung")

# start spam
async def startspam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global spamming
    if not is_admin(update):
        return

    if spamming:
        await update.message.reply_text("Đang chạy rồi")
        return

    if not target_chat_id:
        await update.message.reply_text("Chưa set chat ID!")
        return

    spamming = True
    await update.message.reply_text("Bắt đầu spam")

    context.application.create_task(spam_loop(context.application))

# stop spam
async def stopspam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global spamming
    if not is_admin(update):
        return
    spamming = False
    await update.message.reply_text("Đã dừng")

# set delay
async def setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global delay
    if not is_admin(update):
        return
    try:
        delay = float(context.args[0])
        await update.message.reply_text(f"Delay = {delay}s")
    except:
        await update.message.reply_text("Dùng: /setdelay 1")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("id", getid))
app.add_handler(CommandHandler("setchat", setchat))
app.add_handler(CommandHandler("setmsg", setmsg))
app.add_handler(CommandHandler("startspam", startspam))
app.add_handler(CommandHandler("stopspam", stopspam))
app.add_handler(CommandHandler("setdelay", setdelay))
print("BOT STARTED")
app.run_polling()
