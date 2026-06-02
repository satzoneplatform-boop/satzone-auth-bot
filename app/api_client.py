"""HTTP client for the SAT Zone backend API.

The bot owns no database. To link a Telegram contact to a user it calls the
API's internal endpoint, authenticating with the shared ``INTERNAL_SECRET``
API key. This thin wrapper around a single long-lived
:class:`httpx.AsyncClient` keeps the contract (URL, header, payload shape) in
one place and raises a typed :class:`ApiClientError` on any failure so handlers
can show friendly copy.

Transport failures are retried with exponential backoff; HTTP errors (4xx/5xx)
are not — the API's response is authoritative.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger("bot.api_client")

_LINK_CONTACT_PATH = "/api/v1/auth/telegram/link-contact"
_INTERNAL_SECRET_HEADER = "X-Internal-Secret"

# Retry policy for transient transport errors. HTTP status errors are never
# retried — the API is authoritative on those.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFFS = (0.25, 0.75)  # seconds to sleep before attempts 2 and 3

# Cap the API error body we include in logs so a misbehaving backend can't fill
# disks or leak large payloads.
_MAX_LOGGED_BODY = 512


class ApiClientError(Exception):
    """Raised when the API call fails (network error or non-2xx response)."""


class ApiClient:
    """Async wrapper around the backend API, reusing one HTTP connection pool."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(settings.API_BASE_URL).rstrip("/"),
            headers={_INTERNAL_SECRET_HEADER: settings.INTERNAL_SECRET},
            timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS),
        )

    async def link_contact(
        self,
        *,
        chat_id: int,
        phone: str,
        first_name: str | None,
        last_name: str | None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Link a Telegram contact to a user.

        Returns the API's JSON body — sign-in mode: ``{"code", "expires_in"}``;
        link mode (``state`` set): ``{"linked": true}``. Raises
        :class:`ApiClientError` on transport failure (after retries) or any
        non-2xx status.
        """
        payload = {
            "chat_id": chat_id,
            "phone": phone,
            "first_name": first_name,
            "last_name": last_name,
            "state": state,
        }

        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._client.post(_LINK_CONTACT_PATH, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "link_contact.api_error",
                    chat_id=chat_id,
                    status_code=exc.response.status_code,
                    body=exc.response.text[:_MAX_LOGGED_BODY],
                )
                raise ApiClientError("API rejected the link request") from exc
            except httpx.HTTPError as exc:
                if attempt < _RETRY_ATTEMPTS - 1:
                    backoff = _RETRY_BACKOFFS[attempt]
                    logger.info(
                        "link_contact.retry",
                        chat_id=chat_id,
                        attempt=attempt + 1,
                        backoff=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.warning(
                    "link_contact.transport_error",
                    chat_id=chat_id,
                    error=str(exc),
                )
                raise ApiClientError("Could not reach the API") from exc
            else:
                data: dict[str, Any] = response.json()
                logger.info("link_contact.ok", chat_id=chat_id)
                return data

        # Loop only exits via return or raise; this is for the type checker.
        raise ApiClientError("Could not reach the API")

    async def aclose(self) -> None:
        """Close the underlying connection pool on shutdown."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
