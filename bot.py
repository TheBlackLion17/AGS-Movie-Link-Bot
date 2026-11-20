"""AGS Telegram Button Creator and Post Scheduler.

Private-chat commands. The bot must be an administrator of each target channel/group.
Times are interpreted in the configured TIMEZONE (default Asia/Kolkata).
Existing movie database tables are not modified.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiomysql
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ags-post-scheduler")


def required(key):
    value = os.getenv(key)
    if not value:
        raise RuntimeError("Missing configuration: " + key)
    return value


TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
ADMIN_IDS = {
    int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
}
app = Client(
    "ags_movie_link",
    api_id=int(required("API_ID")),
    api_hash=required("API_HASH"),
    bot_token=required("BOT_TOKEN"),
)
pool = None


async def query(statement, params=(), mode=None):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(statement, params)
            if mode == "one":
                return await cur.fetchone()
            if mode == "all":
                return await cur.fetchall()
            return cur.rowcount


async def setup():
    global pool
    pool = await aiomysql.create_pool(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=required("MYSQL_USER"),
        password=required("MYSQL_PASSWORD"),
        db=os.getenv("MYSQL_DATABASE", "ags_movie_link"),
        autocommit=True, charset="utf8mb4", minsize=1, maxsize=4,
    )
    await query("""CREATE TABLE IF NOT EXISTS scheduled_posts (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        source_chat BIGINT NOT NULL,
        source_message BIGINT NOT NULL,
        target_chat VARCHAR(255) NOT NULL,
        buttons_json TEXT NOT NULL,
        scheduled_utc DATETIME NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        attempts INT NOT NULL DEFAULT 0,
        last_error VARCHAR(512) NULL,
        sent_message BIGINT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_due (status, scheduled_utc),
        INDEX idx_owner (owner_id, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
    # Recover work claimed before an unexpected process restart.
    await query("UPDATE scheduled_posts SET status='pending' WHERE status='sending'")


def parse_target(value):
    if re.fullmatch(r"-100\d{5,20}", value):
        return int(value)
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return value
    raise ValueError("Target must be a public @channel username or -100... chat ID.")


def parse_buttons(lines):
    buttons = []
    for line in lines:
        if not line.strip():
            continue
        if "|" not in line:
            raise ValueError("Each button must be written as: Button name | https://example.com")
        label, url = (part.strip() for part in line.split("|", 1))
        parsed = urlparse(url)
        if not label or len(label) > 64 or len(url) > 2048:
            raise ValueError("Button name or URL has an invalid length.")
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("Buttons require full http:// or https:// URLs.")
        if parsed.username or parsed.password or any(c.isspace() for c in url):
            raise ValueError("Invalid button URL.")
        buttons.append([label, url])
    if len(buttons) > 20:
        raise ValueError("Maximum 20 buttons per post.")
    return buttons


def keyboard(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, url=url)] for label, url in buttons
    ])


async def check_permission(client, message, target):
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        raise ValueError("You are not authorized to use this bot.")
    try:
        me = await client.get_me()
        bot_member = await client.get_chat_member(target, me.id)
        user_member = await client.get_chat_member(target, message.from_user.id)
    except RPCError as exc:
        raise ValueError("Cannot verify channel/group permissions. Add the bot as admin and try again.") from exc
    admin_states = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    if bot_member.status not in admin_states:
        raise ValueError("The bot must be an admin in the destination.")
    if user_member.status not in admin_states:
        raise ValueError("You must be an admin in the destination.")
    if target and bot_member.status == ChatMemberStatus.ADMINISTRATOR:
        if not getattr(bot_member.privileges, "can_post_messages", True):
            raise ValueError("The bot needs permission to post messages.")
    return target


