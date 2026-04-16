from __future__ import annotations

import asyncio

from app.models.operator import Operator

# Queue is created lazily inside the FastAPI lifespan so it binds to the
# uvicorn event loop (not whichever loop exists at module import time).
_queue: asyncio.Queue | None = None


def init_queue() -> asyncio.Queue:
    """Create a fresh queue bound to the current running event loop.

    Call once during lifespan startup. Subsequent calls to get_queue() will
    return the same instance.
    """
    global _queue
    _queue = asyncio.Queue()
    return _queue


def get_queue() -> asyncio.Queue:
    if _queue is None:
        raise RuntimeError("Queue not initialised. Call init_queue() first.")
    return _queue


def enqueue(payload: dict, operator: Operator) -> None:
    get_queue().put_nowait((payload, operator))
