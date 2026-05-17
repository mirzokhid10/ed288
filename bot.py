# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import secrets
import aiohttp
import subprocess
import uuid
import aiofiles
import aiomysql
from datetime import datetime
from enum import Enum
import json
from maxapi.enums.upload_type import UploadType

from maxapi.types.attachments.buttons import CallbackButton, LinkButton, OpenAppButton
from maxapi.types.attachments import AttachmentButton
from maxapi.types.attachments.attachment import ButtonsPayload
from maxapi.types import MessageCallback
from maxapi.enums.attachment import AttachmentType

from dotenv import load_dotenv
from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, MessageCreated, Command

# ========================================
# LOGGING
# ========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========================================
# ENV
# ========================================
load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN")
CHANNEL_ID       = int(os.getenv("CHANNEL_ID"))
CHANNEL_LINK     = os.getenv("CHANNEL_LINK", "https://max.ru/")
MINI_APP_DEEPLINK = os.getenv("MINI_APP_DEEPLINK", "https://max.ru/id501806959398_1_bot")

# Admin IDs — comma-separated: ADMIN_IDS=111111,222222
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = (
    [int(uid.strip()) for uid in ADMIN_IDS_RAW.split(",") if uid.strip()]
    if ADMIN_IDS_RAW else []
)

# MySQL — Railway public credentials
MYSQL_CONFIG = {
    'host':      os.getenv("MYSQL_HOST",     "localhost"),
    'port':      int(os.getenv("MYSQL_PORT", 3306)),
    'user':      os.getenv("MYSQL_USER",     "root"),
    'password':  os.getenv("MYSQL_PASSWORD", ""),
    'db':        os.getenv("MYSQL_DATABASE", "max_bot_db"),
    'charset':   'utf8mb4',
    'autocommit': True,
}

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env!")

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

db_pool  = None
BOT_ID   = None
BOT_USERNAME = None

# ========================================
# POST CREATION STATE
# ========================================

class PostStep(Enum):
    WAITING_TEXT         = 1
    WAITING_BUTTON_LABEL = 2
    WAITING_URL          = 3

# {user_id: {"step": PostStep, "text": str, "button_label": str}}
post_states: dict[int, dict] = {}

# ========================================
# DATABASE — INIT & TABLES
# ========================================

async def init_db_pool():
    global db_pool
    try:
        db_pool = await aiomysql.create_pool(
            host=MYSQL_CONFIG['host'],
            port=MYSQL_CONFIG['port'],
            user=MYSQL_CONFIG['user'],
            password=MYSQL_CONFIG['password'],
            db=MYSQL_CONFIG['db'],
            charset=MYSQL_CONFIG['charset'],
            autocommit=MYSQL_CONFIG['autocommit'],
            minsize=1,
            maxsize=10,
        )
        logger.info("MySQL connection pool created")

        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:

                # subscribers table
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS subscribers (
                        user_id    BIGINT PRIMARY KEY,
                        first_name VARCHAR(255),
                        joined_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ) CHARACTER SET utf8mb4
                """)

                # post_configs table  <-- replaces post_configs.json
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS post_configs (
                        post_id      VARCHAR(12)  PRIMARY KEY,
                        url          TEXT         NOT NULL,
                        button_label VARCHAR(100) NOT NULL,
                        created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP
                    ) CHARACTER SET utf8mb4
                """)

                logger.info("Tables verified/created: subscribers, post_configs")

    except Exception as e:
        logger.error(f"init_db_pool error: {e}", exc_info=True)
        raise


async def close_db_pool():
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logger.info("MySQL pool closed")

# ========================================
# POST CONFIG — MySQL CRUD
# ========================================

async def save_post_config(post_id: str, url: str, button_label: str) -> bool:
    """Insert a new post config into MySQL."""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO post_configs (post_id, url, button_label) VALUES (%s, %s, %s)",
                    (post_id, url, button_label)
                )
                logger.info(f"Post config saved to DB: {post_id} -> {url}")
                return True
    except Exception as e:
        logger.error(f"save_post_config error: {e}", exc_info=True)
        return False


