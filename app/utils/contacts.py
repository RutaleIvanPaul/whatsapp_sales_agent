from __future__ import annotations

import asyncio

import httpx

from app.models.operator import Operator
from app.utils.crypto import decrypt
from app.utils.log import log
from app.utils.phone import from_whapi

WHAPI_BASE = "https://gate.whapi.cloud"
HTTP_TIMEOUT = 15.0


class ContactsCache:
    """Per-operator cache of the operator's saved WhatsApp contacts.

    The bot must not respond to messages from the operator's personal contacts.
    Loaded at startup and refreshed hourly. On Whapi API failure, serves stale
    cache rather than opening the privacy boundary.
    """

    def __init__(self, encryption_key: bytes) -> None:
        self._key = encryption_key
        self._contacts: dict[str, set[str]] = {}

    def is_contact(self, operator_id: str, phone: str) -> bool:
        return phone in self._contacts.get(operator_id, set())

    async def load_for_operator(self, operator: Operator) -> None:
        token = decrypt(operator.whapi_channel_token, self._key)
        url = f"{WHAPI_BASE}/contacts?token={token}"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    log(
                        "error",
                        component="contacts",
                        error_type="load_failed",
                        message=f"http_{resp.status_code}",
                        operator_id=operator.operator_id,
                    )
                    return

                data = resp.json()
                raw_contacts = data.get("contacts", [])
                phones: set[str] = set()
                for c in raw_contacts:
                    raw = c.get("id") or c.get("phone") or ""
                    if not raw:
                        continue
                    try:
                        phones.add(from_whapi(raw))
                    except ValueError:
                        continue

                self._contacts[operator.operator_id] = phones
                log(
                    "contacts_loaded",
                    operator_id=operator.operator_id,
                    contact_count=len(phones),
                )
        except Exception as e:
            log(
                "error",
                component="contacts",
                error_type="load_exception",
                message=type(e).__name__,
                operator_id=operator.operator_id,
            )

    async def start_refresh(
        self, operators: list[Operator], interval_s: int = 3600
    ) -> None:
        while True:
            await asyncio.sleep(interval_s)
            for operator in operators:
                await self.load_for_operator(operator)
