"""Session-aware intent gate — two-stage classifier.

Stage 1: fast keyword check (microseconds, no API call).
Stage 2: LLM classifier (cheap model, only for ambiguous messages).

The intent gate only fires for genuinely new or unknown contacts
(no session or empty history). Existing customers skip it entirely.
See DECISIONS.md #16 for rationale.
"""

from __future__ import annotations

import re
import time

from app.adapters.llm.base import LLMAdapter
from app.engine.system_prompt import CUSTOMER_BLOCK_CLOSE, CUSTOMER_BLOCK_OPEN
from app.utils.log import log

CLASSIFIER_MAX_TOKENS = 10
SYSTEM_PROMPT = "You are a message intent classifier. Reply with exactly one word."

VALID_INTENTS = {"SALES", "NOT_SALES"}

# Stage 1 keyword lists — lowercase substrings
SALES_KEYWORDS = [
    "price", "available", "size", "colour", "color", "do you have",
    "want to buy", "how much", "delivery", "order", "stock",
    "looking for", "need a", "need some", "shoes", "shirt", "bag",
    "buy", "purchase", "cost", "payment", "pay", "mpesa", "momo",
    "mobile money", "catalog", "catalogue", "shop", "store",
    "in stock", "any", "sell", "offer",
]

NOT_SALES_KEYWORDS = [
    "wrong number", "is this", "who is this", "sorry wrong",
    "not the right", "wrong chat", "i think you have the wrong",
]

_SALES_RE = re.compile("|".join(re.escape(k) for k in SALES_KEYWORDS), re.IGNORECASE)
_NOT_SALES_RE = re.compile("|".join(re.escape(k) for k in NOT_SALES_KEYWORDS), re.IGNORECASE)


async def classify_intent(text: str, classifier_llm: LLMAdapter) -> str:
    """Classify whether a message is sales-related or not.

    Returns "SALES" or "NOT_SALES". Defaults to "SALES" on any
    ambiguity or failure (fail open — never silence a real customer).
    """
    if not text.strip():
        return "SALES"  # empty → let the conversation engine handle it

    # ── Stage 1: keyword check ───────────────────────────────────────
    has_sales = bool(_SALES_RE.search(text))
    has_not_sales = bool(_NOT_SALES_RE.search(text))

    if has_sales:
        log("intent_classified", result="SALES", stage="keyword", chars=len(text))
        return "SALES"

    if has_not_sales and not has_sales:
        log("intent_classified", result="NOT_SALES", stage="keyword", chars=len(text))
        return "NOT_SALES"

    # ── Stage 2: LLM classifier (ambiguous messages only) ────────────
    user_prompt = (
        "Classify whether this message is from someone interested in "
        "buying products from a shop, or is a non-sales message "
        "(wrong number, personal, spam, etc). Reply: SALES or NOT_SALES.\n"
        f"{CUSTOMER_BLOCK_OPEN}\n{text}\n{CUSTOMER_BLOCK_CLOSE}"
    )

    start = time.monotonic()
    try:
        response = await classifier_llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
            system=SYSTEM_PROMPT,
            max_tokens=CLASSIFIER_MAX_TOKENS,
        )
    except Exception as e:
        log(
            "error",
            component="intent",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return "SALES"  # fail open

    latency_ms = int((time.monotonic() - start) * 1000)
    raw = (response.text or "").strip().upper()
    label = raw.split()[0].rstrip(".,;:!?") if raw else "SALES"

    if label not in VALID_INTENTS:
        label = "SALES"  # fail open on unrecognised response

    log("intent_classified", result=label, stage="llm", chars=len(text), duration_ms=latency_ms)
    return label
