"""Phase 1 verification script.

Usage:
  python scripts/check_session.py                       # run Phase 1 self-tests
  python scripts/check_session.py --create-test-operator  # insert a test operator
                                                          #   into the real DB
                                                          #   (for Phase 3 local
                                                          #   testing)
"""

import json
import os
import secrets
import sys
import tempfile

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from app.config import validate
from app.adapters.storage.sqlite_adapter import SqliteStorageAdapter
from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
from app.models.session import Session, Stage
from app.models.operator import Operator, OperatorStatus
from app.utils.crypto import decrypt, encrypt
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log, normalise, to_whapi


def main():
    print("=" * 60)
    print("Phase 1 — Verification Script")
    print("=" * 60)

    # --- Config ---
    print("\n[1] Validating config...")
    cfg = validate()
    print(f"    ENCRYPTION_KEY: {len(cfg.encryption_key)} bytes OK")
    print(f"    STORAGE_URL: {cfg.storage_url}")

    # --- Crypto round-trip ---
    print("\n[2] Testing encrypt/decrypt round-trip...")
    sample = "my-secret-whapi-token-abc123"
    encrypted = encrypt(sample, cfg.encryption_key)
    decrypted = decrypt(encrypted, cfg.encryption_key)
    assert decrypted == sample, f"Round-trip failed: {decrypted!r} != {sample!r}"
    print(f"    Plaintext:  {sample}")
    print(f"    Encrypted:  {encrypted[:40]}...")
    print(f"    Decrypted:  {decrypted}")
    print("    PASS")

    # --- Phone normalisation ---
    print("\n[3] Testing phone normalisation...")
    test_cases = [
        ("+256700123456", "+256700123456"),
        ("+256 700 123 456", "+256700123456"),
        ("00256700123456", "+256700123456"),
        ("+447911123456", "+447911123456"),
        ("+254712345678", "+254712345678"),
        ("+254 712-345-678", "+254712345678"),
    ]
    for raw, expected in test_cases:
        result = normalise(raw)
        assert result == expected, f"normalise({raw!r}) = {result!r}, expected {expected!r}"
        print(f"    {raw:24s} → {result}")

    # Verify missing country code raises ValueError
    try:
        normalise("0700123456")
        assert False, "Expected ValueError for number without country code"
    except ValueError:
        print(f"    {'0700123456':24s} → ValueError (expected)")

    phone_hash = hash_for_log("+256700123456")
    print(f"    hash_for_log: {phone_hash}")
    assert len(phone_hash) == 16

    # Whapi format adapters
    assert from_whapi("256705878284") == "+256705878284"
    assert from_whapi("256705878284@s.whatsapp.net") == "+256705878284"
    assert from_whapi("+256705878284") == "+256705878284"
    assert to_whapi("+256705878284") == "256705878284"
    assert to_whapi("+447911123456") == "447911123456"
    print("    from_whapi/to_whapi: PASS (round-trip + JID strip)")
    print("    PASS")

    # --- Logging ---
    print("\n[4] Testing structured logger...")
    log("test_event", operator_id="op-001", phone_hash=phone_hash, count=42)
    print("    (see JSON line above)")
    print("    PASS")

    # --- SQLite storage adapter ---
    print("\n[5] Testing session storage adapter...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        storage = SqliteStorageAdapter(db_path)

        now = datetime.utcnow()
        session = Session(
            operator_id="op-001",
            phone="+256700123456",
            name="Test Customer",
            language="en",
            history=[{"role": "user", "content": "Hello"}],
            intent="buy shoes",
            constraints={"size": "42", "colour": "black"},
            shown_product_ids=["prod-1"],
            stage=Stage.EXPLORING,
            handed_off_at=None,
            last_active=now,
            created_at=now,
        )

        storage.set("op-001", "+256700123456", session)
        retrieved = storage.get("op-001", "+256700123456")
        assert retrieved is not None, "Session not found after save"
        assert retrieved.operator_id == session.operator_id
        assert retrieved.phone == session.phone
        assert retrieved.name == session.name
        assert retrieved.stage == Stage.EXPLORING
        assert retrieved.constraints == {"size": "42", "colour": "black"}
        assert retrieved.shown_product_ids == ["prod-1"]
        print(f"    Saved and retrieved session for {hash_for_log(session.phone)}")

        storage.delete("op-001", "+256700123456")
        assert storage.get("op-001", "+256700123456") is None
        print("    Delete verified")
        print("    PASS")

    # --- SQLite operator adapter ---
    print("\n[6] Testing operator adapter (with encryption)...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        operators = SqliteOperatorAdapter(db_path, cfg.encryption_key)

        now = datetime.utcnow()
        operator = Operator(
            operator_id="op-001",
            shop_name="Kampala Shoes",
            owner_name="Ivan",
            owner_personal_phone="+256700999999",
            whapi_channel_id="CHAN-XXXXX",
            whapi_channel_token="secret-channel-token-123",
            whapi_webhook_secret="secret-webhook-abc",
            whapi_connected_phone="+256700111111",
            google_sheets_id="sheet-id-123",
            luganda_canned_response="Webale okutuukirira!",
            llm_model="claude-sonnet-4-6",
            status=OperatorStatus.ACTIVE,
            created_at=now,
        )

        operators.save(operator)
        retrieved = operators.get_by_channel_id("CHAN-XXXXX")
        assert retrieved is not None, "Operator not found after save"
        assert retrieved.operator_id == "op-001"
        assert retrieved.shop_name == "Kampala Shoes"
        # Sensitive fields are stored as ciphertext in the Operator dataclass
        # to avoid holding plaintext tokens in long-lived memory caches.
        # Callers decrypt per use.
        assert decrypt(retrieved.whapi_channel_token, cfg.encryption_key) == "secret-channel-token-123"
        assert decrypt(retrieved.whapi_webhook_secret, cfg.encryption_key) == "secret-webhook-abc"
        assert retrieved.status == OperatorStatus.ACTIVE
        print(f"    Saved and retrieved operator: {retrieved.shop_name}")
        print(f"    Channel token ciphertext stored; decrypts to plaintext correctly")

        active = operators.get_all_active()
        assert len(active) == 1
        print(f"    get_all_active: {len(active)} operator(s)")

        operators.update_status("op-001", OperatorStatus.DISCONNECTED)
        active_after = operators.get_all_active()
        assert len(active_after) == 0
        print("    update_status to DISCONNECTED verified")
        print("    PASS")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED — Phase 1 foundation is solid.")
    print("=" * 60)


def create_test_operator():
    """Insert a test operator into the real (non-temp) SQLite DB.

    Reads WHAPI_CHANNEL_ID, WHAPI_CHANNEL_TOKEN, and OWNER_PERSONAL_PHONE
    from the environment. Generates a fresh webhook_secret and prints it
    so it can be configured in the Whapi dashboard as the
    X-Salelular-Token header value.
    """
    from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
    from app.models.operator import Operator, OperatorStatus

    cfg = validate()

    channel_id = os.getenv("WHAPI_CHANNEL_ID", "")
    channel_token = os.getenv("WHAPI_CHANNEL_TOKEN", "")
    owner_phone = os.getenv("OWNER_PERSONAL_PHONE", "")
    shop_name = os.getenv("OPERATOR_SHOP_NAME", "Test Shop")
    owner_name = os.getenv("OPERATOR_OWNER_NAME", "Test Owner")

    if not channel_id or not channel_token or not owner_phone:
        print(
            "FATAL: WHAPI_CHANNEL_ID, WHAPI_CHANNEL_TOKEN, and "
            "OWNER_PERSONAL_PHONE must all be set in .env",
            file=sys.stderr,
        )
        raise SystemExit(1)

    webhook_secret = secrets.token_urlsafe(32)
    operator_id = f"op-{secrets.token_hex(4)}"

    # Encrypt sensitive fields before storing in the Operator dataclass
    # (our adapter expects ciphertext on both save and load).
    op_adapter = SqliteOperatorAdapter(cfg.storage_db_path, cfg.encryption_key)

    # Remove any existing operator with the same channel_id
    existing = op_adapter.get_by_channel_id(channel_id)
    if existing is not None:
        print(f"Operator with channel_id={channel_id} already exists. Overwriting.")
        op_adapter._conn.execute(
            "DELETE FROM operators WHERE channel_id=?", (channel_id,)
        )
        op_adapter._conn.commit()

    operator = Operator(
        operator_id=operator_id,
        shop_name=shop_name,
        owner_name=owner_name,
        owner_personal_phone=owner_phone,
        whapi_channel_id=channel_id,
        # Adapter's save() encrypts these before storing — so pass plaintext.
        whapi_channel_token=channel_token,
        whapi_webhook_secret=webhook_secret,
        whapi_connected_phone=None,
        google_sheets_id=os.getenv("GOOGLE_SHEETS_ID", ""),
        luganda_canned_response=(
            "Webale okutuukirira! Nnyinza okuyamba oluvannyuma."
        ),
        llm_model="claude-sonnet-4-6",
        status=OperatorStatus.ACTIVE,
        created_at=datetime.utcnow(),
    )

    # We need to encrypt the sensitive fields before calling save(), because
    # save() just stores what's in the dataclass directly now (no encryption
    # on save — encryption happens via encrypt()). Actually, re-check: the
    # current _serialise() encrypts channel_token and webhook_secret. So we
    # pass plaintext and save() encrypts on write. Good.
    op_adapter.save(operator)

    print("=" * 60)
    print("Test operator created.")
    print("=" * 60)
    print(f"  operator_id:   {operator_id}")
    print(f"  shop_name:     {shop_name}")
    print(f"  channel_id:    {channel_id}")
    print(f"  owner_phone:   {owner_phone}")
    print()
    print("Configure this as your Whapi webhook header:")
    print(f"  X-Salelular-Token: {webhook_secret}")
    print()
    print("Whapi dashboard → your channel → Settings → Webhooks:")
    print("  URL:     https://<your-ngrok>.ngrok.io/webhook")
    print("  Events:  messages (post), users (post), users (delete)")
    print("  Header:  X-Salelular-Token: <the secret above>")


def list_sessions():
    """List sessions for an operator, optionally filtered by stage."""
    cfg = validate()
    storage = SqliteStorageAdapter(cfg.storage_db_path)

    op_id = None
    stage_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--list" and i + 1 < len(sys.argv):
            op_id = sys.argv[i + 1]
        if arg == "--stage" and i + 1 < len(sys.argv):
            stage_filter = sys.argv[i + 1]

    if not op_id:
        print("Usage: --list {operator_id} [--stage {stage}]", file=sys.stderr)
        raise SystemExit(1)

    # Get all sessions by scanning all stages
    all_stages = [s.value for s in Stage]
    sessions = []
    for stage_val in all_stages:
        sessions.extend(storage.get_by_stage(op_id, stage_val))

    if stage_filter:
        sessions = [s for s in sessions if s.stage.value == stage_filter]

    if not sessions:
        print(f"No sessions found for {op_id}" +
              (f" (stage={stage_filter})" if stage_filter else ""))
        return

    print(f"Sessions for {op_id}:")
    for s in sessions:
        print(f"  {s.phone}  stage={s.stage.value}  name={s.name or '?'}  "
              f"intent={s.intent or '?'}  history={len(s.history)} turns  "
              f"last_active={s.last_active}")


def reset_cap():
    """Reset daily message count for a phone (testing only)."""
    from app.pipeline.runner import _daily_counts, _daily_alerted

    phone = None
    for i, arg in enumerate(sys.argv):
        if arg == "--reset-cap" and i + 1 < len(sys.argv):
            phone = sys.argv[i + 1]

    if not phone:
        print("Usage: --reset-cap {phone}", file=sys.stderr)
        raise SystemExit(1)

    # Can't reset the in-memory dict of a running server from here.
    # This is for documentation/testing purposes.
    print(f"Note: daily cap is in-memory on the running server process.")
    print(f"To reset, restart the server (caps reset on restart).")
    print(f"Or wait until midnight UTC (caps reset via date key change).")


if __name__ == "__main__":
    if "--create-test-operator" in sys.argv:
        create_test_operator()
    elif "--list" in sys.argv:
        list_sessions()
    elif "--reset-cap" in sys.argv:
        reset_cap()
    else:
        main()
