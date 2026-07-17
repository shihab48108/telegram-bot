import json
import os
import asyncio
import time
from datetime import datetime
from threading import Thread

from apify_client import ApifyClient
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)
from flask import Flask

# ==============================
# FLASK WEB SERVER (For Render Keep-Alive)
# ==============================
# Render-এ ফ্রি টায়ার সচল রাখতে এবং পোর্ট বাইন্ডিং এরর এড়াতে এই সার্ভারটি দরকার।
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    # Render নিজে থেকেই PORT এনভায়রনমেন্ট ভ্যারিয়েবল প্রোভাইড করে
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# ==============================
# ENVIRONMENT VARIABLES
# ==============================
# সিকিউরিটির জন্য টোকেনগুলো Render Environment-এ সেট করবেন
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")

if not BOT_TOKEN or not APIFY_TOKEN:
    raise ValueError("Error: BOT_TOKEN or APIFY_TOKEN is missing in Environment Variables!")

# ==============================
# GLOBALS & FILES
# ==============================
is_running = True       
is_auto_enabled = True  
last_manual_check_time = 0 
last_ran_minute = -1  

USERNAMES_FILE = "usernames.json"
LAST_POSTS_FILE = "last_posts.json"
CHAT_ID_FILE = "chat_id.txt"

apify_client = ApifyClient(APIFY_TOKEN)

# ==============================
# LOAD / SAVE DATA
# ==============================
def load_usernames():
    if os.path.exists(USERNAMES_FILE):
        try:
            with open(USERNAMES_FILE, "r", encoding="utf-8") as file:
                return set(json.load(file))
        except Exception:
            return set()
    return set()

def save_usernames():
    with open(USERNAMES_FILE, "w", encoding="utf-8") as file:
        json.dump(list(usernames), file, indent=2)

def load_last_posts():
    if os.path.exists(LAST_POSTS_FILE):
        try:
            with open(LAST_POSTS_FILE, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return {}
    return {}

def save_last_posts():
    with open(LAST_POSTS_FILE, "w", encoding="utf-8") as file:
        json.dump(last_posts, file, indent=2)

def save_chat_id(chat_id):
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as file:
        file.write(str(chat_id))

def load_chat_id():
    if os.path.exists(CHAT_ID_FILE):
        try:
            with open(CHAT_ID_FILE, "r", encoding="utf-8") as file:
                return int(file.read().strip())
        except Exception:
            return None
    return None

usernames = load_usernames()
last_posts = load_last_posts()

# ==============================
# INSTAGRAM CHECK
# ==============================
def get_latest_post(username):
    try:
        run_input = {"username": [username], "resultsLimit": 1}
        run = apify_client.actor("apify/instagram-post-scraper").call(run_input=run_input)
        dataset_id = getattr(run, "default_dataset_id", None)
        if not dataset_id: return None
        items = list(apify_client.dataset(dataset_id).iterate_items())
        if not items: return None
        post = items[0]
        return {"url": post.get("url"), "type": post.get("type", "Post"), "username": post.get("ownerUsername", username)}
    except Exception as e:
        print(f"Apify Error for {username}: {e}")
        return None

async def process_single_username(username, context, chat_id, manual):
    try:
        latest = await asyncio.to_thread(get_latest_post, username)
        if not latest: 
            if manual:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ @{username} এর ডেটা পাওয়া যায়নি বা স্ক্র্যাপার ব্যর্থ হয়েছে।")
            return
            
        post_url = latest["url"]
        post_type = latest["type"]
        old_url = last_posts.get(username)

        if not old_url:
            last_posts[username] = post_url
            save_last_posts()
            await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username} connected!\n📱 {post_type}\n🔗 {post_url}")
            return

        if old_url != post_url:
            last_posts[username] = post_url
            save_last_posts()
            await context.bot.send_message(chat_id=chat_id, text=f"🚨 NEW POST!\n👤 @{username}\n📱 {post_type}\n🔗 {post_url}")
        else:
            if manual:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন পোস্ট নেই।")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ @{username} Error: `{e}`")

