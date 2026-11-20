# AGS Movie Link Bot

Independent Python Pyrogram + MySQL bot. Indexes **only new** documents/videos posted to storage channel `-1002518840015`; does not scan history.

## Features
- Automatic new-file indexing into its own MySQL database.
- Title and quality extraction from filename or caption.
- Compatible with OTT Updates Bot's existing `search_Movie_Title` Telegram deep links.
- Sends the only matching file directly to the user's private chat; offers choices for multiple matches.
- Stores channel and message IDs; never reuploads media.
- Independent repository and systemd service.

## VPS setup

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv mysql-server
sudo git clone https://github.com/TheBlackLion17/AGS-Movie-Link-Bot.git /opt/ags-movie-link
cd /opt/ags-movie-link
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
sudo nano .env
```

Create the separate MySQL database `ags_movie_link` and a restricted database user, then fill `.env` with Telegram API credentials, a distinct bot token and MySQL credentials. Add the bot as admin to the storage channel so it receives new posts.

```bash
sudo cp ags-movie-link.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ags-movie-link.service
sudo journalctl -u ags-movie-link.service -n 50 --no-pager
```

To link the existing OTT bot, set `AUTOFILTER_USERNAME=agsautofilterbot` in **OTT's own VPS-only** `.env`, then restart `ott-updates.service`. Existing Telegram announcements retain their old links.

Update independently with `cd /opt/ags-movie-link && git pull --ff-only origin main && sudo systemctl restart ags-movie-link.service`.

**Notes:** Only posts received after startup are indexed. No user session is required. Matching is heuristic; year-less or ambiguously named files may need manual checking. Avoid using the same bot token in two running services.
