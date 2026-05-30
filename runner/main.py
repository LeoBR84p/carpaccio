"""
Carpaccio Cloud Run runner.
Triggered by the bot via authenticated HTTP POST /run.
Downloads via yt-dlp, uploads to Iceland VPS via SFTP,
registers a one-time token, then POSTs a signed callback to the bot.
"""

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

import httpx
import paramiko
from fastapi import FastAPI, Header, HTTPException, Request

logger = logging.getLogger(__name__)

BOT_RUNNER_SECRET: str = os.environ["BOT_RUNNER_SECRET"]
CALLBACK_URL: str = os.environ["CALLBACK_URL"]       # https://vps.example.is/callback
DELIVERY_HOST: str = os.environ["DELIVERY_HOST"]     # https://vps.example.is
DELIVERY_SECRET: str = os.environ["DELIVERY_SECRET"]
SFTP_HOST: str = os.environ["SFTP_HOST"]
SFTP_USER: str = os.environ["SFTP_USER"]
SFTP_KEY: str = os.environ["SFTP_KEY"]               # PEM private key (newlines as \n)
SFTP_BASE: str = os.environ.get("SFTP_BASE", "/srv/delivery")

app = FastAPI()


@app.post("/run")
async def run_job(request: Request, x_signature: str = Header(...)) -> dict:
    body = await request.body()
    expected = hmac.new(
        BOT_RUNNER_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(x_signature, expected):
        raise HTTPException(403, "Bad signature")

    data = json.loads(body)
    url: str = data["url"]
    format_id: str = data["format_id"]
    job_id: str = data["job_id"]
    stars: int = data["stars"]

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            out_path = await _download(url, format_id, tmpdir)
            link = await _upload_and_register(out_path, job_id)
            await _callback(job_id, link, stars, success=True)
        except Exception as exc:
            logger.error("job %s failed: %s", job_id, exc)
            await _callback(job_id, "", stars, success=False, error=str(exc)[:200])

    return {"status": "done"}


async def _download(url: str, format_id: str, tmpdir: str) -> Path:
    cmd = [
        "yt-dlp",
        "--format", format_id,
        "--output", f"{tmpdir}/%(id)s.%(ext)s",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--quiet",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp: {stderr.decode(errors='replace')[-400:]}")

    files = list(Path(tmpdir).iterdir())
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    return files[0]


async def _upload_and_register(local_path: Path, job_id: str) -> str:
    token = str(uuid.uuid4())

    loop = asyncio.get_event_loop()
    remote_path = await loop.run_in_executor(None, _sftp_put, local_path, job_id)

    payload = {"token": token, "filepath": remote_path}
    body = json.dumps(payload).encode()
    sig = hmac.new(DELIVERY_SECRET.encode(), body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{DELIVERY_HOST}/register",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": sig},
            timeout=15,
        )
        resp.raise_for_status()

    return f"{DELIVERY_HOST}/d/{token}"


def _sftp_put(local_path: Path, job_id: str) -> str:
    pkey = paramiko.RSAKey.from_private_key(io.StringIO(SFTP_KEY.replace("\\n", "\n")))
    transport = paramiko.Transport((SFTP_HOST, 22))
    transport.connect(username=SFTP_USER, pkey=pkey)
    sftp = paramiko.SFTPClient.from_transport(transport)

    remote_dir = f"{SFTP_BASE}/{job_id}"
    try:
        sftp.mkdir(remote_dir)
    except OSError:
        pass

    remote_path = f"{remote_dir}/{local_path.name}"

    sftp = paramiko.SFTPClient.from_transport(transport)
    if sftp is None:
        transport.close()
        raise RuntimeError("Could not open SFTP channel")

    sftp.put(str(local_path), remote_path)

    sftp.close()
    transport.close()
    return remote_path


async def _callback(
    job_id: str, link: str, stars: int, success: bool, error: str = ""
) -> None:
    payload = {
        "job_id": job_id,
        "link": link,
        "stars": stars,
        "success": success,
        "error": error,
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(BOT_RUNNER_SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                CALLBACK_URL,
                content=body,
                headers={"Content-Type": "application/json", "X-Signature": sig},
                timeout=10,
            )
        except Exception as exc:
            logger.error("Callback failed for job %s: %s", job_id, exc)
