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

from maxapi.types.attachments.buttons import CallbackButton, LinkButton
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://max.ru/")

# Admin IDs — comma-separated in .env: ADMIN_IDS=123456,789012
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = (
    [int(uid.strip()) for uid in ADMIN_IDS_RAW.split(",") if uid.strip()]
    if ADMIN_IDS_RAW else []
)

# MySQL
MYSQL_CONFIG = {
    'host':      os.getenv("MYSQL_HOST", "localhost"),
    'port':      int(os.getenv("MYSQL_PORT", 3306)),
    'user':      os.getenv("MYSQL_USER", "root"),
    'password':  os.getenv("MYSQL_PASSWORD", ""),
    'db':        os.getenv("MYSQL_DATABASE", "max_bot_db"),
    'charset':   'utf8mb4',
    'autocommit': True,
}

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env!")

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

# Global async DB pool (aiomysql — shared with the async bot event loop)
db_pool = None

# ========================================
# POST CREATION STATE
# ========================================

class PostStep(Enum):
    WAITING_TEXT         = 1
    WAITING_BUTTON_LABEL = 2
    WAITING_URL          = 3

# {user_id: {"step": PostStep, "text": str, "button_label": str}}
post_states: dict[int, dict] = {}

# {post_id: {"url": str, "button_label": str, "created_at": datetime}}
post_configs: dict[str, dict] = {}

POST_CONFIGS_FILE = "post_configs.json"

# ========================================
# POST PERSISTENCE
# ========================================

async def save_post_configs():
    """Persist post configs to JSON so gate_server.py can read them."""
    try:
        data = {
            pid: {
                "url":          cfg["url"],
                "button_label": cfg["button_label"],
                "created_at":   cfg["created_at"].isoformat(),
            }
            for pid, cfg in post_configs.items()
        }
        async with aiofiles.open(POST_CONFIGS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        logger.info(f"💾 Saved {len(post_configs)} post configs")
    except Exception as e:
        logger.error(f"❌ save_post_configs: {e}", exc_info=True)


async def load_post_configs():
    """Load post configs from JSON on startup."""
    global post_configs
    try:
        if not os.path.exists(POST_CONFIGS_FILE):
            logger.info("ℹ️ No post_configs.json found — starting fresh")
            return
        async with aiofiles.open(POST_CONFIGS_FILE, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())
        post_configs = {
            pid: {
                "url":          cfg["url"],
                "button_label": cfg["button_label"],
                "created_at":   datetime.fromisoformat(cfg["created_at"]),
            }
            for pid, cfg in data.items()
        }
        logger.info(f"✅ Loaded {len(post_configs)} post configs")
    except Exception as e:
        logger.error(f"❌ load_post_configs: {e}", exc_info=True)
        post_configs = {}

# ========================================
# DATABASE
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
        logger.info("✅ MySQL connection pool created")

        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES LIKE 'subscribers'")
                if await cur.fetchone():
                    logger.info("✅ Table 'subscribers' exists")
                else:
                    raise Exception("Table 'subscribers' not found — run SQL setup first!")
    except Exception as e:
        logger.error(f"❌ init_db_pool: {e}", exc_info=True)
        raise


async def close_db_pool():
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logger.info("✅ MySQL pool closed")


async def add_subscriber_to_db(user_id: int, first_name: str = None) -> bool:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT IGNORE INTO subscribers (user_id, first_name) VALUES (%s, %s)",
                    (user_id, first_name),
                )
                added = cur.rowcount > 0
                logger.info(f"{'➕ Added' if added else 'ℹ️ Already in DB'}: user {user_id}")
                return added
    except Exception as e:
        logger.error(f"❌ add_subscriber_to_db({user_id}): {e}")
        return False


async def remove_subscriber_from_db(user_id: int) -> bool:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM subscribers WHERE user_id = %s", (user_id,)
                )
                removed = cur.rowcount > 0
                logger.info(f"{'➖ Removed' if removed else 'ℹ️ Not in DB'}: user {user_id}")
                return removed
    except Exception as e:
        logger.error(f"❌ remove_subscriber_from_db({user_id}): {e}")
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
        logger.error(f"❌ is_subscriber_in_db({user_id}): {e}")
        return True  # fail-open


