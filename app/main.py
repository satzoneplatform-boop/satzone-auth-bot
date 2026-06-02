"""Bot entrypoint.

Builds the aiogram :class:`Bot` + :class:`Dispatcher` and runs two coroutines
concurrently for the lifetime of the process:

1. **Long polling** — receives Telegram updates and dispatches them to handlers.
2. **Internal HTTP server** — exposes ``/internal/send-otp`` for the API to push
   codes the bot relays to the target chat, plus ``/healthz`` for probes.

Both share a single :class:`Bot` (one Telegram session) and a single
:class:`ApiClient` (one HTTP pool). The process responds to ``SIGINT`` and
``SIGTERM`` (the latter is what Docker / Kubernetes send on shutdown) by
cancelling the workers and closing the bot session and HTTP pool cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from app.api_client import ApiClient
from app.config import settings
from app.handlers import router
from app.internal_server import build_internal_app

logger = structlog.get_logger("bot.main")


def configure_logging() -> None:
    """Configure structlog console rendering. Call once at startup."""
    level = logging.getLevelNamesMapping().get(settings.LOG_LEVEL, logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)


async def _run_polling(bot: Bot, dispatcher: Dispatcher, stop_event: asyncio.Event) -> None:
    """Drop any backlog and receive updates until ``stop_event`` is set."""
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("polling.start")
    try:
        # aiogram supports a stop signal via an external event-like sentinel; we
        # pass our shutdown flag so SIGTERM cleanly unwinds polling.
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        # Belt-and-braces: if polling returns on its own, signal global shutdown.
        stop_event.set()


async def _run_internal_server(stop_event: asyncio.Event, bot: Bot) -> None:
    """Serve the internal endpoints until ``stop_event`` is set."""
    app = build_internal_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.INTERNAL_HOST, port=settings.INTERNAL_PORT)
    await site.start()
    logger.info(
        "internal_server.start",
        host=settings.INTERNAL_HOST,
        port=settings.INTERNAL_PORT,
    )
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
        logger.info("internal_server.stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Bind SIGINT/SIGTERM to a shutdown event so we can exit gracefully.

    ``loop.add_signal_handler`` is the preferred path on POSIX (where the bot
    actually runs). On Windows it isn't implemented; we fall back to
    ``signal.signal`` so local development on Windows still responds to Ctrl-C.
    """
    loop = asyncio.get_running_loop()

    def _trigger() -> None:
        logger.info("bot.shutdown_signal")
        stop_event.set()

    def _windows_handler(_signum: int, _frame: object) -> None:
        # signal.signal handlers run between bytecodes; bounce back to the loop.
        loop.call_soon_threadsafe(_trigger)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, _windows_handler)


async def _amain() -> None:
    """Async entrypoint: wire dependencies and run both services concurrently."""
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    api_client = ApiClient()
    dispatcher["api_client"] = api_client

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    logger.info("bot.starting", username=settings.BOT_USERNAME)

    polling_task = asyncio.create_task(
        _run_polling(bot, dispatcher, stop_event),
        name="polling",
    )
    server_task = asyncio.create_task(
        _run_internal_server(stop_event, bot),
        name="internal-server",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")

    try:
        # Wake on whichever finishes first — a worker dying, or a shutdown signal.
        done, _pending = await asyncio.wait(
            {polling_task, server_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is stop_task:
                continue
            exc = task.exception()
            if exc is not None:
                logger.error("bot.worker_failed", task=task.get_name(), error=str(exc))
    finally:
        # 1. Tell both workers it's time to wrap up.
        stop_event.set()
        if not polling_task.done():
            try:
                await dispatcher.stop_polling()
            except RuntimeError:
                # Polling never reached the "started" state (e.g. start-up failure).
                pass
        stop_task.cancel()

        # 2. Let workers run their own cleanup paths (close runners, etc.).
        #    Cancel only if they hang past the shutdown grace window — otherwise
        #    we'd interrupt aiohttp's runner.cleanup() mid-flight.
        try:
            async with asyncio.timeout(settings.HTTP_TIMEOUT_SECONDS):
                await asyncio.gather(polling_task, server_task, return_exceptions=True)
        except TimeoutError:
            logger.warning("bot.shutdown_timeout")
            polling_task.cancel()
            server_task.cancel()
            await asyncio.gather(polling_task, server_task, return_exceptions=True)
        await asyncio.gather(stop_task, return_exceptions=True)

        # 3. Tear down the shared HTTP pools last.
        await api_client.aclose()
        await bot.session.close()
        logger.info("bot.stopped")


def main() -> None:
    """Process entrypoint."""
    configure_logging()
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot.interrupted")


if __name__ == "__main__":
    main()
