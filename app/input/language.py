"""Language classifier — uses an LLMAdapter so it works with any provider.

Phase 4 originally used a direct Anthropic call here. With multi-provider
support (Anthropic / Groq), routing through the LLMAdapter abstraction is
the simpler path. The classifier just needs `chat()` with no tools.
"""

from __future__ import annotations

import time

from app.adapters.llm.base import LLMAdapter, LLMTimeoutError
from app.utils.log import log

CLASSIFIER_MAX_TOKENS = 10
SYSTEM_PROMPT = "You are a language classifier. Reply with exactly one word."

VALID_LABELS = {"ENGLISH", "LUGANDA", "MIXED", "UNKNOWN"}


async def classify(text: str, llm: LLMAdapter) -> str:
    """Classify the language of a message. Returns one of:
    ENGLISH | LUGANDA | MIXED | UNKNOWN. Returns ENGLISH on any failure.
    """
    if not text.strip():
        return "ENGLISH"

    user_prompt = (
        "Classify the language of this message. "
        "Reply with exactly one of: ENGLISH, LUGANDA, MIXED, UNKNOWN.\n"
        f"Message: {text}"
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
    # Strip punctuation — some models append periods (e.g. "ENGLISH.")
    label = raw.split()[0].rstrip(".,;:!?") if raw else "ENGLISH"
    if label not in VALID_LABELS:
        log(
            "language_unrecognised",
            raw=raw[:50],
            latency_ms=latency_ms,
        )
        label = "UNKNOWN"

    log("language_classified", result=label, duration_ms=latency_ms)
    return label
