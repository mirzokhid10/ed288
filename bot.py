import asyncio
import logging
import os
import aiohttp
import subprocess
import uuid
import aiofiles
import aiomysql
from datetime import datetime
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

# MySQL Configuration
MYSQL_CONFIG = {
    'host': os.getenv("MYSQL_HOST", "localhost"),
    'port': int(os.getenv("MYSQL_PORT", 3306)),
    'user': os.getenv("MYSQL_USER", "root"),
    'password': os.getenv("MYSQL_PASSWORD", ""),
    'db': os.getenv("MYSQL_DATABASE", "max_bot_db"),
    'charset': 'utf8mb4',
    'autocommit': True
}

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Global connection pool
db_pool = None

# ========================================
# DATABASE FUNCTIONS
# ========================================

async def init_db_pool():
    """Инициализация пула соединений с MySQL"""
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
            maxsize=10
        )
        logger.info("✅ MySQL connection pool created")
        
        # Verify table exists
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SHOW TABLES LIKE 'subscribers'")
                result = await cursor.fetchone()
                if result:
                    logger.info("✅ Table 'subscribers' exists")
                else:
                    logger.error("❌ Table 'subscribers' NOT found! Run SQL setup first!")
                    raise Exception("Database table missing")
                    
    except Exception as e:
        logger.error(f"❌ Database connection error: {e}", exc_info=True)
        raise


async def close_db_pool():
    """Закрытие пула соединений"""
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logger.info("✅ MySQL connection pool closed")


async def add_subscriber_to_db(user_id: int, first_name: str = None):
    """Добавить подписчика в базу данных"""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT IGNORE INTO subscribers (user_id, first_name) VALUES (%s, %s)",
                    (user_id, first_name)
                )
                if cursor.rowcount > 0:
                    logger.info(f"➕ User {user_id} added to database")
                    return True
                else:
                    logger.info(f"ℹ️ User {user_id} already in database")
                    return False
    except Exception as e:
        logger.error(f"❌ Error adding subscriber {user_id}: {e}")
        return False


async def remove_subscriber_from_db(user_id: int):
    """Удалить подписчика из базы данных"""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM subscribers WHERE user_id = %s",
                    (user_id,)
                )
                if cursor.rowcount > 0:
                    logger.info(f"➖ User {user_id} removed from database")
                    return True
                else:
                    logger.info(f"ℹ️ User {user_id} not found in database")
                    return False
    except Exception as e:
        logger.error(f"❌ Error removing subscriber {user_id}: {e}")
        return False


async def is_subscriber_in_db(user_id: int) -> bool:
    """Проверить, есть ли подписчик в базе данных"""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT user_id FROM subscribers WHERE user_id = %s",
                    (user_id,)
                )
                result = await cursor.fetchone()
                return result is not None
    except Exception as e:
        logger.error(f"❌ Error checking subscriber {user_id}: {e}")
        # В случае ошибки БД - разрешаем доступ (fail-open)
        return True


async def get_all_subscribers_from_db():
    """Получить всех подписчиков из базы данных"""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT user_id FROM subscribers")
                results = await cursor.fetchall()
                return set(row[0] for row in results)
    except Exception as e:
        logger.error(f"❌ Error fetching subscribers: {e}")
        return set()


async def get_subscriber_count():
    """Получить количество подписчиков в базе"""
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT COUNT(*) FROM subscribers")
                result = await cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logger.error(f"❌ Error counting subscribers: {e}")
        return 0

# ========================================
# INITIAL POPULATION
# ========================================

async def populate_initial_members():
    """
    Загрузка существующих подписчиков канала в базу данных
    Выполняется один раз при старте бота
    """
    logger.info("🔄 Starting initial member population...")
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": BOT_TOKEN}
            
            # Пробуем оба варианта ID
            channel_ids = [CHANNEL_ID, -abs(CHANNEL_ID)]
            
            for channel_id in channel_ids:
                offset = 0
                limit = 100
                total_added = 0
                
                while offset < 50000:  # Safety limit
                    url = f"https://platform-api.max.ru/chats/{channel_id}/members?offset={offset}&limit={limit}"
                    
                    try:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status != 200:
                                logger.warning(f"⚠️ API status {resp.status} for channel_id={channel_id}")
                                break
                            
                            data = await resp.json()
                            members = data.get("members", [])
                            
                            if not members:
                                logger.info(f"📄 No more members at offset {offset}")
                                break
                            
                            # Добавляем в базу
                            for member in members:
                                user_id = member.get("user_id")
                                first_name = member.get("first_name", "Unknown")
                                
                                if user_id:
                                    if await add_subscriber_to_db(user_id, first_name):
                                        total_added += 1
                            
                            logger.info(f"📋 Processed {len(members)} members (offset: {offset}, total added: {total_added})")
                            
                            # Если получили меньше limit, это последняя страница
                            if len(members) < limit:
                                logger.info(f"✅ Initial population complete: {total_added} members added")
                                return total_added
                            
                            offset += limit
                            
                            # Небольшая задержка между запросами
                            await asyncio.sleep(0.5)
                            
                    except Exception as e:
                        logger.error(f"❌ Error fetching members at offset {offset}: {e}")
                        break
                
                # Если успешно получили данные с этим channel_id, не пробуем другой
                if total_added > 0:
                    return total_added
        
        logger.warning("⚠️ No members found with either channel_id")
        return 0
        
    except Exception as e:
        logger.error(f"❌ Error in initial population: {e}", exc_info=True)
        return 0

