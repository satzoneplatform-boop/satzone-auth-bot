"""Telegram message handlers (aiogram v3 Router).

Two interactions matter:

- ``/start`` greets the user and shows a one-tap "Share phone number" button
  (a ``request_contact`` keyboard button — the only reliable way to obtain a
  *verified* phone number from Telegram).
- A **contact** message → call the API to link ``chat_id`` ↔ ``phone`` ↔ user,
  then reply with the returned code and instructions to enter it on the website.

The router pulls its :class:`ApiClient` from the dispatcher's workflow data
(injected in ``main``) rather than constructing one per update, so the HTTP
connection pool is shared across all handlers.

Phone-number authenticity is guaranteed by Telegram: we only accept ``Contact``
updates produced by the ``request_contact`` button, and we additionally verify
that the contact's ``user_id`` equals the sender's ``user_id``. Anyone typing a
number as plain text is routed to the fallback handler.
"""

from __future__ import annotations

import time

import structlog
from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.api_client import ApiClient, ApiClientError
from app.config import settings
from app.rate_limit import RateLimiter

logger = structlog.get_logger("bot.handlers")

router = Router(name="link-contact")

_SHARE_PHONE_LABEL = "📱 Share phone number"


class _LinkStateStore:
    """In-memory store for deep-link ``state`` values with per-entry TTL.

    A logged-in user starting from ``t.me/<bot>?start=<state>`` arrives with a
    payload that the API needs to tie this chat to their account. We keep the
    state for ``LINK_STATE_TTL_SECONDS`` and prune lazily on every access so
    abandoned flows don't leak memory.

    In-memory is fine for a single long-polling instance; switch to Redis if
    the bot ever needs to run multiple replicas.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._data: dict[int, tuple[str, float]] = {}

    def put(self, chat_id: int, state: str) -> None:
        self._prune()
        self._data[chat_id] = (state, time.monotonic() + self._ttl)

    def pop(self, chat_id: int) -> str | None:
        self._prune()
        entry = self._data.pop(chat_id, None)
        return entry[0] if entry else None

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._data.items() if exp <= now]
        for key in expired:
            self._data.pop(key, None)


_link_state_store = _LinkStateStore(ttl_seconds=settings.LINK_STATE_TTL_SECONDS)

# Per-chat throttle for user-initiated OTP requests. Counts every contact-share
# attempt (success OR failure) so a misbehaving client can't bypass the limit
# by deliberately triggering API errors.
_otp_request_limiter = RateLimiter(
    max_per_window=settings.CONTACT_RATE_LIMIT_MAX,
    window_seconds=settings.CONTACT_RATE_LIMIT_WINDOW_SECONDS,
)


def _share_phone_keyboard() -> ReplyKeyboardMarkup:
    """Build the one-button keyboard that requests the user's verified contact."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_SHARE_PHONE_LABEL, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Tap the button below to share your number",
    )


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject) -> None:
    """Greet the user and prompt them to share their phone number.

    A deep-link payload (``/start <state>``) means a logged-in user is linking
    from the app; remember it for this chat so the contact share can be tied
    back to their account.
    """
    if command.args:
        _link_state_store.put(message.chat.id, command.args)

    first_name = message.from_user.first_name if message.from_user else "there"
    await message.answer(
        f"Hi {first_name}! 👋\n\n"
        "This is the SAT Zone verification bot. To receive your login codes here, "
        "tap the button below to share your phone number.\n\n"
        "We only use it to match your Telegram account to your SAT Zone profile.",
        reply_markup=_share_phone_keyboard(),
    )


@router.message(F.contact)
async def handle_contact(message: Message, api_client: ApiClient) -> None:
    """Handle a shared contact: link it via the API and relay the returned code.

    Rejects anything that isn't a genuine Telegram-verified contact for the
    sender's own number (forwarded contact cards carry a ``user_id`` that
    doesn't match the sender; typed numbers don't arrive here at all).
    """
    contact = message.contact
    sender = message.from_user

    if (
        contact is None
        or sender is None
        or contact.user_id != sender.id
        or not contact.phone_number
    ):
        logger.info(
            "handle_contact.rejected_unverified",
            chat_id=message.chat.id,
            has_contact=contact is not None,
            has_sender=sender is not None,
        )
        await message.answer(
            "Please share <b>your own</b> phone number using the button below, "
            "not a saved contact.",
            reply_markup=_share_phone_keyboard(),
        )
        return

    chat_id = message.chat.id

    if not _otp_request_limiter.allow(chat_id):
        retry_seconds = int(_otp_request_limiter.retry_after(chat_id))
        minutes = max(1, (retry_seconds + 59) // 60)
        logger.info(
            "handle_contact.rate_limited",
            chat_id=chat_id,
            retry_seconds=retry_seconds,
        )
        await message.answer(
            "You've requested a verification code too many times. "
            f"Please wait about {minutes} minute{'s' if minutes != 1 else ''} "
            "before trying again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    state = _link_state_store.pop(chat_id)
    try:
        result = await api_client.link_contact(
            chat_id=chat_id,
            phone=contact.phone_number,
            first_name=contact.first_name,
            last_name=contact.last_name,
            state=state,
        )
    except ApiClientError:
        logger.warning("handle_contact.link_failed", chat_id=chat_id)
        await message.answer(
            "Sorry, something went wrong linking your account. "
            "Please try again in a moment, or contact support if it keeps happening.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if result.get("linked"):
        logger.info("handle_contact.linked_account", chat_id=chat_id)
        await message.answer(
            "✅ Your Telegram is now linked to your SAT Zone account!\n\n"
            "You'll receive verification codes here. You can return to the website.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    code = result.get("code")
    if not code:
        logger.error("handle_contact.missing_code", chat_id=chat_id)
        await message.answer(
            "Your number is linked, but we couldn't generate a code right now. "
            "Please request a new code from the website.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    logger.info("handle_contact.linked", chat_id=chat_id)
    await message.answer(
        "✅ Your phone number is linked!\n\n"
        f"Your SAT Zone verification code is:\n\n<b>{code}</b>\n\n"
        "Enter it on the website to finish signing in. "
        "The code is valid for a few minutes — don't share it with anyone.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message()
async def handle_fallback(message: Message) -> None:
    """Nudge users who type free-text toward the share-phone flow."""
    await message.answer(
        "To link your account, tap the button below to share your phone number. "
        "Typed numbers aren't accepted — Telegram has to send the verified "
        "contact for us. If you don't see the button, send /start.",
        reply_markup=_share_phone_keyboard(),
    )
