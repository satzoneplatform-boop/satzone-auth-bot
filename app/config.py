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
    # API key shared with the backend; sent as the `X-Internal-Secret` header.
    # Must match the API's TELEGRAM_INTERNAL_SECRET. Required to be reasonably
    # long to make brute-force impractical.
    INTERNAL_SECRET: str = Field(min_length=16)

    # ---- Internal OTP-push server (the API POSTs codes here) ----
    INTERNAL_HOST: str = "0.0.0.0"  # noqa: S104 - bind all interfaces inside the container network
    INTERNAL_PORT: int = Field(default=8081, ge=1, le=65535)

    # ---- Operational tuning ----
    LOG_LEVEL: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    HTTP_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0.0, le=60.0)
    LINK_STATE_TTL_SECONDS: float = Field(default=900.0, gt=0.0)
    OTP_RATE_LIMIT_PER_MIN: int = Field(default=10, ge=1)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
