"""Telegram message handlers (aiogram v3 Router).

Two interactions matter:

- ``/start`` greets the user and shows a one-tap "Share phone number" button
  (a ``request_contact`` keyboard button — the only reliable way to obtain a
  *verified* phone number from Telegram).
- A **contact** message → look up the user by phone:

    * If a verified user already exists → greet them by name. Done.
    * Otherwise → mint a fresh OTP via the API and show it. The user types it
      on the website to finish verification.

The router pulls its :class:`ApiClient` from the dispatcher's workflow data
(injected in ``main``) rather than constructing one per update, so the HTTP
connection pool is shared across all handlers.

Phone-number authenticity is guaranteed by Telegram: we only accept ``Contact``
updates produced by the ``request_contact`` button, and we additionally verify
that the contact's ``user_id`` equals the sender's ``user_id``. Anyone typing a
number as plain text is routed to the fallback handler.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.api_client import ApiClient, ApiClientError, PhoneTakenError
from app.config import settings
from app.rate_limit import RateLimiter

logger = structlog.get_logger("bot.handlers")

router = Router(name="phone-verify")

_SHARE_PHONE_LABEL = "📱 Share phone number"

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
async def handle_start(message: Message) -> None:
    """Greet the user and prompt them to share their phone number."""
    first_name = message.from_user.first_name if message.from_user else "there"
    await message.answer(
        f"Hi {first_name}! 👋\n\n"
        "This is the SAT Zone verification bot. To receive your login code here, "
        "tap the button below to share your phone number.\n\n"
        "We only use it to match your Telegram account to your SAT Zone profile.",
        reply_markup=_share_phone_keyboard(),
    )


@router.message(F.contact)
async def handle_contact(message: Message, api_client: ApiClient) -> None:
    """Handle a shared contact: look up the user, then either greet or issue an OTP.

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

    phone = contact.phone_number

    # Step 1: returning verified user? Greet and stop.
    try:
        user = await api_client.lookup_user_by_phone(phone)
    except ApiClientError:
        logger.warning("handle_contact.lookup_failed", chat_id=chat_id)
        await message.answer(
            "Sorry, something went wrong. Please try again in a moment.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if user is not None and user.get("is_phone_verified"):
        full_name = (user.get("full_name") or "").strip() or "there"
        logger.info("handle_contact.returning_user", chat_id=chat_id)
        await message.answer(
            f"Welcome back, <b>{full_name}</b>! 👋\n\n"
            "Your phone is already linked to your SAT Zone account. "
            "You can sign in on the website any time.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Step 2: new (or unverified) user → mint a fresh OTP and show it.
    try:
        result = await api_client.issue_otp(phone)
    except PhoneTakenError:
        logger.info("handle_contact.phone_taken", chat_id=chat_id)
        await message.answer(
            "This phone number is already verified on another SAT Zone account.\n\n"
            "Please log in to that account, or use a different number.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    except ApiClientError:
        logger.warning("handle_contact.issue_otp_failed", chat_id=chat_id)
        await message.answer(
            "Sorry, something went wrong issuing your code. "
            "Please try again in a moment.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    otp = result.get("otp")
    if not otp:
        logger.error("handle_contact.missing_otp", chat_id=chat_id)
        await message.answer(
            "We couldn't generate a code right now. Please try again shortly.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    minutes = max(1, int(result.get("expires_in", 0)) // 60)
    logger.info("handle_contact.otp_issued", chat_id=chat_id)
    await message.answer(
        "✅ Your phone number is verified!\n\n"
        f"Your SAT Zone verification code is:\n\n<code>{otp}</code>\n\n"
        f"Enter it on the website within {minutes} minutes to finish signing in. "
        "Don't share it with anyone.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message()
async def handle_fallback(message: Message) -> None:
    """Nudge users who type free-text toward the share-phone flow."""
    await message.answer(
        "To get your verification code, tap the button below to share your phone "
        "number. Typed numbers aren't accepted — Telegram has to send the verified "
        "contact for us. If you don't see the button, send /start.",
        reply_markup=_share_phone_keyboard(),
    )
