# SAT Zone — Telegram Bot

A standalone [aiogram v3](https://docs.aiogram.dev/) service that lets users
verify their phone number via Telegram and receive a one-time code to type on
the SAT Zone website.

It is **decoupled from the backend**: it never imports the API package and
talks to it only over HTTP, authenticated by a shared API key
(`INTERNAL_API_KEY`). See [`BACKEND_CONTRACT.md`](BACKEND_CONTRACT.md) for the
full API contract.

## Flow

1. **User opens the bot** (`https://t.me/<BOT_USERNAME>`) and taps `/start`.
2. **Bot shows a one-tap *Share phone number* button** (Telegram's
   `request_contact` keyboard — the only way to obtain a *Telegram-verified*
   phone number; typed numbers are rejected).
3. **User shares their contact.** The bot calls:
   - `POST /api/v1/internal/users/lookup-by-phone` — if a verified user already
     exists for this phone, the bot greets them and stops.
   - Otherwise: `POST /api/v1/internal/phone/issue-otp` — the backend mints a
     fresh OTP, the bot displays it in chat.
4. **User types the OTP on the website** while logged in. The frontend calls
   the backend's `/auth/verify-phone` to bind the phone to that account.

## Cross-service contract (summary)

| Direction | Endpoint | Auth header | Body |
| --- | --- | --- | --- |
| Bot → API | `POST {API_BASE_URL}/api/v1/internal/users/lookup-by-phone` | `X-Internal-API-Key` | `{phone_number}` → `200 {id, email, full_name, is_phone_verified, ...}` or `404 user_not_found` |
| Bot → API | `POST {API_BASE_URL}/api/v1/internal/phone/issue-otp` | `X-Internal-API-Key` | `{phone_number}` → `200 {otp, expires_in}` or `409 phone_taken` |
| (anyone) | `GET /healthz` | — | `200 {"status":"ok"}` (liveness probe) |

The bot exposes **no authenticated endpoints** — the backend never calls it.

## Configuration

Copy `.env.example` to `.env` and fill in the values. The `INTERNAL_API_KEY`
must match the backend's `INTERNAL_API_KEY`. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Tunables (`LOG_LEVEL`, `HTTP_TIMEOUT_SECONDS`, `CONTACT_RATE_LIMIT_MAX`,
`CONTACT_RATE_LIMIT_WINDOW_SECONDS`) have safe defaults; see `.env.example`
for the shipped values.

## Network isolation

`INTERNAL_HOST=0.0.0.0` is safe **only** when the service is reachable from
the backend over a private network (e.g. a Docker Compose network or a k8s
`ClusterIP` Service). When running locally, prefer publishing the port on
loopback only: `docker run -p 127.0.0.1:8081:8081 ...`.

## Operational notes

- **Single replica only.** Telegram allows one long-polling consumer per bot
  token. Switch to webhooks before scaling out.
- **Graceful shutdown.** `SIGTERM` and `SIGINT` stop polling, close the
  internal HTTP server, drain in-flight retries, and close both HTTP pools.
- **Rate limiting.** User-initiated OTP requests (contact shares) are capped
  per chat (`CONTACT_RATE_LIMIT_MAX` per `CONTACT_RATE_LIMIT_WINDOW_SECONDS`,
  default 3 per 5 min) so a user can't spam the backend with fresh-code
  requests.
- **OTPs are never logged.** Logs record `chat_id` + `expires_in` only.

## Run

```bash
pip install .
python -m app.main
```

Or via Docker:

```bash
docker build -t satzone-bot .
docker run --env-file .env -p 127.0.0.1:8081:8081 satzone-bot
```

For reproducible builds, generate a lockfile from `pyproject.toml` (e.g.
`uv lock` or `pip-compile`) and pin it in your deployment image.
