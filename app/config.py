"""Bot configuration.

A single ``Settings`` object is the source of truth for all runtime config.
Values are loaded from environment variables / ``.env`` (see ``.env.example``)
and validated by Pydantic. Import the cached singleton via ``get_settings()``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Telegram ----
    BOT_TOKEN: str = Field(min_length=1)
    BOT_USERNAME: str = "satzone_bot"

    # ---- Backend API ----
    API_BASE_URL: AnyHttpUrl
    # API key shared with the backend; sent as the `X-Internal-API-Key` header.
    # Must match the backend's INTERNAL_API_KEY. Required to be reasonably
    # long to make brute-force impractical.
    INTERNAL_API_KEY: str = Field(min_length=16)

    # ---- Internal HTTP server (just /healthz now; no inbound OTP push) ----
    INTERNAL_HOST: str = "0.0.0.0"  # noqa: S104 - bind all interfaces inside the container network
    INTERNAL_PORT: int = Field(default=8081, ge=1, le=65535)

    # ---- Operational tuning ----
    LOG_LEVEL: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    HTTP_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0.0, le=60.0)
    # Per-chat throttle for user-initiated OTP requests (contact shares). Stops
    # a user (or stolen chat) from spamming the backend with fresh-code requests.
    CONTACT_RATE_LIMIT_MAX: int = Field(default=3, ge=1)
    CONTACT_RATE_LIMIT_WINDOW_SECONDS: float = Field(default=300.0, gt=0.0)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
