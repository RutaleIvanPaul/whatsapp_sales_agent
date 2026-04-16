from __future__ import annotations

import asyncio

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.vision.base import VisionAdapter
from app.input import image as image_handler
from app.input import link as link_handler
from app.input import text as text_handler
from app.input import voice as voice_handler
from app.utils.log import log

MAX_UNIFIED_CHARS = 3000
URL_HINT_RE = link_handler.URL_RE


async def process(
    payloads: list[dict],
    vision: VisionAdapter,
    inventory: InventoryAdapter,
) -> str:
    """Process a list of buffered Whapi payloads into unified_text.

    Each payload contains messages[]; we take the first message of each.
    Handlers run concurrently via asyncio.gather; results are joined in
    arrival order. If total > MAX_UNIFIED_CHARS, oldest text outputs are
    truncated first while preserving image/link placeholders (per S7).
    """
    if not payloads:
        return ""

    msgs = [p.get("messages", [{}])[0] for p in payloads]

    coros = [_dispatch(m, vision, inventory) for m in msgs]
    parts = await asyncio.gather(*coros)

    # Count types for logging
    types_present = sorted({m.get("type", "unknown") for m in msgs})
    total_chars = sum(len(p) for p in parts)

    log(
        "input_processed",
        types_present=",".join(types_present),
        total_chars=total_chars,
    )

    if total_chars <= MAX_UNIFIED_CHARS:
        return " ".join(p for p in parts if p)

    # Truncate text outputs (oldest first) while keeping bracketed placeholders
    return _truncate(parts, msgs, MAX_UNIFIED_CHARS)


async def _dispatch(
    msg: dict, vision: VisionAdapter, inventory: InventoryAdapter
) -> str:
    msg_type = msg.get("type", "")
    try:
        if msg_type == "text":
            body = (msg.get("text") or {}).get("body", "")
            if URL_HINT_RE.search(body):
                # Text with embedded URL — route through link handler
                return await link_handler.extract(msg, inventory)
            return text_handler.clean(body)
        if msg_type == "image":
            return await image_handler.describe(msg, vision)
        if msg_type == "voice":
            return voice_handler.transcribe(msg)
        if msg_type == "link_preview":
            return await link_handler.extract(msg, inventory)
        # Unknown type — return placeholder, never raise
        return f"[{msg_type or 'unknown'} message received]"
    except Exception as e:
        log(
            "error",
            component="input_processor",
            error_type=type(e).__name__,
            message_type=msg_type,
        )
        return f"[{msg_type or 'unknown'} message received]"


def _truncate(parts: list[str], msgs: list[dict], limit: int) -> str:
    # Score each part: placeholders (start with "[") are kept; plain text is
    # the first to be dropped (oldest first). Walk forward dropping from the
    # oldest plain text until we fit.
    keep = list(parts)
    while sum(len(p) for p in keep if p) > limit:
        dropped = False
        for i, p in enumerate(keep):
            if p and not p.startswith("["):
                keep[i] = ""
                dropped = True
                break
        if not dropped:
            # Only placeholders left; truncate from the front
            joined = " ".join(p for p in keep if p)
            return joined[:limit]
    return " ".join(p for p in keep if p)
