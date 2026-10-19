import re
import os
import html
import time
import math
import shutil
import yt_dlp
import logging
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("8994282970:AAHqbEmQl2aFSMhHWm7-dDCs3lqjRZ793_k")
if not BOT_TOKEN:
    raise RuntimeError(
        "متغیر محیطی TELEGRAM_BOT_TOKEN تنظیم نشده. "
        "توکن بات خودت رو از @BotFather بگیر و در Environment Variables ست کن."
    )

MAX_TELEGRAM_MB = 50
MAX_TELEGRAM_BYTES = MAX_TELEGRAM_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 2
USER_COOLDOWN_SECONDS = 10

ARIA2C_AVAILABLE = shutil.which("aria2c") is not None

QUALITY_LADDER = [
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "worst",
]

YOUTUBE_QUALITY_LADDER = [
    "18",
    "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "worst",
]

URL_REGEX = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
LAST_REQUEST_TIME: dict[int, float] = {}

def check_user_cooldown(user_id: int) -> float:
    now = time.monotonic()
    last = LAST_REQUEST_TIME.get(user_id)
    if last is not None:
        elapsed = now - last
        if elapsed < USER_COOLDOWN_SECONDS:
            return USER_COOLDOWN_SECONDS - elapsed
    LAST_REQUEST_TIME[user_id] = now
    return 0.0

def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def _base_ydl_opts(url: str, out_dir: str, fmt: str) -> dict:
    output_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    opts = {
        "format": fmt,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "postprocessor_args": {
            "ffmpeg": ["-movflags", "+faststart"]
        },
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 1,
        "writesubtitles": False,
        "writethumbnail": False,
        "writeinfojson": False,
        "cachedir": False,
        "hls_prefer_native": True,
    }
    if ARIA2C_AVAILABLE:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }
    if is_youtube_url(url):
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android"]
            }
        }
    return opts

def download_with_format(url: str, out_dir: str, fmt: str) -> tuple[str | None, str | None]:
    ydl_opts = _base_ydl_opts(url, out_dir, fmt)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") if isinstance(info, dict) else None
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        for ext in (".mp4", ".mkv", ".webm"):
            candidate = base + ext
            if os.path.exists(candidate):
                return candidate, title
        return (filename, title) if os.path.exists(filename) else (None, title)

def generate_thumbnail(video_path: str, out_dir: str) -> str | None:
    thumb_path = os.path.join(out_dir, "thumb.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "00:00:01", "-i", video_path,
                "-vframes", "1",
                "-vf", "scale=320:-1",
                thumb_path
            ],
            capture_output=True, timeout=30, check=True
        )
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.warning("Failed to generate thumbnail: %s", e)
    return None

def _looks_like_nonformat_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        p in text
        for p in (
            "404", "403", "410",
            "unable to download webpage",
            "video unavailable",
            "private video",
            "this video is unavailable",
            "unable to extract",
            "sign in to confirm",
            "connection refused",
            "name or service not known",
            "temporary failure in name resolution",
            "no video formats found",
        )
    )

def download_best_fitting(url: str, out_dir: str) -> tuple[str | None, str, str | None]:
    ladder = YOUTUBE_QUALITY_LADDER if is_youtube_url(url) else QUALITY_LADDER

    last_error = ""
    for fmt in ladder:
        try:
            filepath, title = download_with_format(url, out_dir, fmt)
        except yt_dlp.utils.UnsupportedError as e:
            return None, "unsupported", None
        except Exception as e:
            last_error = str(e)
            logger.warning("دانلود با فرمت %s شکست خورد: %s", fmt, e)
            if _looks_like_nonformat_error(e):
                break
            continue

        if not filepath or not os.path.exists(filepath):
            continue

        size = os.path.getsize(filepath)
        if size <= MAX_TELEGRAM_BYTES:
            return filepath, "ok", title

        os.remove(filepath)
        logger.info(
            "فایل با فرمت %s حجمش %.1f مگابایت بود، رفتن سراغ کیفیت پایین‌تر",
            fmt,
            size / (1024 * 1024),
        )

    if last_error:
        return None, f"خطا در دانلود: {last_error}", None
    return None, "even_lowest_quality_too_big", None

