"""Language classifier — direct Anthropic call, no LLMAdapter.

Architectural exception (per Phase 4 plan, decision #2): this is a
single-purpose constrained call (max 10 tokens, fixed model). The
LLMAdapter abstraction adds no value for a one-shot classification, so
we use AsyncAnthropic directly. If we ever need to swap the classifier
provider, refactor this single file.
"""

from __future__ import annotations

import time

import anthropic
from anthropic import AsyncAnthropic

from app.utils.log import log

CLASSIFIER_TIMEOUT_S = 10.0
CLASSIFIER_MAX_TOKENS = 10
SYSTEM_PROMPT = "You are a language classifier. Reply with exactly one word."

VALID_LABELS = {"ENGLISH", "LUGANDA", "MIXED", "UNKNOWN"}


async def classify(text: str, api_key: str, model: str) -> str:
    if not text.strip():
        return "ENGLISH"

    user_prompt = (
        "Classify the language of this message. "
        "Reply with exactly one of: ENGLISH, LUGANDA, MIXED, UNKNOWN.\n"
        f"Message: {text}"
    )

    client = AsyncAnthropic(api_key=api_key, timeout=CLASSIFIER_TIMEOUT_S)
    start = time.monotonic()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        text_parts = [b.text for b in response.content if b.type == "text"]
        raw = "".join(text_parts).strip().upper()
        # Take first whitespace-separated token to be defensive
        label = raw.split()[0] if raw else "ENGLISH"
        if label not in VALID_LABELS:
            log(
                "language_unrecognised",
                raw=raw[:50],
                latency_ms=latency_ms,
            )
            label = "UNKNOWN"

        log(
            "language_classified",
            result=label,
            duration_ms=latency_ms,
        )
        return label
    except (anthropic.APIError, anthropic.APITimeoutError, Exception) as e:
        log(
            "error",
            component="language",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return "ENGLISH"