# ========================================
# BACKGROUND SYNC TASK
# ========================================

async def sync_members_task():
    """
    Фоновая задача: синхронизация базы данных с API каждые 6 часов
    Это подстраховка на случай пропущенных событий
    """
    while True:
        try:
            await asyncio.sleep(6 * 60 * 60)  # 6 часов
            
            logger.info("🔄 Starting periodic member sync...")
            
            # Получаем текущих подписчиков из API
            api_members = set()
            
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": BOT_TOKEN}
                channel_ids = [CHANNEL_ID, -abs(CHANNEL_ID)]
                
                for channel_id in channel_ids:
                    offset = 0
                    limit = 100
                    
                    while offset < 50000:
                        url = f"https://platform-api.max.ru/chats/{channel_id}/members?offset={offset}&limit={limit}"
                        
                        try:
                            async with session.get(url, headers=headers) as resp:
                                if resp.status != 200:
                                    break
                                
                                data = await resp.json()
                                members = data.get("members", [])
                                
                                if not members:
                                    break
                                
                                for member in members:
                                    user_id = member.get("user_id")
                                    if user_id:
                                        api_members.add(user_id)
                                
                                if len(members) < limit:
                                    break
                                
                                offset += limit
                                await asyncio.sleep(0.5)
                                
                        except Exception as e:
                            logger.error(f"❌ Sync error at offset {offset}: {e}")
                            break
                    
                    if len(api_members) > 0:
                        break
            
            # Получаем подписчиков из базы
            db_members = await get_all_subscribers_from_db()
            
            # Находим разницу
            to_add = api_members - db_members  # В API есть, в БД нет
            to_remove = db_members - api_members  # В БД есть, в API нет
            
            # Синхронизируем
            added = 0
            removed = 0
            
            for user_id in to_add:
                if await add_subscriber_to_db(user_id):
                    added += 1
            
            for user_id in to_remove:
                if await remove_subscriber_from_db(user_id):
                    removed += 1
            
            logger.info(f"✅ Sync complete: +{added} added, -{removed} removed (API: {len(api_members)}, DB: {len(db_members)})")
            
        except Exception as e:
            logger.error(f"❌ Error in sync task: {e}", exc_info=True)
            # Продолжаем работу даже при ошибке


# ========================================
# SUBSCRIPTION CHECK
# ========================================

def get_subscribe_keyboard():
    return AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(
            buttons=[
                [LinkButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
                [CallbackButton(text="✅ Готово", payload="check_subscription")]
            ]
        )
    )

async def is_subscribed(user_id: int) -> bool:
    """
    Проверка подписки через БАЗУ ДАННЫХ (не API!)
    Быстро, надёжно, масштабируемо
    """
    return await is_subscriber_in_db(user_id)

# ========================================
# USER INFO
# ========================================

def get_user_info(event):
    """Универсальная функция получения информации о пользователе"""
    if hasattr(event, "user") and event.user:
        name = getattr(event.user, 'first_name', None) or getattr(event.user, 'name', None) or "пользователь"
        return event.user.user_id, name

    if hasattr(event, "message") and hasattr(event.message, "sender"):
        sender = event.message.sender
        if sender:
            name = getattr(sender, 'first_name', None) or getattr(sender, 'name', None) or "пользователь"
            return sender.user_id, name

    raise Exception("Не удалось получить информацию о пользователе")


