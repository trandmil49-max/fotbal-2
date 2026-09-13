"""Source download, cautious vision analysis, and deterministic overlay rendering."""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

GREEN = (0, 255, 0)  # exact chroma key colour, RGB


@dataclass
class Goal:
    second: float
    team: str  # home or away
    awarded: bool = True


@dataclass
class MatchFacts:
    home: str = "HOME"
    away: str = "AWAY"
    stage: str = ""
    season: str = ""
    confidence: float = 0.0
    goals: list[Goal] = field(default_factory=list)

    @property
    def final_home(self) -> int: return sum(g.team == "home" and g.awarded for g in self.goals)

    @property
    def final_away(self) -> int: return sum(g.team == "away" and g.awarded for g in self.goals)


def download_url(url: str, job_dir: Path) -> Path:
    """Download with a hard size cap and errors that Telegram can display."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("Bağlantı http:// veya https:// ile başlamalı.")
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp kurulu değil. Railway dağıtım kaydını kontrol edin.") from exc
    maximum = int(os.getenv("MAX_LINK_DOWNLOAD_MB", "300")) * 1024 * 1024
    destination = str(job_dir / "linked-source.%(ext)s")
    extractor_args: dict[str, dict[str, list[str]]] = {}
    po_token = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
    if po_token:
        extractor_args["youtube"] = {"player_client": ["default", "mweb"], "po_token": [f"mweb.gvs+{po_token}"]}
    opts = {"outtmpl": destination, "quiet": True, "noplaylist": True, "max_filesize": maximum,
            "format": "mp4[height<=1080]/mp4/best[height<=1080]/best", "retries": 3,
            "fragment_retries": 3, "extractor_args": extractor_args}
    cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if cookies_b64:
        try:
            cookie_path = job_dir / "youtube-cookies.txt"
            cookie_path.write_bytes(base64.b64decode(cookies_b64, validate=True))
            opts["cookiefile"] = str(cookie_path)
        except Exception as exc:
            raise RuntimeError("YOUTUBE_COOKIES_B64 geçersiz. Railway değişkenine cookies.txt içeriğinin Base64 hâlini girin.") from exc
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as exc:
        detail = str(exc)
        if "Sign in to confirm you’re not a bot" in detail or "Sign in to confirm you're not a bot" in detail:
            raise RuntimeError(
                "YouTube, Railway sunucusundan bu link için bot doğrulaması istiyor. Bu, bağlantı biçimi hatası değil. "
                "Railway Variables bölümüne yetkili bir YouTube oturumunun Base64 cookies.txt değerini YOUTUBE_COOKIES_B64, "
                "gerekiyorsa eşleşen PO Token'ı YOUTUBE_PO_TOKEN olarak ekleyip yeniden deneyin. "
                "Bu değerleri Telegram'a veya GitHub'a göndermeyin. Alternatif olarak videoyu doğrudan Telegram'a yükleyin."
            ) from exc
        if "Private video" in detail or "not available" in detail.lower():
            raise RuntimeError("Bu bağlantı herkese açık değil veya bu bölgeden erişilemiyor. Herkese açık başka bir video bağlantısı deneyin.") from exc
        raise RuntimeError(f"Bağlantı indirilemedi ({type(exc).__name__}): {detail[:450]}") from exc
    if not path.exists():
        candidates = sorted(job_dir.glob("linked-source.*"))
        if not candidates: raise RuntimeError("Kaynak indirme tamamlandı dedi fakat video dosyası oluşmadı.")
        path = candidates[0]
    if path.stat().st_size > maximum: raise RuntimeError("İndirilen video sunucu için belirlenen boyut sınırını aşıyor.")
    # A public title is useful corroboration for match identification, but never
    # used to create score events. Keep it local to the job.
    (job_dir / "source-details.txt").write_text(str(info.get("title") or ""), encoding="utf-8")
    return path


def _frames(path: Path, count: int = 12) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise RuntimeError("Video açılamadı. MP4/H.264 olarak tekrar yükleyin veya başka bir herkese açık bağlantı deneyin.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    if duration <= 0 or duration > 15 * 60: raise RuntimeError("Video süresi okunamadı veya video 15 dakikadan uzun.")
    positions = np.linspace(0, max(0, total - 1), count, dtype=int)
    result = []
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos)); ok, frame = cap.read()
        if ok: result.append(frame)
    cap.release()
    if not result: raise RuntimeError("Videoda okunabilir görüntü karesi bulunamadı.")
    return result, duration


def _vision(frames: list[np.ndarray], hint: str, duration: float) -> MatchFacts:
    key = os.getenv("OPENAI_API_KEY")
    if not key: return MatchFacts()
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI paketi kurulu değil. Railway dağıtım kaydını kontrol edin.") from exc
    # Four representative frames fit comfortably and avoid treating celebrations as proof.
    image_parts = []
    for frame in frames[::max(1, len(frames)//4)][:4]:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok: image_parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(encoded).decode()}})
    prompt = """Identify this football clip cautiously. Return JSON only with keys home, away, stage, season, confidence (0..1), goals.
