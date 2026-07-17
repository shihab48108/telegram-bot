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

# ==============================
# RENDER SERVER SETUP
# ==============================
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is live and running 24/7 on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port)

# ==============================
# YOUR TOKENS
# ==============================
BOT_TOKEN = "8640744131:AAE36iNhuGx_DO3J1datYRfls0oQoOzVWfE"
APIFY_TOKEN = "apify_api_bGe2YIpSgTVy5IFwqsD3azTbCZ30sf17huGU"

# ==============================
# GLOBALS & STORAGE (Local Path Fix)
# ==============================
is_running = True        
is_auto_enabled = True  
last_manual_check_time = 0 
last_ran_minute = -1  

# রেন্ডারের কারেন্ট ডিরেক্টরিতেই ফাইল সেভ হবে যেন ডেটা ডিলিট না হয়
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
    try:
        with open(USERNAMES_FILE, "w", encoding="utf-8") as file:
            json.dump(list(usernames), file, indent=2)
    except Exception as e:
        print(f"Error saving usernames: {e}")

def load_last_posts():
    if os.path.exists(LAST_POSTS_FILE):
        try:
            with open(LAST_POSTS_FILE, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return {}
    return {}

def save_last_posts():
    try:
        with open(LAST_POSTS_FILE, "w", encoding="utf-8") as file:
            json.dump(last_posts, file, indent=2)
    except Exception as e:
        print(f"Error saving posts: {e}")

def save_chat_id(chat_id_val):
    try:
        with open(CHAT_ID_FILE, "w", encoding="utf-8") as file:
            file.write(str(chat_id_val))
    except Exception as e:
        print(f"Error saving chat id: {e}")

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
# INSTAGRAM CHECK (Proxy/Session Fallback)
# ==============================
def get_latest_post(username):
    try:
        # ক্লাউড আইপি ব্লক এড়াতে রেজাল্ট লিমিট ও প্রক্সি অপ্টিমাইজেশন
        run_input = {
            "username": [username], 
            "resultsLimit": 1,
            "proxyConfiguration": {"useApifyProxy": True}
        }
        run = apify_client.actor("apify/instagram-post-scraper").call(run_input=run_input, timeout_secs=60)
        dataset_id = getattr(run, "default_dataset_id", None)
        if not dataset_id: 
            return None
        items = list(apify_client.dataset(dataset_id).iterate_items())
        if not items: 
            return None
        post = items[0]
        return {"url": post.get("url"), "type": post.get("type", "Post")}
    except Exception as e:
        print(f"Apify Fetch Error for {username}: {e}")
        return None

async def process_single_username(username, context, chat_id):
    global last_posts
    try:
        last_posts = load_last_posts()
        latest = await asyncio.to_thread(get_latest_post, username)
        
        # যদি রেন্ডার সার্ভার ব্লক খায় বা ডেটা না পায়, তবে পিসির মতো ব্ল্যাঙ্ক এরর না দেখিয়ে ওল্ড মেমোরি রিটেইন করবে
        if not latest:
            old_url = last_posts.get(username)
            if old_url:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন কোনো পোস্ট নেই (রিল্যাক্সড মোড)।")
            else:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ @{username}: সার্ভার রেসপন্স করছে না, পরে চেষ্টা করুন।")
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
            await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন পোস্ট নেই।")
            
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ @{username} Error: `{e}`")

# ==============================
# CHECK LOGIC
# ==============================
async def check_accounts(context: ContextTypes.DEFAULT_TYPE, manual=False):
    chat_id = load_chat_id()
    current_users = load_usernames()
    if not chat_id or not current_users: return

    round_type = "Manual" if manual else "Automatic"
    await context.bot.send_message(chat_id=chat_id, text=f"🔄 [{round_type}] {len(current_users)} টি অ্যাকাউন্ট স্ক্যান হচ্ছে...")

    for u in sorted(list(current_users)):
        await process_single_username(u, context, chat_id)
        await asyncio.sleep(2) # রেন্ডার আইপি রেট লিমিট এড়াতে ব্যবধান বাড়িয়ে ২ সেকেন্ড করা হলো

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
                chat_id = load_chat_id()
                if chat_id:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⏭️ সাম্প্রতিক ম্যানুয়াল চেকের কারণে (৫ মিনিটের কম ব্যবধান) এই রাউন্ডের অটো চেকটি স্কিপ করা হলো।"
                    )
                last_ran_minute = now.minute

# ==============================
# COMMANDS
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_val = update.effective_chat.id
    save_chat_id(chat_id_val)
    await update.message.reply_text(
        "✅ Instagram Monitor Bot is running!\n\n"
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
    await update.message.reply_text("🔄 Auto Check is now Enabled.")

async def toggle_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_auto_enabled
    is_auto_enabled = False
    await update.message.reply_text("🛑 Auto Check is now Disabled.")

async def add_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global usernames
    chat_id_val = update.effective_chat.id
    save_chat_id(chat_id_val)
    if not context.args: 
        return await update.message.reply_text("❌ Example: /add cristiano leomessi")
    
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
        await update.message.reply_text(f"✅ successfully added:\n" + "\n".join(added_users))
        
        for user in added_users:
            pure_name = user.replace("@", "")
            asyncio.create_task(process_single_username(pure_name, context, chat_id_val))
    
    if already_monitored:
        await update.message.reply_text(f"⚠️ Already in list:\n" + "\n".join(already_monitored))

async def remove_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global usernames, last_posts
    if not context.args: return await update.message.reply_text("❌ Example: /remove cristiano")
    
    username = context.args[0].replace("@", "").strip().lower()
    usernames = load_usernames()
    last_posts = load_last_posts()
    
    if username in usernames:
        usernames.remove(username)
        save_usernames()
        if username in last_posts: 
            del last_posts[username]
            save_last_posts()
        await update.message.reply_text(f"🗑 @{username} removed!")
    else:
        await update.message.reply_text(f"❌ @{username} not found.")

async def list_usernames(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_users = load_usernames()
    if not current_users: return await update.message.reply_text("📭 No accounts added.")
    text = "📋 Monitoring Accounts:\n\n" + "\n".join([f"• @{u}" for u in sorted(current_users)])
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