# ========================================
# DOWNLOAD VIDEO
# ========================================
async def download_video(url: str, filename: str) -> str:
    """Скачивание видео по URL"""
    os.makedirs("videos", exist_ok=True)
    filepath = f"videos/{filename}"

    logger.info(f"📥 Скачиваю видео...")
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Ошибка скачивания: HTTP {resp.status}")

            async with aiofiles.open(filepath, "wb") as f:
                await f.write(await resp.read())

    file_size = os.path.getsize(filepath) / (1024 * 1024)  # MB
    logger.info(f"✅ Видео скачано ({file_size:.2f} MB)")
    return filepath


# ========================================
# CONVERT TO CIRCLE
# ========================================

def convert_to_circle(input_path: str, output_path: str):
    logger.info(f"🔄 Создаю кружочек...")

    command = [
        "ffmpeg",
        "-i", input_path,
        "-i", "bg/space_bg.png",
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
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-aspect", "1:1",
        "-movflags", "+faststart",
        "-t", "60",
        "-y",
        output_path
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        logger.error(f"❌ FFmpeg stderr:\n{result.stderr}")
        raise Exception("Ошибка конвертации")

    logger.info(f"✅ Кружочек готов")

# ========================================
# HANDLERS
# ========================================

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    user_id, user_name = get_user_info(event)
    logger.info(f"👤 Новый пользователь: {user_name} (ID: {user_id})")

    if not await is_subscribed(user_id):
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=f"Привет, {user_name}! 👋\n\nДля использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()]
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
        )
    )


@dp.message_created(Command("start"))
async def start_handler(event: MessageCreated):
    user_id, user_name = get_user_info(event)
    logger.info(f"⚡ /start от {user_name} (ID: {user_id})")

    if not await is_subscribed(user_id):
        await event.message.answer(
            text="❌ Для использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()]
        )
        return

    await event.message.answer(
        "✅ Готово к работе!\n\n"
        "📹 Отправь мне видео 🎥"
    )


# ========================================
# CHANNEL EVENT HANDLERS
# ========================================

@dp.user_added()
async def user_added_handler(event):
    """Обработчик события добавления пользователя в канал"""
    try:
        # Проверяем, что это наш канал (с учётом отрицательного ID)
        if hasattr(event, 'chat_id') and (event.chat_id == CHANNEL_ID or event.chat_id == -abs(CHANNEL_ID)):
            if hasattr(event, 'user') and hasattr(event.user, 'user_id'):
                user_id = event.user.user_id
                first_name = getattr(event.user, 'first_name', None)
                
                await add_subscriber_to_db(user_id, first_name)
                logger.info(f"✅ User {user_id} ({first_name}) joined channel → added to DB")
                
    except Exception as e:
        logger.error(f"❌ Error in user_added_handler: {e}", exc_info=True)


@dp.user_removed()
async def user_removed_handler(event):
    """Обработчик события удаления пользователя из канала"""
    try:
        # Проверяем, что это наш канал (с учётом отрицательного ID)
        if hasattr(event, 'chat_id') and (event.chat_id == CHANNEL_ID or event.chat_id == -abs(CHANNEL_ID)):
            if hasattr(event, 'user') and hasattr(event.user, 'user_id'):
                user_id = event.user.user_id
                
                await remove_subscriber_from_db(user_id)
                logger.info(f"❌ User {user_id} left channel → removed from DB")
                
    except Exception as e:
        logger.error(f"❌ Error in user_removed_handler: {e}", exc_info=True)


@dp.message_callback()
async def handle_callback(event: MessageCallback):
    user_id = event.callback.user.user_id
    user_name = getattr(event.callback.user, 'first_name', None) or "пользователь"
    chat_id = event.message.recipient.chat_id

    if event.callback.payload == "check_subscription":
        logger.info(f"🔄 User {user_id} ({user_name}) checking subscription...")
        
        # Даём немного времени на синхронизацию (если пользователь только что подписался)
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
                )
            )
        else:
            await event.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Ты ещё не подписан. Подпишись и нажми кнопку снова!\n\n"
                    "💡 Убедись, что:\n"
                    "1. Ты нажал кнопку 'Подписаться'\n"
                    "2. Подтвердил подписку в канале\n"
                    "3. Не отписался сразу после подписки"
                ),
                attachments=[get_subscribe_keyboard()]
            )


