"""Language classifier — two-stage: keyword pre-filter then LLM fallback.

Stage 1 catches obvious English and Luganda via character set and marker
words (microseconds, no API call). Stage 2 uses the LLM only for
genuinely ambiguous messages. Same two-stage pattern as intent.py.
"""

from __future__ import annotations

import re
import time

from app.adapters.llm.base import LLMAdapter, LLMTimeoutError
from app.engine.system_prompt import CUSTOMER_BLOCK_CLOSE, CUSTOMER_BLOCK_OPEN
from app.utils.log import log

CLASSIFIER_MAX_TOKENS = 10
SYSTEM_PROMPT = "You are a language classifier. Reply with exactly one word."

VALID_LABELS = {"ENGLISH", "LUGANDA", "MIXED", "UNKNOWN"}

# Stage 1 keyword lists
_ENGLISH_WORDS = {
    "the", "a", "is", "do", "have", "you", "any", "how", "much", "what",
    "can", "i", "me", "my", "we", "got", "want", "need", "yes", "no",
    "ok", "please", "thanks", "hi", "hello", "hey", "looking", "for",
    "price", "available", "buy", "order", "size", "this", "that", "it",
    "your", "are", "there", "some", "would", "like", "show", "more",
    "about", "tell", "get", "one", "which", "where", "them", "these",
}

_LUGANDA_MARKERS = {
    "webale", "gyendi", "nkuwe", "nsaba", "kiki", "nja", "nga", "nze",
    "boleka", "meka", "yee", "nedda", "banange", "ssebo", "nnyabo",
    "mpa", "nkugamba", "oli", "otya", "bulungi", "nnyo", "okola",
    "ekintu", "emere", "engeri", "okugula", "omuwendo",
}


def _keyword_classify(text: str) -> str | None:
    """Fast keyword/character-set check. Returns label or None if ambiguous."""
    words = set(re.findall(r"[a-zA-Z]+", text.lower()))
    if not words:
        return None

    has_english = bool(words & _ENGLISH_WORDS)
    has_luganda = bool(words & _LUGANDA_MARKERS)

    if has_english and has_luganda:
        return "MIXED"
    if has_luganda and not has_english:
        return "LUGANDA"
    if has_english and not has_luganda:
        # Check if ALL chars are ASCII-ish (no unusual scripts)
        if all(ord(c) < 256 for c in text):
            return "ENGLISH"
    return None  # ambiguous — fall through to LLM


async def classify(text: str, llm: LLMAdapter) -> str:
    """Classify the language of a message. Returns one of:
    ENGLISH | LUGANDA | MIXED | UNKNOWN. Returns ENGLISH on any failure.
    """
    if not text.strip():
        return "ENGLISH"

    # ── Stage 1: keyword pre-filter ──────────────────────────────────
    keyword_result = _keyword_classify(text)
    if keyword_result is not None:
        log("language_classified", result=keyword_result, stage="keyword",
            duration_ms=0)
        return keyword_result

    # ── Stage 2: LLM classifier (ambiguous messages only) ────────────
    user_prompt = (
        "Classify the language of this message. "
        "Reply with exactly one of: ENGLISH, LUGANDA, MIXED, UNKNOWN.\n"
        f"{CUSTOMER_BLOCK_OPEN}\n{text}\n{CUSTOMER_BLOCK_CLOSE}"
    )

    start = time.monotonic()
    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
            system=SYSTEM_PROMPT,
            max_tokens=CLASSIFIER_MAX_TOKENS,
        )
    except LLMTimeoutError:
        log("language_timeout")
        return "ENGLISH"
    except Exception as e:
        log(
            "error",
            component="language",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return "ENGLISH"

    latency_ms = int((time.monotonic() - start) * 1000)

    raw = (response.text or "").strip().upper()
    label = raw.split()[0].rstrip(".,;:!?") if raw else "ENGLISH"
    if label not in VALID_LABELS:
        log("language_unrecognised", raw=raw[:50], latency_ms=latency_ms)
        label = "UNKNOWN"

    log("language_classified", result=label, stage="llm", duration_ms=latency_ms)
    return label
