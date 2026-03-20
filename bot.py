import os
import re
import json
import asyncio
import aiohttp
import logging
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)


# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
VK_TOKEN = os.environ.get("VK_TOKEN", "")
WORKER_URL = os.environ.get("WORKER_URL", "")  # https://xxx.workers.dev
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
#  UPLOAD FUNCTIONS
# ══════════════════════════════════════════
async def vk_api(method, params):
    params["access_token"] = VK_TOKEN
    params["v"] = "5.199"
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://api.vk.com/method/{method}",
            params=params
        ) as r:
            return await r.json()


async def upload_to_vk_by_url(video_url, title="Video", desc=""):
    """Direct URL upload to VK (no download needed!)"""
    result = await vk_api("video.save", {
        "name": title,
        "description": desc,
        "is_private": 0,
        "link": video_url,
    })

    if "response" in result:
        data = result["response"]
        owner_id = data.get("owner_id", "")
        video_id = data.get("video_id", "")

        # Upload URL pe POST karo (VK require karta hai)
        upload_url = data.get("upload_url", "")
        if upload_url:
            async with aiohttp.ClientSession() as s:
                async with s.get(upload_url) as r:
                    await r.read()

        return {
            "ok": True,
            "owner_id": owner_id,
            "video_id": video_id,
            "vk_url": f"https://vk.com/video{owner_id}_{video_id}",
        }

    return {"ok": False, "error": result.get("error", {}).get("error_msg", "Unknown")}


async def upload_to_vk_by_file(file_path, title="Video", desc=""):
    """File upload to VK"""
    result = await vk_api("video.save", {
        "name": title,
        "description": desc,
        "is_private": 0,
    })

    if "response" not in result:
        return {"ok": False, "error": result.get("error", {}).get("error_msg", "Unknown")}

    data = result["response"]
    upload_url = data.get("upload_url", "")
    owner_id = data.get("owner_id", "")
    video_id = data.get("video_id", "")

    # File upload
    async with aiohttp.ClientSession() as s:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("video_file", f, filename="video.mp4")
            async with s.post(upload_url, data=form) as r:
                upload_result = await r.json()

    return {
        "ok": True,
        "owner_id": owner_id,
        "video_id": video_id,
        "vk_url": f"https://vk.com/video{owner_id}_{video_id}",
    }


def generate_stream_link(vk_url):
    """Worker stream link generate karo"""
    if not WORKER_URL:
        return None
    # Worker ke /gen endpoint ko call nahi karenge
    # Kyunki wo async hai, yahan sync mein karna padega
    # Instead VK URL return karenge
    return vk_url


# ══════════════════════════════════════════
# DOWNLOAD HELPERS
# ══════════════════════════════════════════
async def download_file(url, path):
    """URL se file download karo"""
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status == 200:
                with open(path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                return True
    return False


def is_url(text):
    """Check if text is a URL"""
    return bool(re.match(r'https?://', text.strip()))


def extract_filename(url):
    """URL se filename extract karo"""
    clean = url.split("?")[0].split("#")[0]
    name = clean.split("/")[-1]
    if not name or "." not in name:
        name = "video.mp4"
    return name


# ══════════════════════════════════════════
# BOT HANDLERS
# ══════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized")
        return

    await update.message.reply_text(
        "🎬 **VK Video Uploader Bot**\n\n"
        "Send me:\n"
        "📎 Video file (forward or send)\n"
        "🔗 Video URL (direct link)\n\n"
        "I'll upload it to VK and give you the link!",
        parse_mode="Markdown"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Video file receive hua"""
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    msg = await update.message.reply_text("⬇️ Downloading from Telegram...")

    video = update.message.video or update.message.document
    if not video:
        await msg.edit_text("❌ No video found")
        return

    # File size check (Telegram Bot API limit: 20MB download)
    if video.file_size and video.file_size > 20 * 1024 * 1024:
        await msg.edit_text(
            "⚠️ File > 20MB\n"
            "Telegram Bot API limit hai.\n\n"
            "**Workaround:** Video ka direct URL bhejo instead."
        , parse_mode="Markdown")
        return

    try:
        file = await context.bot.get_file(video.file_id)
        file_path = f"/tmp/{video.file_id}.mp4"
        await file.download_to_drive(file_path)

        await msg.edit_text("⬆️ Uploading to VK...")

        title = update.message.caption or "Video"
        result = await upload_to_vk_by_file(file_path, title)

        # Cleanup
        try:
            os.remove(file_path)
        except:
            pass

        if result["ok"]:
            stream_link = ""
            if WORKER_URL:
                try:
                    async with aiohttp.ClientSession() as s:
                        gen_url = f"{WORKER_URL}/gen?url={quote(result['vk_url'])}"
                        async with s.get(gen_url) as r:
                            text = await r.text()
                            try:
                                gen_data = json.loads(text)
                                if gen_data.get("ok"):
                                    stream_link = gen_data.get("link", "")
                            except:
                                stream_link = ""
                except:
                    stream_link = ""

            
            text = (
                f"✅ **Upload Successful!**\n\n"
                f"📺 VK: `{result['vk_url']}`\n"
            )
            if stream_link:
                text += f"🔗 Stream: `{stream_link}`\n"

            text += f"\n⚠️ VK processing mein 1-5 min lagta hai"

            await msg.edit_text(text, parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Upload failed: {result['error']}")

    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """URL receive hua"""
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip()
    if not is_url(text):
        await update.message.reply_text(
            "🤔 Video file ya URL bhejo\n"
            "URL https:// se start hona chahiye"
        )
        return

    msg = await update.message.reply_text("⬆️ Uploading URL to VK...")

    try:
        title = extract_filename(text).replace(".mp4", "").replace("-", " ")
        result = await upload_to_vk_by_url(text, title)

        if result["ok"]:
            stream_link = ""
            if WORKER_URL:
                try:
                    async with aiohttp.ClientSession() as s:
                        gen_url = f"{WORKER_URL}/gen?url={quote(result['vk_url'])}"
                        async with s.get(gen_url) as r:
                            text = await r.text()
                            try:
                                gen_data = json.loads(text)
                                if gen_data.get("ok"):
                                    stream_link = gen_data.get("link", "")
                            except:
                                stream_link = ""
                except:
                    stream_link = ""
                    

            text_reply = (
                f"✅ **Upload Successful!**\n\n"
                f"📺 VK: `{result['vk_url']}`\n"
            )
            if stream_link:
                text_reply += f"🔗 Stream: `{stream_link}`\n"

            text_reply += f"\n⚠️ VK processing mein 1-5 min lagta hai"

            await msg.edit_text(text_reply, parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Upload failed: {result['error']}")

    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwarded video"""
    if update.message.video or update.message.document:
        await handle_video(update, context)


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    if not VK_TOKEN:
        logger.error("VK_TOKEN not set!")
        return

    # Render ke liye dummy HTTP server (port bind)
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot running")
        def log_message(self, *args):
            pass

    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"HTTP server on port {port}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO, handle_video
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_url
    ))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.VIDEO | filters.Document.VIDEO),
        handle_forward
    ))

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