async def get_all_post_configs() -> list[dict]:
    """Fetch all post configs from MySQL (for /listposts)."""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT post_id, url, button_label, created_at FROM post_configs ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                return [
                    {
                        "post_id":      row[0],
                        "url":          row[1],
                        "button_label": row[2],
                        "created_at":   row[3],
                    }
                    for row in rows
                ]
    except Exception as e:
        logger.error(f"get_all_post_configs error: {e}", exc_info=True)
        return []

# ========================================
# SUBSCRIBER — MySQL CRUD
# ========================================

async def add_subscriber_to_db(user_id: int, first_name: str = None) -> bool:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT IGNORE INTO subscribers (user_id, first_name) VALUES (%s, %s)",
                    (user_id, first_name)
                )
                added = cur.rowcount > 0
                logger.info(f"{'Added' if added else 'Already exists'}: user {user_id}")
                return added
    except Exception as e:
        logger.error(f"add_subscriber_to_db({user_id}): {e}")
        return False


async def remove_subscriber_from_db(user_id: int) -> bool:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM subscribers WHERE user_id = %s", (user_id,)
                )
                removed = cur.rowcount > 0
                logger.info(f"{'Removed' if removed else 'Not found'}: user {user_id}")
                return removed
    except Exception as e:
        logger.error(f"remove_subscriber_from_db({user_id}): {e}")
        return False


async def is_subscriber_in_db(user_id: int) -> bool:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM subscribers WHERE user_id = %s LIMIT 1", (user_id,)
                )
                return await cur.fetchone() is not None
    except Exception as e:
        logger.error(f"is_subscriber_in_db({user_id}): {e}")
        return True  # fail-open


async def get_all_subscribers_from_db() -> set:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM subscribers")
                return {row[0] for row in await cur.fetchall()}
    except Exception as e:
        logger.error(f"get_all_subscribers_from_db: {e}")
        return set()


async def get_subscriber_count() -> int:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM subscribers")
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception as e:
        logger.error(f"get_subscriber_count: {e}")
        return 0

# ========================================
# INITIAL POPULATION
# ========================================

async def populate_initial_members():
    logger.info("Starting initial member population...")
    total_added = 0
    page = 1
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": BOT_TOKEN}
            marker  = None
            while True:
                url = (
                    f"https://platform-api.max.ru/chats/{CHANNEL_ID}/members?count=100"
                    + (f"&marker={marker}" if marker else "")
                )
                try:
                    async with session.get(url, headers=headers) as resp:
                        logger.info(f"Page {page}: status={resp.status}")
                        if resp.status != 200:
                            break
                        data    = await resp.json()
                        members = data.get("members", [])
                        marker  = data.get("marker")
                        if not members:
                            break
                        for m in members:
                            uid = m.get("user_id")
                            if uid and await add_subscriber_to_db(uid, m.get("first_name")):
                                total_added += 1
                        if not marker:
                            break
                        page += 1
                        await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"Page {page} error: {e}", exc_info=True)
                    break
    except Exception as e:
        logger.error(f"populate_initial_members: {e}", exc_info=True)
    logger.info(f"Population done: {total_added} added ({page} pages)")
    return total_added

# ========================================
# BACKGROUND SYNC (every 6 hours)
# ========================================

async def sync_members_task():
    while True:
        try:
            await asyncio.sleep(6 * 60 * 60)
            logger.info("Periodic sync started...")
            api_members: set[int] = set()
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": BOT_TOKEN}
                marker  = None
                while True:
                    url = (
                        f"https://platform-api.max.ru/chats/{CHANNEL_ID}/members?count=100"
                        + (f"&marker={marker}" if marker else "")
                    )
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            break
                        data    = await resp.json()
                        members = data.get("members", [])
                        marker  = data.get("marker")
                        for m in members:
                            uid = m.get("user_id")
                            if uid:
                                api_members.add(uid)
                        if not members or not marker:
                            break
                        await asyncio.sleep(0.3)
            db_members = await get_all_subscribers_from_db()
            added   = sum(1 for uid in api_members - db_members if await add_subscriber_to_db(uid))
            removed = sum(1 for uid in db_members - api_members if await remove_subscriber_from_db(uid))
            logger.info(f"Sync done: +{added} / -{removed}")
        except Exception as e:
            logger.error(f"sync_members_task: {e}", exc_info=True)

