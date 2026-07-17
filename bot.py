import json
import os
import asyncio
import time
from datetime import datetime
from threading import Thread
from flask import Flask

from apify_client import ApifyClient
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)
from pymongo import MongoClient

# ==============================
# CONFIG FROM ENVIRONMENT VARIABLES
# ==============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# ==============================
# MONGO DATABASE SETUP
# ==============================
if MONGO_URI:
    db_client = MongoClient(MONGO_URI)
    db = db_client["instagram_monitor_db"]
    config_col = db["config"]
    usernames_col = db["usernames"]
    posts_col = db["last_posts"]
    use_mongo = True
    print("✅ MongoDB Connected Successfully!")
else:
    use_mongo = False
    print("⚠️ MONGO_URI missing. Data will reset on Render restarts!")

# GLOBALS
is_running = True        
is_auto_enabled = True  
last_manual_check_time = 0 
last_ran_minute = -1  

apify_client = ApifyClient(APIFY_TOKEN)

# Dummy Flask App for Render Port Binding
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive and running 24/7 on Render!"

def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# ==============================
# LOAD / SAVE DATA (MongoDB Cloud)
# ==============================
def load_usernames():
    if use_mongo:
        data = usernames_col.find_one({"_id": "monitored_list"})
        return set(data["list"]) if data else set()
    return set()

def save_usernames():
    if use_mongo:
        usernames_col.update_one(
            {"_id": "monitored_list"},
            {"$set": {"list": list(usernames)}},
            upsert=True
        )

def load_last_posts():
    if use_mongo:
        data = posts_col.find_one({"_id": "posts_tracker"})
        return data["tracker"] if data else {}
    return {}

def save_last_posts():
    if use_mongo:
        posts_col.update_one(
            {"_id": "posts_tracker"},
            {"$set": {"tracker": last_posts}},
            upsert=True
        )

def save_chat_id(chat_id):
    if use_mongo:
        config_col.update_one(
            {"_id": "bot_config"},
            {"$set": {"chat_id": chat_id}},
            upsert=True
        )

def load_chat_id():
    if use_mongo:
        data = config_col.find_one({"_id": "bot_config"})
        return data["chat_id"] if data else None
    return None

# ডাটাবেস থেকে ফ্রেশ ডেটা লোড
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

async def process_single_username(username, context, chat_id):
    try:
        latest = await asyncio.to_thread(get_latest_post, username)
        
        # টেলিগ্রাম যাতে জ্যাম না লাগে সেজন্য প্রতি মেসেজের মাঝে সামান্য বিরতি
        await asyncio.sleep(0.5) 
        
        if not latest:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ @{username}: কোনো ডেটা পাওয়া যায়নি।")
            return
            
        post_url = latest["url"]
        post_type = latest["type"]
        
        global last_posts
        last_posts = load_last_posts()
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
            await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন কোনো পোস্ট নেই।")
            
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ @{username} Error: `{e}`")

# ==============================
# CHECK LOGIC
# ==============================
async def check_accounts(context: ContextTypes.DEFAULT_TYPE, manual=False):
    chat_id = load_chat_id()
    global usernames
    usernames = load_usernames()
    
    if not chat_id or not usernames: return

    round_type = "Manual Check" if manual else "Auto Check"
    
    # স্ক্যান শুরুর কনফার্মেশন মেসেজটি নিশ্চিতভাবে পাঠানো
    try:
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"🔄 [{round_type}] {len(usernames)} টি অ্যাকাউন্ট একসাথে স্ক্যান করা শুরু হচ্ছে..."
        )
    except Exception as e:
        print(f"Start message error: {e}")

    # জ্যাম এড়াতে সেমাফোর সাইজ কমিয়ে ৫ করা হলো যাতে টেলিগ্রাম মেসেজ ড্রপ না করে
    semaphore = asyncio.Semaphore(5) 
    async def worker(username):
        async with semaphore:
            await process_single_username(username, context, chat_id)

    await asyncio.gather(*[worker(u) for u in sorted(list(usernames))])
    
    # স্ক্যান শেষের কনফার্মেশন মেসেজ
    try:
        await asyncio.sleep(1)
        await context.bot.send_message(chat_id=chat_id, text=f"🏁 [{round_type}] রাউন্ড সম্পূর্ণ শেষ হয়েছে!")
    except Exception as e:
        print(f"End message error: {e}")

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
        "List Commands:\n/add username1 username2\n/remove username\n/list"
    )

async def toggle_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running; is_running = True
    await update.message.reply_text("✅ Bot monitoring is now activated! [ON]")

async def toggle_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running; is_running = False
    await update.message.reply_text("🛑 Bot monitoring is now deactivated! [OFF]")

async def toggle_auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_auto_enabled; is_auto_enabled = True
    await update.message.reply_text("🔄 Auto Check is now Enabled! Bot will scan every 10 minutes.")

async def toggle_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_auto_enabled; is_auto_enabled = False
    await update.message.reply_text("🛑 Auto Check is now Disabled! Automatic scanning stopped.")

async def add_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    if not context.args: 
        return await update.message.reply_text("❌ Example: /add cristiano leomessi neymarjr")
    
    global usernames
    usernames = load_usernames()
    
    added_users = []
    already_monitored = []
    
    for arg in context.args:
        cleaned_args = arg.replace("@", "").replace(",", " ").split()
        for u_name in cleaned_args:
            username = u_name.strip().lower()
            if not username:
                continue
                
            if username in usernames:
                already_monitored.append(f"@{username}")
            else:
                usernames.add(username)
                added_users.append(f"@{username}")
            
    if added_users:
        save_usernames()
        await update.message.reply_text(f"✅ Processing & Adding Accounts:\n" + "\n".join(added_users))
        
        for u in added_users:
            pure_name = u.replace("@", "")
            asyncio.create_task(process_single_username(pure_name, context, update.effective_chat.id))
    
    if already_monitored:
        await update.message.reply_text(f"⚠️ Already in list:\n" + "\n".join(already_monitored))

async def remove_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global usernames, last_posts
    usernames = load_usernames()
    last_posts = load_last_posts()
    
    if not context.args: return await update.message.reply_text("❌ Example: /remove cristiano")
    username = context.args[0].replace("@", "").strip().lower()
    if username in usernames:
        usernames.remove(username)
        save_usernames()
        if username in last_posts: 
            del last_posts[username]
            save_last_posts()
        await update.message.reply_text(f"🗑 @{username} successfully removed from monitor list!")
    else:
        await update.message.reply_text(f"❌ @{username} not found in the list.")

async def list_usernames(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global usernames
    usernames = load_usernames()
    if not usernames: return await update.message.reply_text("📭 No accounts added yet.")
    text = "📋 Current Monitoring Accounts:\n\n" + "\n".join([f"• @{u}" for u in sorted(usernames)])
    await update.message.reply_text(text)

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_manual_check_time
    save_chat_id(update.effective_chat.id)
    if not is_running: 
        await update.message.reply_text("🛑 Action Denied! Bot monitoring is currently OFF. Please turn it /on first.")
        return
    last_manual_check_time = time.time()
    await check_accounts(context, manual=True)

def main():
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
    
    print("Instagram Monitor Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