def _is_reply_target_missing(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return (
        "replied message not found" in text
        or "message to reply not found" in text
        or "message to be replied not found" in text
        or "reply message not found" in text
        or ("message" in text and "not found" in text)
    )

def _is_edit_target_missing(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return (
        "message to edit not found" in text
        or "message to delete not found" in text
        or "message can't be edited" in text
        or ("message" in text and "not found" in text)
    )

async def safe_reply_text(msg, chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    try:
        return await msg.reply_text(text, **kwargs)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_reply_text(msg, chat_id, context, text, **kwargs)
    except BadRequest as e:
        if _is_reply_target_missing(e):
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=getattr(msg, "message_thread_id", None),
                **kwargs,
            )
        raise

async def safe_edit_text(status_msg, chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    try:
        return await status_msg.edit_text(text, **kwargs)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_edit_text(status_msg, chat_id, context, text, **kwargs)
    except BadRequest as e:
        if _is_edit_target_missing(e):
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=getattr(status_msg, "message_thread_id", None),
                **kwargs,
            )
        raise

async def safe_delete(status_msg):
    try:
        await status_msg.delete()
    except BadRequest:
        pass

async def safe_reply_video(msg, chat_id: int, context: ContextTypes.DEFAULT_TYPE, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    try:
        return await msg.reply_video(**kwargs)
    except RetryAfter as e:
        if "video" in kwargs and hasattr(kwargs["video"], "seek"):
            kwargs["video"].seek(0)
        if "thumbnail" in kwargs and hasattr(kwargs["thumbnail"], "seek"):
            kwargs["thumbnail"].seek(0)
        await asyncio.sleep(e.retry_after)
        return await safe_reply_video(msg, chat_id, context, **kwargs)
    except BadRequest as e:
        if _is_reply_target_missing(e):
            if "video" in kwargs and hasattr(kwargs["video"], "seek"):
                kwargs["video"].seek(0)
            if "thumbnail" in kwargs and hasattr(kwargs["thumbnail"], "seek"):
                kwargs["thumbnail"].seek(0)
            return await context.bot.send_video(
                chat_id=chat_id,
                message_thread_id=getattr(msg, "message_thread_id", None),
                **kwargs,
            )
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"سلام! <tg-emoji emoji-id='6325517457184261445'>👋</tg-emoji>\nلینک ویدیو از یوتیوب، اینستاگرام، توییتر/X، تیک‌تاک و هر چیزی که بتونم ازش پشتیبانی کنم رو بفرست تا برات دانلودش کنم.\n\nاگه فایل بزرگ باشه، خودکار کیفیت رو کم می‌کنم تا زیر <b>{MAX_TELEGRAM_MB} مگابایت</b> بشه.",
        parse_mode=ParseMode.HTML,
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    match = URL_REGEX.search(text)

    if not match:
        chat_type = msg.chat.type if msg.chat else "private"
        is_private = chat_type == "private"
        if is_private:
            await safe_reply_text(
                msg, msg.chat_id, context,
                "<tg-emoji emoji-id='5895763643726829873'>😑</tg-emoji> یه لینک معتبر بفرست (باید با <code>http://</code> یا <code>https://</code> شروع بشه).",
            )
        return

    url = match.group(0).rstrip(".,)")
    await process_url(update, context, url)

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    msg = update.message
    chat_id = msg.chat_id
    chat_type = msg.chat.type if msg.chat else "private"
    is_group = chat_type in ("group", "supergroup")

    user_id = msg.from_user.id if msg.from_user else chat_id
    remaining = check_user_cooldown(user_id)
    if remaining > 0:
        wait_seconds = math.ceil(remaining)
        await safe_reply_text(
            msg, chat_id, context,
            f"<tg-emoji emoji-id='5861931460486761846'>😰</tg-emoji> مشتی {wait_seconds} ثانیه صبر کن بعد لینک بده.",
        )
        return

    status_msg = await safe_reply_text(msg, chat_id, context, "<tg-emoji emoji-id='5854750635945107237'>🚀</tg-emoji> در صف دانلود...")

    async with DOWNLOAD_SEMAPHORE:
        await safe_edit_text(status_msg, chat_id, context, "<tg-emoji emoji-id='5854750635945107237'>🚀</tg-emoji> در حال دانلود... ممکنه کمی طول بکشه.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            filepath, status, title = await asyncio.to_thread(download_best_fitting, url, tmp_dir)

            if status == "unsupported":
                if is_group:
                    await safe_delete(status_msg)
                    return
                await safe_edit_text(status_msg, chat_id, context, "<tg-emoji emoji-id='6032606743500951856'>❌</tg-emoji> این لینک پشتیبانی نمی‌شه.")
                return

            if status == "even_lowest_quality_too_big":
                await safe_edit_text(
                    status_msg,
                    chat_id,
                    context,
                    f"<tg-emoji emoji-id='6325547178357949929'>😩</tg-emoji> حتی با پایین‌ترین کیفیت هم فایل بزرگ‌تر از <b>{MAX_TELEGRAM_MB} مگابایت</b> "
                    "هست و نمی‌تونم بفرستمش.",
                )
                return

            if not filepath:
                if is_group:
                    await safe_delete(status_msg)
                    return
                await safe_edit_text(
                    status_msg,
                    chat_id,
                    context,
                    f"<tg-emoji emoji-id='6032606743500951856'>❌</tg-emoji> نشد دانلودش کنم.\n<code>{html.escape(status)}</code>",
                )
                return

            thumb_path = await asyncio.to_thread(generate_thumbnail, filepath, tmp_dir)
            thumb_file = None

            caption_source = title or os.path.basename(filepath)
            caption = html.escape(caption_source)[:1024]

            try:
                await safe_edit_text(status_msg, chat_id, context, "<tg-emoji emoji-id='5782805273697592067'>⚡</tg-emoji> در حال آپلود به تلگرام...")
                with open(filepath, "rb") as f:
                    kwargs = {
                        "video": f,
                        "caption": caption,
                        "supports_streaming": True,
                        "read_timeout": 120,
                        "write_timeout": 120,
                        "connect_timeout": 60,
                    }
                    if thumb_path:
                        thumb_file = open(thumb_path, "rb")
                        kwargs["thumbnail"] = thumb_file
                    await safe_reply_video(msg, chat_id, context, **kwargs)
                await safe_delete(status_msg)
            except Exception as e:
                logger.exception("ارسال فایل شکست خورد")
                error_text = f"<tg-emoji emoji-id='6032606743500951856'>❌</tg-emoji>آپلود فایل شکست خورد: <code>{html.escape(str(e))}</code>"
                try:
                    await safe_edit_text(status_msg, chat_id, context, error_text)
                except Exception:
                    await context.bot.send_message(
                        chat_id=chat_id, text=error_text, parse_mode=ParseMode.HTML
                    )
            finally:
                if thumb_file:
                    try:
                        thumb_file.close()
                    except Exception:
                        pass

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    
if __name__ == "__main__":
    main()
