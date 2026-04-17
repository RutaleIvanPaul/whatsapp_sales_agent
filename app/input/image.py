from __future__ import annotations

from app.adapters.vision.base import VisionAdapter
from app.utils.log import log

FALLBACK = "[image received, could not be described]"


async def describe(payload_msg: dict, vision_adapter: VisionAdapter) -> str:
    try:
        link = (payload_msg.get("image") or {}).get("link", "")
        if not link:
            return FALLBACK
        description = await vision_adapter.describe(link)
        return description
    except Exception as e:
        log(
            "error",
            component="input_image",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return FALLBACK
