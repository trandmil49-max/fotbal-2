"""Source download, local scoreboard OCR, and deterministic overlay rendering."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
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
    analysis_note: str = ""

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
    po_token = os.getenv("YOUTUBE_PO_TOKEN", "").strip()

    base_opts = {"outtmpl": destination, "quiet": True, "noplaylist": True, "max_filesize": maximum,
                 "format": "mp4[height<=1080]/mp4/best[height<=1080]/best", "retries": 3,
                 "fragment_retries": 3}

    cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    cookie_path = None
    if cookies_b64:
        try:
            cookie_path = job_dir / "youtube-cookies.txt"
            # Hem Base64'e çevrilmiş hem de düz metin (cookies.txt'den doğrudan
            # kopyala-yapıştır) hali kabul ediliyor - kullanıcı ekstra bir
            # çevirme adımı yapmak zorunda değil.
            try:
                decoded = base64.b64decode(cookies_b64, validate=True).decode("utf-8")
            except Exception:
                decoded = cookies_b64
            # Kopyala-yapıştır sırasında satır sonları bozulabiliyor
            # (Windows/Mac farkı) - bunu normalize ediyoruz, yoksa yt-dlp
            # dosyayı geçersiz sayabiliyor.
            decoded = decoded.replace("\r\n", "\n").replace("\r", "\n")
            cookie_path.write_text(decoded, encoding="utf-8")
        except Exception as exc:
            raise RuntimeError("YOUTUBE_COOKIES_B64 değeri okunamadı. Railway değişkenine cookies.txt içeriğini olduğu gibi yapıştırın.") from exc

    # YouTube, "web" istemcisinde cookies ile bile bazen ekstra bir doğrulama
    # (PO Token) istiyor - bunu aşmak için farklı "istemci kimliklerini"
    # (telefon uygulaması gibi görünme) SIRAYLA deniyoruz. Cookies varsa her
    # denemeye ekleniyor, PO token varsa web denemesine ekleniyor.
    client_strategies = ["android", "ios", "tv", "web", "mweb"]
    attempts = []
    for client in client_strategies:
        attempt = dict(base_opts)
        extractor_args = {"youtube": {"player_client": [client]}}
        if po_token and client in ("web", "mweb"):
            extractor_args["youtube"]["po_token"] = [f"{client}.gvs+{po_token}"]
        attempt["extractor_args"] = extractor_args
        if cookie_path:
            attempt["cookiefile"] = str(cookie_path)
        attempts.append(attempt)

    errors: list[Exception] = []
    info = None
    path = None
    for attempt in attempts:
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
            break
        except Exception as exc:
            errors.append(exc)
    if path is None or info is None:
        exc = errors[-1]
        detail = str(exc)
        if "Sign in to confirm you’re not a bot" in detail or "Sign in to confirm you're not a bot" in detail or "reloaded" in detail.lower():
            raise RuntimeError(
                "YouTube, Railway sunucusundan gelen bu isteği bot sanıp reddediyor (denediğim tüm "
                f"yöntemler başarısız oldu: {', '.join(client_strategies)}). Bu, bağlantı biçimi hatası değil, "
                "YouTube'un kendi engeli. En güvenilir çözüm: videoyu YouTube'dan kendi bilgisayarına indirip "
                "doğrudan buraya (Telegram'a) yüklemek. Son hata: " + detail[:300]
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


def _duration(path: Path) -> float:
    """Sadece video süresini öğrenir - kare biriktirmeden, hafif."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise RuntimeError("Video açılamadı. MP4/H.264 olarak tekrar yükleyin veya başka bir herkese açık bağlantı deneyin.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    cap.release()
    if duration <= 0 or duration > 15 * 60: raise RuntimeError("Video süresi okunamadı veya video 15 dakikadan uzun.")
    return duration