goals must be an array of {second:number,team:'home'|'away',awarded:boolean}. Add an event ONLY when it is clearly an
AWARDED goal: visible scoreboard change, goal confirmation graphic, or unambiguous official confirmation. A shot,
celebration, offside, VAR review, penalty appeal, or disallowed goal is not an awarded goal. If uncertain, return no goal.
Do not infer a full-match score from a short highlight. The clip duration is %s seconds. User's untrusted hint: %s""" % (round(duration,1), hint or "none")
    content = [{"type":"text", "text":prompt}, *image_parts]
    try:
        response = OpenAI(api_key=key).chat.completions.create(
            model=os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"),
            response_format={"type":"json_object"}, messages=[{"role":"user", "content":content}], temperature=0,
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        raise RuntimeError(f"Görsel analiz başarısız oldu: {type(exc).__name__}: {exc}") from exc
    goals = []
    for x in data.get("goals", []):
        if x.get("awarded") is True and x.get("team") in ("home", "away"):
            goals.append(Goal(max(0, min(float(x.get("second", 0)), duration)), x["team"], True))
    return MatchFacts(str(data.get("home") or "HOME")[:28], str(data.get("away") or "AWAY")[:28],
                      str(data.get("stage") or "")[:20].upper(), str(data.get("season") or "")[:20],
                      max(0, min(float(data.get("confidence", 0)), 1)), sorted(goals, key=lambda g:g.second))


def analyse_clip(path: Path, hint: str) -> MatchFacts:
    # Around one frame per two seconds catches most edit transitions without
    # blindly trusting a single celebration frame. The vision request itself
    # selects evenly spaced representatives to control cost.
    _, duration = _frames(path, 2)
    frames, _ = _frames(path, min(60, max(12, int(duration / 2))))
    title_file = path.parent / "source-details.txt"
    if title_file.exists():
        hint = (hint + " | public source title: " + title_file.read_text(encoding="utf-8")[:180]).strip(" |")
    facts = _vision(frames, hint, duration)
    # An explicit human hint may provide labels, never score events. It is intentionally only a fallback.
    if facts.home == "HOME" and hint:
        m = re.search(r"([^,;]+?)\s+(?:vs\.?|v\.?|[-–])\s+([^,;]+)", hint, re.I)
        if m: facts.home, facts.away = m.group(1).strip()[:28], m.group(2).strip()[:28]
    return facts


def _font(size: int) -> ImageFont.ImageFont:
    # The condensed bold face matches the supplied large score reference and
    # is installed explicitly in the Railway Docker image.
    candidates = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for candidate in candidates:
        if Path(candidate).exists():
            try: return ImageFont.truetype(candidate, size)
            except OSError: pass
    return ImageFont.load_default()


def _logo(path: Path, size: int = 230) -> Image.Image:
    try: image = Image.open(path).convert("RGBA")
    except Exception as exc: raise RuntimeError(f"Logo açılamadı ({path.name}). PNG, JPG veya WebP gönder.") from exc
    px = np.asarray(image).copy()
    # Only remove near-white / near-black pixels connected to the border; white inside a crest remains.
    rgb = px[:,:,:3]; alpha = px[:,:,3]
    candidate = (((rgb.min(axis=2) > 238) | (rgb.max(axis=2) < 12)) & (alpha > 0)).astype(np.uint8)
    flood = np.zeros((candidate.shape[0]+2, candidate.shape[1]+2), np.uint8)
    cv2.floodFill(candidate, flood, (0,0), 2)
    px[candidate == 2, 3] = 0
    result = Image.fromarray(px).convert("RGBA")
    result.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0,0,0,0)); canvas.alpha_composite(result, ((size-result.width)//2, (size-result.height)//2))
    return canvas


def _center(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.ImageFont, fill=(255,255,255)) -> None:
    box = draw.textbbox((0,0), text, font=font, stroke_width=3)
    draw.text(((1080-(box[2]-box[0]))/2, y), text, font=font, fill=fill, stroke_width=3, stroke_fill=(0,0,0))


def make_overlay(source: Path, logo_home: Path, logo_away: Path, facts: MatchFacts, job_dir: Path) -> Path:
    frames, duration = _frames(source, 2)
    # TikTok-friendly vertical overlay. The content sits in the top safe area, the rest is pure green.
    width, height, fps = 1080, 1920, 12
    output = job_dir / "green-screen-score-overlay.mp4"
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened(): raise RuntimeError("MP4 yazıcısı kullanılamıyor. Railway dağıtım kaydını kontrol edin.")
    left, right = _logo(logo_home), _logo(logo_away)
    events = [g for g in facts.goals if g.awarded]
    home = away = event_index = 0
    total = max(1, int(duration * fps))
    for n in range(total):
        t = n / fps
        while event_index < len(events) and events[event_index].second <= t:
            if events[event_index].team == "home": home += 1
            else: away += 1
            event_index += 1
        image = Image.new("RGB", (width,height), GREEN); draw = ImageDraw.Draw(image)
        if facts.stage: _center(draw, facts.stage, 100, _font(54))
        elif facts.confidence < .75: _center(draw, "UNVERIFIED MATCH", 100, _font(42), (255,230,0))
        image.paste(left, (120,190), left); image.paste(right, (730,190), right)
        _center(draw, "VS", 250, _font(70))
        _center(draw, f"{home}  -  {away}", 450, _font(150))
        _center(draw, facts.home.upper(), 670, _font(38)); _center(draw, facts.away.upper(), 730, _font(38))
        if facts.season: _center(draw, facts.season.upper(), 805, _font(34), (220,220,220))
        writer.write(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    writer.release()
    if not output.exists() or output.stat().st_size < 1024: raise RuntimeError("Overlay oluşturuldu fakat kullanılabilir MP4 üretilmedi.")
    return output
