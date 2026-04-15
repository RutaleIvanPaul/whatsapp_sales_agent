# Phase 1 — Foundation

## Goal

Build the data layer. No web server. No LLM calls. No Whapi calls.
Everything tested by running Python scripts directly.

## Read these SPEC sections first

  S3  — Data models (Operator, Session, Product, Stage)
  S16 — Security (encryption, phone normalisation)
  S17 — Logging (structured JSON logger)

## What to build (in this order)

### 1. app/utils/crypto.py
  AES-256-GCM encryption using the cryptography library.
  encrypt(plaintext: str) -> str  (returns base64 ciphertext)
  decrypt(ciphertext: str) -> str
  Key loaded from ENCRYPTION_KEY env var (32 bytes, base64-encoded).
  Test: encrypt then decrypt a sample string, verify round-trip.

### 2. app/utils/phone.py
  normalise(phone: str) -> str
    Strip spaces, dashes, parentheses.
    Ensure E.164 format: leading + and country code.
    Raise ValueError on invalid input.
  hash_for_log(phone: str) -> str
    SHA-256 hash of the normalised number, first 16 hex chars only.
  Test: normalise several Uganda number formats (0700..., +256700..., 256700...)
  All should produce the same E.164 result.

### 3. app/utils/logging.py
  Structured JSON logger.
  log(event: str, **kwargs) -> None
    Writes one JSON line to stdout:
    { "ts": "ISO8601", "event": event_name, ...kwargs }
  No direct use of Python's logging module — use print() with json.dumps().
  All other modules import this function and use it exclusively.

### 4. app/models/ (all four files)
  operator.py  — Operator dataclass + OperatorStatus enum (as in S3)
  session.py — Session dataclass + Stage enum (as in S3)
  product.py — Product dataclass (as in S3)
  message.py — simple dataclass for normalised inbound message:
    @dataclass
    class InboundMessage:
        message_id: str
        sender_phone: str       # E.164 normalised
        sender_name: str | None
        type: str               # text|image|voice|link_preview
        text: str | None
        image_link: str | None
        voice_link: str | None
        from_me: bool
        chat_id: str
        timestamp: int
        channel_id: str
        operator_id: str

### 5. app/config.py
  validate() function:
    Reads all required env vars from S20.
    For Phase 1, only validate:
      ENCRYPTION_KEY (required, must decode to 32 bytes)
      STORAGE_URL (required)
    Raise SystemExit with descriptive message if any missing.
  Export typed config values as module-level constants.

### 6. app/adapters/storage/base.py
  Abstract base class StorageAdapter with the three methods from CLAUDE.md.

### 7. app/adapters/storage/sqlite_adapter.py
  SQLite implementation of StorageAdapter.
  Schema:
    CREATE TABLE IF NOT EXISTS sessions (
      operator_id TEXT NOT NULL,
      phone TEXT NOT NULL,
      data TEXT NOT NULL,      -- JSON serialised Session
      updated_at TEXT NOT NULL,
      PRIMARY KEY (operator_id, phone)
    )
  get: SELECT data WHERE operator_id=? AND phone=?  → deserialise or None
  set: INSERT OR REPLACE with json.dumps(session.__dict__)
  delete: DELETE WHERE operator_id=? AND phone=?

### 8. app/adapters/operator/base.py + sqlite_adapter.py
  Same pattern as storage. Schema:
    CREATE TABLE IF NOT EXISTS operators (
      operator_id TEXT PRIMARY KEY,
      data TEXT NOT NULL,       -- JSON serialised Operator
      channel_id TEXT NOT NULL, -- indexed for fast lookup
      status TEXT NOT NULL
    )
    CREATE INDEX IF NOT EXISTS idx_channel_id ON operators(channel_id)
  get_by_channel_id: SELECT WHERE channel_id=?
  get_all_active: SELECT WHERE status='active'
  update_status: UPDATE WHERE operator_id=?

### 9. scripts/check_session.py
  Creates a test Operator and test Session.
  Saves both to SQLite.
  Reads both back.
  Prints them to console.
  Verifies they match.
  Verifies encrypt/decrypt round-trip on a sample token.
  Run with: python scripts/check_session.py

## Success criteria

Phase 1 passes when:
  python scripts/check_session.py runs without errors
  Session is saved and retrieved correctly
  Phone normalise() produces correct E.164 for Uganda numbers
  encrypt/decrypt round-trip verified
  Logging outputs valid JSON lines

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase1.md.
  Read SPEC.md sections S3, S16, and S17.
  Invoke @architect with your plan before writing code.
  Use Plan Mode.
