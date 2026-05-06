import asyncio
import logging
import os
import aiohttp
import subprocess
import uuid
import aiofiles

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
    Когда получите CHANNEL_ID от клиента
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
    
    Параметры:
    - Квадратное разрешение 480x480
    - Обрезка по центру до квадрата
    - Копирование аудио без пережатия
    """
    logger.info(f"🔄 Конвертирую в кружочек...")
    
    command = [
        "ffmpeg",
        "-i", input_path,
        
        # Видео фильтры:
        # 1. Обрезка до квадрата (по центру)
        # 2. Масштабирование до 480x480
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480",
        
        # Аудио: копируем без изменений
        "-c:a", "copy",
        
        # Видео кодек
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        
        # Ограничение по длительности (60 секунд максимум)
        "-t", "60",
        
        # Перезаписать выходной файл если существует
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
# UPLOAD VIDEO TO MAX
# ========================================
async def upload_video_to_max(filepath: str):
    logger.info(f"📤 Загружаю на MAX...")
    
    try:
        # Вариант 1: Попробуйте InputMedia
        try:
            from maxapi import InputMedia
            media = InputMedia(path=filepath)
        except ImportError:
            # Вариант 2: Просто передайте path как dict
            media = {"path": filepath}
        
        attachment = await bot.upload_media(media)
        logger.info(f"✅ Видео загружено")
        return attachment
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
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

    # 🔒 Проверка подписки ДО обработки
    if not await is_subscribed(user_id):
        await event.message.answer(
            f"❌ Подпишись на канал:\n{CHANNEL_LINK}\n\nПотом нажми /start"
        )
        return

    # Проверяем наличие вложений
    attachments = event.message.body.attachments or []

    if not attachments:
        await event.message.answer(
            "📹 Отправь мне видео для конвертации в кружочек!"
        )
        return

    # Ищем видео во вложениях
    for attachment in attachments:
        att_type = getattr(attachment, "type", None)

        if att_type == "video":
            logger.info(f"📹 Получено видео от {user_name} (ID: {user_id})")
            await event.message.answer("⏳ Обрабатываю видео, подожди немного...")

            file_id = str(uuid.uuid4())
            input_path = None
            output_path = None

            try:
                # 1. Получаем URL видео
                video_url = attachment.payload.url

                # 2. Скачиваем видео
                input_path = await download_video(
                    video_url, 
                    f"{file_id}_input.mp4"
                )
                
                await event.message.answer("✅ Скачано! Конвертирую в кружочек...")

                # 3. Конвертируем в кружочек
                output_path = f"videos/{file_id}_circle.mp4"
                convert_to_circle(input_path, output_path)
                
                await event.message.answer("✅ Готово! Загружаю на MAX...")

                # 4. Загружаем на MAX и получаем attachment
                circle_attachment = await upload_video_to_max(output_path)
                
                # 5. Отправляем пользователю
                await event.message.answer(
                    "🎉 Вот твой кружочек!",
                    attachments=[circle_attachment]
                )
                
                logger.info(f"✅ Кружочек отправлен: {user_name} (ID: {user_id})")

            except Exception as e:
                logger.error(f"❌ Ошибка обработки: {e}", exc_info=True)
                await event.message.answer(
                    "❌ Произошла ошибка при обработке видео.\n\n"
                    "Возможные причины:\n"
                    "• Слишком длинное видео (макс 60 сек)\n"
                    "• Слишком большой размер (макс 50 МБ)\n"
                    "• Неподдерживаемый формат\n\n"
                    "Попробуйте другое видео!"
                )

            finally:
                # 6. Очищаем временные файлы
                try:
                    if input_path and os.path.exists(input_path):
                        os.remove(input_path)
                        logger.info(f"🗑️ Удален: {input_path}")
                    if output_path and os.path.exists(output_path):
                        os.remove(output_path)
                        logger.info(f"🗑️ Удален: {output_path}")
                except Exception as cleanup_err:
                    logger.warning(f"⚠️ Не удалось удалить файлы: {cleanup_err}")

            return  # Обработали видео, выходим

    # Если не нашли видео
    await event.message.answer(
        "❌ Отправь именно видео-файл!\n"
        "Поддерживаемые форматы: MP4, MOV, AVI"
    )


# ========================================
# START BOT
# ========================================
async def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("🤖 БОТ 'КРУЖОЧЕК ДЛЯ ВИДЕО' ЗАПУСКАЕТСЯ")
    logger.info("=" * 60)

    # Удаляем webhook если был установлен
    try:
        await bot.delete_webhook()
        logger.info("✅ Webhook удален")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить webhook: {e}")

    # Получаем информацию о боте
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот авторизован: @{me.username}")
        logger.info(f"   ID: {me.user_id}")
        logger.info(f"   Имя: {me.first_name}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения информации о боте: {e}")

    # Запускаем polling
    logger.info("🔄 Запуск polling...")
    logger.info("✅ Бот готов принимать видео!")
    logger.info("=" * 60)
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n" + "=" * 60)
        logger.info("👋 Бот остановлен пользователем (Ctrl+C)")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)