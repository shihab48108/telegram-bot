import os
import asyncio
import time
from datetime import datetime
from threading import Thread
from flask import Flask
from apify_client import ApifyClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from pymongo import MongoClient

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# DB SETUP
db_client = MongoClient(MONGO_URI)
db = db_client["instagram_monitor_db"]
config_col = db["config"]
usernames_col = db["usernames"]
posts_col = db["last_posts"]

apify_client = ApifyClient(APIFY_TOKEN)

# GLOBAL VARS
is_running = True
is_auto_enabled = True
last_manual_check_time = 0
last_ran_minute = -1

# Flask for Render
app_flask = Flask(__name__)
@app_flask.route('/')
def home(): return "Bot is live!"

def run_flask(): app_flask.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# DATA HELPERS
def load_data():
    u_data = usernames_col.find_one({"_id": "monitored_list"})
    return set(u_data["list"]) if u_data else set()

def save_data(usernames):
    usernames_col.update_one({"_id": "monitored_list"}, {"$set": {"list": list(usernames)}}, upsert=True)

def load_posts():
    p_data = posts_col.find_one({"_id": "posts_tracker"})
    return p_data["tracker"] if p_data else {}

def save_posts(posts):
    posts_col.update_one({"_id": "posts_tracker"}, {"$set": {"tracker": posts}}, upsert=True)

# CORE CHECK LOGIC
def get_latest_post(username):
    try:
        run = apify_client.actor("apify/instagram-post-scraper").call(run_input={"username": [username], "resultsLimit": 1})
        items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())
        return {"url": items[0]["url"], "type": items[0].get("type", "Post")} if items else None
    except: return None

async def process_username(username, context, chat_id):
    latest = await asyncio.to_thread(get_latest_post, username)
    posts = load_posts()
    old_url = posts.get(username)

    if not latest:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ @{username}: কোনো পোস্ট পাওয়া যায়নি।")
    elif not old_url:
        posts[username] = latest["url"]
        save_posts(posts)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username} connected!\n🔗 {latest['url']}")
    elif old_url != latest["url"]:
        posts[username] = latest["url"]
        save_posts(posts)
        await context.bot.send_message(chat_id=chat_id, text=f"🚨 NEW POST! @{username}\n📱 {latest['type']}\n🔗 {latest['url']}")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন কোনো পোস্ট নেই।")
    await asyncio.sleep(1) # Telegram rate limit prevention

async def check_accounts(context, manual=False):
    chat_id = config_col.find_one({"_id": "bot_config"}).get("chat_id")
    usernames = load_data()
    if not usernames: return

    mode = "Manual" if manual else "Auto"
    await context.bot.send_message(chat_id=chat_id, text=f"🔄 [{mode}] {len(usernames)} টি অ্যাকাউন্ট স্ক্যান শুরু হচ্ছে...")
    
    for u in usernames:
        await process_username(u, context, chat_id)
        
    await context.bot.send_message(chat_id=chat_id, text=f"🏁 All accounts {mode} check complete!")

# COMMANDS
async def start(u, c):
    config_col.update_one({"_id": "bot_config"}, {"$set": {"chat_id": u.effective_chat.id}}, upsert=True)
    await u.message.reply_text("✅ Bot is ready!")

async def add_users(u, c):
    if not c.args: return await u.message.reply_text("❌ Use: /add @user1 @user2")
    usernames = load_data()
    added = []
    for arg in c.args:
        name = arg.replace("@", "").lower()
        if name not in usernames:
            usernames.add(name)
            added.append(name)
    save_data(usernames)
    await u.message.reply_text(f"✅ Added: {', '.join(added)}")

async def check_cmd(u, c):
    global last_manual_check_time
    last_manual_check_time = time.time()
    await check_accounts(c, manual=True)

async def toggle_on(u, c):
    global is_running; is_running = True
    await u.message.reply_text("✅ Monitoring ON.")

async def toggle_off(u, c):
    global is_running; is_running = False
    await u.message.reply_text("🛑 Monitoring OFF.")

async def auto_toggle(u, c):
    global is_auto_enabled; is_auto_enabled = not is_auto_enabled
    status = "Enabled" if is_auto_enabled else "Disabled"
    await u.message.reply_text(f"🔄 Auto Check is now {status}.")

async def clock_scheduler(c):
    if not is_running or not is_auto_enabled: return
    # Skip if manual check happened < 5 mins ago
    if time.time() - last_manual_check_time < 300:
        if datetime.now().minute % 10 == 0:
            await c.bot.send_message(chat_id=config_col.find_one({"_id":"bot_config"})["chat_id"], text="⏩ Manual check occurred recently, skipping this auto-check.")
        return
    
    if datetime.now().minute % 10 == 0:
        await check_accounts(c, manual=False)

def main():
    Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_users))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("on", toggle_on))
    app.add_handler(CommandHandler("off", toggle_off))
    app.add_handler(CommandHandler("autoon", auto_toggle))
    app.add_handler(CommandHandler("list", lambda u, c: u.message.reply_text(f"📋: {', '.join(load_data())}")))
    app.job_queue.run_repeating(clock_scheduler, interval=60, first=5)
    app.run_polling()

if __name__ == "__main__": main()
