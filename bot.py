"""Telegram entry point.  All long work runs off the update handler."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from engine import MatchFacts, analyse_clip, download_url, make_overlay

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger(__name__)
ROOT = Path(__file__).parent
WORK = ROOT / "jobs"
WORK.mkdir(exist_ok=True)
MAX_TG = int(os.getenv("MAX_TELEGRAM_DOWNLOAD_MB", "19")) * 1024 * 1024
URL_RE = re.compile(r"https?://\S+", re.I)


@dataclass
class Session:
    logos: list[Path] = field(default_factory=list)
    video: Path | None = None
    url: str | None = None
    note: str = ""
    running: bool = False
    job_dir: Path | None = None


SESSIONS: dict[int, Session] = {}


def session_for(user_id: int) -> Session:
    return SESSIONS.setdefault(user_id, Session())


def source_ready(s: Session) -> bool:
    return s.video is not None or s.url is not None


def status(s: Session) -> str:
    bits = [f"logos: {len(s.logos)}/2", "video: ready" if s.video else "video: none", "link: ready" if s.url else "link: none"]
    return " • ".join(bits)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    await update.effective_message.reply_text(
        "Football overlay bot is ready. Send two logo images and a short video or public link in any order. "
        "I keep your current upload; /start does not reset it.\n\n"
        "I will return a #00FF00 green-screen MP4. /status shows progress; /new clears only this session.\n"
        f"Current: {status(s)}"
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    old = SESSIONS.pop(update.effective_user.id, None)
    if old and old.job_dir:
        shutil.rmtree(old.job_dir, ignore_errors=True)
    await update.effective_message.reply_text("New session created. Send two logos and then a clip or public link.")


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    missing = []
    if len(s.logos) < 2: missing.append(f"{2-len(s.logos)} logo image")
    if not source_ready(s): missing.append("a video or public URL")
    suffix = " Processing now." if s.running else ("Ready to process." if not missing else " Still needed: " + ", ".join(missing) + ".")
    await update.effective_message.reply_text(status(s) + suffix)


async def save_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    if len(s.logos) >= 2:
        await update.effective_message.reply_text("I already have two logos. Use /new only if you want to replace them.")
        return
    msg = update.effective_message
    photo = msg.photo[-1] if msg.photo else msg.document
    if getattr(photo, "file_size", 0) > 10 * 1024 * 1024:
        await msg.reply_text("That logo is over 10 MB. Please send a smaller PNG/JPG/WebP image.")
        return
    if not s.job_dir:
        s.job_dir = Path(tempfile.mkdtemp(prefix=f"tg-{update.effective_user.id}-", dir=WORK))
    try:
        target = s.job_dir / f"logo-{len(s.logos)+1}.img"
        await (await photo.get_file()).download_to_drive(target)
        s.logos.append(target)
        await msg.reply_text(f"Logo {len(s.logos)}/2 saved. {status(s)}")
        await maybe_process(update, context, s)
    except Exception:
        LOG.exception("logo download failed")
        await msg.reply_text("I could not download that logo. Please send it again as PNG, JPG, or WebP.")


async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    msg = update.effective_message
    media = msg.video or msg.document
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_TG:
        await msg.reply_text(
            f"This file is {size/1024/1024:.1f} MB. With Telegram's normal cloud Bot API I can download up to "
            f"{MAX_TG/1024/1024:.0f} MB. Send a public link instead, or use a Local Bot API server for large uploads."
        )
        return
    if not s.job_dir:
        s.job_dir = Path(tempfile.mkdtemp(prefix=f"tg-{update.effective_user.id}-", dir=WORK))
    try:
        await msg.reply_text("Video received; downloading it now…")
        target = s.job_dir / "source.mp4"
        await (await media.get_file()).download_to_drive(target)
        s.video = target
        if msg.caption: s.note = msg.caption
        await msg.reply_text("Video saved. " + status(s))
        await maybe_process(update, context, s)
    except Exception:
        LOG.exception("video download failed")
        await msg.reply_text("I could not download that video. Please retry or send a public video link.")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    value = update.effective_message.text.strip()
    found = URL_RE.search(value)
    if found:
        s.url = found.group(0).rstrip(".,)")
        s.note = value.replace(found.group(0), "").strip()
        await update.effective_message.reply_text("Public link saved. I will download it when both logos are ready. " + status(s))
        await maybe_process(update, context, s)
    else:
        s.note = value
        await update.effective_message.reply_text("Match note saved. It will be used as a hint, not treated as a confirmed score.")


async def maybe_process(update: Update, context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    if s.running or len(s.logos) != 2 or not source_ready(s): return
    s.running = True
    context.application.create_task(run_job(update, context, s), update=update)


async def run_job(update: Update, context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    msg = await update.effective_message.reply_text("Everything is ready. I’m checking the source and starting analysis…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
        if s.video is None:
            await msg.edit_text("Downloading the public link…")
            s.video = await asyncio.to_thread(download_url, s.url, s.job_dir)
        await msg.edit_text("Sampling the clip and verifying match/goal events…")
        facts = await asyncio.to_thread(analyse_clip, s.video, s.note)
        await msg.edit_text("Building the green-screen score timeline…")
        output = await asyncio.to_thread(make_overlay, s.video, s.logos[0], s.logos[1], facts, s.job_dir)
        confidence = "verified" if facts.confidence >= 0.75 else "UNVERIFIED"
        caption = f"Green-screen overlay ready ({confidence}). {facts.home} {facts.final_home}-{facts.final_away} {facts.away}."
        await context.bot.send_document(update.effective_chat.id, document=output.open("rb"), caption=caption)
        await msg.delete()
    except Exception as exc:
        LOG.exception("job failed")
        await msg.edit_text(f"I couldn’t finish this job: {str(exc)[:700]}\nYour logos/link are still saved. Fix the source and send it again; /start is not needed.")
    finally:
        s.running = False


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and add the BotFather token.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, save_logo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, save_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__": main()