async def get_all_subscribers_from_db() -> set:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT user_id FROM subscribers")
                return {row[0] for row in await cur.fetchall()}
    except Exception as e:
        logger.error(f"❌ get_all_subscribers_from_db: {e}")
        return set()


async def get_subscriber_count() -> int:
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM subscribers")
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception as e:
        logger.error(f"❌ get_subscriber_count: {e}")
        return 0

# ========================================
# INITIAL POPULATION
# ========================================

async def populate_initial_members():
    """Pull all current channel members from MAX API and save to DB."""
    logger.info("🔄 Starting initial member population...")
    total_added = 0
    page = 1

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": BOT_TOKEN}
            marker = None

            while True:
                url = (
                    f"https://platform-api.max.ru/chats/{CHANNEL_ID}/members?count=100"
                    + (f"&marker={marker}" if marker else "")
                )
                try:
                    async with session.get(url, headers=headers) as resp:
                        logger.info(f"📡 Page {page}: status={resp.status}, marker={marker}")
                        if resp.status != 200:
                            logger.error(f"❌ API error {resp.status}")
                            break

                        data    = await resp.json()
                        members = data.get("members", [])
                        marker  = data.get("marker")

                        logger.info(f"📋 Page {page}: {len(members)} members, next_marker={marker}")
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
                    logger.error(f"❌ Page {page} error: {e}", exc_info=True)
                    break

    except Exception as e:
        logger.error(f"❌ populate_initial_members: {e}", exc_info=True)

    logger.info(f"✅ Population done: {total_added} new members added ({page} pages)")
    return total_added

# ========================================
# BACKGROUND SYNC (every 6 hours)
# ========================================

async def sync_members_task():
    while True:
        await asyncio.sleep(6 * 60 * 60)
        logger.info("🔄 Periodic sync started...")

        api_members: set[int] = set()
        try:
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

        except Exception as e:
            logger.error(f"❌ sync fetch error: {e}")

        db_members = await get_all_subscribers_from_db()
        added   = sum([1 for uid in api_members - db_members if await add_subscriber_to_db(uid)])
        removed = sum([1 for uid in db_members - api_members if await remove_subscriber_from_db(uid)])
        logger.info(f"✅ Sync done: +{added} / -{removed} (API={len(api_members)}, DB={len(db_members)})")

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
                [LinkButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
                [CallbackButton(text="✅ Я подписался", payload="check_subscription")],
            ]
        ),
    )


def get_user_info(event):
    """Extract (user_id, name) from any event type."""
    if hasattr(event, "user") and event.user:
        name = (
            getattr(event.user, "first_name", None)
            or getattr(event.user, "name", None)
            or "пользователь"
        )
        return event.user.user_id, name

    if hasattr(event, "message") and hasattr(event.message, "sender"):
        sender = event.message.sender
        if sender:
            name = (
                getattr(sender, "first_name", None)
                or getattr(sender, "name", None)
                or "пользователь"
            )
            return sender.user_id, name

    raise Exception("Не удалось получить информацию о пользователе")

# ========================================
# VIDEO HELPERS
# ========================================

async def download_video(url: str, filename: str) -> str:
    os.makedirs("videos", exist_ok=True)
    filepath = f"videos/{filename}"
    logger.info("📥 Скачиваю видео...")
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Ошибка скачивания: HTTP {resp.status}")
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(await resp.read())
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    logger.info(f"✅ Видео скачано ({size_mb:.2f} MB)")
    return filepath


def convert_to_circle(input_path: str, output_path: str):
    logger.info("🔄 Создаю кружочек...")
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
        "-aspect", "1:1",
        "-movflags", "+faststart",
        "-t", "60",
        "-y", output_path,
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        logger.error(f"❌ FFmpeg error:\n{result.stderr}")
        raise Exception("Ошибка конвертации видео")
    logger.info("✅ Кружочек готов")

# ========================================
# HANDLER 1 — Bot started (DM button press)
# ========================================

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    user_id, user_name = get_user_info(event)
    logger.info(f"👤 Bot started: {user_name} ({user_id})")

    if not await is_subscribed(user_id):
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=f"Привет, {user_name}! 👋\n\nДля использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()],
        )
        return

    await event.bot.send_message(
        chat_id=event.chat_id,
        text=(
            f"Привет, {user_name}! 👋\n\n"
            "✅ Ты подписан на канал!\n\n"
            "📹 Отправь мне видео, и я превращу его в кружочек\n\n"
            "⚙️ Требования:\n"
            "• Длительность: до 60 секунд\n"
            "• Форматы: MP4, MOV, AVI\n"
            "• Размер: до 50 МБ"
        ),
    )

