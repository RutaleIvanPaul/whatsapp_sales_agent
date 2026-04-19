"""Track message IDs sent by the bot so we can distinguish bot-sent echoes
from operator-typed messages when Whapi fires `from_me: true` webhooks.

Whapi echoes every message sent via the API back as a webhook with
`from_me: true`. The operator typing in the same thread also arrives as
`from_me: true`. The only way to distinguish them is by checking whether
we sent that message ID ourselves.

TECH DEBT: If the Whapi API accepts our POST but we fail to read the
response body (network drop after server-side accept), the message ID
won't be captured here. The echo will then be treated as operator typing,
causing a false OWNER_ACTIVE flip. This is extremely rare (HTTP responses
arrive atomically) and self-healing (operator can `resume`). A future fix
could add a zero-width Unicode marker as a secondary signal, but WhatsApp
may strip it. Tracked as accepted risk.
"""

from __future__ import annotations

import time
import threading

TTL_SECONDS = 60


class SentTracker:
    """Thread-safe set of recently sent message IDs with auto-expiry."""

    def __init__(self, ttl_s: int = TTL_SECONDS) -> None:
        self._ttl = ttl_s
        self._ids: dict[str, float] = {}
        self._lock = threading.Lock()

    def register(self, message_id: str) -> None:
        """Record a message ID we just sent via the API."""
        with self._lock:
            self._ids[message_id] = time.monotonic()

    def is_bot_sent(self, message_id: str) -> bool:
        """Check if this message ID was sent by us. Also purges expired entries."""
        with self._lock:
            self._purge()
            return message_id in self._ids

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [mid for mid, ts in self._ids.items() if now - ts > self._ttl]
        for mid in expired:
            del self._ids[mid]


# Module-level singleton — shared across messaging adapter and receiver.
sent_tracker = SentTracker()
