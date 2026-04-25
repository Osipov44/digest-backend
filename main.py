import asyncio
import base64
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

SESSION_FILE = "digest_user.session"


def _restore_session() -> None:
    """Decode SESSION_BASE64 env var → digest_user.session file.

    Runs at startup so Railway (which has no persistent filesystem)
    can use a session exported from a local machine.
    Skipped silently when the env var is absent (local dev with real file).
    """
    encoded = os.environ.get("SESSION_BASE64", "").strip()
    if not encoded:
        return
    session_path = Path(SESSION_FILE)
    if session_path.exists():
        return  # already present (local dev)
    session_bytes = base64.b64decode(encoded)
    session_path.write_bytes(session_bytes)
    print(f"[startup] Session restored from SESSION_BASE64 ({len(session_bytes):,} bytes)")


client: TelegramClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    _restore_session()
    # Uses the user session created by auth.py (not bot token).
    # Bot tokens cannot call GetHistoryRequest for channel history.
    client = TelegramClient(SESSION_FILE.removesuffix(".session"), API_ID, API_HASH)
    await client.start()
    yield
    await client.disconnect()


app = FastAPI(title="Digest API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _media_type(message) -> str | None:
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        return "document"
    return None


def _utc_iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def _fetch_channel(username: str, limit: int) -> list[dict]:
    username = username.lstrip("@")
    try:
        entity = await client.get_entity(f"@{username}")
    except (UsernameNotOccupiedError, UsernameInvalidError):
        raise HTTPException(status_code=404, detail=f"Channel @{username} not found")
    except ChannelPrivateError:
        raise HTTPException(status_code=403, detail=f"Channel @{username} is private")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram error for @{username}: {exc}")

    channel_name = getattr(entity, "title", username)

    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    posts: list[dict] = []
    # Iterate newest→oldest (default); stop as soon as we cross the 24h boundary.
    # offset_date is intentionally omitted — it shifts the start point backward,
    # which is the opposite of what we need here.
    async for msg in client.iter_messages(entity, limit=200):
        if msg.date < since:
            break
        if not msg.text and not msg.media:
            continue
        posts.append(
            {
                "id": msg.id,
                "channel_name": channel_name,
                "channel_username": username,
                "text": msg.text or "",
                "date": _utc_iso(msg.date),
                "views": msg.views or 0,
                "media_type": _media_type(msg),
            }
        )

    return posts[:limit]


@app.get("/posts")
async def get_posts(
    channels: str = Query(..., description="Comma-separated channel usernames, e.g. @rbc_news,@bbcrussian"),
    limit: int = Query(10, ge=1, le=50, description="Posts per channel"),
):
    usernames = [c.strip() for c in channels.split(",") if c.strip()]
    if not usernames:
        raise HTTPException(status_code=422, detail="No channels provided")

    results = await asyncio.gather(
        *[_fetch_channel(u, limit) for u in usernames],
        return_exceptions=True,
    )

    posts: list[dict] = []
    errors: list[str] = []

    for username, result in zip(usernames, results):
        if isinstance(result, HTTPException):
            errors.append(f"{username}: {result.detail}")
        elif isinstance(result, Exception):
            errors.append(f"{username}: {result}")
        else:
            posts.extend(result)

    posts.sort(key=lambda p: p["date"], reverse=True)

    return {"posts": posts, "errors": errors}


@app.get("/health")
async def health():
    return {"status": "ok", "connected": client.is_connected() if client else False}
