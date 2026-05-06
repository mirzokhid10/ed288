import asyncio
import aiohttp
import json
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def test_upload():
    """Тестируем загрузку по документации MAX"""
    
    # Шаг 1: Получаем URL для загрузки
    async with aiohttp.ClientSession() as session:
        # POST /uploads
        async with session.post(
            "https://platform-api.max.ru/uploads",
            headers={"Authorization": BOT_TOKEN},
            json={"type": "video"}
        ) as resp:
            upload_data = await resp.json()
            print("✅ Upload URL получен:")
            print(json.dumps(upload_data, indent=2, ensure_ascii=False))
            
            upload_url = upload_data.get("url")
            
        # Шаг 2: Загружаем файл
        test_video = "videos/test.mp4"  # Укажите путь к тестовому видео
        
        if os.path.exists(test_video):
            with open(test_video, "rb") as f:
                video_data = f.read()
            
            async with session.post(
                upload_url,
                data=video_data,
                headers={"Content-Type": "video/mp4"}
            ) as resp:
                result = await resp.text()
                print("\n✅ Видео загружено:")
                print(result)
                
                result_json = json.loads(result)
                token = result_json.get("token")
                print(f"\n🎯 TOKEN: {token}")
                
        else:
            print(f"❌ Файл {test_video} не найден")

asyncio.run(test_upload())