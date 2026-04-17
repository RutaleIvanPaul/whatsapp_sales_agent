from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def clean(msg: dict) -> str:
    """Extract and clean text from a message payload.

    If the message is a reply (has a `context.quoted_content` field),
    prepend the quoted context so the LLM knows what "this" / "that one"
    / "I want it" refers to.

    Whapi puts quoted message data in:
      msg.context.quoted_content.body    (for text replies)
      msg.context.quoted_content.caption (for image/media replies)
    """
    body = ((msg.get("text") or {}).get("body") or "").strip()
    body = _WHITESPACE_RE.sub(" ", body)

    quoted_body = _extract_quoted(msg)

    if quoted_body:
        quoted_body = _WHITESPACE_RE.sub(" ", quoted_body.strip())
        return f'[replying to: "{quoted_body[:200]}"] {body}'

    return body


def _extract_quoted(msg: dict) -> str:
    """Extract the quoted message text from the Whapi payload."""
    context = msg.get("context") or {}
    quoted = context.get("quoted_content") or {}

    # Text reply: quoted_content.body
    text = quoted.get("body", "")
    if text:
        return text

    # Image/media reply: quoted_content.caption
    caption = quoted.get("caption", "")
    if caption:
        return caption

    return ""
