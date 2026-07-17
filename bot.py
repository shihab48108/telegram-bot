import os
import asyncio
import time
from datetime import datetime
from threading import Thread
from flask import Flask
from apify_client import ApifyClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
apify_client = ApifyClient(APIFY_TOKEN)

# GLOBAL STORAGE (In-Memory)
is_running = True
is_auto_enabled = True
last_manual_check_time = 0
last_ran_minute = -1
usernames = set()
last_posts = {}
chat_id = None

# Flask for Render
app_flask = Flask(__name__)
@app_flask.route('/')
def home(): return "Bot is live!"

def run_flask(): app_flask.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# CORE CHECK LOGIC
def get_latest_post(username):
    try:
        run = apify_client.actor("apify/instagram-post-scraper").call(run_input={"username": [username], "resultsLimit": 1})
        items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())
        return {"url": items[0]["url"], "type": items[0].get("type", "Post")} if items else None
    except: return None

async def process_username(username, context):
    latest = await asyncio.to_thread(get_latest_post, username)
    old_url = last_posts.get(username)

    if not latest:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ @{username}: কোনো ডেটা পাওয়া যায়নি।")
    elif not old_url:
        last_posts[username] = latest["url"]
        await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username} connected!\n🔗 {latest['url']}")
    elif old_url != latest["url"]:
        last_posts[username] = latest["url"]
        await context.bot.send_message(chat_id=chat_id, text=f"🚨 NEW POST! @{username}\n📱 {latest['type']}\n🔗 {latest['url']}")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"✅ @{username}: নতুন কোনো পোস্ট নেই।")
    await asyncio.sleep(1)

async def check_accounts(context, manual=False):
    if not chat_id or not usernames: return
    mode = "Manual" if manual else "Automatic"
    await context.bot.send_message(chat_id=chat_id, text=f"🔄 [{mode}] {len(usernames)} টি অ্যাকাউন্ট স্ক্যান শুরু হচ্ছে...")
    
    for u in usernames:
        await process_username(u, context)
        
    await context.bot.send_message(chat_id=chat_id, text=f"🏁 All accounts {mode} check complete!")

# COMMANDS
async def start(u, c):
    global chat_id
    chat_id = u.effective_chat.id
    await u.message.reply_text("✅ Bot is online!")

async def add_users(u, c):
    if not c.args: return await u.message.reply_text("❌ Use: /add @user1 @user2")
    added = []
    for arg in c.args:
        name = arg.replace("@", "").lower()
        if name not in usernames:
            usernames.add(name)
            added.append(name)
    await u.message.reply_text(f"✅ Added: {', '.join([f'@{n}' for n in added])}")

async def check_cmd(u, c):
    global last_manual_check_time
    last_manual_check_time = time.time()
    await check_accounts(c, manual=True)

# Async Background Scheduler (Alternative to JobQueue)
async def start_async_scheduler(application):
    global last_ran_minute
    while True:
        try:
            await asyncio.sleep(30) # প্রতি ৩০ সেকেন্ড পর পর কন্ডিশন চেক করবে
            if not is_running or not is_auto_enabled or not chat_id: 
                continue
            
            # ৫ মিনিটের মধ্যে ম্যানুয়াল চেক হয়ে থাকলে অটো স্কিপ করবে
            if time.time() - last_manual_check_time < 300:
                now = datetime.now()
                if now.minute % 10 == 0 and now.minute != last_ran_minute:
                    last_ran_minute = now.minute
                    await application.bot.send_message(chat_id=chat_id, text="⏩ Manual check occurred recently, skipping this auto-check.")
                continue
            
            now = datetime.now()
            if now.minute % 10 == 0 and now.minute != last_ran_minute:
                last_ran_minute = now.minute
                await application.bot.send_message(chat_id=chat_id, text="🔄 [Automatic] Auto check started...")
                await check_accounts(application, manual=False)
                await application.bot.send_message(chat_id=chat_id, text="🏁 [Automatic] Auto check complete!")
        except Exception as e:
            print(f"Scheduler Error: {e}")

def main():
    Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_users))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("on", lambda u, c: (globals().update({'is_running': True}), u.message.reply_text("✅ Monitoring ON"))))
    app.add_handler(CommandHandler("off", lambda u, c: (globals().update({'is_running': False}), u.message.reply_text("🛑 Monitoring OFF"))))
    app.add_handler(CommandHandler("autoon", lambda u, c: (globals().update({'is_auto_enabled': True}), u.message.reply_text("🔄 Auto Check ON"))))
    app.add_handler(CommandHandler("autooff", lambda u, c: (globals().update({'is_auto_enabled': False}), u.message.reply_text("🛑 Auto Check OFF"))))
    app.add_handler(CommandHandler("list", lambda u, c: u.message.reply_text(f"📋: {', '.join([f'@{n}' for n in usernames])}")))
    
    # বোতাম বা পোলিং স্টার্ট হওয়ার সাথে সাথে ব্যাকগ্রাউন্ড টাস্ক লুপ চালু করা
    loop = asyncio.get_event_loop()
    loop.create_task(start_async_scheduler(app))
    
    app.run_polling()

if __name__ == "__main__": main()