# ========================================
# HANDLER 2 — /start command
# ========================================

@dp.message_created(Command("start"))
async def start_handler(event: MessageCreated):
    user_id, user_name = get_user_info(event)
    logger.info(f"⚡ /start: {user_name} ({user_id})")

    if not await is_subscribed(user_id):
        await event.message.answer(
            text="❌ Для использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()],
        )
        return

    await event.message.answer(
        "✅ Готово к работе!\n\n"
        "📹 Отправь мне видео 🎥"
    )

# ========================================
# HANDLER 3 — /post command (admin only)
# ========================================

@dp.message_created(Command("post"))
async def post_handler(event: MessageCreated):
    user_id, user_name = get_user_info(event)

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        logger.warning(f"⛔ Non-admin {user_id} tried /post")
        await event.message.answer("⛔ Эта команда доступна только администратору.")
        return

    post_states[user_id] = {"step": PostStep.WAITING_TEXT}
    logger.info(f"📝 /post started by {user_name} ({user_id})")

    await event.message.answer(
        "📝 Создание поста для канала\n\n"
        "Шаг 1/3: Отправьте текст поста.\n\n"
        "Для отмены: /cancel"
    )

# ========================================
# HANDLER 4 — /cancel command
# ========================================

@dp.message_created(Command("cancel"))
async def cancel_handler(event: MessageCreated):
    user_id, _ = get_user_info(event)

    if user_id in post_states:
        del post_states[user_id]
        await event.message.answer("🚫 Создание поста отменено.")
    else:
        await event.message.answer("ℹ️ У вас нет активного создания поста.")

# ========================================
# HANDLER 5 — /listposts command (admin only)
# ========================================

@dp.message_created(Command("listposts"))
async def listposts_handler(event: MessageCreated):
    user_id, _ = get_user_info(event)

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await event.message.answer("⛔ Эта команда доступна только администратору.")
        return

    if not post_configs:
        await event.message.answer("ℹ️ Нет активных постов с подпиской.")
        return

    lines = ["📋 Активные посты:\n"]
    for pid, cfg in post_configs.items():
        lines.append(
            f"🔑 ID: {pid}\n"
            f"🔘 Кнопка: {cfg['button_label']}\n"
            f"🔗 URL: {cfg['url']}\n"
            f"📅 Создан: {cfg['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
        )
    await event.message.answer("\n".join(lines))

# ========================================
# HANDLER 6 — Channel user_added event
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
                logger.info(f"✅ User {uid} joined channel → added to DB")
    except Exception as e:
        logger.error(f"❌ user_added_handler: {e}", exc_info=True)

# ========================================
# HANDLER 7 — Channel user_removed event
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
                logger.info(f"❌ User {uid} left channel → removed from DB")
    except Exception as e:
        logger.error(f"❌ user_removed_handler: {e}", exc_info=True)

# ========================================
# HANDLER 8 — Callback buttons
# ========================================

@dp.message_callback()
async def handle_callback(event: MessageCallback):
    user_id   = event.callback.user.user_id
    user_name = getattr(event.callback.user, "first_name", None) or "пользователь"
    chat_id   = event.message.recipient.chat_id
    payload   = event.callback.payload

    if payload == "check_subscription":
        logger.info(f"🔄 Subscription check: {user_name} ({user_id})")

        # Small delay to allow webhook DB sync if user just subscribed
        await asyncio.sleep(2)

        if await is_subscribed(user_id):
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Подписка подтверждена!\n\n"
                    "📹 Отправь мне видео, и я превращу его в кружочек\n\n"
                    "⚙️ Требования:\n"
                    "• Длительность: до 60 секунд\n"
                    "• Форматы: MP4, MOV, AVI\n"
                    "• Размер: до 50 МБ"
                ),
            )
        else:
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Ты ещё не подписан. Подпишись и нажми кнопку снова!\n\n"
                    "💡 Убедись, что:\n"
                    "1. Нажал «Подписаться на канал»\n"
                    "2. Подтвердил подписку в канале\n"
                    "3. Не отписался сразу после подписки"
                ),
                attachments=[get_subscribe_keyboard()],
            )

