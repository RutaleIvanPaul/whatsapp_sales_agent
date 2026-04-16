from __future__ import annotations

import re
from urllib.parse import urlparse

from app.adapters.inventory.base import InventoryAdapter
from app.utils.log import log

URL_RE = re.compile(r"https?://[^\s]+")


async def extract(payload_msg: dict, inventory: InventoryAdapter) -> str:
    try:
        url = _find_url(payload_msg)
        if not url:
            return "[customer shared a link]"

        slug = _extract_slug(url)
        if not slug:
            return "[customer shared a link not in our catalogue]"

        for product in inventory.get_all():
            if product.slug and product.slug == slug:
                return f"[customer shared a link to: {product.name}]"

        return "[customer shared a link not in our catalogue]"
    except Exception as e:
        log(
            "error",
            component="input_link",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return "[customer shared a link]"


def _find_url(msg: dict) -> str | None:
    # link_preview type
    preview = msg.get("link_preview") or {}
    canonical = preview.get("canonical_url") or preview.get("url")
    if canonical:
        return canonical
    # text body containing URL
    body = (msg.get("text") or {}).get("body", "")
    match = URL_RE.search(body)
    return match.group(0) if match else None


def _extract_slug(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None
