"""AGS Movie Link Bot. New channel posts only; independent of OTT database."""
import asyncio
import html
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
    # Add optional display metadata without dropping existing indexed files.
    for name, definition in [("poster_file_id", "VARCHAR(512) NULL"),
                             ("release_year", "VARCHAR(4) NULL"),
                             ("language", "VARCHAR(100) NULL"),
                             ("rating", "VARCHAR(16) NULL"),
                             ("genres", "VARCHAR(255) NULL"),
                             ("media_type", "VARCHAR(32) NOT NULL DEFAULT 'Movie'")]:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='files' AND column_name=%s""", (name,))
                exists = (await cur.fetchone())[0]
                if not exists:
                    await cur.execute("ALTER TABLE files ADD COLUMN " + name + " " + definition)

@app.on_message(filters.channel & filters.chat(SOURCE) & (filters.document | filters.video))
async def on_new_media(_, message):
    parsed = parse_media(message)
    if not parsed:
        log.warning("Unrecognized media title for message %s", message.id)
        return
    title, quality = parsed
    media = message.document or message.video
    raw = (getattr(media, "file_name", None) or "") + " " + (message.caption or "")
    year = re.search(r"\\b(?:19|20)\\d{2}\\b", raw)
    language = re.search(r"\\b(Malayalam|English|Hindi|Tamil|Telugu|Kannada)\\b", raw, re.I)
    # Optional structured metadata in the file caption: Rating: 4.5, Genres: Drama, Type: TV Series.
    caption = message.caption or ""
    rating = re.search(r"(?im)^\\s*(?:⭐\\s*)?rating(?:s)?\\s*:\\s*([^\\n]{1,16})", caption)
    genres = re.search(r"(?im)^\\s*genres?\\s*:\\s*([^\\n]{1,255})", caption)
    kind = re.search(r"(?im)^\\s*type\\s*:\\s*(Movie|TV Series|Series)\\s*$", caption)
    # Poster can be supplied as Telegram photo file ID via caption: PosterFileID: <id>.
    poster = re.search(r"(?im)^\\s*PosterFileID\\s*:\\s*([A-Za-z0-9_-]{15,512})\\s*$", caption)
    try:
        await sql("""INSERT INTO files
            (source_channel,source_message,title,normalized_title,quality,filename,poster_file_id,release_year,language,rating,genres,media_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE title=VALUES(title),
            normalized_title=VALUES(normalized_title), quality=VALUES(quality),
            filename=VALUES(filename),poster_file_id=COALESCE(VALUES(poster_file_id),poster_file_id),
            release_year=VALUES(release_year),language=VALUES(language),
            rating=VALUES(rating),genres=VALUES(genres),media_type=VALUES(media_type)""",
            (SOURCE, message.id, title, normalize(title), quality,
             (getattr(media, "file_name", None) or "")[:1024],
             poster.group(1) if poster else None,
             year.group(0) if year else None, language.group(1).title() if language else None,
             rating.group(1).strip() if rating else None,
             genres.group(1).strip() if genres else None,
             kind.group(1) if kind else "Movie"))
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
    rows = await sql("""SELECT id,title,quality,filename,poster_file_id,release_year,language,rating,genres,media_type FROM files
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
        return await show_movie(message, rows[0])
    buttons = [[InlineKeyboardButton(
        (r["filename"] or r["title"])[:42] + " · " + r["quality"],
        callback_data="preview:" + str(r["id"]))] for r in rows]
    await message.reply_text("🎬 Choose a version to view its details:", reply_markup=InlineKeyboardMarkup(buttons))

async def show_movie(message, row):
    def safe(value):
        return html.escape(str(value or "Not available"))
    caption = ("🎬 <b>" + safe(row["title"]) + "</b>\\n\\n"
               "🎞 <b>Type:</b> " + safe(row["media_type"]) + "\\n"
               "📅 <b>Year:</b> " + safe(row["release_year"]) + "\\n"
               "⭐ <b>Rating:</b> " + safe(row["rating"]) + "\\n"
               "🔊 <b>Language:</b> " + safe(row["language"]) + "\\n"
               "🎭 <b>Genres:</b> " + safe(row["genres"]) + "\\n"
               "📺 <b>Quality:</b> " + safe(row["quality"]) + "\\n\\n"
               "📥 Tap DOWNLOAD to receive the file privately.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        "📥 DOWNLOAD", callback_data="file:" + str(row["id"]))]])
    if row.get("poster_file_id"):
        try:
            return await message.reply_photo(row["poster_file_id"], caption=caption,
                                             parse_mode="html", reply_markup=keyboard)
        except RPCError:
            log.warning("Poster unavailable for file %s; showing text", row["id"])
    return await message.reply_text(caption, parse_mode="html", reply_markup=keyboard)

@app.on_callback_query(filters.regex(r"^preview:\\d+$"))
async def preview(_, callback):
    await callback.answer()
    row = await sql("""SELECT id,title,quality,poster_file_id,release_year,
        language,rating,genres,media_type FROM files WHERE id=%s""",
        (int(callback.data.split(":")[1]),), fetch="one")
    if not row:
        return await callback.message.reply_text("File not found.")
    await show_movie(callback.message, row)

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
