import os
import re
import json
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote

import aiohttp
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
VK_TOKEN = os.environ.get("VK_TOKEN", "")
WORKER_URL = os.environ.get("WORKER_URL", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# DUMMY HTTP SERVER (Render ke liye)
# ══════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def start_http():
    server = HTTPServer(("0.0.0.0", PORT), Health)
    logger.info(f"HTTP on port {PORT}")
    server.serve_forever()


# ══════════════════════════════════════════
# VK FUNCTIONS
# ══════════════════════════════════════════
async def vk_api(method, params):
    params["access_token"] = VK_TOKEN
    params["v"] = "5.199"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.vk.com/method/{method}", params=params) as r:
            return await r.json()


async def upload_to_vk_by_url(video_url, title="Video"):
    result = await vk_api("video.save", {
        "name": title,
        "is_private": 0,
        "link": video_url,
    })
    if "response" in result:
        data = result["response"]
        oid = data.get("owner_id", "")
        vid = data.get("video_id", "")
        upload_url = data.get("upload_url", "")
        if upload_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(upload_url) as r:
                        await r.read()
            except:
                pass
        return {"ok": True, "owner_id": oid, "video_id": vid, "vk_url": f"https://vk.com/video{oid}_{vid}"}
    return {"ok": False, "error": result.get("error", {}).get("error_msg", "Unknown error")}


async def upload_to_vk_by_file(file_path, title="Video"):
    result = await vk_api("video.save", {
        "name": title,
        "is_private": 0,
    })
    if "response" not in result:
        return {"ok": False, "error": result.get("error", {}).get("error_msg", "Unknown error")}
    data = result["response"]
    upload_url = data.get("upload_url", "")
    oid = data.get("owner_id", "")
    vid = data.get("video_id", "")
    try:
        async with aiohttp.ClientSession() as s:
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("video_file", f, filename="video.mp4")
                async with s.post(upload_url, data=form) as r:
                    await r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "owner_id": oid, "video_id": vid, "vk_url": f"https://vk.com/video{oid}_{vid}"}


async def get_stream_link(vk_url):
    if not WORKER_URL:
        return ""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{WORKER_URL}/gen?url={quote(vk_url)}") as r:
                text = await r.text()
                try:
                    data = json.loads(text)
                    if data.get("ok"):
                        return data.get("link", "")
                except:
                    pass
    except:
        pass
    return ""


# ══════════════════════════════════════════
# BOT HANDLERS
# ══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized")
        return
    await update.message.reply_text(
        "🎬 *VK Video Uploader*\n\n"
        "📎 Video file bhejo (< 20MB)\n"
        "🔗 Ya video URL bhejo\n\n"
        "Auto upload to VK + stream link!",
        parse_mode="Markdown"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    video = update.message.video or update.message.document
    if not video:
        return

    if video.file_size and video.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ File > 20MB limit. URL bhejo instead.")
        return

    msg = await update.message.reply_text("⬇️ Downloading...")

    try:
        file = await context.bot.get_file(video.file_id)
        file_path = f"/tmp/{video.file_id}.mp4"
        await file.download_to_drive(file_path)

        await msg.edit_text("⬆️ Uploading to VK...")
        title = update.message.caption or "Video"
        result = await upload_to_vk_by_file(file_path, title)

        try:
            os.remove(file_path)
        except:
            pass

        if result["ok"]:
            stream = await get_stream_link(result["vk_url"])
            text = f"✅ *Upload Done!*\n\n📺 VK: `{result['vk_url']}`\n"
            if stream:
                text += f"🔗 Stream: `{stream}`\n"
            text += "\n⏳ VK processing: 1-5 min"
            await msg.edit_text(text, parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Failed: {result['error']}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip()
    if not re.match(r'https?://', text):
        await update.message.reply_text("🔗 Valid URL bhejo (https://...)")
        return

    msg = await update.message.reply_text("⬆️ Uploading to VK...")

    try:
        title = text.split("/")[-1].split("?")[0].replace("-", " ") or "Video"
        result = await upload_to_vk_by_url(text, title)

        if result["ok"]:
            stream = await get_stream_link(result["vk_url"])
            reply = f"✅ *Upload Done!*\n\n📺 VK: `{result['vk_url']}`\n"
            if stream:
                reply += f"🔗 Stream: `{stream}`\n"
            reply += "\n⏳ VK processing: 1-5 min"
            await msg.edit_text(reply, parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Failed: {result['error']}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════
if __name__ == "__main__":
    # Start HTTP server first (Render needs this)
    t = threading.Thread(target=start_http, daemon=True)
    t.start()

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing!")
        # Keep running so Render doesn't restart
        while True:
            import time
            time.sleep(60)

    if not VK_TOKEN:
        logger.error("VK_TOKEN missing!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.VIDEO | filters.Document.VIDEO), handle_video
    ))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)
