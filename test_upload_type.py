# test_upload_type.py
import asyncio
from maxapi.types import Attachment
from maxapi import Bot
import os
import inspect
BOT_TOKEN = os.getenv("BOT_TOKEN")




async def test():
    bot = Bot(BOT_TOKEN)
    
    
    # Check get_upload_url signature
    print("=== get_upload_url signature ===")
    sig = inspect.signature(bot.get_upload_url)
    print(sig)
    
    # Try to find the source
    try:
        from maxapi.methods.get_upload_url import GetUploadURL
        print("\n=== GetUploadURL source ===")
      
        source = inspect.getsource(GetUploadURL)
        print(source[:500])  # First 500 chars
    except Exception as e:
        print(f"Can't get source: {e}")
    
    # Check what's in the library files
    print("\n=== Looking for upload type enum ===")
    import maxapi
    lib_path = maxapi.__file__.replace("__init__.py", "")
    
    # Search for enum files

    for root, dirs, files in os.walk(lib_path):
        for file in files:
            if 'type' in file.lower() or 'enum' in file.lower():
                print(f"Found: {os.path.join(root, file)}")

asyncio.run(test())