"""
Run once to authenticate and save the session:
    python auth.py

The session file `digest_user.session` will be created and reused by main.py.
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


async def main():
    async with TelegramClient("digest_user", API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Logged in as: {me.first_name} (@{me.username})")
        print("Session saved to digest_user.session — now run: uvicorn main:app")


asyncio.run(main())
