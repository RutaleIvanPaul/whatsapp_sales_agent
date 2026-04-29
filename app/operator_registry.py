"""Lazy operator registration.

The FastAPI startup hook bulk-loads operators from SQLite into memory
for low-latency webhook dispatch. But new operators can be onboarded
while the server is live — the registry here handles that by lazy-
loading from the DB and initialising per-operator caches on first
webhook hit.

Thread-safe via an asyncio lock so two concurrent webhooks for the
same brand-new operator don't both kick off a Sheets load.
"""
from __future__ import annotations

import asyncio

from app.adapters.inventory.cache import InventoryCache
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.models.operator import Operator
from app.utils.log import log

_lazy_load_locks: dict[str, asyncio.Lock] = {}
_background_tasks: set[asyncio.Task] = set()


def _lock_for(channel_id: str) -> asyncio.Lock:
    if channel_id not in _lazy_load_locks:
        _lazy_load_locks[channel_id] = asyncio.Lock()
    return _lazy_load_locks[channel_id]


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def ensure_registered(state, channel_id: str) -> Operator | None:
    """Look up operator by channel_id. If not in memory, consult SQLite.
    If found there, initialise per-operator caches (inventory, contacts)
    and add to memory. Returns the Operator or None.
    """
    operator = state.operators_by_channel_id.get(channel_id)
    if operator is not None:
        return operator

    # Unknown channel — take the per-channel lock and re-check
    lock = _lock_for(channel_id)
    async with lock:
        operator = state.operators_by_channel_id.get(channel_id)
        if operator is not None:
            return operator

        operator = state.operator_adapter.get_by_channel_id(channel_id)
        if operator is None:
            return None

        log(
            "operator_lazy_loaded",
            operator_id=operator.operator_id,
            channel_id=channel_id,
        )

        # Build per-operator inventory cache if credentials are available
        cfg = state.config
        if cfg.google_credentials_json_b64 and operator.google_sheets_id:
            loader = GoogleSheetsLoader(
                cfg.google_credentials_json_b64,
                operator.google_sheets_id,
                operator.google_sheet_name,
            )
            cache = InventoryCache(search_threshold=cfg.search_threshold)
            try:
                products = await loader.load()
                cache.build_index(products)
                log(
                    "inventory_refreshed",
                    operator_id=operator.operator_id,
                    product_count=len(products),
                )
            except SheetsLoadError as e:
                log(
                    "error",
                    component="inventory",
                    error_type="lazy_load_failed",
                    operator_id=operator.operator_id,
                    message=str(e)[:200],
                )
            state.inventories_by_operator_id[operator.operator_id] = cache
            _spawn(cache.start_refresh(loader, cfg.inventory_refresh_interval_s))

        # Load contacts into cache for the new operator
        try:
            await state.contacts_cache.load_for_operator(operator)
        except Exception as e:
            log(
                "error",
                component="contacts",
                error_type="lazy_load_failed",
                operator_id=operator.operator_id,
                message=type(e).__name__,
            )

        # Now register in memory
        state.operators_by_channel_id[channel_id] = operator
        return operator
