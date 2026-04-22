from __future__ import annotations

import asyncio

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.models.product import Product
from app.utils.log import log

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

    Text is sent synchronously (customer sees it immediately).
    Images are sent fire-and-forget so a broken URL doesn't block the reply.
    Only sends images for products the LLM actually mentioned in its reply —
    search may return 5 results but the LLM only presents 1-3 to the customer.
    Long replies are split at the last "\n\n" before MAX_TEXT_CHARS.
    """
    if reply_text:
        for chunk in _split_text(reply_text, MAX_TEXT_CHARS):
            await messaging.send_text(phone, chunk, operator)

    # Only send images for products the LLM actually referenced in its text.
    # The products list contains ALL search results, but the LLM may have
    # chosen to show only a subset. Sending images for unmentioned products
    # confuses the customer (e.g. Samsung images after iPhone text).
    mentioned = _filter_mentioned(products, reply_text)

    # Images fire-and-forget — don't block the pipeline timing
    for product in mentioned[:MAX_PRODUCT_IMAGES]:
        if not product.image_url:
            continue
        caption = f"{product.name}\n{product.price}\n{product.description}"
        asyncio.create_task(_send_image_safe(messaging, phone, product, caption, operator))


async def _send_image_safe(
    messaging: MessagingAdapter,
    phone: str,
    product: Product,
    caption: str,
    operator: Operator,
) -> None:
    """Background image send with its own error handling."""
    try:
        await messaging.send_image(phone, product.image_url, caption, operator)
    except Exception as e:
        log(
            "error",
            component="response_builder",
            error_type="image_send_failed",
            product_id=product.id,
        )


def _filter_mentioned(products: list[Product], reply_text: str) -> list[Product]:
    """Return only products the LLM actually referenced in its reply text.

    Uses a word-overlap approach: a product is "mentioned" if enough of its
    significant name words appear in the reply. This handles the common case
    where the LLM rephrases slightly ("Lightning Cable" vs "Lightning Cable 1m").
    """
    if not reply_text:
        return []

    reply_lower = reply_text.lower()
    # Words too common to be distinctive
    stop_words = {"the", "a", "an", "and", "or", "for", "in", "of", "with", "to"}
    mentioned = []

    for p in products:
        name_words = [w for w in p.name.lower().split() if w not in stop_words and len(w) >= 2]
        if not name_words:
            continue
        matched = sum(1 for w in name_words if w in reply_lower)
        # Require at least half the significant words to match
        if matched >= max(1, len(name_words) // 2):
            mentioned.append(p)

    return mentioned


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