# ========================================
# HANDLER 9 — Post wizard (MUST be before video handler)
#
# Catches ALL message_created events, but only acts when
# the sender is in the post_states dict (i.e. running /post).
# Returns immediately otherwise so the next handler can run.
# ========================================

@dp.message_created()
async def post_wizard_handler(event: MessageCreated):
    if not event.message.sender:
        return

    user_id, user_name = get_user_info(event)

    if user_id not in post_states:
        return  # not in wizard → fall through to video handler

    state        = post_states[user_id]
    message_text = event.message.body.text

    if not message_text:
        await event.message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
        return

    # ── Step 1: collect post text ──────────────────────────────
    if state["step"] == PostStep.WAITING_TEXT:
        state["text"] = message_text
        state["step"] = PostStep.WAITING_BUTTON_LABEL
        logger.info(f"📝 Post text received from {user_id} ({len(message_text)} chars)")
        await event.message.answer(
            "✅ Текст поста сохранён!\n\n"
            "Шаг 2/3: Введите текст кнопки\n\n"
            "Пример: ✅ ЗАБРАТЬ ИНСТРУКЦИЮ"
        )

    # ── Step 2: collect button label ───────────────────────────
    elif state["step"] == PostStep.WAITING_BUTTON_LABEL:
        if len(message_text) > 100:
            await event.message.answer("❌ Текст кнопки слишком длинный (макс. 100 символов)")
            return
        state["button_label"] = message_text
        state["step"]         = PostStep.WAITING_URL
        logger.info(f"🔘 Button label: {message_text}")
        await event.message.answer(
            "✅ Текст кнопки сохранён!\n\n"
            "Шаг 3/3: Введите URL для перехода\n\n"
            "Пример: https://disk.yandex.ru/i/1cS-6DUH_eTu0w"
        )

    # ── Step 3: collect URL → publish post ─────────────────────
    elif state["step"] == PostStep.WAITING_URL:
        url = message_text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await event.message.answer(
                "❌ Неверный формат URL\n\n"
                "URL должен начинаться с http:// или https://\n\n"
                "Попробуйте ещё раз:"
            )
            return

        # Generate post ID — hex token, safe for open_app payload
        post_id = secrets.token_hex(6)  # 12 chars

        post_configs[post_id] = {
            "url":          url,
            "button_label": state["button_label"],
            "created_at":   datetime.now(),
        }
        logger.info(f"💾 Post config saved: {post_id} → {url}")

        try:
            await event.message.answer("⏳ Публикую пост в канале...")

            # open_app button — opens the mini-app with post_id as start_param
            post_button = AttachmentButton(
                type=AttachmentType.INLINE_KEYBOARD,
                payload=ButtonsPayload(
                    buttons=[
                        [{
                            "type":    "open_app",
                            "text":    state["button_label"],
                            "payload": f"postid_{post_id}",
                        }]
                    ]
                ),
            )

            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=state["text"],
                attachments=[post_button],
            )
            logger.info(f"✅ Post published: {post_id}")

            await event.message.answer(
                f"✅ Пост опубликован в канале!\n\n"
                f"🔑 Post ID: {post_id}\n"
                f"🔗 URL: {url}\n"
                f"🔘 Кнопка: {state['button_label']}\n\n"
                f"Список постов: /listposts"
            )

            await save_post_configs()

        except Exception as e:
            logger.error(f"❌ Publish error: {e}", exc_info=True)
            await event.message.answer(
                "❌ Ошибка публикации поста.\n"
                "Проверьте права бота в канале."
            )
        finally:
            del post_states[user_id]

# ========================================
# HANDLER 10 — Video processing
#
# Catches ALL message_created events that were NOT consumed
# by the post wizard above.
# ========================================

