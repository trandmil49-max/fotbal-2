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
    bits = [f"logo: {len(s.logos)}/2", "video: hazır" if s.video else "video: yok", "link: hazır" if s.url else "link: yok"]
    return " • ".join(bits)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    await update.effective_message.reply_text(
        "Futbol skor overlay botu hazır. İki logo görselini ve kısa videoyu veya herkese açık linki istediğin sırayla gönder. "
        "Mevcut yüklemeni korurum; /start hiçbir şeyi sıfırlamaz.\n\n"
        "Sana #00FF00 yeşil ekranlı MP4 vereceğim. /status durumu gösterir; sadece /new bu oturumu temizler.\n"
        f"Şu an: {status(s)}"
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    old = SESSIONS.pop(update.effective_user.id, None)
    if old and old.job_dir:
        shutil.rmtree(old.job_dir, ignore_errors=True)
    await update.effective_message.reply_text("Yeni oturum oluşturuldu. İki logo ile video veya herkese açık link gönder.")


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    missing = []
    if len(s.logos) < 2: missing.append(f"{2-len(s.logos)} logo görseli")
    if not source_ready(s): missing.append("video veya herkese açık link")
    suffix = " Şu anda işleniyor." if s.running else ("İşlem için hazır." if not missing else " Hâlâ gerekli: " + ", ".join(missing) + ".")
    await update.effective_message.reply_text(status(s) + suffix)


async def save_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    if len(s.logos) >= 2:
        await update.effective_message.reply_text("Zaten iki logo var. Değiştirmek istersen yalnızca /new kullan.")
        return
    msg = update.effective_message
    photo = msg.photo[-1] if msg.photo else msg.document
    if getattr(photo, "file_size", 0) > 10 * 1024 * 1024:
        await msg.reply_text("Bu logo 10 MB'den büyük. Daha küçük PNG, JPG veya WebP olarak gönder.")
        return
    if not s.job_dir:
        s.job_dir = Path(tempfile.mkdtemp(prefix=f"tg-{update.effective_user.id}-", dir=WORK))
    try:
        target = s.job_dir / f"logo-{len(s.logos)+1}.img"
        await (await photo.get_file()).download_to_drive(target)
        s.logos.append(target)
        await msg.reply_text(f"Logo {len(s.logos)}/2 kaydedildi. {status(s)}")
        await maybe_process(update, context, s)
    except Exception:
        LOG.exception("logo download failed")
        await msg.reply_text("Bu logo indirilemedi. PNG, JPG veya WebP olarak tekrar gönder.")


async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    msg = update.effective_message
    media = msg.video or msg.document
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_TG:
        await msg.reply_text(
            f"Bu dosya {size/1024/1024:.1f} MB. Normal Telegram Bulut Bot API ile en fazla "
            f"{MAX_TG/1024/1024:.0f} MB indirebilirim. Herkese açık link gönder veya büyük yüklemeler için Local Bot API sunucusu kullan."
        )
        return
    if not s.job_dir:
        s.job_dir = Path(tempfile.mkdtemp(prefix=f"tg-{update.effective_user.id}-", dir=WORK))
    try:
        await msg.reply_text("Video alındı; şimdi indiriyorum…")
        target = s.job_dir / "source.mp4"
        await (await media.get_file()).download_to_drive(target)
        s.video = target
        if msg.caption: s.note = msg.caption
        await msg.reply_text("Video kaydedildi. " + status(s))
        await maybe_process(update, context, s)
    except Exception:
        LOG.exception("video download failed")
        await msg.reply_text("Bu video indirilemedi. Tekrar dene veya herkese açık video linki gönder.")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    value = update.effective_message.text.strip()
    found = URL_RE.search(value)
    if found:
        s.url = found.group(0).rstrip(".,)")
        s.note = value.replace(found.group(0), "").strip()
        await update.effective_message.reply_text("Herkese açık link kaydedildi. İki logo da hazır olunca indireceğim. " + status(s))
        await maybe_process(update, context, s)
    else:
        s.note = value
        await update.effective_message.reply_text("Maç notu kaydedildi. Bunu ipucu olarak kullanırım; doğrulanmış skor saymam.")


async def maybe_process(update: Update, context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    if s.running or len(s.logos) != 2 or not source_ready(s): return
    s.running = True
    context.application.create_task(run_job(update, context, s), update=update)


async def run_job(update: Update, context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    msg = await update.effective_message.reply_text("Her şey hazır. Kaynağı kontrol edip analizi başlatıyorum…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
        source_problem = ""
        if s.video is None:
            await msg.edit_text("Herkese açık link indiriliyor…")
            try:
                s.video = await asyncio.to_thread(download_url, s.url, s.job_dir)
            except RuntimeError as exc:
                # YouTube can refuse cloud IPs. Send a useful template instead
                # of abandoning the user's logos and session.
                source_problem = str(exc)
        if s.video:
            await msg.edit_text("Video örnekleniyor; maç ve sayılan goller doğrulanıyor…")
            facts = await asyncio.to_thread(analyse_clip, s.video, s.note)
        else:
            facts = MatchFacts(analysis_note="Kaynak indirilemedi; 10 saniyelik başlangıç overlay'i oluşturuldu")
        await msg.edit_text("Yeşil ekran skor zaman çizelgesi oluşturuluyor…")
        output = await asyncio.to_thread(make_overlay, s.video, s.logos[0], s.logos[1], facts, s.job_dir)
        confidence = "doğrulandı" if facts.confidence >= 0.75 else "DOĞRULANMADI"
        caption = f"Yeşil ekran overlay hazır ({confidence}). {facts.home} {facts.final_home}-{facts.final_away} {facts.away}."
        if facts.analysis_note:
            caption += "\nNot: " + facts.analysis_note
        if source_problem:
            caption += "\nNot: Linkteki video indirilemediği için 10 saniyelik başlangıç şablonu üretildi."
        await context.bot.send_document(update.effective_chat.id, document=output.open("rb"), caption=caption)
        await msg.delete()
    except Exception as exc:
        LOG.exception("job failed")
        await msg.edit_text(f"İşlem tamamlanamadı: {str(exc)[:700]}\nLogo ve linklerin kayıtlı. Sorunu düzeltip yeniden gönder; /start gerekli değil.")
    finally:
        s.running = False


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN eksik. Railway Variables bölümüne BotFather token'ını ekle.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, save_logo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, save_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__": main()
