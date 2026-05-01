import base64
import json
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv
from app.utils.log import log as structured_log


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
    max_messages_per_user_day: int
    port: int

    # LLM (Phase 4 + multi-provider)
    llm_provider: str           # anthropic | groq
    llm_api_key: str
    llm_model: str
    vision_provider: str        # anthropic | groq
    vision_api_key: str
    vision_model: str
    classifier_model: str       # uses LLM_PROVIDER + LLM_API_KEY
    max_history_turns: int
    session_expiry_days: int

    # Cost tracking rates (USD per 1K tokens)
    input_token_rate_per_1k: float
    output_token_rate_per_1k: float

    # Server URL (for webhook config in onboarding)
    server_url: str

    # Semantic search (Phase 8)
    semantic_search_enabled: bool = True
    semantic_weight: float = 0.6


def validate() -> Config:
    """Load and validate all env vars. Collects ALL errors before exiting."""
    load_dotenv()
    errors: list[str] = []

    # ── ENCRYPTION_KEY ───────────────────────────────────────────────
    encryption_key = b""
    encryption_key_b64 = os.getenv("ENCRYPTION_KEY", "")
    if not encryption_key_b64:
        errors.append("ENCRYPTION_KEY: required but not set")
    else:
        try:
            encryption_key = base64.b64decode(encryption_key_b64)
            if len(encryption_key) != 32:
                errors.append(
                    f"ENCRYPTION_KEY: must decode to 32 bytes, got {len(encryption_key)}"
                )
        except Exception:
            errors.append("ENCRYPTION_KEY: not valid base64")

    # ── STORAGE_URL ──────────────────────────────────────────────────
    storage_url = os.getenv("STORAGE_URL", "")
    if not storage_url:
        errors.append("STORAGE_URL: required but not set")
    storage_db_path = _sqlite_path(storage_url) if storage_url else ""

    # ── GOOGLE_CREDENTIALS_JSON ──────────────────────────────────────
    google_credentials_json_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if google_credentials_json_b64:
        try:
            creds_raw = base64.b64decode(google_credentials_json_b64)
            creds_json = json.loads(creds_raw)
            for key in ("type", "project_id", "private_key", "client_email"):
                if key not in creds_json:
                    errors.append(
                        f"GOOGLE_CREDENTIALS_JSON: missing required key '{key}'"
                    )
        except Exception as e:
            errors.append(
                f"GOOGLE_CREDENTIALS_JSON: invalid base64 or JSON ({type(e).__name__})"
            )

    google_sheets_id = os.getenv("GOOGLE_SHEETS_ID", "")
    google_sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")

    # ── LLM_PROVIDER ────────────────────────────────────────────────
    llm_provider = os.getenv("LLM_PROVIDER", "anthropic")
    if llm_provider not in ("anthropic", "groq"):
        errors.append(
            f"LLM_PROVIDER: must be 'anthropic' or 'groq', got '{llm_provider}'"
        )

    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    llm_model = os.getenv("LLM_MODEL", "claude-sonnet-4-6")

    vision_provider = os.getenv("VISION_PROVIDER", llm_provider)
    if vision_provider not in ("anthropic", "groq"):
        errors.append(
            f"VISION_PROVIDER: must be 'anthropic' or 'groq', got '{vision_provider}'"
        )
    vision_api_key = os.getenv("VISION_API_KEY") or llm_api_key
    vision_model = os.getenv("VISION_MODEL", "claude-sonnet-4-6")

    classifier_model = os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")

    # ── Numeric vars ─────────────────────────────────────────────────
    numeric_vars = {
        "SEARCH_THRESHOLD": ("70", 1, 100),
        "INVENTORY_REFRESH_INTERVAL_S": ("300", 10, 86400),
        "BUFFER_DEBOUNCE_MS": ("3000", 500, 30000),
        "BUFFER_RATE_LIMIT_S": ("8", 1, 300),
        "WHAPI_HEALTH_CHECK_INTERVAL_S": ("1800", 60, 86400),
        "MAX_MESSAGES_PER_USER_DAY": ("100", 1, 10000),
        "PORT": ("8000", 1, 65535),
        "MAX_HISTORY_TURNS": ("10", 2, 100),
        "SESSION_EXPIRY_DAYS": ("7", 1, 365),
    }
    parsed_nums: dict[str, int] = {}
    for var, (default, min_val, max_val) in numeric_vars.items():
        raw = os.getenv(var, default)
        try:
            val = int(raw)
            if val < min_val or val > max_val:
                errors.append(f"{var}: {val} out of range [{min_val}, {max_val}]")
            parsed_nums[var] = val
        except ValueError:
            errors.append(f"{var}: must be an integer, got '{raw}'")
            parsed_nums[var] = int(default)

    # ── Cost rates ───────────────────────────────────────────────────
    if llm_provider == "groq":
        default_input_rate, default_output_rate = "0.0005", "0.001"
    else:
        default_input_rate, default_output_rate = "0.003", "0.015"
    try:
        input_token_rate = float(os.getenv("INPUT_TOKEN_RATE_PER_1K", default_input_rate))
    except ValueError:
        errors.append("INPUT_TOKEN_RATE_PER_1K: must be a number")
        input_token_rate = float(default_input_rate)
    try:
        output_token_rate = float(os.getenv("OUTPUT_TOKEN_RATE_PER_1K", default_output_rate))
    except ValueError:
        errors.append("OUTPUT_TOKEN_RATE_PER_1K: must be a number")
        output_token_rate = float(default_output_rate)

    server_url = os.getenv("SERVER_URL", "")

    # ── Semantic search configuration ────────────────────────────────
    semantic_search_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "true").lower() != "false"
    semantic_weight_raw = os.getenv("SEMANTIC_WEIGHT", "0.6")
    semantic_weight = 0.6
    try:
        semantic_weight = float(semantic_weight_raw)
        if not 0.0 <= semantic_weight <= 1.0:
            raise ValueError(f"out of range")
    except ValueError:
        errors.append(f"SEMANTIC_WEIGHT must be a float between 0.0 and 1.0, got: {semantic_weight_raw}")
        semantic_weight = 0.6

    # ── Report errors ────────────────────────────────────────────────
    if errors:
        print("FATAL: Configuration errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)

    # ── Success summary ──────────────────────────────────────────────
    def _mask(s: str) -> str:
        return s[:4] + "***" if len(s) > 4 else "***"

    structured_log(
        "config_validated",
        storage_url=storage_url,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=_mask(llm_api_key),
        encryption_key=_mask(encryption_key_b64),
    )

    return Config(
        encryption_key=encryption_key,
        storage_url=storage_url,
        storage_db_path=storage_db_path,
        google_credentials_json_b64=google_credentials_json_b64,
        google_sheets_id=google_sheets_id,
        google_sheet_name=google_sheet_name,
        search_threshold=parsed_nums["SEARCH_THRESHOLD"],
        inventory_refresh_interval_s=parsed_nums["INVENTORY_REFRESH_INTERVAL_S"],
        buffer_debounce_ms=parsed_nums["BUFFER_DEBOUNCE_MS"],
        buffer_rate_limit_s=parsed_nums["BUFFER_RATE_LIMIT_S"],
        whapi_health_check_interval_s=parsed_nums["WHAPI_HEALTH_CHECK_INTERVAL_S"],
        max_messages_per_user_day=parsed_nums["MAX_MESSAGES_PER_USER_DAY"],
        port=parsed_nums["PORT"],
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        vision_provider=vision_provider,
        vision_api_key=vision_api_key,
        vision_model=vision_model,
        classifier_model=classifier_model,
        max_history_turns=parsed_nums["MAX_HISTORY_TURNS"],
        session_expiry_days=parsed_nums["SESSION_EXPIRY_DAYS"],
        input_token_rate_per_1k=input_token_rate,
        output_token_rate_per_1k=output_token_rate,
        server_url=server_url,
        semantic_search_enabled=semantic_search_enabled,
        semantic_weight=semantic_weight,
    )


def _sqlite_path(url: str) -> str:
    """Extract filesystem path from a sqlite:/// URL."""
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url
