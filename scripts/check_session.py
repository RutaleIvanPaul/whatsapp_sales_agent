"""Phase 1 verification script. Run with: python scripts/check_session.py"""

import json
import os
import sys
import tempfile

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from app.config import validate
from app.adapters.storage.sqlite_adapter import SqliteStorageAdapter
from app.adapters.tenant.sqlite_adapter import SqliteTenantAdapter
from app.models.session import Session, Stage
from app.models.tenant import Tenant, TenantStatus
from app.utils.crypto import decrypt, encrypt
from app.utils.log import log
from app.utils.phone import hash_for_log, normalise


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
    print("    PASS")

    # --- Logging ---
    print("\n[4] Testing structured logger...")
    log("test_event", tenant_id="t-001", phone_hash=phone_hash, count=42)
    print("    (see JSON line above)")
    print("    PASS")

    # --- SQLite storage adapter ---
    print("\n[5] Testing session storage adapter...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        storage = SqliteStorageAdapter(db_path)

        now = datetime.utcnow()
        session = Session(
            tenant_id="t-001",
            phone="+256700123456",
            name="Test Customer",
            language="en",
            history=[{"role": "user", "content": "Hello"}],
            intent="buy shoes",
            constraints={"size": "42", "colour": "black"},
            shown_product_ids=["prod-1"],
            stage=Stage.EXPLORING,
            active_handoff_phone=None,
            handed_off_at=None,
            last_holding_sent=None,
            last_active=now,
            created_at=now,
        )

        storage.set("t-001", "+256700123456", session)
        retrieved = storage.get("t-001", "+256700123456")
        assert retrieved is not None, "Session not found after save"
        assert retrieved.tenant_id == session.tenant_id
        assert retrieved.phone == session.phone
        assert retrieved.name == session.name
        assert retrieved.stage == Stage.EXPLORING
        assert retrieved.constraints == {"size": "42", "colour": "black"}
        assert retrieved.shown_product_ids == ["prod-1"]
        print(f"    Saved and retrieved session for {hash_for_log(session.phone)}")

        storage.delete("t-001", "+256700123456")
        assert storage.get("t-001", "+256700123456") is None
        print("    Delete verified")
        print("    PASS")

    # --- SQLite tenant adapter ---
    print("\n[6] Testing tenant adapter (with encryption)...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        tenants = SqliteTenantAdapter(db_path, cfg.encryption_key)

        now = datetime.utcnow()
        tenant = Tenant(
            tenant_id="t-001",
            shop_name="Kampala Shoes",
            owner_name="Ivan",
            owner_personal_phone="+256700999999",
            whapi_channel_id="CHAN-XXXXX",
            whapi_channel_token="secret-channel-token-123",
            whapi_webhook_secret="secret-webhook-abc",
            whapi_connected_phone="+256700111111",
            google_sheets_id="sheet-id-123",
            luganda_canned_response="Webale okutuukirira!",
            llm_model="gpt-4o",
            status=TenantStatus.ACTIVE,
            created_at=now,
        )

        tenants.save(tenant)
        retrieved = tenants.get_by_channel_id("CHAN-XXXXX")
        assert retrieved is not None, "Tenant not found after save"
        assert retrieved.tenant_id == "t-001"
        assert retrieved.shop_name == "Kampala Shoes"
        assert retrieved.whapi_channel_token == "secret-channel-token-123"
        assert retrieved.whapi_webhook_secret == "secret-webhook-abc"
        assert retrieved.status == TenantStatus.ACTIVE
        print(f"    Saved and retrieved tenant: {retrieved.shop_name}")
        print(f"    Channel token decrypted correctly: {retrieved.whapi_channel_token[:10]}...")

        active = tenants.get_all_active()
        assert len(active) == 1
        print(f"    get_all_active: {len(active)} tenant(s)")

        tenants.update_status("t-001", TenantStatus.DISCONNECTED)
        active_after = tenants.get_all_active()
        assert len(active_after) == 0
        print("    update_status to DISCONNECTED verified")
        print("    PASS")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED — Phase 1 foundation is solid.")
    print("=" * 60)


if __name__ == "__main__":
    main()
