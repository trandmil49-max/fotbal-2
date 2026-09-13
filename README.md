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
3. In the Railway service's **Variables** section, add:
   - `TELEGRAM_BOT_TOKEN` — token from BotFather
   - `OPENAI_API_KEY` — needed for automatic match and awarded-goal analysis
   - optionally `OPENAI_VISION_MODEL`, `MAX_TELEGRAM_DOWNLOAD_MB`, and
     `MAX_LINK_DOWNLOAD_MB` from `.env.example`
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

## Run on your own computer (optional)

1. Install Python 3.11+ and create a virtual environment:
   `py -m venv .venv`
2. Activate it: `.\.venv\Scripts\Activate.ps1`
3. Install packages: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`, add the token from BotFather and, for
   automatic analysis, an OpenAI API key.
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

## Analysis reliability

With `OPENAI_API_KEY`, the bot samples the actual clip and asks the vision
model for structured match identification and only **counted** goal events.
It requires a visible score change or an explicit confirmation that the goal
was awarded; a goal marked `offside`, `disallowed`, or `not awarded` is never
added to the timeline. A low-confidence match identity is labelled
`UNVERIFIED`, rather than being presented as fact. No system can reliably
identify every historical match or VAR decision from a short edited clip, so
the bot preserves that uncertainty in its status message.

Without an API key the bot still produces a valid 0–0 green-screen overlay
from the logos; it does not fabricate a match or score. Add match details in
the caption or enable vision analysis for automatic scoring.