# ========================================
# HELPERS
# ========================================

async def is_subscribed(user_id: int) -> bool:
    return await is_subscriber_in_db(user_id)


def get_subscribe_keyboard():
    return AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(
            buttons=[
                [LinkButton(text="Podpisatsya na kanal", url=CHANNEL_LINK)],
                [CallbackButton(text="Ya podpisalsya", payload="check_subscription")],
            ]
        ),
    )


def get_user_info(event):
    if hasattr(event, "user") and event.user:
        name = (
            getattr(event.user, "first_name", None)
            or getattr(event.user, "name", None)
            or "polzovatel"
        )
        return event.user.user_id, name
    if hasattr(event, "message") and hasattr(event.message, "sender"):
        sender = event.message.sender
        if sender:
            name = (
                getattr(sender, "first_name", None)
                or getattr(sender, "name", None)
                or "polzovatel"
            )
            return sender.user_id, name
    raise Exception("Cannot get user info from event")

# ========================================
# VIDEO HELPERS
# ========================================

async def download_video(url: str, filename: str) -> str:
    os.makedirs("videos", exist_ok=True)
    filepath = f"videos/{filename}"
    logger.info("Downloading video...")
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Download error: HTTP {resp.status}")
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(await resp.read())
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    logger.info(f"Video downloaded ({size_mb:.2f} MB)")
    return filepath


def convert_to_circle(input_path: str, output_path: str):
    logger.info("Converting to circle...")
    command = [
        "ffmpeg", "-i", input_path, "-i", "bg/space_bg.png",
        "-filter_complex",
        (
            "[1:v]scale=480:480[bg];"
            "[0:v]"
            "crop='min(iw,ih)':'min(iw,ih)',"
            "scale=400:400,"
            "format=yuva420p,"
            "geq="
            "lum='p(X,Y)':"
            "cb='cb(X,Y)':"
            "cr='cr(X,Y)':"
            "a='if(lt(sqrt((X-200)^2+(Y-200)^2),200),255,0)'"
            "[circle];"
            "[bg][circle]overlay=x=40:y=40"
        ),
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-aspect", "1:1", "-movflags", "+faststart",
        "-t", "60", "-y", output_path,
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg error:\n{result.stderr}")
        raise Exception("Video conversion failed")
    logger.info("Circle video ready")

# ========================================
# HANDLER 1 — Bot started
# ========================================

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    user_id, user_name = get_user_info(event)
    logger.info(f"Bot started: {user_name} ({user_id})")

    if not await is_subscribed(user_id):
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=f"Privet, {user_name}!\n\nDlya ispolzovaniya bota podpishis na kanal",
            attachments=[get_subscribe_keyboard()],
        )
        return

    await event.bot.send_message(
        chat_id=event.chat_id,
        text=(
            f"Privet, {user_name}!\n\n"
            "Ty podpisan na kanal!\n\n"
            "Otprav mne video i ya prevrashchu ego v kruzhochek\n\n"
            "Trebovaniya:\n"
            "- Dlitelnost: do 60 sekund\n"
            "- Formaty: MP4, MOV, AVI\n"
            "- Razmer: do 50 MB"
        ),
    )

# ========================================
# HANDLER 2 — /start
# ========================================

@dp.message_created(Command("start"))
async def start_handler(event: MessageCreated):
    user_id, user_name = get_user_info(event)
    logger.info(f"/start: {user_name} ({user_id})")

    if not await is_subscribed(user_id):
        await event.message.answer(
            text="Dlya ispolzovaniya bota podpishis na kanal",
            attachments=[get_subscribe_keyboard()],
        )
        return

    await event.message.answer("Gotovo k rabote!\n\nOtprav mne video")

