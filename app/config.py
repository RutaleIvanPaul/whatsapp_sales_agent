import base64
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # Core (Phase 1)
    encryption_key: bytes
    storage_url: str
    storage_db_path: str

    # Inventory (Phase 2)
    google_credentials_json_b64: str
    google_sheets_id: str
    google_sheet_name: str
    search_threshold: int
    inventory_refresh_interval_s: int

    # Webhook / buffer / server (Phase 3)
    buffer_debounce_ms: int
    buffer_rate_limit_s: int
    whapi_health_check_interval_s: int
    port: int

    # LLM (Phase 4)
    anthropic_api_key: str
    llm_model: str
    vision_model: str
    classifier_model: str
    max_history_turns: int
    session_expiry_days: int


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

    # Parse sqlite:///path to get the file path
    storage_db_path = _sqlite_path(storage_url)

    google_credentials_json_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    google_sheets_id = os.getenv("GOOGLE_SHEETS_ID", "")
    google_sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")

    search_threshold = int(os.getenv("SEARCH_THRESHOLD", "70"))
    inventory_refresh_interval_s = int(os.getenv("INVENTORY_REFRESH_INTERVAL_S", "300"))

    buffer_debounce_ms = int(os.getenv("BUFFER_DEBOUNCE_MS", "3000"))
    buffer_rate_limit_s = int(os.getenv("BUFFER_RATE_LIMIT_S", "8"))
    whapi_health_check_interval_s = int(os.getenv("WHAPI_HEALTH_CHECK_INTERVAL_S", "1800"))
    port = int(os.getenv("PORT", "8000"))

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    llm_model = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    vision_model = os.getenv("VISION_MODEL", "claude-sonnet-4-6")
    classifier_model = os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    max_history_turns = int(os.getenv("MAX_HISTORY_TURNS", "10"))
    session_expiry_days = int(os.getenv("SESSION_EXPIRY_DAYS", "7"))

    return Config(
        encryption_key=encryption_key,
        storage_url=storage_url,
        storage_db_path=storage_db_path,
        google_credentials_json_b64=google_credentials_json_b64,
        google_sheets_id=google_sheets_id,
        google_sheet_name=google_sheet_name,
        search_threshold=search_threshold,
        inventory_refresh_interval_s=inventory_refresh_interval_s,
        buffer_debounce_ms=buffer_debounce_ms,
        buffer_rate_limit_s=buffer_rate_limit_s,
        whapi_health_check_interval_s=whapi_health_check_interval_s,
        port=port,
        anthropic_api_key=anthropic_api_key,
        llm_model=llm_model,
        vision_model=vision_model,
        classifier_model=classifier_model,
        max_history_turns=max_history_turns,
        session_expiry_days=session_expiry_days,
    )


def _sqlite_path(url: str) -> str:
    """Extract filesystem path from a sqlite:/// URL."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url
