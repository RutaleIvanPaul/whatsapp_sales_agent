from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.inventory.cache import InventoryCache
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.adapters.llm import factory as llm_factory
from app.adapters.messaging.whapi import WhapiMessagingAdapter
from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
from app.adapters.storage.sqlite_adapter import SqliteStorageAdapter
from app.adapters.vision import factory as vision_factory
from app.buffer.buffer import MessageBuffer
from app.config import validate
from app.models.operator import Operator
from app.pipeline import runner as pipeline_runner
from app.queue.queue import init_queue
from app.queue.worker import QueueWorker
from app.utils.contacts import ContactsCache
from app.utils.log import log
from app.webhook.receiver import router as webhook_router

# Background tasks are pinned here so they aren't garbage-collected.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Step 1: config ---
    cfg = validate()
    log("startup", phase="config_loaded")

    # --- Step 2: operator adapter + load active operators ---
    operator_adapter = SqliteOperatorAdapter(cfg.storage_db_path, cfg.encryption_key)
    operators: list[Operator] = operator_adapter.get_all_active()
    operators_by_channel_id: dict[str, Operator] = {
        o.whapi_channel_id: o for o in operators
    }
    log("startup", phase="operators_loaded", operator_count=len(operators))

    if not operators:
        log(
            "error",
            component="main",
            error_type="no_active_operators",
            message="No active operators found. Run scripts/check_session.py --create-test-operator.",
        )

    # --- Step 3: session storage adapter ---
    storage_adapter = SqliteStorageAdapter(cfg.storage_db_path)
    log("startup", phase="storage_ready")

    # --- Step 4: per-operator inventory cache + refresh ---
    inventories_by_operator_id: dict[str, InventoryCache] = {}
    if cfg.google_credentials_json_b64 and cfg.google_sheets_id:
        for op in operators:
            loader = GoogleSheetsLoader(
                cfg.google_credentials_json_b64,
                op.google_sheets_id or cfg.google_sheets_id,
                cfg.google_sheet_name,
            )
            cache = InventoryCache(search_threshold=cfg.search_threshold)
            try:
                products = await loader.load()
                cache.build_index(products)
                log(
                    "inventory_refreshed",
                    operator_id=op.operator_id,
                    product_count=len(products),
                    duration_ms=0,
                )
            except SheetsLoadError as e:
                log(
                    "error",
                    component="inventory",
                    error_type="initial_load_failed",
                    operator_id=op.operator_id,
                    message=str(e)[:200],
                )

            inventories_by_operator_id[op.operator_id] = cache
            _spawn(cache.start_refresh(loader, cfg.inventory_refresh_interval_s))
    else:
        log("startup", phase="inventory_skipped", reason="no_google_credentials")

    # --- Step 5: messaging adapter ---
    messaging_adapter = WhapiMessagingAdapter(cfg.encryption_key)
    log("startup", phase="messaging_ready")

    # --- Step 5b: LLM + vision adapters (Phase 4) ---
    llm_adapter = llm_factory.from_config(cfg)
    vision_adapter = vision_factory.from_config(cfg)
    log("startup", phase="llm_ready", llm_model=cfg.llm_model, vision_model=cfg.vision_model)

    # --- Step 6: contacts cache + hourly refresh ---
    contacts_cache = ContactsCache(cfg.encryption_key)
    for op in operators:
        await contacts_cache.load_for_operator(op)
    _spawn(contacts_cache.start_refresh(operators, interval_s=3600))
    log("startup", phase="contacts_loaded")

    # --- Step 7: queue + buffer + worker ---
    # Init queue here so it binds to uvicorn's event loop, not the import-time loop.
    message_queue = init_queue()
    buffer = MessageBuffer(
        debounce_s=cfg.buffer_debounce_ms / 1000,
        rate_limit_s=cfg.buffer_rate_limit_s,
    )

    async def flush_for_operator(payloads: list[dict], operator: Operator) -> None:
        # Per-operator inventory; if missing, fall back to first available cache
        inv = inventories_by_operator_id.get(operator.operator_id)
        if inv is None and inventories_by_operator_id:
            inv = next(iter(inventories_by_operator_id.values()))
        if inv is None:
            log(
                "error",
                component="main",
                error_type="no_inventory_for_operator",
                operator_id=operator.operator_id,
            )
            return
        await pipeline_runner.run(
            payloads,
            operator,
            llm=llm_adapter,
            vision=vision_adapter,
            inventory=inv,
            messaging=messaging_adapter,
            storage=storage_adapter,
            anthropic_api_key=cfg.anthropic_api_key,
            classifier_model=cfg.classifier_model,
            max_history_turns=cfg.max_history_turns,
            session_expiry_days=cfg.session_expiry_days,
        )

    worker = QueueWorker(message_queue, buffer, flush_for_operator)
    _spawn(worker.run())
    log("startup", phase="worker_started")

    # --- Attach state for receiver ---
    app.state.operators_by_channel_id = operators_by_channel_id
    app.state.operator_adapter = operator_adapter
    app.state.storage_adapter = storage_adapter
    app.state.inventories_by_operator_id = inventories_by_operator_id
    app.state.messaging_adapter = messaging_adapter
    app.state.contacts_cache = contacts_cache
    app.state.encryption_key = cfg.encryption_key
    app.state.message_queue = message_queue
    app.state.buffer = buffer
    log("startup", phase="ready", port=cfg.port)

    yield

    # --- Shutdown ---
    for task in list(_background_tasks):
        task.cancel()
    log("shutdown")


app = FastAPI(lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
