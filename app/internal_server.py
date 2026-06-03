"""Internal HTTP server (aiohttp) — liveness probe only.

The backend does NOT push OTPs through this service in the current contract;
verification happens on the website after the bot displays the code. The only
endpoint left here is an unauthenticated liveness probe used by the Docker
healthcheck and any external orchestrator.

If a backend → bot push channel is ever needed again, add an authenticated
``POST /internal/...`` handler here and re-introduce the shared-secret check.
"""

from __future__ import annotations

import structlog
from aiohttp import web

logger = structlog.get_logger("bot.internal_server")


async def _handle_healthz(request: web.Request) -> web.Response:  # noqa: ARG001
    """Liveness probe. Returns 200 OK if the process is running."""
    return web.json_response({"status": "ok"})


def build_internal_app() -> web.Application:
    """Build the aiohttp application."""
    app = web.Application()
    app.add_routes([web.get("/healthz", _handle_healthz)])
    return app
