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
sessions = {}


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
    await query("""CREATE TABLE IF NOT EXISTS registered_groups (
        chat_id BIGINT PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        active TINYINT(1) NOT NULL DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
    columns = await query("SHOW COLUMNS FROM registered_groups", mode="all")
    if not any(row["Field"] == "chat_type" for row in columns):
        await query("ALTER TABLE registered_groups ADD COLUMN chat_type VARCHAR(20) NOT NULL DEFAULT 'group'")
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


async def check_permission(client, message, target, actor_id=None):
    actor_id = actor_id or message.from_user.id
    if ADMIN_IDS and actor_id not in ADMIN_IDS:
        raise ValueError("You are not authorized to use this bot.")
    try:
        me = await client.get_me()
        bot_member = await client.get_chat_member(target, me.id)
        user_member = await client.get_chat_member(target, actor_id)
    except RPCError as exc:
        raise ValueError("Cannot verify channel/group permissions. Add the bot as admin and try again.") from exc
    admin_states = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    if bot_member.status not in admin_states:
        raise ValueError("The bot must be an admin in the destination.")
    if user_member.status not in admin_states:
        raise ValueError("You must be an admin in the destination.")
    chat = await client.get_chat(target)
    if chat.type.value == "channel" and bot_member.status == ChatMemberStatus.ADMINISTRATOR:
        if not getattr(bot_member.privileges, "can_post_messages", False):
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
    "/demo — preview a post with buttons privately\n"
    "/scheduled — show pending posts\n"
    "/cancel 123 — cancel your scheduled post\n\n"
    "For private groups/channels use their -100... ID instead of @username.\n"
    "The bot and you must both be destination admins."
)


@app.on_message(filters.private & filters.command(["start", "help"]))
async def start(_, message):
    sessions.pop(message.from_user.id, None)
    await message.reply_text("🛠 AGS Post Manager — choose an action:", reply_markup=main_menu())


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




async def save_group(chat, active=True):
    if chat.type.value not in ("group", "supergroup", "channel"):
        return
    await query(
        """INSERT INTO registered_groups (chat_id,title,active,chat_type)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE title=VALUES(title),active=VALUES(active),chat_type=VALUES(chat_type)""",
        (chat.id, (chat.title or "Unnamed Chat")[:255], int(active), chat.type.value),
    )


@app.on_message(filters.private & filters.forwarded, group=2)
async def register_forwarded_channel(client, message):
    source = getattr(message, "forward_from_chat", None)
    if not source or source.type.value != "channel":
        return
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return
    try:
        await check_permission(client, message, source.id)
        await save_group(source)
        await message.reply_text("✅ Channel connected! Open Groups & Channels to select it.")
    except (ValueError, RPCError) as exc:
        await message.reply_text("❌ Channel not connected: " + str(exc))


@app.on_chat_member_updated()
async def track_bot_membership(client, update):
    try:
        if not update.new_chat_member or not update.chat:
            return
        member = update.new_chat_member
        me = await client.get_me()
        if not member.user or member.user.id != me.id:
            return
        active = member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
        await save_group(update.chat, active=active)
    except Exception:
        log.exception("Could not track group membership")


@app.on_message((filters.group | filters.channel) & filters.command("register"))
async def register_group(client, message):
    if not message.from_user:
        return
    try:
        await check_permission(client, message, message.chat.id)
        await save_group(message.chat)
        await message.reply_text("✅ Group registered. Select it from the bot's private group picker.")
    except (ValueError, RPCError) as exc:
        await message.reply_text("❌ " + str(exc))


async def group_picker(client, user_id):
    rows = await query(
        "SELECT chat_id,title,chat_type FROM registered_groups WHERE active=1 ORDER BY title LIMIT 80",
        mode="all",
    )
    available = []
    me = await client.get_me()
    for row in rows:
        try:
            bot = await client.get_chat_member(row["chat_id"], me.id)
            user = await client.get_chat_member(row["chat_id"], user_id)
            admins = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
            if bot.status in admins and user.status in admins:
                available.append(row)
        except RPCError:
            continue
    return available


def group_keyboard(groups):
    buttons = [
        [InlineKeyboardButton(("📢 " if row["chat_type"] == "channel" else "👥 ") + row["title"][:45], callback_data=f"ui:group:{row['chat_id']}")]
        for row in groups[:40]
    ]
    buttons.append([InlineKeyboardButton("🔄 Refresh Groups", callback_data="ui:refresh")])
    buttons.append([InlineKeyboardButton("✍️ Enter ID Manually", callback_data="ui:manual")])
    buttons.append([InlineKeyboardButton("🏠 Cancel", callback_data="ui:home")])
    return InlineKeyboardMarkup(buttons)


async def target_selected(message, state, target):
    state["target"] = target
    if state["mode"] == "schedule":
        state["step"] = "date"
        return await message.reply_text(
            f"🕒 Send date/time ({TZ}):\nYYYY-MM-DD HH:MM",
            reply_markup=cancel_menu(),
        )
    state["step"] = "confirm"
    return await message.reply_text(
        f"Publish to {target}?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Publish Now", callback_data="ui:publish")],
            [InlineKeyboardButton("🏠 Cancel", callback_data="ui:home")],
        ]),
    )


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Create Post", callback_data="ui:create")],
        [InlineKeyboardButton("🗓 Schedule Post", callback_data="ui:schedule")],
        [InlineKeyboardButton("👁 Preview Demo", callback_data="ui:demo")],
        [InlineKeyboardButton("📋 Scheduled Posts", callback_data="ui:list")],
        [InlineKeyboardButton("👥 Groups & Channels", callback_data="ui:groups")],
        [InlineKeyboardButton("➕ Connect Group / Channel", callback_data="ui:connect")],
        [InlineKeyboardButton("❓ Help", callback_data="ui:help")],
    ])


def draft_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add URL Button", callback_data="ui:add")],
        [InlineKeyboardButton("👁 Preview", callback_data="ui:preview"),
         InlineKeyboardButton("➡️ Continue", callback_data="ui:next")],
        [InlineKeyboardButton("🏠 Cancel", callback_data="ui:home")],
    ])


def cancel_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Cancel", callback_data="ui:home")]])


@app.on_callback_query(filters.regex(r"^ui:"))
async def menu_click(client, cb):
    user = cb.from_user
    if not user or not cb.message or cb.message.chat.id != user.id:
        return await cb.answer("Open the bot in private chat.", show_alert=True)
    if ADMIN_IDS and user.id not in ADMIN_IDS:
        return await cb.answer("Not authorized.", show_alert=True)
    await cb.answer()
    action = cb.data[3:]
    state = sessions.get(user.id)
    if action == "home":
        sessions.pop(user.id, None)
        return await cb.message.reply_text("🏠 Main menu", reply_markup=main_menu())
    if action in ("groups", "refresh"):
        groups = await group_picker(client, user.id)
        intro = ("👥 Select a group or channel to post to:" if state and state.get("step") == "target"
                 else "👥 Registered groups and channels where you and the bot are admins:")
        if not groups:
            intro += "\nNo destinations found. Add the bot as admin or register an existing group/channel."
        return await cb.message.reply_text(intro, reply_markup=group_keyboard(groups))
    if action == "connect":
        me = await client.get_me()
        link = "https://t.me/" + me.username + "?startgroup=connect"
        channel_link = "https://t.me/" + me.username + "?startchannel=connect&admin=post_messages"
        return await cb.message.reply_text(
            "➕ Connect a Telegram group\\n\\n"
            "Tap Select Group below and choose your group. "
            "Telegram will ask you to add the bot. Give the bot admin rights "
            "so it can publish scheduled posts.\\n\\n"
            "After adding it, tap Refresh Groups.\\n\\n"
            "If the bot is already in your group, send /register inside "
            "that group once; Telegram does not send a new join event "
            "when a bot is already a member.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Select Group in Telegram", url=link)],
                [InlineKeyboardButton("📢 Select Channel in Telegram", url=channel_link)],
                [InlineKeyboardButton("🔄 Refresh Groups", callback_data="ui:refresh")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="ui:home")],
            ]),
        )
    if action == "help":
        return await cb.message.reply_text(HELP, reply_markup=main_menu())
    if action == "list":
        rows = await query(
            "SELECT id,target_chat,scheduled_utc FROM scheduled_posts WHERE owner_id=%s AND status='pending' ORDER BY scheduled_utc LIMIT 20",
            (user.id,), mode="all")
        lines = ["📋 Pending posts:"]
        for row in rows:
            local = row["scheduled_utc"].replace(tzinfo=timezone.utc).astimezone(TZ)
            lines.append(f"#{row['id']} · {row['target_chat']} · {local:%Y-%m-%d %H:%M}")
        if not rows:
            lines.append("None")
        return await cb.message.reply_text("\n".join(lines) + "\nUse /cancel ID to cancel.", reply_markup=main_menu())
    if action in ("create", "schedule", "demo"):
        sessions[user.id] = {"mode": action, "step": "media", "buttons": []}
        return await cb.message.reply_text("📸 Send a photo, video, document or text with its caption.", reply_markup=cancel_menu())
    if action.startswith("group:"):
        if not state or state.get("step") != "target":
            return await cb.message.reply_text("Start a post first, then select a group.", reply_markup=main_menu())
        try:
            target = int(action.split(":", 1)[1])
            registered = await query("SELECT chat_id FROM registered_groups WHERE chat_id=%s AND active=1", (target,), mode="one")
            if not registered:
                raise ValueError("Group is no longer registered.")
            await check_permission(client, cb.message, target, actor_id=user.id)
            return await target_selected(cb.message, state, target)
        except (ValueError, RPCError) as exc:
            return await cb.message.reply_text("❌ " + str(exc), reply_markup=cancel_menu())
    if action == "manual":
        if not state or state.get("step") != "target":
            return await cb.message.reply_text("Start a post first.", reply_markup=main_menu())
        return await cb.message.reply_text("Send @group or -100... group ID.", reply_markup=cancel_menu())
    if not state or "source" not in state:
        return await cb.message.reply_text("Start from the main menu.", reply_markup=main_menu())
    if action == "add":
        state["step"] = "button"
        return await cb.message.reply_text("🔗 Send a button:\nDOWNLOAD | https://example.com", reply_markup=cancel_menu())
    if action in ("preview", "next") and (action == "preview" or state["mode"] == "demo"):
        try:
            await client.copy_message(user.id, user.id, state["source"], reply_markup=keyboard(state["buttons"]))
        except RPCError:
            return await cb.message.reply_text("❌ Preview failed. Is the original post still available?")
        if action == "next":
            sessions.pop(user.id, None)
            return await cb.message.reply_text("✅ Demo complete. Nothing published.", reply_markup=main_menu())
        return await cb.message.reply_text("👆 Preview above.", reply_markup=draft_menu())
    if action == "next":
        state["step"] = "target"
        groups = await group_picker(client, user.id)
        return await cb.message.reply_text(
            "📢 Select a registered group, or enter a destination manually.\n"
            "Missing a group/channel? Forward a channel post to this bot to connect it.",
            reply_markup=group_keyboard(groups),
        )
    if state.get("step") != "confirm":
        return await cb.message.reply_text("Complete the earlier steps first.")
    try:
        await check_permission(client, cb.message, state["target"], actor_id=user.id)
        if action == "publish" and state["mode"] == "create":
            sent = await client.copy_message(state["target"], user.id, state["source"], reply_markup=keyboard(state["buttons"]))
            sessions.pop(user.id, None)
            return await cb.message.reply_text(f"✅ Published! Message ID: {sent.id}", reply_markup=main_menu())
        if action == "save" and state["mode"] == "schedule":
            await query(
                "INSERT INTO scheduled_posts (owner_id,source_chat,source_message,target_chat,buttons_json,scheduled_utc) VALUES (%s,%s,%s,%s,%s,%s)",
                (user.id, user.id, state["source"], str(state["target"]), json.dumps(state["buttons"]), state["due"]))
            sessions.pop(user.id, None)
            return await cb.message.reply_text("✅ Post scheduled.", reply_markup=main_menu())
    except (ValueError, RPCError, FloodWait) as exc:
        return await cb.message.reply_text("❌ " + str(exc), reply_markup=cancel_menu())
    except Exception:
        log.exception("Menu action failed")
        return await cb.message.reply_text("❌ Action failed. Check VPS logs.")


@app.on_message(filters.private & ~filters.command(["start", "help", "demo", "buttons", "schedule", "scheduled", "cancel"]), group=1)
async def menu_input(client, message):
    if not message.from_user:
        return
    state = sessions.get(message.from_user.id)
    if not state:
        return
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return
    step = state["step"]
    if step == "media":
        if not (message.text or message.photo or message.video or message.document):
            return await message.reply_text("Send a text, photo, video or document.")
        state["source"] = message.id
        state["step"] = "ready"
        return await message.reply_text("✅ Post received. Add buttons or continue.", reply_markup=draft_menu())
    if not message.text:
        return await message.reply_text("Send text for this step.")
    try:
        if step == "button":
            new = parse_buttons([message.text])
            if len(state["buttons"]) + len(new) > 20:
                raise ValueError("Maximum 20 buttons.")
            state["buttons"].extend(new)
            state["step"] = "ready"
            return await message.reply_text(f"✅ Button added ({len(state['buttons'])}/20).", reply_markup=draft_menu())
        if step == "target":
            target = parse_target(message.text.strip())
            await check_permission(client, message, target)
            return await target_selected(message, state, target)
        if step == "date":
            local = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            due = local.astimezone(timezone.utc).replace(tzinfo=None)
            if due <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise ValueError("Choose a future date/time.")
            state["due"] = due
            state["step"] = "confirm"
            return await message.reply_text(f"Schedule for {local:%Y-%m-%d %H:%M} ({TZ})?", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Schedule", callback_data="ui:save")],
                [InlineKeyboardButton("🏠 Cancel", callback_data="ui:home")]]))
    except (ValueError, RPCError) as exc:
        await message.reply_text("❌ " + str(exc), reply_markup=cancel_menu())
    except Exception:
        log.exception("Menu input failed")
        await message.reply_text("❌ Error. Check VPS logs.")


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
