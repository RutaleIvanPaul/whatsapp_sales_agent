from __future__ import annotations

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.models.product import Product

MAX_TEXT_CHARS = 1500
MAX_PRODUCT_IMAGES = 3


async def send_response(
    phone: str,
    reply_text: str,
    products: list[Product],
    operator: Operator,
    messaging: MessagingAdapter,
) -> None:
    """Send the LLM reply text + up to 3 product images via the messaging adapter.

    Long replies are split at the last "\n\n" before MAX_TEXT_CHARS.
    """
    if reply_text:
        for chunk in _split_text(reply_text, MAX_TEXT_CHARS):
            await messaging.send_text(phone, chunk, operator)

    for product in products[:MAX_PRODUCT_IMAGES]:
        if not product.image_url:
            continue
        caption = f"{product.name}\n{product.price}\n{product.description}"
        await messaging.send_image(phone, product.image_url, caption, operator)


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Find last "\n\n" before the limit
        slice_ = remaining[:limit]
        cut = slice_.rfind("\n\n")
        if cut == -1:
            # No paragraph break — fall back to the last single newline
            cut = slice_.rfind("\n")
        if cut == -1 or cut < limit // 2:
            # Last resort — hard cut at limit
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
