import asyncio
import logging
import os
import aiohttp
import subprocess
import uuid
import aiofiles
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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ========================================
# IN-MEMORY SUBSCRIPTION TRACKING
# ========================================
subscribed_users = set()

# ========================================
# SUBSCRIPTION CHECK (IMPROVED)
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
    """Проверка подписки пользователя на канал через API с пагинацией"""
    
    # Сначала проверяем in-memory кэш
    if user_id in subscribed_users:
        logger.info(f"✅ User {user_id} found in cache")
        return True
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": BOT_TOKEN}
            
            # Пробуем с пагинацией
            offset = 0
            limit = 100
            total_checked = 0
            
            while True:
                url = f"https://platform-api.max.ru/chats/{CHANNEL_ID}/members?offset={offset}&limit={limit}"
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ Members API error: {resp.status}")
                        # В случае ошибки API - разрешаем доступ (fail-open)
                        return True
                    
                    data = await resp.json()
                    members = data.get("members", [])
                    
                    if not members:
                        break
                    
                    total_checked += len(members)
                    logger.info(f"📋 Fetched {len(members)} members (offset: {offset}, total: {total_checked})")
                    
                    # Проверяем, есть ли пользователь в этой партии
                    for member in members:
                        member_id = member.get("user_id")
                        if member_id == user_id:
                            logger.info(f"✅ User {user_id} IS subscribed (found at offset {offset})")
                            subscribed_users.add(user_id)  # Добавляем в кэш
                            return True
                    
                    # Если получили меньше limit, значит это последняя страница
                    if len(members) < limit:
                        logger.info(f"📄 Last page reached (got {len(members)} < {limit})")
                        break
                    
                    offset += limit
                    
                    # Защита от бесконечного цикла
                    if offset > 10000:
                        logger.warning(f"⚠️ Stopped pagination at offset {offset}")
                        break
            
            logger.info(f"❌ User {user_id} is NOT subscribed (checked {total_checked} members)")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка проверки подписки: {e}", exc_info=True)
        # В случае ошибки - разрешаем доступ (fail-open)
        return True

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
            "scale=480:480,"
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
        "-aspect", "1:1",           # ← принудительно квадратное
        "-movflags", "+faststart",  # ← оптимизация для стриминга
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
# UPLOAD VIDEO TO MAX (ИСПРАВЛЕННАЯ ВЕРСИЯ)
# ========================================
async def upload_video_to_max(filepath: str):
    """
    Загрузка видео на серверы MAX (правильный способ)
    """
    logger.info(f"📤 Загружаю на MAX...")
    
    try:
        # Используем правильный метод библиотеки
        from maxapi.types import InputMedia
        
        # Создаём InputMedia объект
        media = InputMedia(path=filepath, type=UploadType.VIDEO)
        
        logger.info(f"✅ InputMedia создан")

        return media
        
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}", exc_info=True)
        raise


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
# НОВЫЙ ОБРАБОТЧИК: ОТСЛЕЖИВАНИЕ ПОДПИСОК
# ========================================
@dp.user_added()
async def user_added_handler(event):
    """Обработчик события добавления пользователя в канал"""
    try:
        # Проверяем, что это наш канал
        if hasattr(event, 'chat_id') and event.chat_id == CHANNEL_ID:
            if hasattr(event, 'user_id'):
                user_id = event.user_id
                subscribed_users.add(user_id)
                logger.info(f"✅ User {user_id} подписался на канал (event: user_added)")
            elif hasattr(event, 'user') and hasattr(event.user, 'user_id'):
                user_id = event.user.user_id
                subscribed_users.add(user_id)
                logger.info(f"✅ User {user_id} подписался на канал (event: user_added)")
    except Exception as e:
        logger.error(f"❌ Ошибка в user_added_handler: {e}", exc_info=True)


@dp.user_removed()
async def user_removed_handler(event):
    """Обработчик события удаления пользователя из канала"""
    try:
        # Проверяем, что это наш канал
        if hasattr(event, 'chat_id') and event.chat_id == CHANNEL_ID:
            if hasattr(event, 'user_id'):
                user_id = event.user_id
                subscribed_users.discard(user_id)
                logger.info(f"❌ User {user_id} отписался от канала (event: user_removed)")
            elif hasattr(event, 'user') and hasattr(event.user, 'user_id'):
                user_id = event.user.user_id
                subscribed_users.discard(user_id)
                logger.info(f"❌ User {user_id} отписался от канала (event: user_removed)")
    except Exception as e:
        logger.error(f"❌ Ошибка в user_removed_handler: {e}", exc_info=True)

    
@dp.message_callback()
async def handle_callback(event: MessageCallback):
    user_id = event.callback.user.user_id
    user_name = getattr(event.callback.user, 'first_name', None) or "пользователь"
    chat_id = event.message.recipient.chat_id

    if event.callback.payload == "check_subscription":
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
                text="❌ Ты ещё не подписан. Подпишись и нажми кнопку снова!",
                attachments=[get_subscribe_keyboard()]
            )


@dp.message_created()
async def handle_message(event: MessageCreated):
    """Обработчик всех сообщений с видео"""
    # Игнорируем сообщения из каналов (нет sender)
    if not event.message.sender:
        return
    
    user_id, user_name = get_user_info(event)

    # 🔒 Проверка подписки
    if not await is_subscribed(user_id):
        await event.message.answer(
            text="❌ Для использования бота подпишись на канал 👇",
            attachments=[get_subscribe_keyboard()]
        )
        return

    # Проверяем вложения - сначала в body, потом в forwarded message
    attachments = event.message.body.attachments or []
    
    # Если это пересланное сообщение, берём вложения оттуда
    if not attachments and event.message.link and event.message.link.message:
        logger.info("🔗 Обнаружено пересланное сообщение")
        attachments = event.message.link.message.attachments or []

    if not attachments:
        await event.message.answer(
            "📹 Отправь мне видео для конвертации в кружочек!"
        )
        return
    
    for i, att in enumerate(attachments):
        logger.info(f"\n📎 Вложение #{i + 1}:")
        logger.info(f"   type: {att.type}")
        
        if hasattr(att, 'payload'):
            logger.info(f"   📦 PAYLOAD:")
            if hasattr(att.payload, '__dict__'):
                for key, value in att.payload.__dict__.items():
                    logger.info(f"      {key}: {value}")
            
            # Проверяем специальные поля
            if hasattr(att, 'width'):
                logger.info(f"   ⚠️ width: {att.width}")
            if hasattr(att, 'height'):
                logger.info(f"   ⚠️ height: {att.height}")
            if hasattr(att, 'duration'):
                logger.info(f"   ⚠️ duration: {att.duration}")
                
    logger.info("=" * 60)

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
    logger.info("🤖 БОТ 'КРУЖОЧЕК ДЛЯ ВИДЕО'")
    logger.info("=" * 60)

    try:
        await bot.delete_webhook()
        logger.info("✅ Webhook удален")
    except:
        pass

    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот: @{me.username}")
        logger.info(f"   ID: {me.user_id}")
        logger.info(f"   Имя: {me.first_name}")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")


    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": BOT_TOKEN}
        async with session.get("https://platform-api.max.ru/chats", headers=headers) as resp:
            data = await resp.json()
            logger.info(f"📢 ALL CHATS: {json.dumps(data, ensure_ascii=False, indent=2)}")
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Остановлен")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)