# ========================================
# HANDLER 3 — /post (admin only)
# ========================================

@dp.message_created(Command("post"))
async def post_handler(event: MessageCreated):
    user_id, user_name = get_user_info(event)

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        logger.warning(f"Non-admin {user_id} tried /post")
        await event.message.answer("Eta komanda dostupna tolko administratoru.")
        return

    post_states[user_id] = {"step": PostStep.WAITING_TEXT}
    logger.info(f"/post started by {user_name} ({user_id})")

    await event.message.answer(
        "Sozdanie posta dlya kanala\n\n"
        "Shag 1/3: Otpravte tekst posta.\n\n"
        "Dlya otmeny: /cancel"
    )

# ========================================
# HANDLER 4 — /cancel
# ========================================

@dp.message_created(Command("cancel"))
async def cancel_handler(event: MessageCreated):
    user_id, _ = get_user_info(event)
    if user_id in post_states:
        del post_states[user_id]
        await event.message.answer("Sozdanie posta otmeneno.")
    else:
        await event.message.answer("U vas net aktivnogo sozdaniya posta.")

# ========================================
# HANDLER 5 — /listposts (admin only)
# ========================================

@dp.message_created(Command("listposts"))
async def listposts_handler(event: MessageCreated):
    user_id, _ = get_user_info(event)

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await event.message.answer("Eta komanda dostupna tolko administratoru.")
        return

    configs = await get_all_post_configs()
    if not configs:
        await event.message.answer("Net aktivnykh postov.")
        return

    lines = ["Aktivnye posty:\n"]
    for cfg in configs:
        created = cfg['created_at'].strftime('%Y-%m-%d %H:%M') if cfg['created_at'] else '?'
        lines.append(
            f"ID: {cfg['post_id']}\n"
            f"Knopka: {cfg['button_label']}\n"
            f"URL: {cfg['url']}\n"
            f"Sozdan: {created}\n"
        )
    await event.message.answer("\n".join(lines))

# ========================================
# HANDLER 6 — Channel join
# ========================================

@dp.user_added()
async def user_added_handler(event):
    try:
        if hasattr(event, "chat_id") and (
            event.chat_id == CHANNEL_ID or event.chat_id == -abs(CHANNEL_ID)
        ):
            if hasattr(event, "user") and hasattr(event.user, "user_id"):
                uid   = event.user.user_id
                fname = getattr(event.user, "first_name", None)
                await add_subscriber_to_db(uid, fname)
                logger.info(f"User {uid} joined channel -> added to DB")
    except Exception as e:
        logger.error(f"user_added_handler: {e}", exc_info=True)

# ========================================
# HANDLER 7 — Channel leave
# ========================================

@dp.user_removed()
async def user_removed_handler(event):
    try:
        if hasattr(event, "chat_id") and (
            event.chat_id == CHANNEL_ID or event.chat_id == -abs(CHANNEL_ID)
        ):
            if hasattr(event, "user") and hasattr(event.user, "user_id"):
                uid = event.user.user_id
                await remove_subscriber_from_db(uid)
                logger.info(f"User {uid} left channel -> removed from DB")
    except Exception as e:
        logger.error(f"user_removed_handler: {e}", exc_info=True)

# ========================================
# HANDLER 8 — Callback buttons
# ========================================

