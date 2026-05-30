"""
Carpaccio Telegram bot — aiogram 3, webhook mode, FSM.
Runs on the Iceland VPS alongside the delivery server.
Listens on 0.0.0.0:8080; nginx terminates TLS and routes:
  /telegram  → this process (Telegram webhook)
  /callback  → this process (Cloud Run job callback)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from pricing import fmt_bytes, stars_for_size
from probe import probe_url
from states import DownloadFlow

logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
WEBHOOK_HOST: str = os.environ["WEBHOOK_HOST"]   # https://vps.example.is
BOT_RUNNER_SECRET: str = os.environ["BOT_RUNNER_SECRET"]
RUNNER_URL: str = os.environ["RUNNER_URL"]        # Cloud Run service URL

WEBHOOK_PATH = "/telegram"
MAX_OPTIONS = 6

router = Router()

# Single-process in-memory stores (persistent VPS, MemoryStorage FSM)
_sessions: dict[int, dict] = {}     # chat_id → {url, options, title}
_job_chat: dict[str, int] = {}      # job_id  → chat_id
_pending: dict[str, dict] = {}      # job_id  → {link, stars}


# ── Command handler ────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.set_state(DownloadFlow.waiting_url)
    await message.answer(
        "Send me any video URL and I will show available quality options."
    )


# ── URL intake ─────────────────────────────────────────────────────────────

@router.message(StateFilter(None, DownloadFlow.waiting_url))
async def handle_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith("http"):
        await message.answer("Send a valid URL starting with http.")
        return

    status = await message.answer("Probing URL…")

    try:
        info = await probe_url(url)
    except Exception as exc:
        await status.edit_text(f"Could not extract formats:\n{exc}")
        return

    options = _build_options(info)
    if not options:
        await status.edit_text("No downloadable formats found for this URL.")
        return

    title = (info.get("title") or "Video")[:80]
    _sessions[message.chat.id] = {"url": url, "options": options, "title": title}

    duration = info.get("duration")
    dur = f" · {int(duration // 60)}:{int(duration % 60):02d}" if duration else ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{i + 1}. {o['label']}  —  {o['stars']} Stars",
            callback_data=f"pick:{i}",
        )]
        for i, o in enumerate(options)
    ])

    await status.edit_text(
        f"<b>{title}</b>{dur}\n\nSelect quality:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(DownloadFlow.waiting_choice)


# ── Quality selection ──────────────────────────────────────────────────────

@router.callback_query(DownloadFlow.waiting_choice, F.data.startswith("pick:"))
async def handle_pick(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.message.chat.id
    idx = int(callback.data.split(":")[1])
    session = _sessions.get(chat_id)

    if not session or idx >= len(session["options"]):
        await callback.answer("Session expired — send the URL again.")
        await state.set_state(DownloadFlow.waiting_url)
        return

    option = session["options"][idx]
    job_id = str(uuid.uuid4())
    _job_chat[job_id] = chat_id

    await callback.message.edit_text(
        f"<b>{session['title']}</b>\n"
        f"Quality: {option['label']}\n\n"
        "Processing. You will receive a payment request when the file is ready.",
        parse_mode="HTML",
    )
    await callback.answer()
    await state.set_state(DownloadFlow.processing)

    asyncio.create_task(
        _trigger_runner(session["url"], option["format_id"], job_id, option["stars"])
    )


# ── Payment ────────────────────────────────────────────────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def handle_payment(message: Message, state: FSMContext) -> None:
    job_id = message.successful_payment.invoice_payload
    job = _pending.pop(job_id, None)

    if not job:
        await message.answer(
            "Link not found or already expired. Contact support for a refund."
        )
        await state.set_state(DownloadFlow.waiting_url)
        return

    await message.answer(
        "Your download link (one-time — expires after the first byte is served):\n\n"
        + job["link"]
    )
    await state.set_state(DownloadFlow.waiting_url)


# ── Cloud Run callback endpoint ────────────────────────────────────────────

async def handle_runner_callback(request: web.Request) -> web.Response:
    body = await request.read()
    sig = request.headers.get("X-Signature", "")
    expected = hmac.new(
        BOT_RUNNER_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return web.Response(status=403, text="Bad signature")

    data = json.loads(body)
    job_id: str = data["job_id"]
    success: bool = data.get("success", False)
    link: str = data.get("link", "")
    stars: int = data.get("stars", 0)
    error: str = data.get("error", "")

    chat_id = _job_chat.pop(job_id, None)
    if chat_id is None:
        return web.Response(status=404, text="Unknown job")

    bot: Bot = request.app["bot"]

    if not success:
        await bot.send_message(
            chat_id,
            f"Download failed: {error}\n\nNo Stars were charged.",
        )
        return web.Response(status=200)

    _pending[job_id] = {"link": link, "stars": stars}

    await bot.send_invoice(
        chat_id=chat_id,
        title="Download ready",
        description="Your file is ready. Pay to receive the one-time download link.",
        payload=job_id,
        currency="XTR",
        prices=[LabeledPrice(label="Download", amount=stars)],
    )
    return web.Response(status=200)


# ── Internal helpers ───────────────────────────────────────────────────────

def _build_options(info: dict) -> list[dict]:
    formats: list[dict] = info.get("formats", [])
    options: list[dict] = []

    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if audio:
        best = max(audio, key=lambda f: f.get("abr") or 0)
        size = best.get("filesize") or best.get("filesize_approx")
        options.append({
            "format_id": best["format_id"],
            "label": f"Audio · {fmt_bytes(size) if size else 'audio'}",
            "stars": stars_for_size(size, is_audio=True),
        })

    seen: set[int] = set()
    video = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
    video.sort(key=lambda f: f.get("height", 0), reverse=True)

    for fmt in video:
        h: int = fmt["height"]
        if h in seen:
            continue
        seen.add(h)
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        ext = (fmt.get("ext") or "mp4").upper()
        options.append({
            "format_id": fmt["format_id"],
            "label": f"{h}p {ext} · {fmt_bytes(size) if size else '~unknown'}",
            "stars": stars_for_size(size),
        })
        if len(options) >= MAX_OPTIONS:
            break

    return options


async def _trigger_runner(url: str, format_id: str, job_id: str, stars: int) -> None:
    payload = {"url": url, "format_id": format_id, "job_id": job_id, "stars": stars}
    body = json.dumps(payload).encode()
    sig = hmac.new(BOT_RUNNER_SECRET.encode(), body, hashlib.sha256).hexdigest()
    timeout = aiohttp.ClientTimeout(total=3600)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{RUNNER_URL}/run",
                data=body,
                headers={"Content-Type": "application/json", "X-Signature": sig},
                timeout=timeout,
            ):
                pass
    except Exception as exc:
        logger.error("Runner request failed for job %s: %s", job_id, exc)


# ── Entry point ────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    app = web.Application()
    app["bot"] = bot

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.router.add_post("/callback", handle_runner_callback)

    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    logger.warning("Bot listening on 0.0.0.0:8080")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
