# Backend Contract for the SAT Zone Telegram Bot

This document is the **source of truth for what the backend must implement** so
the `auth_bot` service in this repo works. The bot is intentionally dumb: no
database, no business logic, no user model. Everything below is enforced by
code in this repo (paths cited inline) — do **not** change the contract here
without updating both sides.

---

## 1. Shared API key authentication

Both calls from bot → backend are authenticated by a single shared API key,
sent as the HTTP header `X-Internal-API-Key`. The bot reads it from
`INTERNAL_API_KEY` (env var, validated `min_length=16` in `app/config.py:34`).
The backend's value must be byte-identical.

- If the header is missing or wrong → return `401 { "code": "invalid_api_key" }`.
- If the backend was started without an API key configured → return
  `401 { "code": "internal_not_configured" }`.
- Rotation: change the env var on both sides and restart. No overlap window.

The bot does **not** expose any authenticated endpoint of its own (just an
unauthenticated `/healthz` for liveness). The backend never calls the bot.

---

## 2. Endpoints the backend must expose (bot → API)

### 2.1 `POST /api/v1/internal/users/lookup-by-phone`

Called by the bot first, immediately after the user shares their contact, to
detect a returning verified user (so we don't mint an OTP for someone who is
already linked).

**Headers**
- `X-Internal-API-Key: <shared key>`
- `Content-Type: application/json`

**Request body**
```json
{ "phone_number": "+998901234567" }
```
- `phone_number` (string) — E.164. Backend normalizes (strips spaces/dashes/parens).

**Success — `200 OK`**
```json
{
  "id": "8b5e...uuid",
  "email": "user@example.com",
  "full_name": "Jane Doe",
  "is_active": true,
  "is_phone_verified": true
}
```

The bot reads only `full_name` (for the greeting) and `is_phone_verified` (to
decide the branch — see `app/handlers.py:115`).

**Errors**

| HTTP | code | bot reaction |
| --- | --- | --- |
| `404` | `user_not_found` | Treated as the "new user" signal — bot falls through to issue-OTP. NOT an error. (`app/api_client.py:74`) |
| `401` | `internal_not_configured` / `invalid_api_key` | Logged + generic apology to user. |
| `422` | validation error | Same. |
| `5xx` | — | Bot retries transport errors up to 3 times (250ms, 750ms backoff — `app/api_client.py:33`). |

### 2.2 `POST /api/v1/internal/phone/issue-otp`

Called by the bot if the lookup returned `404` or `is_phone_verified=false`.
Mints a fresh OTP for the phone, invalidating any previous one.

**Headers**
- `X-Internal-API-Key: <shared key>`
- `Content-Type: application/json`

**Request body**
```json
{ "phone_number": "+998901234567" }
```

**Success — `200 OK`**
```json
{ "otp": "48372910", "expires_in": 900 }
```
- `otp` (string) — 8-digit numeric. Shown verbatim to the user.
- `expires_in` (int) — seconds until the OTP expires (default 900 = 15 min).
  The bot rounds to minutes in the user-facing message (`app/handlers.py:160`).

**Errors**

| HTTP | code | bot reaction |
| --- | --- | --- |
| `409` | `phone_taken` | Bot tells the user the number is on another account. (`app/api_client.py:97`) |
| `401` | `internal_not_configured` / `invalid_api_key` | Logged + generic apology. |
| `422` | validation error | Logged + generic apology. |
| `5xx` | — | Same retry policy as lookup. |

**Idempotency note.** Re-calling with the same `phone_number` MUST issue a
fresh OTP and invalidate the prior one. The bot's per-chat throttle
(default 3 shares per 5 min — `CONTACT_RATE_LIMIT_MAX` in `app/config.py:46`)
prevents abuse of that behaviour.

---

## 3. End-to-end flow

```
┌─────────┐  taps /start, taps "Share phone"   ┌─────────┐
│  USER   │ ─────────────────────────────────▶ │   BOT   │
└─────────┘                                    └────┬────┘
                                                    │ POST /lookup-by-phone {phone}
                                                    ▼
                                               ┌─────────┐
                                               │   API   │
                                               └────┬────┘
                  ┌─────────── 200 + verified ──────┤
                  │                                 │
                  │                                 └── 404 user_not_found ──┐
                  ▼                                                          ▼
        "Welcome back, Jane!"                              ┌─────────┐
        (done; no OTP)                                     │   BOT   │
                                                           └────┬────┘
                                                                │ POST /issue-otp {phone}
                                                                ▼
                                                           ┌─────────┐
                                                           │   API   │
                                                           └────┬────┘
                  ┌────── 409 phone_taken ──────┐              │
                  │                             │              │
                  │                             │              └── 200 {otp,expires_in} ──┐
                  ▼                             ▼                                          ▼
        "Use a different number"     (server error → generic apology)      "Your code is 12345678"
                                                                                          │
                                                                                          ▼
                                                                                    USER types
                                                                                    OTP on the
                                                                                    WEBSITE,
                                                                                    which calls
                                                                                    /auth/verify-phone
                                                                                    (bearer-authed,
                                                                                    binds phone
                                                                                    to that user)
```

---

## 4. State the backend must own

The bot owns **nothing durable**. Specifically the backend must persist /
manage:

- The OTP itself (Redis or DB), keyed such that the verify endpoint can read
  the phone out by OTP.
- `phone ↔ user_id` binding (created on `/auth/verify-phone`).
- IP-based rate limiting on the public `/auth/verify-phone` endpoint (the OTP
  is the entire binding token — see anti-mistake notes below).

The bot tracks no chat_id ↔ user mapping. The verify endpoint binds the phone
to whichever logged-in user submits the OTP — Telegram identity is not used
in the binding step.

---

## 5. Things the bot already handles — don't ask the backend to do them

- Phone format validation / SMS re-verification — Telegram already verified.
- Per-chat throttling of OTP requests — `app/handlers.py:99` caps it.
- Rejecting forwarded / fake contact cards — `app/handlers.py:91`.
- Retrying transport errors — `app/api_client.py:117`.

---

## 6. Liveness probe

### `GET {BOT_INTERNAL_URL}/healthz`
Unauthenticated, `200 {"status":"ok"}`. Used by the Dockerfile's `HEALTHCHECK`.
The backend doesn't need to call it.

---

## 7. Anti-mistake notes (from the backend contract)

- **The OTP is the entire binding token.** Anyone who knows the 8-digit code
  can claim that phone by typing it on the website while logged in. The
  defences are: (a) 10⁸ space, (b) 15-min TTL, (c) IP rate-limiting on the
  verify endpoint, (d) the bot never logs the OTP.
- **The bot must never expose `lookup-by-phone` or `issue-otp` to end users.**
  They are bot-server-only. Frontend code never calls them.
- **`INTERNAL_API_KEY` rotation is hard-cut.** No overlap window — change on
  the API, restart, then change on the bot, restart.

---

## 8. Config the backend needs

| Env var | Purpose | Must match |
| --- | --- | --- |
| `INTERNAL_API_KEY` | Shared API key for bot → API calls. | Bot's `INTERNAL_API_KEY` |
| `PHONE_CODE_LENGTH` | OTP digit count. | (Bot just displays it; any length works.) |
| `PHONE_VERIFY_EXPIRE_MINUTES` | OTP TTL. | (Bot reports `expires_in` to the user.) |

The bot's full config schema is in `app/config.py`; the template is
`.env.example`.