@dp.message_callback()
async def handle_callback(event: MessageCallback):
    user_id   = event.callback.user.user_id
    user_name = getattr(event.callback.user, "first_name", None) or "polzovatel"
    chat_id   = event.message.recipient.chat_id

    if event.callback.payload == "check_subscription":
        logger.info(f"Subscription check: {user_name} ({user_id})")
        await asyncio.sleep(2)

        if await is_subscribed(user_id):
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Podpiska podtverzhdena!\n\n"
                    "Otprav mne video i ya prevrashchu ego v kruzhochek\n\n"
                    "Trebovaniya:\n"
                    "- Dlitelnost: do 60 sekund\n"
                    "- Formaty: MP4, MOV, AVI\n"
                    "- Razmer: do 50 MB"
                ),
            )
        else:
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Ty eshche ne podpisan. Podpishis i nazh mi knopku snova!\n\n"
                    "Ubedis chto:\n"
                    "1. Nazhal Podpisatsya na kanal\n"
                    "2. Podtverdil podpisku v kanale\n"
                    "3. Ne otpisalsya srazu posle podpiski"
                ),
                attachments=[get_subscribe_keyboard()],
            )

# ========================================
# HANDLER 9 — Post wizard + Video processing
# ========================================

@dp.message_created()
async def handle_message(event: MessageCreated):
    if not event.message.sender:
        return

    user_id, user_name = get_user_info(event)
    message_text = event.message.body.text if event.message.body else None

    # ── Post Creation Wizard (Admin Only) ──────────────────────
    if user_id in post_states:
        state = post_states[user_id]

        if not message_text:
            await event.message.answer("Pozhaluysta otpravte tekstovoe soobshenie.")
            return

        if state["step"] == PostStep.WAITING_TEXT:
            state["text"] = message_text
            state["step"] = PostStep.WAITING_BUTTON_LABEL
            logger.info(f"Post text received from {user_id}")
            await event.message.answer(
                "Tekst posta sokhranyon!\n\n"
                "Shag 2/3: Vvedite tekst knopki\n\n"
                "Primer: ZABRAT INSTRUKTSIYU"
            )
            return

        elif state["step"] == PostStep.WAITING_BUTTON_LABEL:
            if len(message_text) > 100:
                await event.message.answer("Tekst knopki slishkom dlinny (maks. 100 simvolov)")
                return
            state["button_label"] = message_text
            state["step"]         = PostStep.WAITING_URL
            logger.info(f"Button label: {message_text}")
            await event.message.answer(
                "Tekst knopki sokhranyon!\n\n"
                "Shag 3/3: Vvedite URL dlya perekhoda\n\n"
                "Primer: https://disk.yandex.ru/i/abc123"
            )
            return

        elif state["step"] == PostStep.WAITING_URL:
            url = message_text.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                await event.message.answer(
                    "Neverniy format URL\n\n"
                    "URL dolzhen nachinatsa s http:// ili https://\n\n"
                    "Poprobuite eshche raz:"
                )
                return

            post_id = secrets.token_hex(6)  # 12-char hex, safe for startapp payload

            try:
                await event.message.answer("Publikuyu post v kanale...")

                # Save to MySQL FIRST — so gate_server can find it immediately
                saved = await save_post_config(post_id, url, state["button_label"])
                if not saved:
                    raise Exception("Failed to save post config to database")

                # Publish post to channel with LinkButton deeplink
                post_button = AttachmentButton(
                    type=AttachmentType.INLINE_KEYBOARD,
                    payload=ButtonsPayload(
                        buttons=[
                            [LinkButton(
                                text=state["button_label"],
                                url=f"{MINI_APP_DEEPLINK}?startapp=postid_{post_id}",
                            )]
                        ]
                    ),
                )

                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=state["text"],
                    attachments=[post_button],
                )
                logger.info(f"Post published: {post_id}")

                await event.message.answer(
                    f"Post opublikovan v kanale!\n\n"
                    f"ID: {post_id}\n"
                    f"URL: {url}\n"
                    f"Knopka: {state['button_label']}\n\n"
                    f"Spisok postov: /listposts"
                )

            except Exception as e:
                logger.error(f"Publish error: {e}", exc_info=True)
                await event.message.answer(
                    "Oshibka publikatsii posta.\n"
                    "Proverite prava bota v kanale."
                )
            finally:
                del post_states[user_id]

            return

    # ── Video Processing ────────────────────────────────────────

    if not await is_subscribed(user_id):
        await event.message.answer(
            text="Dlya ispolzovaniya bota podpishis na kanal",
            attachments=[get_subscribe_keyboard()],
        )
        return

    attachments = event.message.body.attachments or []
    if not attachments and event.message.link and event.message.link.message:
        logger.info("Forwarded message detected")
        attachments = event.message.link.message.attachments or []

    if not attachments:
        await event.message.answer("Otprav mne video dlya konvertatsii v kruzhochek!")
        return

    for attachment in attachments:
        if getattr(attachment, "type", None) != "video":
            continue

        logger.info(f"Video from {user_name} ({user_id})")
        await event.message.answer("Obrabatyvayu video...")

        file_id     = str(uuid.uuid4())
        input_path  = None
        output_path = None

        try:
            input_path = await download_video(
                attachment.payload.url, f"{file_id}_input.mp4"
            )
            await event.message.answer("Skachano! Konvertiruyu...")

            output_path = f"videos/{file_id}_circle.mp4"
            convert_to_circle(input_path, output_path)
            await event.message.answer("Gotovo! Zagruzhayu...")

            upload_info = await bot.get_upload_url(type=UploadType.VIDEO)
            await bot.upload_file(url=upload_info.url, path=output_path, type=UploadType.VIDEO)

            token = upload_info.token
            if not token:
                raise Exception("Upload token not found")

            from maxapi.types.attachments.upload import AttachmentUpload, AttachmentPayload

            circle_attachment = AttachmentUpload(
                type=UploadType.VIDEO,
                payload=AttachmentPayload(token=token),
            )
            await event.message.answer(
                text="Vot tvoy kruzhochek!",
                attachments=[circle_attachment],
            )

        except Exception as e:
            logger.error(f"Video error: {e}", exc_info=True)
            await event.message.answer(
                "Oshibka obrabotki video.\n\n"
                "Poprobuite:\n"
                "- Bolee korotkoe video (do 60 sek)\n"
                "- Menshiy razmer (do 50 MB)\n"
                "- Drugoy format (MP4, MOV, AVI)"
            )
        finally:
            for path in [input_path, output_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            logger.info("Temp files cleaned up")

        return

    await event.message.answer("Otprav video-fayl!\nFormaty: MP4, MOV, AVI")

# ========================================
# MAIN
# ========================================

async def main():
    global BOT_ID, BOT_USERNAME

    logger.info("=" * 60)
    logger.info("MAX BOT - Kruzhochek + Gated Posts")
    logger.info("=" * 60)
    logger.info(f"Channel ID  : {CHANNEL_ID}")
    logger.info(f"Channel Link: {CHANNEL_LINK}")
    logger.info(f"Deeplink    : {MINI_APP_DEEPLINK}")
    logger.info(f"Admin IDs   : {ADMIN_IDS}")
    logger.info(f"MySQL Host  : {MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}")
    logger.info("=" * 60)

    try:
        # 1. Connect to Railway MySQL, create tables if needed
        await init_db_pool()

        # 2. Remove stale webhook
        await bot.delete_webhook()
        logger.info("Webhook removed")

        # 3. Bot info
        me = await bot.get_me()
        BOT_ID       = me.user_id
        BOT_USERNAME = me.username
        logger.info(f"Bot: @{BOT_USERNAME} (id={BOT_ID})")

        # 4. Populate DB if empty
        count = await get_subscriber_count()
        if count == 0:
            logger.info("DB empty - running initial population...")
            added = await populate_initial_members()
            logger.info(f"Initial population: {added} added")
        else:
            logger.info(f"DB has {count} subscribers - skipping population")

        # 5. Background sync every 6 hours
        asyncio.create_task(sync_members_task())
        logger.info("Background sync task started")

        logger.info("=" * 60)
        logger.info("BOT IS RUNNING")
        logger.info("=" * 60)

        # 6. Start polling
        await dp.start_polling(bot)

    except Exception as e:
        logger.error(f"Startup error: {e}", exc_info=True)
        raise
    finally:
        await close_db_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)