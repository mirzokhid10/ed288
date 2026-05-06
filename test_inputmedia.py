import asyncio
from maxapi import Bot
from dotenv import load_dotenv
import os

load_dotenv()

async def test():
    # Попробуем найти правильный способ создания InputMedia
    try:
        from maxapi import InputMedia
        print("✅ InputMedia импортирован из maxapi")
        print(f"InputMedia: {InputMedia}")
        
        # Посмотрим сигнатуру
        import inspect
        sig = inspect.signature(InputMedia.__init__)
        print(f"Сигнатура __init__: {sig}")
        
        # Попробуем создать
        media = InputMedia(path="test.mp4", type="video")
        print(f"✅ Создан: {media}")
        print(f"Атрибуты: {dir(media)}")
        
    except ImportError as e:
        print(f"❌ InputMedia не найден: {e}")
        
        # Ищем альтернативы
        print("\n=== Ищем в maxapi.types ===")
        try:
            from maxapi.types import InputMedia
            print("✅ Найден в maxapi.types")
        except:
            print("❌ Нет в maxapi.types")
        
        print("\n=== Ищем в maxapi.utils ===")
        try:
            from maxapi.utils.message import InputMedia
            print("✅ Найден в maxapi.utils.message")
        except:
            print("❌ Нет в maxapi.utils.message")

asyncio.run(test())