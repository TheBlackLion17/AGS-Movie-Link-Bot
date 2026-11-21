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
    await query("""CREATE TABLE IF NOT EXISTS bot_settings (setting_key VARCHAR(64) PRIMARY KEY, setting_value TEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
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



async def force_config():
    rows = await query("SELECT setting_key,setting_value FROM bot_settings WHERE setting_key IN ('force_enabled','force_chat','force_link')", mode="all")
    return {row["setting_key"]: row["setting_value"] for row in rows}


async def set_config(key, value):
    await query("INSERT INTO bot_settings (setting_key,setting_value) VALUES (%s,%s) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)", (key, str(value)))


async def subscribed(client, user_id):
    if user_id in ADMIN_IDS:
        return True
    cfg = await force_config()
    if cfg.get("force_enabled") != "1" or not cfg.get("force_chat"):
        return True
    try:
        member = await client.get_chat_member(int(cfg["force_chat"]), user_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) or (
            member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)
        )
    except RPCError:
        return False


async def join_markup():
    cfg = await force_config()
    rows = []
    if cfg.get("force_link"):
        rows.append([InlineKeyboardButton("🔗 Join Required Group / Channel", url=cfg["force_link"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="ui:verify")])
    return InlineKeyboardMarkup(rows)


async def join_prompt(message):
    await message.reply_text(
        "👋 Welcome to AGS Post Manager!\n\n"
        "🖼 Create and publish posts\n"
        "🗓 Schedule posts\n"
        "🔗 Add URL buttons\n"
        "👥 Connect your groups and channels\n\n"
        "🔒 To unlock the bot, join our required group or channel and tap Check Again.",
        reply_markup=await join_markup(),
    )


def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Force Subscribe Settings", callback_data="ui:force")],
        [InlineKeyboardButton("🏠 Home", callback_data="ui:home")],
    ])


def force_menu(enabled):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Update Required Channel", callback_data="ui:force_update")],
        [InlineKeyboardButton("🔴 Disable" if enabled else "🟢 Enable", callback_data="ui:force_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="ui:owner"),
         InlineKeyboardButton("🏠 Home", callback_data="ui:home")],
    ])


async def force_screen(message):
    cfg = await force_config()
    enabled = cfg.get("force_enabled") == "1"
    await message.reply_text(
        f"🔒 Force Subscribe: {'Enabled' if enabled else 'Disabled'}\n"
        f"Required channel: {cfg.get('force_chat') or 'Not configured'}\n\n"
        "The bot must be an admin of the required group/channel to check membership.",
        reply_markup=force_menu(enabled),
    )


async def save_force_channel(client, raw):
    value = raw.strip()
    if value.startswith("https://t.me/"):
        slug = value[len("https://t.me/"):].split("?")[0].strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", slug):
            raise ValueError("Use a public t.me/username link or -100... channel ID.")
        target = "@" + slug
    else:
        target = parse_target(value)
    chat = await client.get_chat(target)
    if chat.type.value not in ("channel", "group", "supergroup"):
        raise ValueError("Choose a Telegram group, supergroup, or channel.")
    me = await client.get_me()
    bot_member = await client.get_chat_member(chat.id, me.id)
    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("Add the bot as an administrator of that group/channel first.")
    link = ("https://t.me/" + chat.username) if chat.username else getattr(chat, "invite_link", None)
    if not link or not link.startswith("https://t.me/"):
        raise ValueError("No join link available. Give the group/channel a public username or configure an invite link accessible to the bot.")
    await set_config("force_chat", chat.id)
    await set_config("force_link", link)
    return chat.title or link


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
    if not await subscribed(_, message.from_user.id):
        return await join_prompt(message)
    await message.reply_text("🛠 AGS Post Manager — choose an action:", reply_markup=main_menu())


@app.on_message(filters.private & filters.command("demo"))
async def demo(client, message):
    """Preview the replied-to media/text post and its URL buttons in private chat."""
    if not await subscribed(client, message.from_user.id):
        return await join_prompt(message)
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
    if not await subscribed(client, message.from_user.id):
        return await join_prompt(message)
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
async def scheduled_list(client, message):
    if not await subscribed(client, message.from_user.id):
        return await join_prompt(message)
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
async def cancel(client, message):
    if not await subscribed(client, message.from_user.id):
        return await join_prompt(message)
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
    if not await subscribed(client, message.from_user.id):
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
        if not await subscribed(client, message.from_user.id):
            return await message.reply_text("🔒 Join the required channel through the bot first.")
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


def main_menu():
    rows = [
        [InlineKeyboardButton("🖼 Create Post", callback_data="ui:create")],
        [InlineKeyboardButton("🗓 Schedule Post", callback_data="ui:schedule")],
        [InlineKeyboardButton("👁 Preview Demo", callback_data="ui:demo")],
        [InlineKeyboardButton("📋 Scheduled Posts", callback_data="ui:list")],
        [InlineKeyboardButton("👥 Groups & Channels", callback_data="ui:groups")],
        [InlineKeyboardButton("➕ Connect Group / Channel", callback_data="ui:connect")],
        [InlineKeyboardButton("❓ Help", callback_data="ui:help")],
    ]
    rows.append([InlineKeyboardButton("👑 Owner Settings", callback_data="ui:owner")])
    return InlineKeyboardMarkup(rows)


def navigation(*rows):
    return InlineKeyboardMarkup(list(rows) + [[
        InlineKeyboardButton("⬅️ Back", callback_data="ui:back"),
        InlineKeyboardButton("🏠 Home", callback_data="ui:home"),
    ]])


def draft_menu():
    return navigation(
        [InlineKeyboardButton("➕ Add URL Button", callback_data="ui:add")],
        [InlineKeyboardButton("👁 Preview", callback_data="ui:preview"),
         InlineKeyboardButton("➡️ Continue", callback_data="ui:next")],
    )


def cancel_menu():
    return navigation()


async def render(client, user_id, state, text, markup, source=None):
    """Edit one persistent bot menu message; user content and previews remain separate."""
    menu_id = state.get("menu_id") if state else None
    if not menu_id and source:
        menu_id = source.id
    if menu_id:
        try:
            return await client.edit_message_text(user_id, menu_id, text, reply_markup=markup)
        except RPCError as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc).upper():
                return
            log.warning("Menu edit failed; creating replacement: %s", exc)
    msg = await client.send_message(user_id, text, reply_markup=markup)
    if state is not None:
        state["menu_id"] = msg.id
    return msg


async def render_cb(client, cb, state, text, markup):
    if state is not None:
        state["menu_id"] = cb.message.id
    return await render(client, cb.from_user.id, state, text, markup, cb.message)


def destination_keyboard(groups, selected, page=0, choosing=False):
    size = 6
    pages = max(1, (len(groups) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    rows = []
    for group in groups[page * size:(page + 1) * size]:
        marker = "✅" if str(group["chat_id"]) in selected else "⬜"
        kind = "📢" if group["chat_type"] == "channel" else "👥"
        rows.append([InlineKeyboardButton(
            f"{marker if choosing else kind} {group['title'][:42]}",
            callback_data=f"ui:dest:{group['chat_id']}" if choosing else f"ui:detail:{group['chat_id']}"
        )])
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"ui:page:{max(page-1,0)}"),
            InlineKeyboardButton(f"{page+1}/{pages}", callback_data="ui:noop"),
            InlineKeyboardButton("▶️", callback_data=f"ui:page:{min(page+1,pages-1)}"),
        ])
    if choosing:
        rows.append([
            InlineKeyboardButton("☑️ Select All", callback_data="ui:all"),
            InlineKeyboardButton("🧹 Clear", callback_data="ui:clear"),
        ])
        rows.append([InlineKeyboardButton(f"➡️ Continue ({len(selected)})", callback_data="ui:confirm")])
    else:
        rows.append([InlineKeyboardButton("➕ Connect Another", callback_data="ui:connect")])
    return navigation(*rows)


def confirmation_keyboard(mode):
    return navigation([InlineKeyboardButton(
        "🗓 Confirm Schedule" if mode == "schedule" else "🚀 Publish Now",
        callback_data="ui:save" if mode == "schedule" else "ui:publish"
    )])


async def destination_screen(client, user_id, state):
    groups = await group_picker(client, user_id)
    state["groups"] = groups
    page = state.get("page", 0)
    choosing = state.get("step") == "target"
    text = ("📢 Select one or more groups/channels.\nTap to toggle selection."
            if choosing else "👥 Your connected groups and channels.")
    if not groups:
        text += "\n\nNo accessible destinations yet. Connect a group or channel first."
    return await render(client, user_id, state, text,
                        destination_keyboard(groups, state.get("targets", []), page, choosing))


async def confirmation_screen(client, user_id, state):
    names = {str(g["chat_id"]): g["title"] for g in state.get("groups", [])}
    lines = [names.get(str(t), str(t)) for t in state["targets"]]
    label = "🗓 Confirm Scheduled Post" if state["mode"] == "schedule" else "✅ Confirm Post"
    text = label + "\n\nDestinations:\n" + "\n".join("• " + n for n in lines)
    if state["mode"] == "schedule" and state.get("due"):
        local = state["due"].replace(tzinfo=timezone.utc).astimezone(TZ)
        text += f"\n\n🕒 {local:%Y-%m-%d %H:%M} ({TZ})"
    text += f"\n\n🔗 URL buttons: {len(state['buttons'])}"
    return await render(client, user_id, state, text, confirmation_keyboard(state["mode"]))


@app.on_callback_query(filters.regex(r"^ui:"))
async def menu_click(client, cb):
    user = cb.from_user
    if not user or not cb.message or cb.message.chat.id != user.id:
        return await cb.answer("Open the bot in private chat.", show_alert=True)
    action = cb.data[3:]
    if action == "noop":
        return await cb.answer()
    if action == "verify":
        if not await subscribed(client, user.id):
            return await cb.answer("Please join the required channel first.", show_alert=True)
        await cb.answer("Membership verified!")
        return await render_cb(client, cb, sessions.get(user.id), "🎉 Access granted! Welcome to AGS Post Manager.", main_menu())
    if not await subscribed(client, user.id):
        await cb.answer("Join the required channel first.", show_alert=True)
        return await render_cb(client, cb, sessions.get(user.id),
                               "🔒 Join our required group or channel to unlock the bot.", await join_markup())
    state = sessions.get(user.id)
    if state and state.get("menu_id") and cb.message.id != state["menu_id"]:
        return await cb.answer("Use the latest menu. Send /start to reset.", show_alert=True)
    await cb.answer()
    if action == "home":
        sessions.pop(user.id, None)
        return await render_cb(client, cb, None, "🏠 AGS Post Manager — choose an action:", main_menu())
    if action == "owner" or action.startswith("force"):
        if user.id not in ADMIN_IDS:
            return await render_cb(client, cb, state,
                "🔐 Owner Settings are restricted.\n\nYour Telegram user ID: " + str(user.id) +
                "\nAdd this ID to ADMIN_IDS in the VPS .env file, then restart the bot.",
                navigation())
        if action == "owner":
            return await render_cb(client, cb, state, "👑 Owner Settings", owner_menu())
        if action == "force":
            cfg = await force_config()
            enabled = cfg.get("force_enabled") == "1"
            return await render_cb(client, cb, state,
                f"🔒 Force Subscribe: {'Enabled' if enabled else 'Disabled'}\nChannel: {cfg.get('force_chat') or 'Not configured'}",
                force_menu(enabled))
        if action == "force_update":
            sessions[user.id] = {"step": "force_channel", "menu_id": cb.message.id}
            return await render_cb(client, cb, sessions[user.id],
                "✏️ Send a group or channel @username, https://t.me/username, or -100... ID.\nThe bot must be a group/channel admin.", cancel_menu())
        if action == "force_toggle":
            cfg = await force_config()
            if cfg.get("force_enabled") != "1" and not cfg.get("force_chat"):
                return await render_cb(client, cb, state, "❌ Configure a channel first.", owner_menu())
            enabled = cfg.get("force_enabled") != "1"
            await set_config("force_enabled", "1" if enabled else "0")
            return await render_cb(client, cb, state,
                f"🔒 Force Subscribe: {'Enabled' if enabled else 'Disabled'}",
                force_menu(enabled))
    if action == "help":
        return await render_cb(client, cb, state, HELP, navigation())
    if action == "list":
        rows = await query("SELECT id,target_chat,scheduled_utc FROM scheduled_posts WHERE owner_id=%s AND status='pending' ORDER BY scheduled_utc LIMIT 20", (user.id,), mode="all")
        lines = ["📋 Pending posts:"]
        for row in rows:
            local = row["scheduled_utc"].replace(tzinfo=timezone.utc).astimezone(TZ)
            lines.append(f"#{row['id']} · {row['target_chat']} · {local:%Y-%m-%d %H:%M}")
        if not rows:
            lines.append("None")
        return await render_cb(client, cb, state, "\n".join(lines), navigation())
    if action == "connect":
        me = await client.get_me()
        return await render_cb(client, cb, state,
            "➕ Connect Group / Channel\n\nChoose Telegram selection or Manual Register.\nThe bot and you must be destination admins.",
            navigation(
                [InlineKeyboardButton("👥 Select Group", url=f"https://t.me/{me.username}?startgroup=connect")],
                [InlineKeyboardButton("📢 Select Channel", url=f"https://t.me/{me.username}?startchannel=connect&admin=post_messages")],
                [InlineKeyboardButton("✍️ Manual Register", callback_data="ui:register")],
                [InlineKeyboardButton("🔄 Connected Destinations", callback_data="ui:groups")],
            ))
    if action == "register":
        state = {"step": "register", "menu_id": cb.message.id}
        sessions[user.id] = state
        return await render_cb(client, cb, state,
            "✍️ Manual Register\n\nSend a -100... group/channel ID, @publicusername, or https://t.me/publicusername.\nBoth you and the bot must be admins.",
            navigation())
    if action in ("create", "schedule", "demo"):
        state = {"mode": action, "step": "media", "buttons": [], "targets": [], "menu_id": cb.message.id}
        sessions[user.id] = state
        return await render_cb(client, cb, state,
            "🖼 Step 1 — Send a photo, video, document or text with its caption.",
            navigation())
    if action == "groups" or action == "refresh":
        state = {"step": "browse", "page": 0, "menu_id": cb.message.id}
        sessions[user.id] = state
        return await destination_screen(client, user.id, state)
    if action == "detail":
        return
    if action.startswith("detail:"):
        target = int(action.split(":", 1)[1])
        row = await query("SELECT title,chat_type FROM registered_groups WHERE chat_id=%s", (target,), mode="one")
        return await render_cb(client, cb, state,
            f"👥 {row['title'] if row else 'Destination not found'}\nID: {target}\n\nUse Create Post to publish.",
            navigation([InlineKeyboardButton("👥 Destinations", callback_data="ui:groups")]))
    if action.startswith("page:"):
        if not state or state.get("step") not in ("target", "browse"):
            return
        state["page"] = int(action.split(":", 1)[1])
        return await destination_screen(client, user.id, state)
    if action == "back":
        if not state:
            return await render_cb(client, cb, None, "🏠 Main menu", main_menu())
        step = state.get("step")
        if step in ("register", "browse"):
            return await menu_click_page(client, cb, "connect")
        if step == "force_channel":
            return await render_cb(client, cb, state, "👑 Owner Settings", owner_menu())
        if step in ("media",):
            sessions.pop(user.id, None)
            return await render_cb(client, cb, None, "🏠 Main menu", main_menu())
        if step in ("button", "ready"):
            state["step"] = "ready" if "source" in state else "media"
            return await render_cb(client, cb, state,
                "🔗 Add URL buttons or continue." if "source" in state else "🖼 Send your post.",
                draft_menu() if "source" in state else navigation())
        if step == "target":
            state["step"] = "ready"
            return await render_cb(client, cb, state, "🔗 Add URL buttons or continue.", draft_menu())
        if step == "confirm":
            state["step"] = "target"
            return await destination_screen(client, user.id, state)
        if step == "date":
            state["step"] = "target"
            return await destination_screen(client, user.id, state)
    if not state or "source" not in state:
        return await render_cb(client, cb, state, "🏠 Start from the main menu.", main_menu())
    if action == "add":
        state["step"] = "button"
        return await render_cb(client, cb, state, "🔗 Send a button:\nDOWNLOAD | https://example.com", navigation())
    if action == "preview":
        try:
            await client.copy_message(user.id, user.id, state["source"], reply_markup=keyboard(state["buttons"]))
        except RPCError:
            return await render_cb(client, cb, state, "❌ Preview failed. Original post may be unavailable.", draft_menu())
        return await render_cb(client, cb, state, "👁 Preview sent above. Continue when ready.", draft_menu())
    if action == "next":
        if state["mode"] == "demo":
            try:
                await client.copy_message(user.id, user.id, state["source"], reply_markup=keyboard(state["buttons"]))
            except RPCError:
                return await render_cb(client, cb, state, "❌ Demo failed.", draft_menu())
            sessions.pop(user.id, None)
            return await render_cb(client, cb, None, "✅ Demo complete. Nothing published.", main_menu())
        state["step"] = "target"
        state["page"] = 0
        return await destination_screen(client, user.id, state)
    if action == "all" or action == "clear":
        if state.get("step") != "target":
            return
        groups = await group_picker(client, user.id)
        state["targets"] = [str(g["chat_id"]) for g in groups] if action == "all" else []
        return await destination_screen(client, user.id, state)
    if action.startswith("dest:"):
        if state.get("step") != "target":
            return
        target = action.split(":", 1)[1]
        available = {str(g["chat_id"]) for g in await group_picker(client, user.id)}
        if target not in available:
            return await render_cb(client, cb, state, "❌ Destination unavailable.", navigation())
        selected = state.setdefault("targets", [])
        if target in selected:
            selected.remove(target)
        else:
            selected.append(target)
        return await destination_screen(client, user.id, state)
    if action == "confirm":
        if state.get("step") != "target" or not state.get("targets"):
            return await render_cb(client, cb, state, "⚠️ Select at least one destination.", navigation([InlineKeyboardButton("👥 Select Destinations", callback_data="ui:refresh")]))
        if state["mode"] == "schedule":
            state["step"] = "date"
            return await render_cb(client, cb, state, f"🕒 Send a future date/time ({TZ}):\nYYYY-MM-DD HH:MM", navigation())
        state["step"] = "confirm"
        return await confirmation_screen(client, user.id, state)
    if action in ("publish", "save") and state.get("step") == "confirm":
        if (action == "publish" and state["mode"] != "create") or (action == "save" and state["mode"] != "schedule"):
            return
        state["step"] = "sending"
        await render_cb(client, cb, state, "⏳ Processing selected destinations...", InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Please wait", callback_data="ui:noop")]]))
        ok, errors = [], []
        for target in state["targets"]:
            try:
                await check_permission(client, cb.message, int(target), actor_id=user.id)
                if action == "publish":
                    await client.copy_message(int(target), user.id, state["source"], reply_markup=keyboard(state["buttons"]))
                else:
                    await query("INSERT INTO scheduled_posts (owner_id,source_chat,source_message,target_chat,buttons_json,scheduled_utc) VALUES (%s,%s,%s,%s,%s,%s)",
                        (user.id, user.id, state["source"], target, json.dumps(state["buttons"]), state["due"]))
                ok.append(target)
            except (RPCError, ValueError, FloodWait) as exc:
                errors.append(f"{target}: {str(exc)[:90]}")
        sessions.pop(user.id, None)
        result = f"✅ {'Scheduled' if action == 'save' else 'Published'}: {len(ok)}\n❌ Failed: {len(errors)}"
        if errors:
            result += "\n\n" + "\n".join(errors[:8])
        return await render_cb(client, cb, None, result, main_menu())


async def menu_click_page(client, cb, page):
    if page == "connect":
        me = await client.get_me()
        sessions.pop(cb.from_user.id, None)
        return await render_cb(client, cb, None,
            "➕ Connect Group / Channel",
            navigation(
                [InlineKeyboardButton("👥 Select Group", url=f"https://t.me/{me.username}?startgroup=connect")],
                [InlineKeyboardButton("📢 Select Channel", url=f"https://t.me/{me.username}?startchannel=connect&admin=post_messages")],
                [InlineKeyboardButton("✍️ Manual Register", callback_data="ui:register")],
                [InlineKeyboardButton("🔄 Connected Destinations", callback_data="ui:groups")],
            ))


@app.on_message(filters.private & ~filters.command(["start", "help", "demo", "buttons", "schedule", "scheduled", "cancel"]), group=1)
async def menu_input(client, message):
    if not message.from_user:
        return
    state = sessions.get(message.from_user.id)
    if not state:
        return
    uid = message.from_user.id
    step = state.get("step")
    if step == "force_channel":
        if uid not in ADMIN_IDS:
            return
        if not message.text:
            return await render(client, uid, state, "Send a channel username, link or ID.", navigation())
        try:
            title = await save_force_channel(client, message.text)
            sessions.pop(uid, None)
            return await render(client, uid, state, f"✅ Required channel updated: {title}", owner_menu())
        except (ValueError, RPCError) as exc:
            return await render(client, uid, state, "❌ " + str(exc), navigation())
    if not await subscribed(client, uid):
        return await join_prompt(message)
    if step == "register":
        if not message.text:
            return await render(client, uid, state, "Send a group/channel ID or public link.", navigation())
        try:
            value = message.text.strip()
            if value.startswith("https://t.me/"):
                slug = value[len("https://t.me/"):].split("?")[0].strip("/")
                if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", slug):
                    raise ValueError("Private invite links are not supported. Send the -100... ID.")
                value = "@" + slug
            target = parse_target(value)
            await check_permission(client, message, target)
            chat = await client.get_chat(target)
            await save_group(chat)
            sessions.pop(uid, None)
            return await render(client, uid, state,
                f"✅ Registered: {chat.title}\nID: {chat.id}", main_menu())
        except (ValueError, RPCError) as exc:
            return await render(client, uid, state, "❌ " + str(exc) + "\n\nTry another ID or public link.", navigation())
    if step == "media":
        if not (message.text or message.photo or message.video or message.document):
            return await render(client, uid, state, "🖼 Send a text, photo, video or document.", navigation())
        state["source"] = message.id
        state["step"] = "ready"
        return await render(client, uid, state, "✅ Post received.\nAdd URL buttons or continue.", draft_menu())
    if not message.text:
        return await render(client, uid, state, "Send text for this step.", navigation())
    try:
        if step == "button":
            new = parse_buttons([message.text])
            if len(state["buttons"]) + len(new) > 20:
                raise ValueError("Maximum 20 buttons.")
            state["buttons"].extend(new)
            state["step"] = "ready"
            return await render(client, uid, state,
                f"✅ Button added ({len(state['buttons'])}/20).\nAdd another or continue.", draft_menu())
        if step == "date":
            local = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            due = local.astimezone(timezone.utc).replace(tzinfo=None)
            if due <= datetime.now(timezone.utc).replace(tzinfo=None):
                raise ValueError("Choose a future date/time.")
            state["due"] = due
            state["step"] = "confirm"
            return await confirmation_screen(client, uid, state)
    except (ValueError, RPCError) as exc:
        return await render(client, uid, state, "❌ " + str(exc), navigation())
    except Exception:
        log.exception("Menu input failed")
        return await render(client, uid, state, "❌ Error. Check VPS logs.", navigation())


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
