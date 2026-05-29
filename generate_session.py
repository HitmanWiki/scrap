"""Generate Telethon session string for Heroku"""
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

async def main():
    api_id = int(os.getenv('MONITOR_API_ID'))
    api_hash = os.getenv('MONITOR_API_HASH')
    phone = os.getenv('MONITOR_PHONE')
    
    print(f"📱 Connecting as {phone}...")
    
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone)
    
    session_string = client.session.save()
    me = await client.get_me()
    
    print(f"\n{'='*60}")
    print(f"✅ Logged in as: @{me.username}" if me.username else f"✅ Logged in as: {me.first_name}")
    print(f"\n📝 COPY THIS TO HEROKU:")
    print(f'heroku config:set MONITOR_SESSION_STRING="{session_string}"')
    print(f"{'='*60}\n")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())