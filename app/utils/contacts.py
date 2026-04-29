from __future__ import annotations

import asyncio

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.utils.log import log


class ContactsCache:
    """Per-operator cache of the operator's saved WhatsApp contacts.

    The bot must not respond to messages from the operator's personal contacts.
    Loaded at startup and refreshed hourly. On provider API failure, serves stale
    cache rather than opening the privacy boundary.
    """

    def __init__(self, messaging: MessagingAdapter) -> None:
        self._messaging = messaging
        self._contacts: dict[str, set[str]] = {}

    def is_contact(self, operator_id: str, phone: str) -> bool:
        return phone in self._contacts.get(operator_id, set())

    async def load_for_operator(self, operator: Operator) -> None:
        try:
            phones = await self._messaging.get_contacts(operator)
            self._contacts[operator.operator_id] = phones
            log(
                "contacts_loaded",
                operator_id=operator.operator_id,
                contact_count=len(phones),
            )
        except Exception as e:
            # Keep stale cache if it exists — better than no data
            log(
                "error",
                component="contacts",
                error_type="load_exception",
                message=type(e).__name__,
                operator_id=operator.operator_id,
                stale_count=len(self._contacts.get(operator.operator_id, set())),
            )

    async def start_refresh(
        self, operators: list[Operator], interval_s: int = 3600
    ) -> None:
        while True:
            await asyncio.sleep(interval_s)
            for operator in operators:
                await self.load_for_operator(operator)
