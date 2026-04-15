import base64
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    encryption_key: bytes
    storage_url: str


def validate() -> Config:
    load_dotenv()

    encryption_key_b64 = os.getenv("ENCRYPTION_KEY", "")
    if not encryption_key_b64:
        print("FATAL: ENCRYPTION_KEY is required but not set.", file=sys.stderr)
        raise SystemExit(1)

    try:
        encryption_key = base64.b64decode(encryption_key_b64)
    except Exception:
        print("FATAL: ENCRYPTION_KEY is not valid base64.", file=sys.stderr)
        raise SystemExit(1)

    if len(encryption_key) != 32:
        print(
            f"FATAL: ENCRYPTION_KEY must decode to 32 bytes, got {len(encryption_key)}.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    storage_url = os.getenv("STORAGE_URL", "")
    if not storage_url:
        print("FATAL: STORAGE_URL is required but not set.", file=sys.stderr)
        raise SystemExit(1)

    return Config(encryption_key=encryption_key, storage_url=storage_url)
