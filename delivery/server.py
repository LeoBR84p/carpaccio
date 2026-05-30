"""
Carpaccio one-time delivery server.
Runs on the Iceland VPS.
  POST /register  — Cloud Run registers a token+filepath (HMAC-signed)
  GET  /d/{token} — Streams the file exactly once, then deletes it
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

DELIVERY_SECRET: str = os.environ["DELIVERY_SECRET"]

app = FastAPI()

_tokens: dict[str, str] = {}   # token → absolute filepath


@app.post("/register")
async def register(request: Request, x_signature: str = Header(...)) -> dict:
    body = await request.body()
    expected = hmac.new(
        DELIVERY_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(x_signature, expected):
        raise HTTPException(403)

    data = json.loads(body)
    token: str = data["token"]
    filepath: str = data["filepath"]

    if not Path(filepath).exists():
        raise HTTPException(400, f"File not found on VPS: {filepath}")

    _tokens[token] = filepath
    asyncio.create_task(_expire_token(token, delay=3600))

    return {"ok": True}


@app.get("/d/{token}")
async def serve(token: str) -> StreamingResponse:
    filepath = _tokens.pop(token, None)
    if not filepath:
        raise HTTPException(404)

    path = Path(filepath)
    if not path.exists():
        raise HTTPException(404)

    async def stream_and_delete():
        async with aiofiles.open(str(path), "rb") as fh:
            while chunk := await fh.read(65536):
                yield chunk
        await _delete(path)

    return StreamingResponse(
        stream_and_delete(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


async def _expire_token(token: str, delay: int) -> None:
    await asyncio.sleep(delay)
    filepath = _tokens.pop(token, None)
    if filepath:
        await _delete(Path(filepath))


async def _delete(path: Path) -> None:
    try:
        await aiofiles.os.remove(str(path))
        try:
            path.parent.rmdir()
        except OSError:
            pass
    except Exception as exc:
        logger.error("Delete failed for %s: %s", path, exc)
