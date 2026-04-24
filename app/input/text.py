from __future__ import annotations

import re

from app.utils.log import log

_WHITESPACE_RE = re.compile(r"\s+")

# Generous limit — image captions include name + price + description +
# attributes which routinely exceeds 200 chars. Cutting loses the
# details the customer is actually referring to.
QUOTED_CHAR_LIMIT = 600


def clean(msg: dict) -> str:
    """Extract and clean text from a message payload.

    If the message is a reply (has a `context` with quoted data),
    prepend the quoted context so the LLM knows what "this" / "that one"
    / "I want it" refers to.
    """
    body = ((msg.get("text") or {}).get("body") or "").strip()
    body = _WHITESPACE_RE.sub(" ", body)

    quoted_body = _extract_quoted(msg)

    if quoted_body:
        quoted_body = _WHITESPACE_RE.sub(" ", quoted_body.strip())
        return f'[replying to: "{quoted_body[:QUOTED_CHAR_LIMIT]}"] {body}'

    return body


def _extract_quoted(msg: dict) -> str:
    """Extract the quoted message text from the Whapi payload.

    Whapi has multiple schemas depending on version and the type of
    message being replied to. Walk every known path and return the
    first non-empty value.
    """
    context = msg.get("context")
    if not isinstance(context, dict):
        return ""

    # Candidate containers — different Whapi versions nest differently.
    containers: list[dict] = []
    for key in ("quoted_content", "quoted", "quoted_message"):
        inner = context.get(key)
        if isinstance(inner, dict):
            containers.append(inner)

    # Direct top-level body/caption on context itself (some payloads).
    containers.append(context)

    for c in containers:
        # Text reply — direct body or nested {"text": {"body": ...}}
        text = c.get("body") or (
            (c.get("text") or {}).get("body") if isinstance(c.get("text"), dict) else ""
        )
        if text:
            return text

        # Image / media reply — direct caption or nested
        # {"image": {"caption": ...}} / {"video": {"caption": ...}} etc.
        caption = c.get("caption")
        if caption:
            return caption
        for media_key in ("image", "video", "document"):
            media = c.get(media_key)
            if isinstance(media, dict) and media.get("caption"):
                return media["caption"]

    # Diagnostic: context was present but we couldn't pull anything.
    # Log so we can spot schema mismatches without leaking content.
    log(
        "quoted_extract_miss",
        component="input_text",
        context_keys=list(context.keys()),
        has_quoted_id=bool(context.get("quoted_id")),
    )
    return ""
