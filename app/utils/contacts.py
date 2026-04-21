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
        base_url = f"{WHAPI_BASE}/contacts?token={token}"
        try:
            phones: set[str] = set()
            page_size = 500
            offset = 0
            load_complete = True

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                while True:
                    url = f"{base_url}&count={page_size}&offset={offset}"
                    resp = await client.get(url)
                    if resp.status_code >= 400:
                        log(
                            "error",
                            component="contacts",
                            error_type="load_failed",
                            message=f"http_{resp.status_code}",
                            operator_id=operator.operator_id,
                        )
                        load_complete = False
                        break

                    data = resp.json()
                    batch = data.get("contacts", [])
                    for c in batch:
                        raw = c.get("id") or c.get("phone") or ""
                        if not raw:
                            continue
                        try:
                            phones.add(from_whapi(raw))
                        except ValueError:
                            continue

                    if len(batch) < page_size:
                        break  # last page
                    offset += page_size

            if load_complete:
                self._contacts[operator.operator_id] = phones
                log(
                    "contacts_loaded",
                    operator_id=operator.operator_id,
                    contact_count=len(phones),
                )
            else:
                # Keep stale cache if it exists — better than partial data
                log(
                    "contacts_load_partial",
                    operator_id=operator.operator_id,
                    reason="http_error_during_pagination",
                    stale_count=len(self._contacts.get(operator.operator_id, set())),
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
