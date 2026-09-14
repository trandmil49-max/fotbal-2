"""Telegram entry point. All long work runs off the update handler.

Bu sürümde:
- Link (YouTube vb.) desteği TAMAMEN KALDIRILDI - kullanıcı sadece telefonundan
  video yüklüyor, link hiç güvenilir çalışmıyordu.
- Video + 2 logo TEK ALBÜM olarak birlikte gönderilebiliyor (en kolay yöntem).
- Her yeni video, önceki maçın logolarını OTOMATİK temizliyor - böylece
  farklı maçların logoları/videoları birbirine karışmıyor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import gc
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from engine import analyse_clip, make_overlay

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger(__name__)
ROOT = Path(__file__).parent
WORK = ROOT / "jobs"
WORK.mkdir(exist_ok=True)
MAX_TG = int(os.getenv("MAX_TELEGRAM_DOWNLOAD_MB", "19")) * 1024 * 1024
GROUP_DEBOUNCE_SECONDS = 1.5  # albümdeki tüm parçaların gelmesini bekleme süresi


@dataclass
class Session:
    logos: list[Path] = field(default_factory=list)
    video: Path | None = None
    note: str = ""
    running: bool = False
    job_dir: Path | None = None


SESSIONS: dict[int, Session] = {}
# media_group_id -> {"messages": [...], "chat_id":.., "user_id":.., "task": Task}
GROUPS: dict[str, dict] = {}


def session_for(user_id: int) -> Session:
    return SESSIONS.setdefault(user_id, Session())


def reset_session(user_id: int) -> None:
    """Bir iş bitince ya da yeni bir video gelince eski logo/video kalıntısı
    kalmasın diye oturumu tamamen temizliyoruz. Bu, önceki bir maçın
    logolarının yanlışlıkla yeni bir videoyla eşleşmesini (yanlış maç
    karışması hatasını) kökten önlüyor."""
    old = SESSIONS.pop(user_id, None)
    if old and old.job_dir:
        shutil.rmtree(old.job_dir, ignore_errors=True)


def status(s: Session) -> str:
    return f"logo: {len(s.logos)}/2 • video: {'hazır' if s.video else 'yok'}"


def _job_dir_for(user_id: int) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"tg-{user_id}-", dir=WORK))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Futbol skor overlay botu hazır.\n\n"
        "EN KOLAY YÖNTEM: video + iki takım logosunu (3 dosyayı) hepsini "
        "AYNI ANDA, tek bir albüm/galeri seçimi olarak gönder - otomatik işlerim.\n\n"
        "İstersen tek tek de gönderebilirsin (video, sonra logo 1, sonra logo 2). "
        "Her yeni video önceki logoları otomatik siler, böylece farklı maçlar karışmaz.\n\n"
        "/status - durumu gösterir\n/new - oturumu temizler"
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_session(update.effective_user.id)
    await update.effective_message.reply_text("Oturum temizlendi. Video + iki logo gönder.")


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    missing = []
    if len(s.logos) < 2: missing.append(f"{2-len(s.logos)} logo görseli")
    if not s.video: missing.append("video")
    suffix = " Şu anda işleniyor." if s.running else (" İşlem için hazır." if not missing else " Hâlâ gerekli: " + ", ".join(missing) + ".")
    await update.effective_message.reply_text(status(s) + suffix)


async def unrecognized_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Link desteği artık yok - sadece video + 2 logo gönder (tek tek ya da hepsini birden albüm olarak)."
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    group_id = msg.media_group_id
    if group_id:
        # Albüm olarak gelen dosyalar ayrı ayrı update'ler halinde ulaşır,
        # hepsi toplanana kadar kısa bir süre bekliyoruz (debounce).
        bucket = GROUPS.setdefault(group_id, {
            "messages": [], "chat_id": update.effective_chat.id, "user_id": update.effective_user.id, "task": None,
        })
        bucket["messages"].append(msg)
        if bucket["task"]:
            bucket["task"].cancel()
        bucket["task"] = context.application.create_task(
            _process_group_after_delay(group_id, context), update=update
        )
        return
    # Tek tek gönderilen dosyalar - eski akış (geriye dönük uyumlu)
    is_video = bool(msg.video) or bool(msg.document and msg.document.mime_type and "video" in msg.document.mime_type)
    if is_video:
        await save_video(update, context)
    else:
        await save_logo(update, context)


async def _process_group_after_delay(group_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(GROUP_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    bucket = GROUPS.pop(group_id, None)
    if not bucket:
        return
    await process_batch(bucket["messages"], bucket["chat_id"], bucket["user_id"], context)


async def process_batch(messages: list, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    video_msg = None
    photo_msgs = []
    for m in messages:
        is_video = bool(m.video) or bool(m.document and m.document.mime_type and "video" in m.document.mime_type)
        if is_video and video_msg is None:
            video_msg = m
        elif m.photo or (m.document and m.document.mime_type and "image" in m.document.mime_type):
            photo_msgs.append(m)

    if video_msg is None or len(photo_msgs) < 2:
        await context.bot.send_message(
            chat_id,
            "Albümde 1 video ve 2 logo görseli olmalı. "
            f"Bulduğum: video {'var' if video_msg else 'yok'}, logo {len(photo_msgs)}/2. Tekrar dener misin?",
        )
        return

    reset_session(user_id)  # her toplu iş kendi başına - eski maçla karışmasın
    s = session_for(user_id)
    s.job_dir = _job_dir_for(user_id)
    s.running = True

    status_msg = await context.bot.send_message(chat_id, "3 dosya alındı, indiriyorum…")
    try:
        video_media = video_msg.video or video_msg.document
        video_size = getattr(video_media, "file_size", 0) or 0
        if video_size > MAX_TG:
            await status_msg.edit_text(
                f"Video {video_size/1024/1024:.1f} MB - en fazla {MAX_TG/1024/1024:.0f} MB indirebilirim. "
                "Videoyu biraz daha düşük çözünürlükte (360p/480p) tekrar gönder."
            )
            s.running = False
            return
        video_path = s.job_dir / "source.mp4"
        await (await video_media.get_file()).download_to_drive(video_path)
        s.video = video_path
        if video_msg.caption:
            s.note = video_msg.caption

        for i, pm in enumerate(photo_msgs[:2]):
            photo = pm.photo[-1] if pm.photo else pm.document
            logo_path = s.job_dir / f"logo-{i+1}.img"
            await (await photo.get_file()).download_to_drive(logo_path)
            s.logos.append(logo_path)

        await run_job(status_msg, chat_id, s, context)
    except Exception:
        LOG.exception("batch failed")
        await status_msg.edit_text("Bu 3 dosya işlenirken bir hata oldu. Tekrar gönder, ya da video boyutunu küçült.")
        s.running = False


async def save_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = session_for(update.effective_user.id)
    if len(s.logos) >= 2:
        await update.effective_message.reply_text(
            "Zaten iki logo var. Yeni bir maç için video göndermen yeterli (eski logoları otomatik temizler), "
            "ya da /new yaz."
        )
        return
    msg = update.effective_message
    photo = msg.photo[-1] if msg.photo else msg.document
    if getattr(photo, "file_size", 0) > 10 * 1024 * 1024:
        await msg.reply_text("Bu logo 10 MB'den büyük. Daha küçük PNG, JPG veya WebP olarak gönder.")
        return
    if not s.job_dir:
        s.job_dir = _job_dir_for(update.effective_user.id)
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
    user_id = update.effective_user.id
    msg = update.effective_message
    media = msg.video or msg.document
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_TG:
        await msg.reply_text(
            f"Bu dosya {size/1024/1024:.1f} MB. En fazla {MAX_TG/1024/1024:.0f} MB indirebilirim. "
            "Videoyu biraz daha düşük çözünürlükte (360p/480p) tekrar gönder."
        )
        return
    # YENİ bir video = YENİ bir maç demektir - önceki maçın logolarını
    # otomatik temizliyoruz ki yanlış logolarla eşleşmesin.
    reset_session(user_id)
    s = session_for(user_id)
    s.job_dir = _job_dir_for(user_id)
    try:
        await msg.reply_text("Video alındı; indiriyorum…")
        target = s.job_dir / "source.mp4"
        await (await media.get_file()).download_to_drive(target)
        s.video = target
        if msg.caption: s.note = msg.caption
        await msg.reply_text("Video kaydedildi. Şimdi iki takımın logosunu gönder. " + status(s))
    except Exception:
        LOG.exception("video download failed")
        await msg.reply_text("Bu video indirilemedi. Tekrar dene.")


async def maybe_process(update: Update, context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    if s.running or len(s.logos) != 2 or not s.video:
        return
    s.running = True
    status_msg = await update.effective_message.reply_text("Her şey hazır. Analiz başlıyor…")
    context.application.create_task(run_job(status_msg, update.effective_chat.id, s, context), update=update)


async def run_job(status_msg, chat_id: int, s: Session, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await status_msg.edit_text("Video örnekleniyor; goller doğrulanıyor…")
        facts = await asyncio.to_thread(analyse_clip, s.video, s.note)
        gc.collect()  # analizden kalan hafızayı, overlay üretmeden önce boşalt
        await status_msg.edit_text("Yeşil ekran skor zaman çizelgesi oluşturuluyor…")
        output = await asyncio.to_thread(make_overlay, s.video, s.logos[0], s.logos[1], facts, s.job_dir)
        confidence = "doğrulandı" if facts.confidence >= 0.75 else "DOĞRULANMADI"
        caption = f"Yeşil ekran overlay hazır ({confidence}). {facts.final_home}-{facts.final_away}."
        if facts.analysis_note:
            caption += "\nNot: " + facts.analysis_note
        with output.open("rb") as f:
            await context.bot.send_document(chat_id, document=f, caption=caption)
        await status_msg.delete()
    except Exception as exc:
        LOG.exception("job failed")
        await status_msg.edit_text(f"İşlem tamamlanamadı: {str(exc)[:700]}\nVideoyu ve logoları tekrar gönder.")
    finally:
        s.running = False


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN eksik. Railway Variables bölümüne BotFather token'ını ekle.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.VIDEO | filters.Document.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unrecognized_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__": main()
