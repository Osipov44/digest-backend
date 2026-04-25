import asyncio
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("digest")

API_ID   = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

_entity_cache: dict[str, object] = {}
_photo_cache:  dict[str, bytes]  = {}


# ── session ───────────────────────────────────────────────────────────────────

def _make_session():
    s = os.environ.get("SESSION_STRING", "").strip()
    if s:
        log.info("[startup] Using StringSession")
        return StringSession(s)
    log.info("[startup] Falling back to digest_user.session file")
    return "digest_user"


client: TelegramClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = TelegramClient(_make_session(), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session not authorized. Run auth.py first.")
    me = await client.get_me()
    log.info(f"[startup] Connected as {me.first_name} (@{me.username})")
    yield
    await client.disconnect()


# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Digest API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _media_type(msg) -> str | None:
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaDocument):
        mime = getattr(msg.media.document, "mime_type", "") or ""
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
    return None


def _utc_iso(dt) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt.tzinfo is None else dt.isoformat()


async def _get_entity(username: str):
    if username not in _entity_cache:
        _entity_cache[username] = await client.get_entity(f"@{username}")
    return _entity_cache[username]


async def _download_bytes(message) -> tuple[bytes, str]:
    """Download media, return (data, mime_type). Uses Telethon default
    which picks the largest photo size automatically."""
    buf = BytesIO()
    await client.download_media(message, file=buf)
    buf.seek(0)
    data = buf.read()

    mime = "image/jpeg"
    if isinstance(message.media, MessageMediaDocument):
        mime = getattr(message.media.document, "mime_type", "application/octet-stream") or "application/octet-stream"

    return data, mime


# ── /posts ────────────────────────────────────────────────────────────────────

async def _fetch_channel(username: str, limit: int) -> list[dict]:
    username = username.lstrip("@")
    try:
        entity = await _get_entity(username)
    except (UsernameNotOccupiedError, UsernameInvalidError):
        raise HTTPException(404, f"@{username} not found")
    except ChannelPrivateError:
        raise HTTPException(403, f"@{username} is private")
    except Exception as e:
        raise HTTPException(502, f"Telegram error for @{username}: {e}")

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
    channels: str = Query(...),
    limit:    int = Query(10, ge=1, le=50),
):
    usernames = [c.strip() for c in channels.split(",") if c.strip()]
    if not usernames:
        raise HTTPException(422, "No channels provided")

    results = await asyncio.gather(*[_fetch_channel(u, limit) for u in usernames],
                                   return_exceptions=True)
    posts, errors = [], []
    for u, r in zip(usernames, results):
        if isinstance(r, HTTPException):
            errors.append(f"{u}: {r.detail}")
        elif isinstance(r, Exception):
            errors.append(f"{u}: {r}")
        else:
            posts.extend(r)

    posts.sort(key=lambda p: p["date"], reverse=True)
    return {"posts": posts, "errors": errors}


# ── /channel-photo ────────────────────────────────────────────────────────────

@app.get("/channel-photo")
async def channel_photo(username: str = Query(...)):
    username = username.lstrip("@")
    if username in _photo_cache:
        return Response(_photo_cache[username], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        entity = await _get_entity(username)
        buf    = BytesIO()
        result = await client.download_profile_photo(entity, file=buf)
        if result is None:
            raise HTTPException(404, "No photo")
        buf.seek(0)
        data = buf.read()
        if not data:
            raise HTTPException(404, "Empty photo")
        _photo_cache[username] = data
        log.info(f"channel-photo @{username} → {len(data)} bytes")
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"channel-photo @{username} error: {traceback.format_exc()}")
        raise HTTPException(502, str(e))


# ── /media  (photos) ──────────────────────────────────────────────────────────

@app.get("/media")
async def get_media(channel: str = Query(...), message_id: int = Query(...)):
    channel = channel.lstrip("@")
    try:
        entity  = await _get_entity(channel)
        message = await client.get_messages(entity, ids=message_id)
        if not message or not message.media:
            raise HTTPException(404, "No media")

        data, mime = await _download_bytes(message)
        if not data:
            raise HTTPException(404, "Empty media")

        log.info(f"media @{channel}/{message_id} → {len(data)} bytes mime={mime}")
        return Response(data, media_type=mime,
                        headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"media @{channel}/{message_id} error: {traceback.format_exc()}")
        raise HTTPException(502, str(e))


# ── /video  (videos with Range support) ───────────────────────────────────────

@app.get("/video")
async def get_video(
    request:    Request,
    channel:    str = Query(...),
    message_id: int = Query(...),
):
    channel = channel.lstrip("@")
    try:
        entity  = await _get_entity(channel)
        message = await client.get_messages(entity, ids=message_id)
        if not message or not message.media:
            raise HTTPException(404, "No media")
        if not isinstance(message.media, MessageMediaDocument):
            raise HTTPException(400, "Not a video")

        mime = getattr(message.media.document, "mime_type", "video/mp4") or "video/mp4"
        if not mime.startswith("video/"):
            raise HTTPException(400, f"Not a video mime: {mime}")

        data, _ = await _download_bytes(message)
        if not data:
            raise HTTPException(404, "Empty video")

        total        = len(data)
        range_header = request.headers.get("range", "")
        log.info(f"video @{channel}/{message_id} → {total} bytes range={range_header!r}")

        if range_header:
            rng          = range_header.replace("bytes=", "")
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            end   = int(end_s)   if end_s   else total - 1
            end   = min(end, total - 1)
            chunk = data[start:end + 1]
            return Response(
                chunk, status_code=206, media_type=mime,
                headers={
                    "Content-Range":  f"bytes {start}-{end}/{total}",
                    "Accept-Ranges":  "bytes",
                    "Content-Length": str(len(chunk)),
                    "Cache-Control":  "public, max-age=3600",
                },
            )

        return Response(
            data, media_type=mime,
            headers={
                "Accept-Ranges":  "bytes",
                "Content-Length": str(total),
                "Cache-Control":  "public, max-age=3600",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"video @{channel}/{message_id} error: {traceback.format_exc()}")
        raise HTTPException(502, str(e))


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "connected": client.is_connected() if client else False}
