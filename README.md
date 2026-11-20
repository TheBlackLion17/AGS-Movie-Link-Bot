# AGS Button Creator & Post Scheduler

Telegram bot for publishing posts with inline URL buttons and scheduling posts to channels/groups. Runs on Pyrogram + MySQL. The previous movie index database is preserved but movie-search handlers are disabled.

## Commands

In the bot's **private chat**, send a text, photo, video, or document, then **reply to that message** with:

### Publish with URL buttons

```text
/buttons @yourchannel
Website | https://example.com
Join group | https://t.me/example
```

Each button is placed on its own row. Up to 20 URL buttons per post.

### Schedule a post

```text
/schedule @yourchannel 2026-11-21 18:30
Website | https://example.com
```

The button lines are optional. Dates are in the configured `TIMEZONE` (default `Asia/Kolkata`). The bot checks pending posts about every 10 seconds. Keep the source message in your private chat until publishing.

`/scheduled` lists pending posts; `/cancel 123` cancels a pending post. `/start` or `/help` shows instructions.

For private channels/groups, use a numeric `-100...` ID. The bot **and the requesting user** must be admins of the target. The bot must have posting rights.

## Configuration

Existing `.env` values remain valid: `API_ID`, `API_HASH`, `BOT_TOKEN`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`. Optional: `TIMEZONE=Asia/Kolkata`, `ADMIN_IDS=123456789,987654321` to restrict access to specific Telegram user IDs. Without `ADMIN_IDS`, only verified target admins can publish or schedule to their destinations.

The bot creates a separate MySQL `scheduled_posts` table. Existing `files` data is not deleted. Scheduled jobs survive restarts, though posts interrupted during sending may need checking for duplicates.

## VPS deployment

```bash
cd /opt/ags-movie-link
git pull --ff-only origin main
venv/bin/python -m py_compile bot.py
sudo systemctl restart ags-movie-link.service
sudo journalctl -u ags-movie-link.service -n 40 --no-pager -l
```

**Note:** Existing OTT update links that use `/start search_...` will no longer search for movie files in this bot. This is a deliberate change of purpose.
