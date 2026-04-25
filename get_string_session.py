"""
Converts existing digest_user.session file → Telethon StringSession.
StringSession is ~350 chars and fits in any env variable.

Run:
    python get_string_session.py
Then copy the printed string into Railway as SESSION_STRING.
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


async def main():
    # Step 1: open existing file session to read its credentials
    file_client = TelegramClient("digest_user", API_ID, API_HASH)
    await file_client.connect()

    # Step 2: copy dc / server / auth_key into a fresh StringSession
    str_session = StringSession()
    str_session.set_dc(
        file_client.session.dc_id,
        file_client.session.server_address,
        file_client.session.port,
    )
    str_session.auth_key = file_client.session.auth_key

    session_string = str_session.save()
    me = await file_client.get_me()
    await file_client.disconnect()

    print(f"\nLogged in as: {me.first_name} (@{me.username})\n")
    print("SESSION_STRING:")
    print(session_string)
    print(f"\nLength: {len(session_string)} chars")
    print("\nAdd this to Railway as SESSION_STRING environment variable.")


asyncio.run(main())
