# Backend Contract for the SAT Zone Telegram Bot

This document is the **source of truth for what the backend must implement** so
the `auth_bot` service in this repo works. Hand it to backend Claude and tell it
to make the API match. The bot is intentionally dumb: no database, no business
logic, no user model. Everything below is enforced by code in this repo (paths
referenced inline) — do **not** change the contract here without updating both
sides.

---

## 1. Shared secret authentication

Both directions of the bot ↔ API channel are authenticated by a single shared
API key, sent as the HTTP header `X-Internal-Secret`. The bot reads it from
`INTERNAL_SECRET` (env var, validated `min_length=16` in `app/config.py:33`).
The backend **must** read the same value from its `TELEGRAM_INTERNAL_SECRET`
env var and compare it in constant time (the bot uses `secrets.compare_digest`
in `app/internal_server.py:70`).

- If the header is missing or wrong → return `401 Unauthorized` with no body
  that differentiates between "no key" and "wrong key".
- The key is symmetric (same value on both sides). No JWT, no rotation logic,
  no per-request signing. Rotate by changing the env var and restarting both
  services.

---

## 2. Endpoint the **backend must expose** (bot → API)

### `POST /api/v1/auth/telegram/link-contact`

Called by the bot when a Telegram user taps the *Share phone number* button.
The path is hardcoded in `app/api_client.py:27`.

**Headers**
- `X-Internal-Secret: <shared key>`
- `Content-Type: application/json`

**Request body**
```json
{
  "chat_id": 123456789,
  "phone": "+15551234567",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "state": "opaque-deep-link-token-or-null"
}
```

- `chat_id` (int) — Telegram chat ID. Use it as the routing key when you later
  push OTPs (see §3). Persist it on the user record.
- `phone` (string) — E.164 phone number, **verified by Telegram** (the bot
  refuses anything that isn't a real `request_contact` share matching the
  sender). Treat as authoritative; do not re-verify by SMS.
- `first_name`, `last_name` (string | null) — taken from the contact card,
  helpful for personalising the account but not authoritative.
- `state` (string | null) — present **only** when the user arrived via a
  deep link `t.me/<bot>?start=<state>`. The frontend mints this token while
  the user is logged in and embeds it in the deep link; if you see it, link
  this Telegram chat to the user identified by that token. If `state` is
  null/missing, the user is signing in (not linking an existing session).

**Response (two modes, both `200 OK`)**

The bot branches on the response shape in `app/handlers.py:160`:

- **Sign-in mode** (no `state`, or `state` was unknown/expired):
  ```json
  {"code": "847163", "expires_in": 300}
  ```
  The bot displays this code to the user, who types it on the website to
  finish signing in. `code` is required; `expires_in` (seconds) is currently
  not read by the bot but should be sent for symmetry with §3.

- **Link mode** (valid `state` resolved to a session):
  ```json
  {"linked": true}
  ```
  The bot shows a "your account is linked" confirmation. No code is delivered
  in this branch — the user is already authenticated in their browser.

**Error responses**

Any non-2xx is surfaced to the user as a generic "something went wrong" message
(see `app/handlers.py:152`). Use:

- `400 Bad Request` — malformed payload (missing `chat_id` / `phone`).
- `409 Conflict` — phone number belongs to a different Telegram chat. The bot
  has no special UX for this; the friendly message is fine.
- `429 Too Many Requests` — apply your own per-IP / per-phone rate limit.
- `5xx` — transport failures are retried by the bot up to 3 times with
  exponential backoff (250ms, 750ms — `app/api_client.py:33`). Make the
  endpoint idempotent on `chat_id` so retries don't create duplicate users.

---

## 3. Endpoint the **bot exposes** (API → bot)

### `POST {BOT_INTERNAL_URL}/internal/send-otp`

Called by the backend whenever it needs to deliver a code to a user whose
Telegram is already linked. The bot has no idea why — purpose is opaque to it.

The bot's address is whatever the backend resolves it to inside the private
network (e.g. `http://auth_bot:8081` in Docker Compose). The bot binds to
`INTERNAL_HOST:INTERNAL_PORT` (default `0.0.0.0:8081` — see
`app/config.py:36`). **This port must never be publicly reachable.**

**Headers**
- `X-Internal-Secret: <shared key>`
- `Content-Type: application/json`

**Request body**
```json
{
  "chat_id": 123456789,
  "code": "847163",
  "purpose": "login"
}
```

- `chat_id` (int) — the value previously returned from link-contact.
- `code` (string) — the OTP to display. The bot does not generate, validate
  or expire it; that's the backend's job.
