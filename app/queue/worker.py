from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from app.buffer.buffer import MessageBuffer
from app.models.operator import Operator
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log

DEDUP_TTL_S = 600

# on_flush callback signature: (operator_id, phone, payloads) -> Awaitable
# The callback is built in main.py with adapters captured in its closure.
OnFlushCallback = Callable[[str, str, list[dict]], Awaitable[None]]

# flush_for_operator callback signature: (payloads, operator) -> Awaitable
# This is the actual pipeline invocation; the worker needs the operator
# reference that came off the queue, not just operator_id.
FlushForOperator = Callable[[list[dict], Operator], Awaitable[None]]


class QueueWorker:
    def __init__(
        self,
        queue: asyncio.Queue,
        buffer: MessageBuffer,
        flush_for_operator: FlushForOperator,
    ) -> None:
        self._queue = queue
        self._buffer = buffer
        self._flush_for_operator = flush_for_operator
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._seen_ids: dict[str, float] = {}
        self._operator_refs: dict[tuple[str, str], Operator] = {}

    async def run(self) -> None:
        while True:
            try:
                payload, operator = await self._queue.get()
                await self._handle(payload, operator)
            except Exception as e:
                log(
                    "error",
                    component="worker",
                    error_type="handle_exception",
                    message=type(e).__name__,
                )

    async def _handle(self, payload: dict, operator: Operator) -> None:
        self._purge_expired()

        msgs = payload.get("messages", [])
        if not msgs:
            return
        msg = msgs[0]
        message_id = msg.get("id", "")
        raw_phone = msg.get("from", "")

        if not message_id or not raw_phone:
            log(
                "message_discarded",
                reason="missing_id_or_from",
                operator_id=operator.operator_id,
            )
            return

        if message_id in self._seen_ids:
            log(
                "message_deduplicated",
                message_id=message_id,
                operator_id=operator.operator_id,
            )
            return

        self._seen_ids[message_id] = time.monotonic()

        try:
            phone = from_whapi(raw_phone)
        except ValueError:
            log(
                "message_discarded",
                reason="invalid_phone",
                message_id=message_id,
                operator_id=operator.operator_id,
            )
            return

        log(
            "message_received",
            operator_id=operator.operator_id,
            phone_hash=hash_for_log(phone),
            message_id=message_id,
            type=msg.get("type", "unknown"),
            from_me=msg.get("from_me", False),
        )

        key = (operator.operator_id, phone)
        self._operator_refs[key] = operator  # used by the flush callback

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._buffer.add(
                operator.operator_id,
                phone,
                payload,
                self._on_flush_from_buffer,
            )

    async def _on_flush_from_buffer(
        self, operator_id: str, phone: str, payloads: list[dict]
    ) -> None:
        key = (operator_id, phone)
        operator = self._operator_refs.get(key)
        if operator is None:
            log(
                "error",
                component="worker",
                error_type="missing_operator_ref",
                operator_id=operator_id,
            )
            return
        await self._flush_for_operator(payloads, operator)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [mid for mid, ts in self._seen_ids.items() if now - ts > DEDUP_TTL_S]
        for mid in expired:
            del self._seen_ids[mid]
