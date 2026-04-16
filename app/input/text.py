from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()