- `purpose` (string) — free-form label for logs (`"login"`, `"reset"`, etc.).
  Currently informational only; the user-facing message text is the same
  regardless (see `app/internal_server.py:99`).

**Responses (the bot returns)**
- `204 No Content` — code was handed to Telegram successfully.
- `400 Bad Request` — body missing / wrong types.
- `401 Unauthorized` — wrong or missing `X-Internal-Secret`.
- `429 Too Many Requests` — per-chat rate limit hit (`OTP_RATE_LIMIT_PER_MIN`,
  default 10/min — `app/config.py:43`). The backend should treat this as a
  soft failure: back off and retry later, or surface a "try again in a moment"
  to the user.
- `502 Bad Gateway` — Telegram rejected the send (chat blocked the bot, was
  deleted, etc.). The chat is effectively unreachable; the backend should
  mark the link as broken and prompt the user to re-link.
- `504 Gateway Timeout` — Telegram didn't respond within `HTTP_TIMEOUT_SECONDS`
  (default 10s). Safe to retry.

---

## 4. Liveness probe

### `GET {BOT_INTERNAL_URL}/healthz`
Unauthenticated, returns `200 {"status":"ok"}`. Wire it up to your k8s
`livenessProbe` / Compose healthcheck. Nothing else hits this.

---

## 5. End-to-end flow the backend must support

```
                                            ┌─────────┐
   Frontend (logged-in or not)              │  USER   │
   shows deep link or QR:                   └────┬────┘
   t.me/<bot>?start=<state>                      │ taps link, taps /start,
                                                 │ taps "Share phone"
                                                 ▼
                                            ┌─────────┐
                                            │   BOT   │
                                            └────┬────┘
                                                 │ POST /api/v1/auth/telegram/link-contact
                                                 │ {chat_id, phone, first_name, last_name, state?}
                                                 ▼
                                            ┌─────────┐
                                            │   API   │  resolve `state` → user?
                                            │         │   yes → attach chat_id, return {linked:true}
                                            │         │   no  → upsert(phone), mint OTP,
                                            │         │         return {code, expires_in}
                                            └────┬────┘
                                                 ▼
                                            ┌─────────┐
                                            │   BOT   │  shows code OR "linked" message
                                            └─────────┘

   Later: backend needs to push a fresh OTP to a known chat.

                                            ┌─────────┐
                                            │   API   │
                                            └────┬────┘
                                                 │ POST /internal/send-otp
                                                 │ {chat_id, code, purpose}
                                                 ▼
                                            ┌─────────┐
                                            │   BOT   │  bot.send_message(chat_id, ...)
                                            └────┬────┘
                                                 │ Telegram
                                                 ▼
                                            ┌─────────┐
                                            │  USER   │
                                            └─────────┘
```

---

## 6. State the backend must own

The bot owns **nothing durable**. Specifically, the backend must persist:

- `chat_id ↔ user_id` mapping — the bot will only ever send you a `chat_id`
  (plus the phone the first time). Without this mapping there's no way to
  later route an OTP back.
- `phone ↔ user_id` mapping — for sign-in mode you upsert by phone.
- `state` tokens — issued by the frontend when a logged-in user starts the
  link flow, must be redeemable exactly once, with a short TTL (the bot keeps
  the chat-side copy for 15 minutes by default — `LINK_STATE_TTL_SECONDS`).
- OTP issuance, validity windows, and verification. The bot is a display
  surface; it does not know which codes are live.

---

## 7. Things to NOT ask the backend to do

These are out of scope on the API side because the bot already handles them:

- Phone format validation / SMS re-verification — Telegram has already done it.
- Rate-limiting OTP **deliveries** to Telegram — the bot enforces this per
  chat (`app/internal_server.py:40`). The backend should still rate-limit OTP
  **issuance** to protect itself.
- Holding the Telegram session, retrying Telegram errors, or knowing the bot
  token — none of these leave this service.
- Long-polling, webhooks, or any direct interaction with `api.telegram.org`.

---

## 8. Config the backend needs

| Env var | Purpose | Must match |
| --- | --- | --- |
| `TELEGRAM_INTERNAL_SECRET` | Shared API key for both directions. | Bot's `INTERNAL_SECRET` |
| `TELEGRAM_BOT_INTERNAL_URL` | Where to POST `/internal/send-otp`. | Bot's `INTERNAL_HOST:INTERNAL_PORT` reachable on the private network |
| `TELEGRAM_BOT_USERNAME` | Used to build deep links in the frontend. | Bot's `BOT_USERNAME` |

The bot's full config schema is in `app/config.py` and the template is
`.env.example`.