HELP = (
    "🛠 Button Creator & Post Scheduler\n\n"
    "Reply to a text, photo, video, or document in this private chat.\n\n"
    "📌 Publish with buttons:\n"
    "/buttons @yourchannel\n"
    "Visit website | https://example.com\n"
    "Join group | https://t.me/example\n\n"
    "🗓 Schedule a post:\n"
    "/schedule @yourchannel 2026-11-21 18:30\n"
    "Visit website | https://example.com\n\n"
    "The button lines are optional for /schedule.\n"
    "Time zone: " + str(TZ) + "\n\n"
    "/demo — preview a post with buttons privately\n"\n    "/scheduled — show pending posts\n"
    "/cancel 123 — cancel your scheduled post\n\n"
    "For private groups/channels use their -100... ID instead of @username.\n"
    "The bot and you must both be destination admins."
)


@app.on_message(filters.private & filters.command(["start", "help"]))
async def start(_, message):
    await message.reply_text(HELP, disable_web_page_preview=True)


@app.on_message(filters.private & filters.command("demo"))
async def demo(client, message):
    """Preview the replied-to media/text post and its URL buttons in private chat."""
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return await message.reply_text("Not authorized.")
    if not message.reply_to_message:
        return await message.reply_text(
            "Reply to a text, photo, video or document with:\n"
            "/demo\n"
            "Website | https://example.com\n"
            "Join Group | https://t.me/example"
        )
    try:
        lines = (message.text or "").splitlines()
        if len(lines[0].split()) != 1:
            raise ValueError("Use /demo on the first line, then optional button lines.")
        buttons = parse_buttons(lines[1:])
        await client.copy_message(
            message.chat.id, message.chat.id, message.reply_to_message.id,
            reply_markup=keyboard(buttons),
        )
        await message.reply_text(
            "👆 Demo preview only. Nothing has been published or scheduled.\n"
            "To publish, reply to the original post using /buttons @yourchannel "
            "with the same button lines."
        )
    except ValueError as exc:
        await message.reply_text("❌ " + str(exc))
    except RPCError:
        log.exception("Could not render demo")
        await message.reply_text("❌ Could not preview this message.")


