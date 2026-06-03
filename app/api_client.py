"""HTTP client for the SAT Zone backend API.

The bot owns no database. To handle a phone share it makes two calls to the
backend's internal endpoints, authenticated with the shared ``INTERNAL_API_KEY``
sent as ``X-Internal-API-Key``:

1. ``POST /api/v1/internal/users/lookup-by-phone`` — does a user already exist
   for this phone? If yes and verified, the bot greets them and stops.
2. ``POST /api/v1/internal/phone/issue-otp`` — otherwise, mint an OTP and show
   it to the user.

This thin wrapper around a single long-lived :class:`httpx.AsyncClient` keeps
the contract (URL, header, payload shape) in one place and raises typed
exceptions on failure so handlers can show friendly copy.

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

_LOOKUP_PATH = "/api/v1/internal/users/lookup-by-phone"
_ISSUE_OTP_PATH = "/api/v1/internal/phone/issue-otp"
_INTERNAL_API_KEY_HEADER = "X-Internal-API-Key"

# Retry policy for transient transport errors. HTTP status errors are never
# retried — the API is authoritative on those.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFFS = (0.25, 0.75)  # seconds to sleep before attempts 2 and 3

# Cap the API error body we include in logs so a misbehaving backend can't fill
# disks or leak large payloads.
_MAX_LOGGED_BODY = 512


class ApiClientError(Exception):
    """Raised when the API call fails (network error or unexpected non-2xx)."""


class PhoneTakenError(ApiClientError):
    """Raised on 409 ``phone_taken`` — the phone is verified on another account."""


class ApiClient:
    """Async wrapper around the backend API, reusing one HTTP connection pool."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(settings.API_BASE_URL).rstrip("/"),
            headers={_INTERNAL_API_KEY_HEADER: settings.INTERNAL_API_KEY},
            timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS),
        )

    async def lookup_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        """Return the user record for ``phone`` or ``None`` if no user exists.

        404 with code ``user_not_found`` is the expected "new user" signal and
        returns ``None`` rather than raising. Other non-2xx → :class:`ApiClientError`.
        """
        try:
            response = await self._request_with_retry(
                _LOOKUP_PATH, {"phone_number": phone}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info("lookup.not_found")
                return None
            self._log_status_error("lookup", exc)
            raise ApiClientError("API rejected the lookup request") from exc

        data: dict[str, Any] = response.json()
        logger.info("lookup.ok", verified=bool(data.get("is_phone_verified")))
        return data

    async def issue_otp(self, phone: str) -> dict[str, Any]:
        """Mint a fresh OTP for ``phone``. Returns ``{otp, expires_in}``.

        Raises :class:`PhoneTakenError` on 409 (number owned by another account)
        and :class:`ApiClientError` on any other failure.
        """
        try:
            response = await self._request_with_retry(
                _ISSUE_OTP_PATH, {"phone_number": phone}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                logger.info("issue_otp.phone_taken")
                raise PhoneTakenError("phone already verified on another account") from exc
            self._log_status_error("issue_otp", exc)
            raise ApiClientError("API rejected the issue-otp request") from exc

        data: dict[str, Any] = response.json()
        # NB: never log the OTP itself.
        logger.info("issue_otp.ok", expires_in=data.get("expires_in"))
        return data

    async def _request_with_retry(
        self, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        """POST ``payload`` to ``path``, retrying transport errors only.

        HTTP status errors propagate so the caller can map specific codes to
        typed exceptions.
        """
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._client.post(path, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError:
                # Authoritative — never retry. Re-raise for the caller to map.
                raise
            except httpx.HTTPError as exc:
                if attempt < _RETRY_ATTEMPTS - 1:
                    backoff = _RETRY_BACKOFFS[attempt]
                    logger.info(
                        "api.retry",
                        path=path,
                        attempt=attempt + 1,
                        backoff=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.warning("api.transport_error", path=path, error=str(exc))
                raise ApiClientError("Could not reach the API") from exc
            else:
                return response

        # Loop only exits via return or raise; this is for the type checker.
        raise ApiClientError("Could not reach the API")

    @staticmethod
    def _log_status_error(op: str, exc: httpx.HTTPStatusError) -> None:
        logger.warning(
            f"{op}.api_error",
            status_code=exc.response.status_code,
            body=exc.response.text[:_MAX_LOGGED_BODY],
        )

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
