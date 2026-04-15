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
