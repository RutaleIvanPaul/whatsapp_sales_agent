import hashlib
import re


def normalise(phone: str) -> str:
    cleaned = re.sub(r"[\s\-\(\)]", "", phone)

    if cleaned.startswith("+"):
        digits = re.sub(r"\D", "", cleaned[1:])
        cleaned = "+" + digits
    elif cleaned.startswith("00"):
        digits = re.sub(r"\D", "", cleaned[2:])
        cleaned = "+" + digits
    else:
        raise ValueError(
            "Phone number must include country code (e.g. +256700123456)"
        )

    if not re.fullmatch(r"\+\d{7,15}", cleaned):
        raise ValueError(f"Invalid E.164 phone: {cleaned!r}")

    return cleaned


def hash_for_log(phone: str) -> str:
    normalised = normalise(phone)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


# ── Provider-specific format adapters ────────────────────────────────────────
# All business logic uses canonical +E.164 ("+256705878284"). Provider wire
# formats are converted at the boundary using these named helpers — so the
# rest of the codebase never sees a Whapi-flavoured number.

def from_whapi(raw: str) -> str:
    """Convert a Whapi-formatted phone (bare international digits, no '+',
    optionally with @s.whatsapp.net suffix) to canonical +E.164.

    Raises ValueError on any input that isn't a valid international number.
    """
    if not raw:
        raise ValueError("Empty phone")
    # Strip JID suffix if present (e.g. "256705878284@s.whatsapp.net")
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    # Whapi does not include the '+'. Prepend it unless caller already did.
    if not raw.startswith("+") and not raw.startswith("00"):
        raw = "+" + raw
    return normalise(raw)


def to_whapi(e164: str) -> str:
    """Convert canonical +E.164 to Whapi wire format (bare digits, no '+').

    Whapi's /messages endpoints reject any 'to' value containing '+' with
    HTTP 400 ('wrong request parameters'). They require bare digits matching
    the regex: ^[\\d-]{9,31}(@[\\w\\.]{1,})?$
    """
    return e164.lstrip("+")