@dp.message_created()
async def handle_message(event: MessageCreated):
    """Обработчик всех сообщений с видео"""
    if not event.message.sender:
        return
    
    user_id, user_name = get_user_info(event)

    # 🔒 Проверка подписки через БД (быстро!)
    if not await is_subscribed(user_id):
        await event.message.answer(
            text="❌ Для использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()]
        )
        return

    attachments = event.message.body.attachments or []
    
    if not attachments and event.message.link and event.message.link.message:
        logger.info("🔗 Обнаружено пересланное сообщение")
        attachments = event.message.link.message.attachments or []

    if not attachments:
        await event.message.answer(
            "📹 Отправь мне видео для конвертации в кружочек!"
        )
        return

    # Ищем видео
    for attachment in attachments:
        att_type = getattr(attachment, "type", None)

        if att_type == "video":
            logger.info(f"📹 Видео от {user_name} (ID: {user_id})")
            await event.message.answer("⏳ Обрабатываю видео...")

            file_id = str(uuid.uuid4())
            input_path = None
            output_path = None

            try:
                # 1. Скачиваем
                video_url = attachment.payload.url
                input_path = await download_video(video_url, f"{file_id}_input.mp4")
                
                await event.message.answer("✅ Скачано! Конвертирую...")

                # 2. Конвертируем
                output_path = f"videos/{file_id}_circle.mp4"
                convert_to_circle(input_path, output_path)
                
                await event.message.answer("✅ Готово! Загружаю...")

                # 3. Получаем URL для загрузки
                upload_info = await bot.get_upload_url(type=UploadType.VIDEO)
                logger.info(f"📦 upload_info: {upload_info.__dict__}")

                # 4. Загружаем файл
                await bot.upload_file(url=upload_info.url, path=output_path, type=UploadType.VIDEO)
                logger.info(f"✅ Файл загружен")

                # 5. Создаём attachment
                token = upload_info.token
                logger.info(f"🔑 Token: {token}")
                
                if not token:
                    raise Exception("Токен не найден")
                
                # 6. Отправляем
                from maxapi.types.attachments.upload import AttachmentUpload, AttachmentPayload

                circle_attachment = AttachmentUpload(
                    type=UploadType.VIDEO,
                    payload=AttachmentPayload(token=token)
                )

                await event.message.answer(
                    text="🎉 Вот твой кружочек!",
                    attachments=[circle_attachment]
                )

            except Exception as e:
                logger.error(f"❌ Ошибка: {e}", exc_info=True)
                await event.message.answer(
                    "❌ Ошибка обработки видео.\n\n"
                    "Попробуйте:\n"
                    "• Более короткое видео (до 60 сек)\n"
                    "• Меньший размер (до 50 МБ)\n"
                    "• Другой формат (MP4, MOV, AVI)"
                )

            finally:
                # Очистка
                try:
                    if input_path and os.path.exists(input_path):
                        os.remove(input_path)
                    if output_path and os.path.exists(output_path):
                        os.remove(output_path)
                    logger.info(f"🗑️ Файлы удалены")
                except:
                    pass

            return

    await event.message.answer(
        "❌ Отправь видео-файл!\n"
        "Форматы: MP4, MOV, AVI"
    )

# ========================================
# START BOT
# ========================================
async def main():
    """Главная функция"""
    logger.info("=" * 60)
    logger.info("🤖 БОТ 'КРУЖОЧЕК ДЛЯ ВИДЕО' v2.0")
    logger.info("=" * 60)
    logger.info(f"📢 Channel ID: {CHANNEL_ID}")
    logger.info(f"🔗 Channel Link: {CHANNEL_LINK}")
    logger.info("=" * 60)

    try:
        # 1. Инициализируем БД
        await init_db_pool()
        
        # 2. Удаляем webhook
        await bot.delete_webhook()
        logger.info("✅ Webhook удален")
        
        # 3. Проверяем бота
        me = await bot.get_me()
        logger.info(f"✅ Бот: @{me.username}")
        logger.info(f"   ID: {me.user_id}")
        logger.info(f"   Имя: {me.first_name}")
        
        # 4. Загружаем начальных подписчиков
        count_before = await get_subscriber_count()
        logger.info(f"📊 Subscribers in DB before population: {count_before}")
        
        if count_before == 0:
            logger.info("🔄 Database is empty, populating initial members...")
            added = await populate_initial_members()
            logger.info(f"✅ Initial population complete: {added} members added")
        else:
            logger.info(f"ℹ️ Database already has {count_before} subscribers, skipping population")
        
        count_after = await get_subscriber_count()
        logger.info(f"📊 Total subscribers in DB: {count_after}")
        
        # 5. Запускаем фоновую синхронизацию
        asyncio.create_task(sync_members_task())
        logger.info("✅ Background sync task started (runs every 6 hours)")
        
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН И ГОТОВ К РАБОТЕ!")
        logger.info("=" * 60)
        
        # 6. Запускаем polling
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}", exc_info=True)
        raise
    finally:
        # Закрываем БД при остановке
        await close_db_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)