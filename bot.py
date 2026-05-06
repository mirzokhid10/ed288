import asyncio
import logging
import os
import aiohttp
import subprocess
import uuid
import aiofiles
import json
from maxapi.enums.upload_type import UploadType


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
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://max.ru/")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ========================================
# SUBSCRIPTION CHECK (TEMP)
# ========================================
async def is_subscribed(user_id: int) -> bool:
    """
    TODO: Реализовать реальную проверку подписки
    """
    logger.warning("⚠️ Проверка подписки отключена (тестовый режим)")
    return True


# ========================================
# USER INFO
# ========================================
def get_user_info(event):
    """Универсальная функция получения информации о пользователе"""
    if hasattr(event, "user"):
        return event.user.user_id, event.user.name or "пользователь"

    if hasattr(event, "message") and hasattr(event.message, "sender"):
        sender = event.message.sender
        return sender.user_id, sender.first_name or "пользователь"

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
    """
    Конвертация видео в кружочек (круглое видео)
    """
    logger.info(f"🔄 Конвертирую в кружочек...")
    
    command = [
        "ffmpeg",
        "-i", input_path,
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480",
        "-c:a", "copy",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-t", "60",  # Максимум 60 секунд
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
        logger.error(f"❌ FFmpeg ошибка: {result.stderr}")
        raise Exception(f"Ошибка конвертации видео")
    
    file_size = os.path.getsize(output_path) / (1024 * 1024)  # MB
    logger.info(f"✅ Кружочек готов ({file_size:.2f} MB)")


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
        
        # InputMedia автоматически загружает файл и создаёт attachment
        # Возвращаем его напрямую для использования в send_message
        return media
        
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}", exc_info=True)
        raise


# ========================================
# HANDLERS
# ========================================

@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    """Обработчик нажатия кнопки 'Начать'"""
    user_id, user_name = get_user_info(event)
    logger.info(f"👤 Новый пользователь: {user_name} (ID: {user_id})")

    if not await is_subscribed(user_id):
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=(
                f"Привет, {user_name}! 👋\n\n"
                "❌ Для использования бота подпишись на канал:\n"
                f"👉 {CHANNEL_LINK}\n\n"
                "После подписки нажми /start"
            )
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
    """Обработчик команды /start"""
    user_id, user_name = get_user_info(event)
    logger.info(f"⚡ /start от {user_name} (ID: {user_id})")

    if not await is_subscribed(user_id):
        await event.message.answer(
            f"❌ Подпишись на канал:\n{CHANNEL_LINK}\n\nПотом нажми /start снова"
        )
        return

    await event.message.answer(
        "✅ Готово к работе!\n\n"
        "📹 Отправь мне видео 🎥"
    )


@dp.message_created()
async def handle_message(event: MessageCreated):
    """Обработчик всех сообщений с видео"""
    user_id, user_name = get_user_info(event)

    # 🔒 Проверка подписки
    if not await is_subscribed(user_id):
        await event.message.answer(
            f"❌ Подпишись на канал:\n{CHANNEL_LINK}\n\nПотом нажми /start"
        )
        return

    # Проверяем вложения
    attachments = event.message.body.attachments or []

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

                # 3. Создаём InputMedia и отправляем
                from maxapi.types import InputMedia
                media = InputMedia(path=output_path, type=UploadType.VIDEO)
                
                # 4. Отправляем (используем answer вместо send_message)
                await event.message.answer(
                    text="🎉 Вот твой кружочек!",
                    attachments=[media]
                )
                
                logger.info(f"✅ Успех! {user_name} (ID: {user_id})")

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

    logger.info("🔄 Запуск polling...")
    logger.info("✅ Готов к работе!")
    logger.info("=" * 60)
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Остановлен")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)