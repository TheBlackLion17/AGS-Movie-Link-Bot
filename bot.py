"""AGS Movie Link Bot. New channel posts only; independent of OTT database."""
import asyncio
import logging
import os
import re
from urllib.parse import unquote

import aiomysql
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ags-movie-link")

def required(key):
    value = os.getenv(key)
    if not value:
        raise RuntimeError("Missing configuration: " + key)
    return value

SOURCE = int(os.getenv("SOURCE_CHANNEL_ID", "-1002518840015"))
USERNAME = required("BOT_USERNAME").lstrip("@")
app = Client("ags_movie_link", api_id=int(required("API_ID")),
             api_hash=required("API_HASH"), bot_token=required("BOT_TOKEN"))
pool = None

def normalize(text):
    text = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v)$", "", text or "", flags=re.I)
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())[:255]

def parse_media(message):
    media = message.document or message.video
    if not media:
        return None
    raw = (getattr(media, "file_name", None) or
           (message.caption or "").split("\n")[0]).strip()
    raw = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v)$", "", raw, flags=re.I)
    raw = re.sub(r"[._]+", " ", raw)
    raw = re.sub(r"^(?:\s*\[[^\]]{1,35}\]\s*[-–|:]?\s*)+", "", raw)
    raw = re.sub(r"^(?:(?:MS|MoviesHub|AGS)\s*[-–|:]\s*)+", "", raw, flags=re.I)
    raw = re.split(r"\b(?:S\d{1,2}E\d{1,3}|S\d{1,2}|480p|720p|1080p|2160p|4K|HEVC|x264|x265|WEB[- ]?DL|BluRay|HDRip|Malayalam|Hindi|Tamil|Telugu|English)\b",
                   raw, maxsplit=1, flags=re.I)[0]
    title = re.sub(r"\s+", " ", raw).strip(" -–|[]()")
    if not title or not re.search(r"[A-Za-z0-9]", title):
        return None
    quality = re.search(r"\b(480p|720p|1080p|2160p|4k)\b", (getattr(media, "file_name", "") or "") + " " + (message.caption or ""), re.I)
    return title[:255], quality.group(1).upper() if quality else "Unknown"

async def sql(statement, params=(), fetch=None):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(statement, params)
            if fetch == "all":
                return await cur.fetchall()
            if fetch == "one":
                return await cur.fetchone()

async def setup():
    global pool
    pool = await aiomysql.create_pool(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=required("MYSQL_USER"), password=required("MYSQL_PASSWORD"),
        db=os.getenv("MYSQL_DATABASE", "ags_movie_link"),
        autocommit=True, charset="utf8mb4", minsize=1, maxsize=4)
    await sql("""CREATE TABLE IF NOT EXISTS files (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        source_channel BIGINT NOT NULL,
        source_message BIGINT NOT NULL,
        title VARCHAR(255) NOT NULL,
        normalized_title VARCHAR(255) NOT NULL,
        quality VARCHAR(24) NOT NULL,
        filename VARCHAR(1024) NOT NULL DEFAULT '',
        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_message (source_channel,source_message),
        INDEX idx_title (normalized_title(191))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")

@app.on_message(filters.channel & filters.chat(SOURCE) & (filters.document | filters.video))
async def on_new_media(_, message):
    parsed = parse_media(message)
    if not parsed:
        log.warning("Unrecognized media title for message %s", message.id)
        return
    title, quality = parsed
    media = message.document or message.video
    try:
        await sql("""INSERT INTO files
            (source_channel,source_message,title,normalized_title,quality,filename)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE title=VALUES(title),
            normalized_title=VALUES(normalized_title),
            quality=VALUES(quality),filename=VALUES(filename)""",
            (SOURCE, message.id, title, normalize(title), quality,
             (getattr(media, "file_name", None) or "")[:1024]))
        log.info("Indexed new file: %s / %s", message.id, title)
    except Exception:
        log.exception("Indexing failed for message %s", message.id)

async def search(message, term):
    key = normalize(unquote(term).replace("_", " "))
    if len(key) < 2:
        return await message.reply_text("Send a movie title to search.")
    tokens = key.split()
    if not tokens:
        return await message.reply_text("Send a movie title to search.")
    pattern = "%" + "%".join(tokens) + "%"
    rows = await sql("""SELECT id,title,quality,filename FROM files
        WHERE normalized_title LIKE %s ORDER BY id DESC LIMIT 12""",
        (pattern,), fetch="all")
    if not rows and re.fullmatch(r"(19|20)\d{2}", tokens[-1]) and len(tokens) > 1:
        # Retry without year only when stored filenames omit it.
        rows = await sql("""SELECT id,title,quality,filename FROM files
            WHERE normalized_title LIKE %s ORDER BY id DESC LIMIT 12""",
            ("%" + "%".join(tokens[:-1]) + "%",), fetch="all")
    if not rows:
        return await message.reply_text("No matching new files indexed yet.")
    if len(rows) == 1:
        return await send_file(message.chat.id, rows[0]["id"])
    buttons = [[InlineKeyboardButton(
        (r["filename"] or r["title"])[:42] + " · " + r["quality"],
        callback_data="file:" + str(r["id"]))] for r in rows]
    await message.reply_text("Choose your file:", reply_markup=InlineKeyboardMarkup(buttons))

async def send_file(user_id, file_id):
    row = await sql("SELECT source_channel,source_message FROM files WHERE id=%s",
                    (file_id,), fetch="one")
    if not row:
        return await app.send_message(user_id, "File not found.")
    try:
        await app.copy_message(user_id, row["source_channel"], row["source_message"])
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        await app.copy_message(user_id, row["source_channel"], row["source_message"])
    except RPCError:
        log.exception("Cannot deliver indexed file %s", file_id)
        await app.send_message(user_id, "File unavailable. Check source channel access.")

@app.on_message(filters.private & filters.command("start"))
async def start(_, message):
    payload = message.command[1] if len(message.command) > 1 else ""
    if payload.startswith("search_"):
        return await search(message, payload[7:])
    await message.reply_text("🎬 Welcome! Send a movie title to find new uploads.")

@app.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def text_search(_, message):
    await search(message, message.text)

@app.on_callback_query(filters.regex(r"^file:\d+$"))
async def deliver(_, callback):
    await callback.answer()
    if callback.from_user:
        await send_file(callback.from_user.id, int(callback.data.split(":")[1]))

async def main():
    await setup()
    await app.start()
    me = await app.get_me()
    if (me.username or "").casefold() != USERNAME.casefold():
        raise RuntimeError("BOT_USERNAME does not match BOT_TOKEN")
    log.info("AGS Movie Link Bot started: @%s, source=%s", me.username, SOURCE)
    try:
        await idle()
    finally:
        await app.stop()
        pool.close()
        await pool.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
