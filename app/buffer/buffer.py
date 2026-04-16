from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from app.utils.log import log
from app.utils.phone import hash_for_log

# Callback signature: on_flush(operator_id, phone, payloads) -> Awaitable
OnFlushCallback = Callable[[str, str, list[dict]], Awaitable[None]]


class MessageBuffer:
    """Per-user message buffer with debounce and rate limiting.

    Keyed by (operator_id, phone). Accumulates raw Whapi payloads and flushes
    them together after a short debounce window, or immediately when the buffer
    reaches the force-flush threshold.

    Rate limit ensures we don't hammer the pipeline with back-to-back flushes
    for the same user.
    """

    def __init__(
        self,
        debounce_s: float = 3.0,
        rate_limit_s: float = 8.0,
        force_flush_at: int = 10,
    ) -> None:
        self._debounce_s = debounce_s
        self._rate_limit_s = rate_limit_s
        self._force_flush_at = force_flush_at
        self._payloads: dict[tuple[str, str], list[dict]] = {}
        self._timers: dict[tuple[str, str], asyncio.Task] = {}
        self._last_flush: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        operator_id: str,
        phone: str,
        payload: dict,
        on_flush: OnFlushCallback,
    ) -> None:
        key = (operator_id, phone)

        async with self._lock:
            self._payloads.setdefault(key, []).append(payload)
            existing = self._timers.pop(key, None)
            if existing is not None and not existing.done():
                existing.cancel()

            should_force_flush = len(self._payloads[key]) >= self._force_flush_at

        if should_force_flush:
            await self._flush(operator_id, phone, on_flush)
        else:
            # Schedule debounced flush
            task = asyncio.create_task(self._debounced_flush(operator_id, phone, on_flush))
            async with self._lock:
                self._timers[key] = task

    async def _debounced_flush(
        self,
        operator_id: str,
        phone: str,
        on_flush: OnFlushCallback,
    ) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
            await self._flush(operator_id, phone, on_flush)
        except asyncio.CancelledError:
            pass

    async def _flush(
        self,
        operator_id: str,
        phone: str,
        on_flush: OnFlushCallback,
    ) -> None:
        key = (operator_id, phone)

        # Rate limit: wait out the remaining window if we flushed recently
        last = self._last_flush.get(key)
        if last is not None:
            delta = time.monotonic() - last
            if delta < self._rate_limit_s:
                await asyncio.sleep(self._rate_limit_s - delta)

        async with self._lock:
            payloads = self._payloads.pop(key, [])
            self._timers.pop(key, None)
            self._last_flush[key] = time.monotonic()

        if not payloads:
            return

        log(
            "buffer_flushed",
            operator_id=operator_id,
            phone_hash=hash_for_log(phone),
            message_count=len(payloads),
        )
        try:
            await on_flush(operator_id, phone, payloads)
        except Exception as e:
            log(
                "error",
                component="buffer",
                error_type="on_flush_failed",
                message=type(e).__name__,
                operator_id=operator_id,
            )

    def handle_deletion(
        self, operator_id: str, phone: str, message_id: str
    ) -> None:
        key = (operator_id, phone)
        buffered = self._payloads.get(key, [])
        for i, payload in enumerate(buffered):
            msgs = payload.get("messages", [])
            if msgs and msgs[0].get("id") == message_id:
                buffered.pop(i)
                log(
                    "message_deleted_from_buffer",
                    operator_id=operator_id,
                    message_id=message_id,
                )
                return

        log(
            "message_deletion_ignored",
            operator_id=operator_id,
            message_id=message_id,
            reason="already_flushed_or_unknown",
        )