def _sample_scores(path: Path, count: int) -> tuple[list[tuple[float, tuple[int, int] | None]], float]:
    """Videoyu örnekleyip her noktada skoru okur.

    ÇOK ÖNEMLİ: kareleri bir listede TOPLAMIYORUZ. Her kareyi okur okumaz
    hemen skoru çıkarıp kareyi hafızadan atıyoruz. Önceki sürüm yüzlerce
    kareyi aynı anda hafızada tutuyordu - uzun videolarda bu, Railway'in
    hafıza sınırını aşıp "-9" (bellek yetersizliğinden öldürüldü) hatasına
    yol açıyordu. Bu şekilde hafıza kullanımı sabit kalıyor, video ne kadar
    uzun/kaç örnek alınırsa alınsın taşmıyor.
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened(): raise RuntimeError("Video açılamadı. MP4/H.264 olarak tekrar yükleyin veya başka bir herkese açık bağlantı deneyin.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    if duration <= 0 or duration > 15 * 60:
        cap.release()
        raise RuntimeError("Video süresi okunamadı veya video 15 dakikadan uzun.")
    # Kare NUMARASINA göre değil, gerçek ZAMANA (milisaniye) göre arama
    # yapıyoruz - bazı videolarda toplam kare sayısı metadata'sı hatalı
    # oluyor, bu da zamanlama kaymalarına yol açıyordu.
    positions_ms = np.linspace(0, max(0.0, duration * 1000 - 40), count)
    samples: list[tuple[float, tuple[int, int] | None]] = []
    for pos_ms in positions_ms:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(pos_ms))
        ok, frame = cap.read()
        if ok:
            actual_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            ts = (actual_ms / 1000.0) if actual_ms and actual_ms > 0 else (pos_ms / 1000.0)
            score = _score_from_frame(frame)
            samples.append((ts, score))
            frame = None  # kareyi hemen bırak, biriktirme
    cap.release()
    return samples, duration


def _score_from_frame(frame: np.ndarray) -> tuple[int, int] | None:
    """Read a broadcast score in the top part of a frame without any web API."""
    try:
        import pytesseract
    except ImportError:
        return None
    height, width = frame.shape[:2]
    # Football broadcasts normally reserve the upper band for the scoreboard.
    crop = frame[:max(100, int(height * .28)), :]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    try:
        text = pytesseract.image_to_string(gray, config="--psm 11 -c tessedit_char_whitelist=0123456789-")
    except Exception:
        return None
    pairs = re.findall(r"(?<!\d)([0-9]{1,2})\s*-\s*([0-9]{1,2})(?!\d)", text)
    for left, right in pairs:
        home, away = int(left), int(right)
        if home <= 15 and away <= 15:  # reject clock/time-like values
            return home, away
    return None


def _local_score_timeline(samples: list[tuple[float, tuple[int, int] | None]]) -> list[Goal]:
    """Turn stable visual score changes into goals; no score change means no goal.
    NOT: Kullanıcının isteği üzerine "iki kere doğrulama" kaldırıldı - artık
    geçerli bir skor değişimi (bir takım için tam +1) görür görmez HEMEN
    kabul ediliyor, ekstra gecikme yok. Kaynak videolar zaten doğrulanmış
    yayın grafikleri olduğu için bu güvenli."""
    stable: tuple[int, int] | None = None
    goals: list[Goal] = []
    for stamp, observed in samples:
        if observed is None:
            continue
        if stable is None:
            stable = observed
            continue
        if observed == stable:
            continue
        home_delta, away_delta = observed[0] - stable[0], observed[1] - stable[1]
        if (home_delta, away_delta) == (1, 0):
            goals.append(Goal(stamp, "home")); stable = observed
        elif (home_delta, away_delta) == (0, 1):
            goals.append(Goal(stamp, "away")); stable = observed
        # Skor bir seferde 1'den fazla değiştiyse (muhtemelen OCR bir kareyi
        # kaçırdı - yayın 0-0'dan 2-0'a "atladı") bunu tek tek gol gibi
        # sayıyoruz ki eksik gol kalmasın.
        elif home_delta > 0 or away_delta > 0:
            for _ in range(max(home_delta, 0)):
                goals.append(Goal(stamp, "home"))
            for _ in range(max(away_delta, 0)):
                goals.append(Goal(stamp, "away"))
            stable = observed
    return goals


def analyse_clip(path: Path, hint: str) -> MatchFacts:
    duration = _duration(path)
    # Zamanlamanın gerçek gol anına olabildiğince yakın olması için sık
    # örnekliyoruz (yaklaşık saniyede 2 kare) - kareler TEK TEK işlenip
    # hemen atıldığı için (bkz. _sample_scores) bu hafızayı şişirmiyor.
    sample_count = min(600, max(24, int(duration * 2)))
    samples, duration = _sample_scores(path, sample_count)
    title_file = path.parent / "source-details.txt"
    if title_file.exists():
        hint = (hint + " | public source title: " + title_file.read_text(encoding="utf-8")[:180]).strip(" |")
    facts = MatchFacts(goals=_local_score_timeline(samples))
    # A source title/caption can label teams, but never creates a score event.
    if hint:
        m = re.search(r"([^,;]+?)\s+(?:vs\.?|v\.?|[-–])\s+([^,;]+)", hint, re.I)
        if m: facts.home, facts.away = m.group(1).strip()[:28], m.group(2).strip()[:28]
    if facts.goals:
        facts.confidence = .75
    else:
        facts.analysis_note = "Videodaki skor tabelasında doğrulanmış değişim bulunamadı"
    return facts


def _font(size: int) -> ImageFont.ImageFont:
    # Anton, referans görsellerdeki kalın/net skor tablosu fontuna en yakın
    # olan ücretsiz font - Dockerfile'da indirilip /app/fonts'a konuluyor.
    candidates = [
        str(Path(__file__).parent / "fonts" / "Anton-Regular.ttf"),
        r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try: return ImageFont.truetype(candidate, size)
            except OSError: pass
    return ImageFont.load_default()


def _logo(path: Path, size: int = 260) -> Image.Image:
    try: image = Image.open(path).convert("RGBA")
    except Exception as exc: raise RuntimeError(f"Logo açılamadı ({path.name}). PNG, JPG veya WebP gönder.") from exc
    px = np.asarray(image).copy()
    # Only remove near-white / near-black pixels connected to the border; white inside a crest remains.
    rgb = px[:,:,:3]; alpha = px[:,:,3]
    candidate = (((rgb.min(axis=2) > 230) | (rgb.max(axis=2) < 18)) & (alpha > 0)).astype(np.uint8)
    h, w = candidate.shape
    flood = np.zeros((h+2, w+2), np.uint8)
    # Dört köşeden de temizlik yap (sadece sol üstten değil) - böylece
    # arka plan hangi köşede olursa olsun tam kapsanır.
    for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if candidate[seed[1], seed[0]] == 1:
            cv2.floodFill(candidate, flood, seed, 2)
    px[candidate == 2, 3] = 0
    result = Image.fromarray(px).convert("RGBA")
    result.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0,0,0,0)); canvas.alpha_composite(result, ((size-result.width)//2, (size-result.height)//2))
    # ÇOK ÖNEMLİ: kenarlardaki yarı saydam pikselleri SERTLEŞTİRİYORUZ
    # (ya tam görünür ya tam görünmez, arası yok). Yarı saydam bir piksel
    # yeşil ekrana bindirilince rengi yeşille KARIŞIYOR ve bu karışım,
    # kullanıcı arka planı kestiğinde logonun etrafında yeşil bir "sızıntı"
    # olarak kalıyordu. Sert kenar bu sorunu kökten çözüyor.
    final_px = np.array(canvas)
    final_px[:, :, 3] = np.where(final_px[:, :, 3] >= 128, 255, 0).astype(np.uint8)
    return Image.fromarray(final_px, mode="RGBA")


def _center(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.ImageFont, fill=(255,255,255)) -> None:
    box = draw.textbbox((0,0), text, font=font, stroke_width=4)
    draw.text(((1080-(box[2]-box[0]))/2, y), text, font=font, fill=fill, stroke_width=4, stroke_fill=(0,0,0))


def make_overlay(source: Path | None, logo_home: Path, logo_away: Path, facts: MatchFacts, job_dir: Path) -> Path:
    # If a link host refuses the download, still return a usable 10-second
    # green-screen template instead of failing the entire Telegram job.
    duration = _duration(source) if source else 10.0
    width, height, fps = 1080, 1920, 25
    output = job_dir / "green-screen-score-overlay.mp4"

    logo_size = 260
    # Referans görsellerdeki gibi grafiği ekranın ALT ÜÇTE BİRİNE koyuyoruz
    # (tam ortada değil).
    center_y = int(height * 0.85)
    logo_top = center_y - logo_size // 2
    logo_gap = 235  # merkezden logoya uzaklık - çift haneli skorlarda bile metinle çakışmaz
    center_font_size = 150

    left, right = _logo(logo_home, logo_size), _logo(logo_away, logo_size)
    events = sorted((g for g in facts.goals if g.awarded), key=lambda g: g.second)

    # Saniye saniye kare üretmek yerine, skorun SABİT kaldığı her bölüm için
    # TEK BİR yüksek kaliteli görsel üretiyoruz, sonra ffmpeg ile bunları
    # gerçek sürelerine göre birleştiriyoruz. Bu hem çok daha hızlı hem de
    # eski yöntemden (kare kare cv2 ile yazma) çok daha net/keskin bir
    # görüntü veriyor - büyütüldüğünde bulanıklaşmıyor.
    segments: list[tuple[float, float, int, int]] = []
    home = away = 0
    prev_t = 0.0
    for g in events:
        if g.second > prev_t:
            segments.append((prev_t, g.second, home, away))
        if g.team == "home": home += 1
        else: away += 1
        prev_t = g.second
    segments.append((prev_t, duration, home, away))

    concat_path = job_dir / "concat_list.txt"
    frame_paths: list[Path] = []
    with open(concat_path, "w") as list_file:
        for i, (start, end, h, a) in enumerate(segments):
            seg_duration = max(end - start, 0.1)
            image = Image.new("RGB", (width, height), GREEN)
            draw = ImageDraw.Draw(image)
            if facts.stage:
                _center(draw, facts.stage, logo_top - 90, _font(54))
            # Referans görsellerdeki gibi: logo - SAYI - tire - SAYI - logo, tek satırda
            center_text = f"{h} - {a}"
            box = draw.textbbox((0, 0), center_text, font=_font(center_font_size), stroke_width=4)
            text_h = box[3] - box[1]
            draw.text(
                (540 - (box[2] - box[0]) / 2, center_y - text_h / 2 - box[1]),
                center_text, font=_font(center_font_size), fill=(255,255,255), stroke_width=4, stroke_fill=(0,0,0),
            )
            image.paste(left, (540 - logo_gap - logo_size, logo_top), left)
            image.paste(right, (540 + logo_gap, logo_top), right)
            if facts.season:
                _center(draw, facts.season.upper(), logo_top + logo_size + 40, _font(36), (220,220,220))
            frame_path = job_dir / f"seg_{i}.png"
            image.save(frame_path)
            frame_paths.append(frame_path)
            list_file.write(f"file '{frame_path}'\n")
            list_file.write(f"duration {seg_duration}\n")
        if frame_paths:
            list_file.write(f"file '{frame_paths[-1]}'\n")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        # ÖNEMLİ: Önceki "-vf fps=..." filtresi, görüntüleri istenen kare
        # hızına çevirmek için içeride gereksiz bir arabellek biriktiriyordu
        # ("100 buffers queued" hatası tam olarak buydu - bu da fazladan
        # yüzlerce megabayt hafıza demekti). "-r" ile doğrudan çıkış kare
        # hızını ayarlamak, filtre katmanı olmadan çalışır ve çok daha az
        # hafıza kullanır.
        "-r", str(fps),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-x264-params", "rc-lookahead=10:ref=1",
        "-threads", "2",
        "-max_muxing_queue_size", "4096",
        "-crf", "18", "-pix_fmt", "yuv420p", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    for p in frame_paths:
        if p.exists(): p.unlink()
    if concat_path.exists(): concat_path.unlink()

    if result.returncode != 0 or not output.exists() or output.stat().st_size < 1024:
        # "-loglevel warning" ile ffmpeg'in gereksiz bilgi mesajlarını
        # (örnek: x264'ün kendi ayar özeti) susturduk, böylece burada
        # görünen satırlar GERÇEK hatayı gösteriyor, gürültü değil.
        stderr_lines = result.stderr.strip().splitlines()
        error_lines = [l for l in stderr_lines if "error" in l.lower() or "invalid" in l.lower()]
        tail = "\n".join(error_lines[-8:] or stderr_lines[-15:])
        raise RuntimeError(f"Overlay video oluşturulamadı (ffmpeg hatası, çıkış kodu {result.returncode}): {tail[:700]}")
    return output
