# Match Overlay Telegram Bot

Creates a green-screen MP4 overlay for football clips: two cleaned-up team
logos, `VS`, score changes, and an optional `FINAL` / `SEMIFINAL` label. The
green is exactly `#00FF00`, so it can be cleanly keyed in an editor.

## What the ZIP contained

The supplied ZIP contains 14 reference screenshots, not a previous codebase.
They are retained in `../work/references`. The screenshots show the intended
layout and an old bot error (`cannot open resource`); this project avoids that
by discovering an installed font and falling back safely.

## GitHub + Railway (computer does not need to stay open)

This project is configured as a Railway **worker**. It uses Telegram long
polling, so it does not need a website URL, a webhook, or an open port.

1. Create a new, private GitHub repository and upload the contents of this
   folder. Do **not** upload `.env`; `.gitignore` already protects it.
2. In Railway, select **New Project → Deploy from GitHub Repo** and choose the
   repository. Railway automatically detects the included `Dockerfile`; it
   also installs FFmpeg for MP4 output.
3. In the Railway service's **Variables** section, add only:
   - `TELEGRAM_BOT_TOKEN` — token from BotFather
   - optionally `MAX_TELEGRAM_DOWNLOAD_MB` and `MAX_LINK_DOWNLOAD_MB` from
     `.env.example`
4. Click **Deploy**. The deployment log must end with polling started and must
   not show a missing-token error. Send `/start` to the Telegram bot once to
   check it answers.

After that, closing the computer is safe: Railway runs the bot. It needs an
active Railway service/plan; pausing or deleting the service stops the bot.
Every new GitHub push triggers a fresh deployment. Uploaded source clips and
generated files live only temporarily inside Railway's container and are
automatically discarded after the job or a redeploy; this is intentional for
privacy and avoids paying for persistent storage.

Never put either secret in GitHub, a Telegram message, or a screenshot. If a
token is exposed, revoke it in BotFather and replace the Railway variable.

### YouTube links on Railway

YouTube sometimes blocks downloads originating from cloud-provider IP
addresses. The error is `Sign in to confirm you're not a bot`; it is imposed
by YouTube, not caused by the Telegram bot or the link format. The downloader
now retries normal public links and gives this Turkish diagnosis instead of
printing a long technical traceback.

If YouTube still blocks a link, export a Netscape-format `cookies.txt` from a
YouTube session you are authorized to use, Base64-encode the file **locally**,
and put only that Base64 text in Railway as the secret variable
`YOUTUBE_COOKIES_B64`. If YouTube requires it for the selected video, add its
matching `YOUTUBE_PO_TOKEN` there too. Never upload either value to GitHub or
send it through Telegram; treat cookies like a password. The values are read
only inside the Railway container and are deleted with the temporary job files.

There is no safe universal code-only bypass for YouTube's anti-automation
check. For a link that YouTube deliberately blocks, the alternatives are a
properly authorized Railway session as above, or sending a compatible source
file. The normal Telegram cloud Bot API itself has a 20 MB download limit;
that is why large direct uploads need either a public source link or a separate
Telegram Local Bot API deployment.

## Run on your own computer (optional)

1. Install Python 3.11+ and create a virtual environment:
   `py -m venv .venv`
2. Activate it: `.\.venv\Scripts\Activate.ps1`
3. Install packages: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and add the token from BotFather.
5. Run: `python bot.py`

The first OpenCV install includes the MP4 writer needed by this project. If the
host cannot write MP4 (`VideoWriter` error), install an FFmpeg build and make
sure `ffmpeg` is on `PATH`, then restart the terminal.

## Telegram workflow

Send the bot, in any order:

1. two team-logo images;
2. a clip (up to 19 MB when using Telegram's normal cloud Bot API) **or** a
   public video URL; and
3. optionally a URL and a caption such as `Real Madrid vs Barcelona, 2017
   Supercopa final`.

It responds to every accepted item, retains the session, and automatically
starts when it has two logos plus a source. `/status` shows what is missing;
`/new` is the only command that clears a session. `/start` never clears work.

For larger clips, send a supported direct/public URL. The bot uses `yt-dlp`
and reports download, site, size, and analysis errors rather than going quiet.
The code limits URL downloads to 300 MB; change the environment value only if
the server has the capacity.

## Ücretsiz yerel skor analizi

Bu sürüm OpenAI, Gemini veya başka ücretli bir analiz API'si kullanmaz.
Docker içindeki açık kaynak Tesseract OCR, videonun üst bölümündeki yayıncı
skor tabelasını iki saniyelik aralıklarla okur. Aynı skor iki kez görülmeden
değişim kabul edilmez; yalnızca `1-0` veya `0-1` şeklinde artan doğrulanmış
değişim zaman çizelgesine gol olarak eklenir. Bu nedenle ekrandaki ofsayt/VAR
pozisyonu skor tabelasına yansımadıysa overlay'e de gol eklenmez.

Bu yaklaşım ücretsizdir; ancak videoda sürekli okunabilir bir skor tabelası
yoksa maç adı, turnuva/final bilgisi veya görünmeyen goller güvenilir biçimde
çıkarılamaz. Böyle bir durumda bot yine yeşil ekran MP4 üretir, fakat skor
uydurmaz. Takım adlarının görünmesi için linkle birlikte `Real Madrid vs
Barcelona` gibi kısa bir not gönderebilirsin.

Skor zaman çizelgesinin iki kez görülen skor değişimini kabul ettiğini kontrol
eden ücretsiz yerel testler de pakette vardır: `python -m unittest
tests/test_timeline.py`.
