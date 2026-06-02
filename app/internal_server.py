"""Internal OTP-push HTTP server (aiohttp).

The API does not hold a Telegram connection; when it needs to deliver a code
it POSTs to this service's ``/internal/send-otp`` endpoint, which then relays
the code to the target chat via the shared :class:`aiogram.Bot` instance.

Endpoints:

- ``POST /internal/send-otp`` — authenticated, rate-limited per chat. Body:
  ``{chat_id, code, purpose}`` → ``204 No Content``.
- ``GET /healthz`` — unauthenticated liveness probe for k8s/load balancers.

The service is *internal only* and must never be exposed publicly. It is
authenticated solely by the shared ``INTERNAL_SECRET`` API key (compared in
constant time to defeat timing oracles), matching the contract the API
enforces in the other direction.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from app.config import settings

logger = structlog.get_logger("bot.internal_server")

_INTERNAL_SECRET_HEADER = "X-Internal-Secret"

_BOT_KEY: web.AppKey[Bot] = web.AppKey("bot", Bot)


class _RateLimiter:
    """Per-key sliding-window rate limiter.

    Cheap and in-memory; sufficient for a single-instance bot. Each key gets a
    deque of recent timestamps; we drop ones outside the window on every check.
    """

    def __init__(self, max_per_window: int, window_seconds: float = 60.0) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._buckets: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, key: int) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


_RATE_LIMITER_KEY: web.AppKey[_RateLimiter] = web.AppKey("rate_limiter", _RateLimiter)


def _is_authorised(request: web.Request) -> bool:
    """Constant-time check of the shared-secret header."""
    presented = request.headers.get(_INTERNAL_SECRET_HEADER, "")
    return secrets.compare_digest(presented, settings.INTERNAL_SECRET)


async def _handle_healthz(request: web.Request) -> web.Response:  # noqa: ARG001
    """Liveness probe. Returns 200 OK if the process is running."""
    return web.json_response({"status": "ok"})


async def _handle_send_otp(request: web.Request) -> web.Response:
    """Deliver a code to a chat. Body: ``{chat_id, code, purpose}`` → 204."""
    if not _is_authorised(request):
        # Don't reveal whether the secret or the payload was at fault.
        raise web.HTTPUnauthorized()

    try:
        body = await request.json()
        chat_id = int(body["chat_id"])
        code = str(body["code"])
        purpose = str(body["purpose"])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("send_otp.bad_request", error=str(exc))
        raise web.HTTPBadRequest() from exc

    rate_limiter = request.app[_RATE_LIMITER_KEY]
    if not rate_limiter.allow(chat_id):
        logger.warning("send_otp.rate_limited", chat_id=chat_id, purpose=purpose)
        raise web.HTTPTooManyRequests()

    bot = request.app[_BOT_KEY]
    text = (
        "🔐 <b>SAT Zone</b>\n\n"
        f"Your verification code is:\n\n<b>{code}</b>\n\n"
        "Enter it on the website to continue. "
        "It expires shortly — never share it with anyone."
    )
    try:
        async with asyncio.timeout(settings.HTTP_TIMEOUT_SECONDS):
            await bot.send_message(chat_id, text)
    except TimeoutError as exc:
        logger.warning("send_otp.timeout", chat_id=chat_id, purpose=purpose)
        raise web.HTTPGatewayTimeout() from exc
    except TelegramAPIError as exc:
        logger.warning(
            "send_otp.delivery_failed",
            chat_id=chat_id,
            purpose=purpose,
            error=str(exc),
        )
        raise web.HTTPBadGateway() from exc

    logger.info("send_otp.delivered", chat_id=chat_id, purpose=purpose)
    return web.Response(status=204)


def build_internal_app(bot: Bot) -> web.Application:
    """Build the aiohttp application, wiring shared state for handlers to use."""
    app = web.Application()
    app[_BOT_KEY] = bot
    app[_RATE_LIMITER_KEY] = _RateLimiter(max_per_window=settings.OTP_RATE_LIMIT_PER_MIN)
    app.add_routes(
        [
            web.get("/healthz", _handle_healthz),
            web.post("/internal/send-otp", _handle_send_otp),
        ]
    )
    return app