@app.on_message(filters.private & filters.command(["buttons", "schedule"]))
async def create_post(client, message):
    if not message.reply_to_message:
        return await message.reply_text("Reply to the message you want to publish with this command.\n\n" + HELP)
    try:
        parts = (message.text or "").splitlines()
        header = parts[0].split()
        scheduled = message.command[0].lower() == "schedule"
        expected = 4 if scheduled else 2
        if len(header) != expected:
            raise ValueError(
                "Use /buttons @channel, or /schedule @channel YYYY-MM-DD HH:MM."
            )
        target = parse_target(header[1])
        await check_permission(client, message, target)
        buttons = parse_buttons(parts[1:])
        if scheduled:
            naive = datetime.strptime(header[2] + " " + header[3], "%Y-%m-%d %H:%M")
            local = naive.replace(tzinfo=TZ)
            due = local.astimezone(timezone.utc).replace(tzinfo=None)
            if due <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise ValueError("Choose a future date and time.")
            await query(
                """INSERT INTO scheduled_posts
                (owner_id,source_chat,source_message,target_chat,buttons_json,scheduled_utc)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (message.from_user.id, message.chat.id, message.reply_to_message.id,
                 str(target), json.dumps(buttons), due),
            )
            row = await query(
                """SELECT id FROM scheduled_posts WHERE owner_id=%s
                ORDER BY id DESC LIMIT 1""", (message.from_user.id,), mode="one"
            )
            return await message.reply_text(
                f"✅ Scheduled post #{row['id']} for {local:%Y-%m-%d %H:%M} ({TZ}).\n"
                "Keep the original message in this chat until it is sent."
            )
        sent = await client.copy_message(
            target, message.chat.id, message.reply_to_message.id,
            reply_markup=keyboard(buttons),
        )
        await message.reply_text(f"✅ Post published! Message ID: {sent.id}")
    except (ValueError, RPCError, FloodWait) as exc:
        await message.reply_text("❌ " + str(exc))
    except Exception:
        log.exception("Failed to create post")
        await message.reply_text("❌ Could not create the post. Check the VPS logs.")


@app.on_message(filters.private & filters.command("scheduled"))
async def scheduled_list(_, message):
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return await message.reply_text("Not authorized.")
    rows = await query(
        """SELECT id,target_chat,scheduled_utc FROM scheduled_posts
        WHERE owner_id=%s AND status='pending'
        ORDER BY scheduled_utc LIMIT 20""",
        (message.from_user.id,), mode="all",
    )
    if not rows:
        return await message.reply_text("No pending posts.")
    lines = ["🗓 Your next scheduled posts:"]
    for row in rows:
        utc = row["scheduled_utc"].replace(tzinfo=timezone.utc)
        local = utc.astimezone(TZ)
        lines.append(f"#{row['id']} · {row['target_chat']} · {local:%Y-%m-%d %H:%M}")
    await message.reply_text("\n".join(lines) + "\n\n/cancel ID to cancel.")


@app.on_message(filters.private & filters.command("cancel"))
async def cancel(_, message):
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return await message.reply_text("Not authorized.")
    try:
        job_id = int(message.command[1])
    except (IndexError, ValueError):
        return await message.reply_text("Usage: /cancel 123")
    changed = await query(
        """UPDATE scheduled_posts SET status='cancelled'
        WHERE id=%s AND owner_id=%s AND status='pending'""",
        (job_id, message.from_user.id),
    )
    await message.reply_text("✅ Cancelled." if changed else "Post not found or already processing.")


async def dispatch_job(job):
    try:
        buttons = json.loads(job["buttons_json"])
        target = parse_target(job["target_chat"])
        sent = await app.copy_message(
            target, job["source_chat"], job["source_message"],
            reply_markup=keyboard(buttons),
        )
        await query(
            """UPDATE scheduled_posts SET status='sent',sent_message=%s,
            last_error=NULL WHERE id=%s""",
            (sent.id, job["id"]),
        )
        log.info("Scheduled post #%s sent", job["id"])
    except FloodWait as exc:
        await query(
            """UPDATE scheduled_posts SET status='pending',
            scheduled_utc=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND),
            attempts=attempts+1, last_error=%s WHERE id=%s""",
            (max(int(exc.value), 1) + 2, "Telegram flood wait", job["id"]),
        )
    except Exception as exc:
        log.exception("Scheduled post #%s failed", job["id"])
        await query(
            """UPDATE scheduled_posts SET status='failed',
            attempts=attempts+1,last_error=%s WHERE id=%s""",
            (str(exc)[:512], job["id"]),
        )
        try:
            await app.send_message(
                job["owner_id"],
                f"❌ Scheduled post #{job['id']} failed. Check the destination and source message.",
            )
        except Exception:
            log.exception("Could not notify owner")


async def scheduler():
    while True:
        try:
            jobs = await query(
                """SELECT * FROM scheduled_posts
                WHERE status='pending' AND scheduled_utc<=UTC_TIMESTAMP()
                ORDER BY scheduled_utc,id LIMIT 20""",
                mode="all",
            )
            for job in jobs:
                claimed = await query(
                    """UPDATE scheduled_posts SET status='sending'
                    WHERE id=%s AND status='pending'""",
                    (job["id"],),
                )
                if claimed:
                    await dispatch_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler iteration failed")
        await asyncio.sleep(10)


async def main():
    await setup()
    task = None
    try:
        await app.start()
        me = await app.get_me()
        log.info("Button Creator & Post Scheduler started: @%s", me.username)
        task = asyncio.create_task(scheduler())
        await idle()
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if app.is_connected:
            await app.stop()
        if pool:
            pool.close()
            await pool.wait_closed()


if __name__ == "__main__":
    # Use Pyrogram's own event loop, avoiding cross-loop dispatcher shutdown errors.
    app.run(main())
