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

def progress_bar(pct):
    filled = int(pct / 5)
    empty = 20 - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {pct}%"
    
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


async def upload_to_vk_by_file(file_path, title="Video", msg=None):
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

    file_size = os.path.getsize(file_path)
    uploaded = 0
    last_edit = 0

    try:
        async with aiohttp.ClientSession() as s:
            # Read file in chunks with progress
            class ProgressReader:
                def __init__(self, fp, total, msg_obj):
                    self.fp = open(fp, "rb")
                    self.total = total
                    self.uploaded = 0
                    self.msg = msg_obj
                    self.last_update = 0

                async def read_chunk(self, size):
                    chunk = self.fp.read(size)
                    if chunk:
                        self.uploaded += len(chunk)
                        pct = int(self.uploaded / self.total * 100)
                        import time
                        now = time.time()
                        if self.msg and (now - self.last_update > 2):
                            self.last_update = now
                            mb_done = self.uploaded / (1024 * 1024)
                            mb_total = self.total / (1024 * 1024)
                            bar = progress_bar(pct)
                            try:
                                await self.msg.edit_text(
                                    f"⬆️ *Uploading to VK...*\n\n"
                                    f"{bar}\n"
                                    f"📊 {pct}% • {mb_done:.1f}/{mb_total:.1f} MB",
                                    parse_mode="Markdown"
                                )
                            except:
                                pass
                    return chunk

                def close(self):
                    self.fp.close()

            reader = ProgressReader(file_path, file_size, msg)

            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("video_file", f, filename="video.mp4")
                
                if msg:
                    try:
                        await msg.edit_text(
                            f"⬆️ *Uploading to VK...*\n\n"
                            f"{progress_bar(0)}\n"
                            f"📊 0% • 0/{file_size/(1024*1024):.1f} MB",
                            parse_mode="Markdown"
                        )
                    except:
                        pass

                async with s.post(upload_url, data=form) as r:
                    await r.json()

            if msg:
                try:
                    await msg.edit_text(
                        f"⬆️ *Upload Complete!*\n\n"
                        f"{progress_bar(100)}\n"
                        f"📊 100% • {file_size/(1024*1024):.1f} MB ✅",
                        parse_mode="Markdown"
                    )
                except:
                    pass

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

    msg = await update.message.reply_text(
        f"⬇️ *Downloading from Telegram...*\n\n"
        f"{progress_bar(0)}",
        parse_mode="Markdown"
    )

    try:
        file = await context.bot.get_file(video.file_id)
        file_path = f"/tmp/{video.file_id}.mp4"
        await file.download_to_drive(file_path)

        await msg.edit_text(
            f"⬇️ *Download Complete!* ✅\n\n"
            f"{progress_bar(100)}\n\n"
            f"⬆️ Starting upload...",
            parse_mode="Markdown"
        )

        title = update.message.caption or "Video"
        result = await upload_to_vk_by_file(file_path, title, msg)

        try:
            os.remove(file_path)
        except:
            pass

        if result["ok"]:
            await msg.edit_text(
                f"⏳ *VK Processing...*\n\n"
                f"{progress_bar(0)}\n"
                f"🔄 Waiting...",
                parse_mode="Markdown"
            )

            vk_url = result["vk_url"]
            oid = result["owner_id"]
            vid = result["video_id"]

            processed = False
            for i in range(30):
                await asyncio.sleep(10)
                pct = min(((i + 1) * 3), 95)
                try:
                    check = await vk_api("video.get", {
                        "owner_id": str(oid),
                        "videos": f"{oid}_{vid}",
                    })
                    if "response" in check:
                        items = check["response"].get("items", [])
                        if items:
                            v = items[0]
                            if v.get("player") or v.get("files"):
                                processed = True
                                pct = 100
                            if v.get("duration", 0) > 0 and v.get("width", 0) > 0:
                                processed = True
                                pct = 100
                except:
                    pass

                try:
                    await msg.edit_text(
                        f"⏳ *VK Processing...*\n\n"
                        f"{progress_bar(pct)}\n"
                        f"🔄 {pct}% • {(i+1)*10} sec",
                        parse_mode="Markdown"
                    )
                except:
                    pass

                if processed:
                    break

            stream = await get_stream_link(vk_url)
            status = "✅ Ready!" if processed else "⏳ Still processing"

            reply = f"✅ *Upload Complete!*\n\n📺 VK: `{vk_url}`\n"
            if stream:
                reply += f"🔗 Stream: `{stream}`\n"
            reply += f"\n📌 Status: {status}"
            await msg.edit_text(reply, parse_mode="Markdown")
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

    msg = await update.message.reply_text("⬇️ Starting download...")
    file_path = f"/tmp/upload_{update.message.message_id}.mp4"

    try:
        # ═══ STEP 1: DOWNLOAD ═══
        async with aiohttp.ClientSession() as s:
            async with s.get(text, timeout=aiohttp.ClientTimeout(total=600), headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }) as r:
                if r.status != 200:
                    await msg.edit_text(f"❌ Download failed: HTTP {r.status}")
                    return

                total = r.content_length or 0
                downloaded = 0
                import time
                last_edit = 0

                with open(file_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_edit > 2:
                            last_edit = now
                            mb = downloaded / (1024 * 1024)
                            if total > 0:
                                pct = int(downloaded / total * 100)
                                total_mb = total / (1024 * 1024)
                                bar = progress_bar(pct)
                                try:
                                    await msg.edit_text(
                                        f"⬇️ *Downloading...*\n\n"
                                        f"{bar}\n"
                                        f"📊 {pct}% • {mb:.1f}/{total_mb:.1f} MB",
                                        parse_mode="Markdown"
                                    )
                                except:
                                    pass
                            else:
                                try:
                                    await msg.edit_text(
                                        f"⬇️ *Downloading...*\n\n"
                                        f"📊 {mb:.1f} MB downloaded",
                                        parse_mode="Markdown"
                                    )
                                except:
                                    pass

        file_size = os.path.getsize(file_path)
        await msg.edit_text(
            f"⬇️ *Download Complete!* ✅\n\n"
            f"{progress_bar(100)}\n"
            f"📊 {file_size/(1024*1024):.1f} MB\n\n"
            f"⬆️ Starting upload...",
            parse_mode="Markdown"
        )

        # ═══ STEP 2: UPLOAD ═══
        title = text.split("/")[-1].split("?")[0].replace("-", " ").replace(".mp4", "") or "Video"
        result = await upload_to_vk_by_file(file_path, title, msg)

        try:
            os.remove(file_path)
        except:
            pass

        if result["ok"]:
            # ═══ STEP 3: PROCESSING CHECK ═══
            await msg.edit_text(
                f"⏳ *VK Processing...*\n\n"
                f"{progress_bar(0)}\n"
                f"🔄 Waiting for VK to process video...",
                parse_mode="Markdown"
            )

            vk_url = result["vk_url"]
            oid = result["owner_id"]
            vid = result["video_id"]

            processed = False
            import time
            for i in range(30):  # Max 5 min wait (30 x 10sec)
                await asyncio.sleep(10)
                pct = min(((i + 1) * 3), 95)

                try:
                    check = await vk_api("video.get", {
                        "owner_id": str(oid),
                        "videos": f"{oid}_{vid}",
                    })
                    if "response" in check:
                        items = check["response"].get("items", [])
                        if items:
                            v = items[0]
                            # Check if video has player/files
                            if v.get("player") or v.get("files"):
                                processed = True
                                pct = 100
                            # Check image (if processed, better thumbnail exists)
                            if v.get("image") and len(v.get("image", [])) > 2:
                                processed = True
                                pct = 100
                            # Check duration > 0
                            if v.get("duration", 0) > 0 and v.get("width", 0) > 0:
                                processed = True
                                pct = 100
                except:
                    pass

                bar = progress_bar(pct)
                try:
                    await msg.edit_text(
                        f"⏳ *VK Processing...*\n\n"
                        f"{bar}\n"
                        f"🔄 {pct}% • {(i+1)*10} sec elapsed",
                        parse_mode="Markdown"
                    )
                except:
                    pass

                if processed:
                    break

            # ═══ FINAL RESULT ═══
            stream = await get_stream_link(vk_url)
            status = "✅ Ready!" if processed else "⏳ Still processing (check later)"

            reply = (
                f"✅ *Upload Complete!*\n\n"
                f"📺 VK: `{vk_url}`\n"
            )
            if stream:
                reply += f"🔗 Stream: `{stream}`\n"
            reply += f"\n📌 Status: {status}"

            await msg.edit_text(reply, parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Upload failed: {result['error']}")

    except asyncio.TimeoutError:
        await msg.edit_text("❌ Download timeout (10 min limit)")
        try:
            os.remove(file_path)
        except:
            pass
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")
        try:
            os.remove(file_path)
        except:
            pass
            

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
