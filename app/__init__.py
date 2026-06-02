"""SAT Zone Telegram bot service.

A standalone aiogram v3 service that links a user's phone number to their
Telegram ``chat_id`` and relays one-time codes. It NEVER imports the backend
application — it talks to the API exclusively over HTTP (see ``api_client``)
and exposes a small internal HTTP endpoint for the API to push codes to.
"""

from __future__ import annotations

__version__ = "0.1.0"