@dp.message_created()
async def handle_video_message(event: MessageCreated):
    if not event.message.sender:
        return

    user_id, user_name = get_user_info(event)

    # Already handled by post wizard
    if user_id in post_states:
        return

    # Subscription gate
    if not await is_subscribed(user_id):
        await event.message.answer(
            text="❌ Для использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()],
        )
        return

    # Collect attachments (direct or forwarded)
    attachments = event.message.body.attachments or []
    if not attachments and event.message.link and event.message.link.message:
        logger.info("🔗 Пересланное сообщение")
        attachments = event.message.link.message.attachments or []

    if not attachments:
        await event.message.answer("📹 Отправь мне видео для конвертации в кружочек!")
        return

    # Find video attachment
    for attachment in attachments:
        if getattr(attachment, "type", None) != "video":
            continue

        logger.info(f"📹 Video from {user_name} ({user_id})")
        await event.message.answer("⏳ Обрабатываю видео...")

        file_id     = str(uuid.uuid4())
        input_path  = None
        output_path = None

        try:
            # 1. Download
            input_path = await download_video(
                attachment.payload.url, f"{file_id}_input.mp4"
            )
            await event.message.answer("✅ Скачано! Конвертирую...")

            # 2. Convert to circle
            output_path = f"videos/{file_id}_circle.mp4"
            convert_to_circle(input_path, output_path)
            await event.message.answer("✅ Готово! Загружаю...")

            # 3. Get upload URL
            upload_info = await bot.get_upload_url(type=UploadType.VIDEO)
            logger.info(f"📦 upload_info: {upload_info.__dict__}")

            # 4. Upload file
            await bot.upload_file(
                url=upload_info.url, path=output_path, type=UploadType.VIDEO
            )

            # 5. Build attachment from token
            token = upload_info.token
            if not token:
                raise Exception("Upload token не найден")

            from maxapi.types.attachments.upload import AttachmentUpload, AttachmentPayload

            circle_attachment = AttachmentUpload(
                type=UploadType.VIDEO,
                payload=AttachmentPayload(token=token),
            )

            # 6. Send result
            await event.message.answer(
                text="🎉 Вот твой кружочек!",
                attachments=[circle_attachment],
            )

        except Exception as e:
            logger.error(f"❌ Video error: {e}", exc_info=True)
            await event.message.answer(
                "❌ Ошибка обработки видео.\n\n"
                "Попробуйте:\n"
                "• Более короткое видео (до 60 сек)\n"
                "• Меньший размер (до 50 МБ)\n"
                "• Другой формат (MP4, MOV, AVI)"
            )
        finally:
            for path in [input_path, output_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            logger.info("🗑️ Temp files cleaned up")

        return  # stop after first video attachment

    await event.message.answer(
        "❌ Отправь видео-файл!\nФорматы: MP4, MOV, AVI"
    )

# ========================================
# MAIN
# ========================================

async def main():
    logger.info("=" * 60)
    logger.info("🤖 MAX BOT — Кружочек + Gated Posts")
    logger.info("=" * 60)
    logger.info(f"📢 Channel ID  : {CHANNEL_ID}")
    logger.info(f"🔗 Channel Link: {CHANNEL_LINK}")
    logger.info(f"👮 Admin IDs   : {ADMIN_IDS}")
    logger.info("=" * 60)

    try:
        # 1. DB
        await init_db_pool()

        # 2. Load saved posts
        await load_post_configs()
        logger.info(f"📋 Post configs loaded: {len(post_configs)}")

        # 3. Remove stale webhook
        await bot.delete_webhook()
        logger.info("✅ Webhook removed")

        # 4. Bot info
        me = await bot.get_me()
        logger.info(f"✅ Bot: @{me.username} (id={me.user_id}, name={me.first_name})")

        # 5. Populate DB if empty
        count = await get_subscriber_count()
        logger.info(f"📊 Subscribers in DB: {count}")
        if count == 0:
            added = await populate_initial_members()
            logger.info(f"✅ Initial population: {added} added")
        else:
            logger.info("ℹ️ DB already populated — skipping initial load")

        logger.info(f"📊 Total subscribers: {await get_subscriber_count()}")

        # 6. Background sync
        asyncio.create_task(sync_members_task())
        logger.info("✅ Background sync task started (every 6 hours)")

        logger.info("=" * 60)
        logger.info("🚀 BOT IS RUNNING")
        logger.info("=" * 60)

        # 7. Start pollingg
        await dp.start_polling(bot)

    except Exception as e:
        logger.error(f"❌ Startup error: {e}", exc_info=True)
        raise
    finally:
        await close_db_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)