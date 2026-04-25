import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

# In-memory cache: channel photos & entity objects
_photo_cache: dict[str, bytes]       = {}
_entity_cache: dict[str, object]     = {}


def _make_session():
    session_string = os.environ.get("SESSION_STRING", "").strip()
    if session_string:
        print("[startup] Using StringSession from SESSION_STRING env var")
        return StringSession(session_string)
    print("[startup] SESSION_STRING not set — falling back to digest_user.session file")
    return "digest_user"


client: TelegramClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = TelegramClient(_make_session(), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telegram session is not authorized. "
            "Set SESSION_STRING env var or run auth.py locally first."
        )
    me = await client.get_me()
    print(f"[startup] Connected as {me.first_name} (@{me.username})")
    yield
    await client.disconnect()


app = FastAPI(title="Digest API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _media_type(message) -> str | None:
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        mime = getattr(message.media.document, "mime_type", "") or ""
        if mime.startswith("image/"):
            return "photo"
    return None


def _utc_iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def _get_entity(username: str):
    if username not in _entity_cache:
        _entity_cache[username] = await client.get_entity(f"@{username}")
    return _entity_cache[username]


# ── /posts ────────────────────────────────────────────────────────────────────

async def _fetch_channel(username: str, limit: int) -> list[dict]:
    username = username.lstrip("@")
    try:
        entity = await _get_entity(username)
    except (UsernameNotOccupiedError, UsernameInvalidError):
        raise HTTPException(status_code=404, detail=f"Channel @{username} not found")
    except ChannelPrivateError:
        raise HTTPException(status_code=403, detail=f"Channel @{username} is private")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram error for @{username}: {exc}")

    channel_name = getattr(entity, "title", username)
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    posts: list[dict] = []
    async for msg in client.iter_messages(entity, limit=200):
        if msg.date < since:
            break
        mtype = _media_type(msg)
        if not msg.text and not mtype:
            continue
        posts.append({
            "id":               msg.id,
            "channel_name":     channel_name,
            "channel_username": username,
            "text":             msg.text or "",
            "date":             _utc_iso(msg.date),
            "views":            msg.views or 0,
            "media_type":       mtype,
        })

    return posts[:limit]


@app.get("/posts")
async def get_posts(
    channels: str = Query(..., description="Comma-separated channel usernames"),
    limit: int    = Query(10, ge=1, le=50),
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


# ── /channel-photo ────────────────────────────────────────────────────────────

@app.get("/channel-photo")
async def channel_photo(username: str = Query(...)):
    username = username.lstrip("@")

    if username in _photo_cache:
        return Response(
            _photo_cache[username],
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    try:
        entity = await _get_entity(username)
        buf = BytesIO()
        path = await client.download_profile_photo(entity, file=buf)
        if path is None:
            raise HTTPException(status_code=404, detail="No photo")
        buf.seek(0)
        data = buf.read()
        if not data:
            raise HTTPException(status_code=404, detail="Empty photo")
        _photo_cache[username] = data
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── /media ────────────────────────────────────────────────────────────────────

@app.get("/media")
async def get_media(
    channel:    str = Query(...),
    message_id: int = Query(...),
):
    channel = channel.lstrip("@")
    try:
        entity  = await _get_entity(channel)
        message = await client.get_messages(entity, ids=message_id)
        if not message or not message.media:
            raise HTTPException(status_code=404, detail="No media")

        buf = BytesIO()
        await client.download_media(message, file=buf)
        buf.seek(0)
        data = buf.read()
        if not data:
            raise HTTPException(status_code=404, detail="Empty media")

        # Determine MIME type
        mime = "image/jpeg"
        if isinstance(message.media, MessageMediaDocument):
            mime = getattr(message.media.document, "mime_type", "image/jpeg") or "image/jpeg"

        return Response(
            data,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "connected": client.is_connected() if client else False}
