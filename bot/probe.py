import asyncio
import json


async def probe_url(url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--dump-json", "--no-playlist", "--quiet", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Probe timed out (45s)")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-300:].strip()
        raise RuntimeError(err or "yt-dlp returned non-zero exit")

    return json.loads(stdout)