# ==============================
# CHECK LOGIC
# ==============================
async def check_accounts(context: ContextTypes.DEFAULT_TYPE, manual=False):
    chat_id = load_chat_id()
    if not chat_id or not usernames: return

    round_type = "Manual" if manual else "Automatic"
    await context.bot.send_message(chat_id=chat_id, text=f"🔄 [{round_type}] {len(usernames)} টি অ্যাকাউন্ট স্ক্যান হচ্ছে...")

    semaphore = asyncio.Semaphore(20) 
    async def worker(username):
        async with semaphore:
            await process_single_username(username, context, chat_id, manual)

    await asyncio.gather(*[worker(u) for u in sorted(list(usernames))])
    await context.bot.send_message(chat_id=chat_id, text="🏁 রাউন্ড সম্পূর্ণ শেষ হয়েছে!")

# ==============================
# CLOCK SCHEDULER
# ==============================
async def clock_scheduler(context: ContextTypes.DEFAULT_TYPE):
    global last_ran_minute
    now = datetime.now()
    
    if now.minute % 10 == 0 and now.minute != last_ran_minute:
        if is_running and is_auto_enabled:
            if (time.time() - last_manual_check_time) >= 300:
                await check_accounts(context, manual=False)
                last_ran_minute = now.minute
            else:
                print("Skipping Auto Check: Manual check happened < 5 mins ago.")
                chat_id = load_chat_id()
                if chat_id:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ সাম্প্রতিক ম্যানুয়াল চেকের কারণে এই রাউন্ডের অটো চেকটি স্কিপ করা হলো।"
                    )
                last_ran_minute = now.minute

# ==============================
# COMMANDS
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    await update.message.reply_text(
        "✅ Instagram Monitor Bot is running on Render!\n\n"
        "Main Commands:\n/on | /off\n/check\n\n"
        "Auto Check Commands:\n/autoon | /autooff\n\n"
        "List Commands:\n/add username\n/remove username\n/list"
    )

async def toggle_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running
    is_running = True
    await update.message.reply_text("✅ Bot monitoring is now ON.")

async def toggle_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running
    is_running = False
    await update.message.reply_text("🛑 Bot monitoring is now OFF.")

async def toggle_auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_auto_enabled
    is_auto_enabled = True
    await update.message.reply_text("🔄 Auto Check is now Enabled. প্রতি ১০ মিনিট পর পর অটো চেক হবে।")

async def toggle_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_auto_enabled
    is_auto_enabled = False
    await update.message.reply_text("🛑 Auto Check is now Disabled.")

async def add_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    if not context.args: return await update.message.reply_text("❌ Example: /add cristiano")
    
    username = context.args[0].replace("@", "").strip().lower()
    if username in usernames: return await update.message.reply_text(f"⚠️ @{username} is already monitored.")
    
    usernames.add(username)
    save_usernames()
    await update.message.reply_text(f"✅ @{username} added!")

async def remove_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ Example: /remove cristiano")
    
    username = context.args[0].replace("@", "").strip().lower()
    if username in usernames:
        usernames.remove(username)
        save_usernames()
        if username in last_posts: del last_posts[username]; save_last_posts()
        await update.message.reply_text(f"🗑 @{username} removed!")
    else:
        await update.message.reply_text(f"❌ @{username} not found.")

async def list_usernames(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not usernames: return await update.message.reply_text("📭 No accounts added.")
    text = "📋 Monitoring Accounts:\n\n" + "\n".join([f"• @{u}" for u in sorted(usernames)])
    await update.message.reply_text(text)

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_manual_check_time
    save_chat_id(update.effective_chat.id)
    
    if not is_running: 
        await update.message.reply_text("🛑 Bot is OFF!")
        return
        
    last_manual_check_time = time.time()
    await check_accounts(context, manual=True)

# ==============================
# MAIN
# ==============================
def main():
    # ব্যাকগ্রাউন্ড থ্রেডে Flask Web Server চালানো হচ্ছে 
    Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("on", toggle_on))
    app.add_handler(CommandHandler("off", toggle_off))
    app.add_handler(CommandHandler("autoon", toggle_auto_on))
    app.add_handler(CommandHandler("autooff", toggle_auto_off))
    app.add_handler(CommandHandler("add", add_username))
    app.add_handler(CommandHandler("remove", remove_username))
    app.add_handler(CommandHandler("list", list_usernames))
    app.add_handler(CommandHandler("check", manual_check))
    
    app.job_queue.run_repeating(clock_scheduler, interval=20, first=5)
    
    print("Instagram Monitor Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
