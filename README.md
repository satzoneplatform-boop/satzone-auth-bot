# SAT Zone — Telegram Bot

A standalone [aiogram v3](https://docs.aiogram.dev/) service that links a
user's phone number to their Telegram `chat_id` and relays one-time
verification codes.

It is **decoupled from the backend**: it never imports the API package and
talks to it only over HTTP, authenticated by a shared API key
(`INTERNAL_SECRET`).

## Flow

1. **Frontend hands the user the bot.** A link like
   `https://t.me/<BOT_USERNAME>?start=<state>` (optional `state` ties the chat
   to an already-logged-in account).
2. **User taps `/start`.** The bot shows a one-tap *Share phone number* button
   (Telegram's `request_contact` keyboard — the only way to obtain a
   *Telegram-verified* phone number; typed numbers are rejected).
3. **User shares their contact.** The bot calls
   `POST /api/v1/auth/telegram/link-contact` on the backend with the chat ID
   and phone, authenticated by `X-Internal-Secret`.
4. **Backend stores the phone, mints an OTP, returns it.** The bot displays the
   OTP in chat. The user enters it on the website to verify.
5. **Subsequent OTPs** the backend needs to deliver are POSTed to
   `POST /internal/send-otp` on this service; the bot relays them to the chat.

## Cross-service contract

| Direction | Endpoint | Auth header | Body |
| --- | --- | --- | --- |
| Bot → API | `POST {API_BASE_URL}/api/v1/auth/telegram/link-contact` | `X-Internal-Secret` | `{chat_id, phone, first_name, last_name, state?}` → `200 {code, expires_in}` or `200 {linked: true}` |
| API → Bot | `POST /internal/send-otp` | `X-Internal-Secret` | `{chat_id, code, purpose}` → `204` |
| (anyone) | `GET /healthz` | — | `200 {"status":"ok"}` (liveness probe) |

## Configuration

Copy `.env.example` to `.env` and fill in the values. The `INTERNAL_SECRET`
API key must match the backend's `TELEGRAM_INTERNAL_SECRET`. Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Tunables (`LOG_LEVEL`, `HTTP_TIMEOUT_SECONDS`, `LINK_STATE_TTL_SECONDS`,
`OTP_RATE_LIMIT_PER_MIN`) have safe defaults; see `.env.example` for the
shipped values.

## Network isolation

`INTERNAL_HOST=0.0.0.0` is safe **only** when the service is reachable from
the backend over a private network (e.g. a Docker Compose network or a k8s
`ClusterIP` Service). The `/internal/send-otp` endpoint is authenticated by
the shared API key but should not be exposed publicly.

## Operational notes

- **Single replica only.** Telegram allows one long-polling consumer per bot
  token, and per-chat deep-link state is in-memory. Switch to webhooks plus
  external state (e.g. Redis) before scaling out.
- **Graceful shutdown.** `SIGTERM` and `SIGINT` stop polling, close the
  internal HTTP server, drain in-flight retries, and close both HTTP pools.
- **Rate limiting.** OTP pushes are capped per chat (`OTP_RATE_LIMIT_PER_MIN`)
  to keep a misbehaving backend from tripping Telegram flood limits.

## Run

```bash
pip install .
python -m app.main
```

Or via Docker:

```bash
docker build -t satzone-bot .
docker run --env-file .env -p 8081:8081 satzone-bot
```

For reproducible builds, generate a lockfile from `pyproject.toml` (e.g.
`uv lock` or `pip-compile`) and pin it in your deployment image.
