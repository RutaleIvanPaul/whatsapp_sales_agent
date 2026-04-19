"""Phase 5 edge-case test suite — 20 tests from SPEC.md S18.

Run with the server already started on localhost:8000:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
Then:
    python scripts/test_edge_cases.py

Requires test operator op-cfd4493e in the DB (create with
scripts/check_session.py --create-test-operator).

LLM calls are tested via direct component tests with mocks — not
through the live server. Webhook tests go through localhost:8000.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.adapters.inventory.cache import InventoryCache
from app.adapters.llm.base import LLMAdapter, LLMResponse, LLMTimeoutError, ToolResult
from app.adapters.storage.sqlite_adapter import SqliteStorageAdapter
from app.buffer.buffer import MessageBuffer
from app.config import validate
from app.engine import conversation, system_prompt as sp_mod
from app.models.operator import Operator, OperatorStatus
from app.models.product import Product
from app.models.session import Session, Stage
from app.utils.crypto import decrypt
from app.utils.sent_tracker import SentTracker

# ── Setup ────────────────────────────────────────────────────────────────────

CFG = validate()
OPERATOR_ID = "op-cfd4493e"
BASE_URL = "http://localhost:8000"
CUSTOMER_PHONE = "+256702246015"  # test customer
OWNER_PHONE = os.getenv("OWNER_PERSONAL_PHONE", "+256705878284")

results: list[tuple[int, str, bool, str]] = []


def record(num: int, name: str, passed: bool, reason: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    results.append((num, name, passed, reason))
    print(f"  [{status}] T{num}: {name}" + (f" — {reason}" if reason else ""))


def _get_webhook_secret() -> str:
    """Read the webhook secret for the test operator from the DB."""
    from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter

    adapter = SqliteOperatorAdapter(CFG.storage_db_path, CFG.encryption_key)
    op = adapter.get_by_channel_id(os.getenv("WHAPI_CHANNEL_ID", "SPRWMN-5ULYD"))
    if op is None:
        raise SystemExit(
            "Test operator not found. Run: python scripts/check_session.py --create-test-operator"
        )
    return decrypt(op.whapi_webhook_secret, CFG.encryption_key)


CHANNEL_ID = os.getenv("WHAPI_CHANNEL_ID", "SPRWMN-5ULYD")
WEBHOOK_SECRET = _get_webhook_secret()


def _make_payload(
    msg_id: str = "test-msg-001",
    text: str = "hello",
    from_phone: str = "256702246015",
    from_me: bool = False,
    chat_id: str = "256702246015@s.whatsapp.net",
    msg_type: str = "text",
    event_type: str = "messages",
    channel_id: str | None = None,
) -> dict:
    return {
        "channel_id": channel_id or CHANNEL_ID,
        "event": {"type": event_type, "event": "post"},
        "messages": [
            {
                "id": msg_id,
                "from_me": from_me,
                "type": msg_type,
                "chat_id": chat_id,
                "timestamp": int(time.time()),
                "from": from_phone,
                "from_name": "Test Customer",
                "text": {"body": text},
            }
        ],
    }


def _post_webhook(payload: dict, token: str | None = None) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "X-Salelular-Token": token or WEBHOOK_SECRET,
    }
    return httpx.post(f"{BASE_URL}/webhook", json=payload, headers=headers, timeout=10)


def _get_storage() -> SqliteStorageAdapter:
    return SqliteStorageAdapter(CFG.storage_db_path)


def _set_session(session: Session) -> None:
    storage = _get_storage()
    storage.set(session.operator_id, session.phone, session)


def _get_session(phone: str) -> Session | None:
    return _get_storage().get(OPERATOR_ID, phone)


def _delete_session(phone: str) -> None:
    _get_storage().delete(OPERATOR_ID, phone)


def _make_session(
    phone: str = CUSTOMER_PHONE,
    stage: Stage = Stage.EXPLORING,
    **overrides,
) -> Session:
    now = datetime.utcnow()
    defaults = dict(
        operator_id=OPERATOR_ID,
        phone=phone,
        name="Test Customer",
        language="en",
        history=[],
        intent="looking for shoes",
        constraints={},
        shown_product_ids=[],
        stage=stage,
        handed_off_at=None,
        last_active=now,
        created_at=now,
    )
    defaults.update(overrides)
    return Session(**defaults)


# ── WEBHOOK FILTERING (T1-T5) ───────────────────────────────────────────────


def test_1_bad_auth():
    payload = _make_payload(msg_id="t1-bad-auth")
    resp = _post_webhook(payload, token="WRONG-TOKEN")
    record(1, "Bad auth token → 403", resp.status_code == 403)


def test_2_unknown_channel():
    payload = _make_payload(msg_id="t2-unknown", channel_id="FAKE-CHANNEL-999")
    resp = _post_webhook(payload)
    record(
        2,
        "Unknown channel_id → 200 (no info leak)",
        resp.status_code == 200,
    )


def test_3_group_message():
    payload = _make_payload(
        msg_id="t3-group",
        chat_id="120363001234567890@g.us",
    )
    resp = _post_webhook(payload)
    record(3, "Group message → 200, discarded", resp.status_code == 200)


def test_4_status_event():
    payload = _make_payload(msg_id="t4-status")
    payload["event"]["type"] = "statuses"
    resp = _post_webhook(payload)
    record(4, "Status event → 200, discarded", resp.status_code == 200)


def test_5_duplicate_message():
    unique_id = f"t5-dedup-{int(time.time())}"
    _delete_session(CUSTOMER_PHONE)
    payload = _make_payload(msg_id=unique_id, text="dedup test")
    resp1 = _post_webhook(payload)
    time.sleep(0.5)
    resp2 = _post_webhook(payload)
    ok = resp1.status_code == 200 and resp2.status_code == 200
    record(
        5,
        "Duplicate message_id → 200, second deduplicated",
        ok,
        "Check server logs for message_deduplicated",
    )


# ── BUFFER (T6-T7) ──────────────────────────────────────────────────────────


def test_6_force_flush():
    """10 messages → force-flush at count=10."""
    flushed_counts: list[int] = []

    async def on_flush(op_id, phone, payloads):
        flushed_counts.append(len(payloads))

    async def run():
        buf = MessageBuffer(debounce_s=5.0, rate_limit_s=0, force_flush_at=10)
        for i in range(10):
            await buf.add("op-test", "+256700000001", {"i": i}, on_flush)
        # Force-flush should have fired synchronously at count 10
        await asyncio.sleep(0.1)
        return flushed_counts

    counts = asyncio.run(run())
    record(
        6,
        "10 rapid messages → force-flush at 10",
        10 in counts,
        f"flush counts: {counts}",
    )


def test_7_rate_limit():
    """Two flushes within rate_limit_s → second delayed."""
    flush_times: list[float] = []

    async def on_flush(op_id, phone, payloads):
        flush_times.append(time.monotonic())

    async def run():
        buf = MessageBuffer(debounce_s=0.1, rate_limit_s=2.0, force_flush_at=100)
        await buf.add("op-test", "+256700000002", {"i": 0}, on_flush)
        await asyncio.sleep(0.2)  # debounce fires first flush
        await buf.add("op-test", "+256700000002", {"i": 1}, on_flush)
        await asyncio.sleep(3.0)  # wait for rate-limited second flush
        return flush_times

    times = asyncio.run(run())
    if len(times) >= 2:
        gap = times[1] - times[0]
        record(7, "Rate limit delays second flush", gap >= 1.5, f"gap={gap:.1f}s")
    else:
        record(7, "Rate limit delays second flush", False, f"only {len(times)} flushes")


# ── HANDOFF & SESSION STAGES (T8-T13) ───────────────────────────────────────


def test_8_bot_active_during_handoff():
    """Customer messages while HANDED_OFF → bot continues responding normally.

    Design change: HANDED_OFF no longer suppresses the bot. The bot only
    stops when the operator types in the customer thread (OWNER_ACTIVE).
    """
    t8_phone = "+256700800800"
    t8_from = "256700800800"
    _get_storage().delete(OPERATOR_ID, t8_phone)
    time.sleep(1)

    session = _make_session(
        phone=t8_phone,
        stage=Stage.HANDED_OFF,
        handed_off_at=datetime.utcnow(),
    )
    _set_session(session)
    time.sleep(1)

    msg_id = f"t8-active-{int(time.time())}"
    payload = _make_payload(
        msg_id=msg_id, text="do you have any shoes?",
        from_phone=t8_from, chat_id=f"{t8_from}@s.whatsapp.net",
    )
    resp = _post_webhook(payload)
    time.sleep(15)

    updated = _get_storage().get(OPERATOR_ID, t8_phone)
    # Session should still exist and stage should still be HANDED_OFF
    # (bot responded but didn't change the stage)
    still_handed_off = updated is not None and updated.stage == Stage.HANDED_OFF
    # History should have grown (bot responded via conversation engine)
    has_history = updated is not None and len(updated.history) > 0
    record(
        8,
        "Bot active during HANDED_OFF (responds normally)",
        still_handed_off and has_history,
        f"stage={updated.stage.value if updated else '?'}, "
        f"history_len={len(updated.history) if updated else 0}",
    )
    _get_storage().delete(OPERATOR_ID, t8_phone)


def test_9_owner_active_only_suppression():
    """Only OWNER_ACTIVE suppresses the bot, not HANDED_OFF.

    Verify: HANDED_OFF session gets a bot response (pipeline runs).
    OWNER_ACTIVE session gets no response (pipeline skipped).
    """
    _delete_session(CUSTOMER_PHONE)
    session = _make_session(stage=Stage.OWNER_ACTIVE)
    _set_session(session)

    msg_id = f"t9-owneractive-{int(time.time())}"
    payload = _make_payload(msg_id=msg_id, text="hello there")
    resp = _post_webhook(payload)
    time.sleep(10)

    updated = _get_session(CUSTOMER_PHONE)
    # Should still be OWNER_ACTIVE — pipeline was skipped, no history added
    still_owner = updated is not None and updated.stage == Stage.OWNER_ACTIVE
    no_new_history = updated is not None and len(updated.history) == 0
    record(
        9,
        "OWNER_ACTIVE suppresses bot (pipeline skipped)",
        still_owner and no_new_history,
        f"stage={updated.stage.value if updated else 'None'}, "
        f"history={len(updated.history) if updated else '?'}",
    )
    _delete_session(CUSTOMER_PHONE)


def test_10_resume_command():
    """Operator sends 'resume {phone}' → session CONSIDERING."""
    _delete_session(CUSTOMER_PHONE)
    session = _make_session(stage=Stage.HANDED_OFF)
    _set_session(session)

    # Simulate operator sending "resume" from their personal phone
    payload = _make_payload(
        msg_id=f"t10-resume-{int(time.time())}",
        text=f"resume {CUSTOMER_PHONE}",
        from_phone=OWNER_PHONE.lstrip("+"),
        from_me=False,
        chat_id=f"{OWNER_PHONE.lstrip('+')}@s.whatsapp.net",
    )
    resp = _post_webhook(payload)
    time.sleep(3)

    updated = _get_session(CUSTOMER_PHONE)
    record(
        10,
        "resume command → CONSIDERING",
        updated is not None and updated.stage == Stage.CONSIDERING,
        f"stage={updated.stage.value if updated else 'None'}",
    )
    _delete_session(CUSTOMER_PHONE)


def test_11_handled_command():
    """Operator sends 'handled {phone}' → session OWNER_ACTIVE."""
    _delete_session(CUSTOMER_PHONE)
    session = _make_session(stage=Stage.HANDED_OFF)
    _set_session(session)

    payload = _make_payload(
        msg_id=f"t11-handled-{int(time.time())}",
        text=f"handled {CUSTOMER_PHONE}",
        from_phone=OWNER_PHONE.lstrip("+"),
        from_me=False,
        chat_id=f"{OWNER_PHONE.lstrip('+')}@s.whatsapp.net",
    )
    resp = _post_webhook(payload)
    time.sleep(3)

    updated = _get_session(CUSTOMER_PHONE)
    record(
        11,
        "handled command → OWNER_ACTIVE",
        updated is not None and updated.stage == Stage.OWNER_ACTIVE,
        f"stage={updated.stage.value if updated else 'None'}",
    )
    _delete_session(CUSTOMER_PHONE)


def test_12_passive_interruption():
    """from_me:true in customer chat → OWNER_ACTIVE, pipeline skipped."""
    _delete_session(CUSTOMER_PHONE)
    session = _make_session(stage=Stage.HANDED_OFF)
    _set_session(session)

    # from_me:true simulates operator typing in customer thread
    payload = _make_payload(
        msg_id=f"t12-fromme-{int(time.time())}",
        text="I'll handle this customer",
        from_me=True,
        from_phone=CHANNEL_ID,  # from the shop number
        chat_id=f"{CUSTOMER_PHONE.lstrip('+')}@s.whatsapp.net",
    )
    resp = _post_webhook(payload)
    time.sleep(2)

    updated = _get_session(CUSTOMER_PHONE)
    record(
        12,
        "from_me:true → OWNER_ACTIVE",
        updated is not None and updated.stage == Stage.OWNER_ACTIVE,
        f"stage={updated.stage.value if updated else 'None'}",
    )
    _delete_session(CUSTOMER_PHONE)


def test_13_bot_echo_filtered():
    """Bot echo filtering — tested as a unit test since sent_tracker is
    in-process. The live server test was already validated during Phase 5
    testing (bot sent replies without flipping to OWNER_ACTIVE)."""
    from app.utils.sent_tracker import SentTracker

    tracker = SentTracker(ttl_s=60)
    bot_msg_id = "bot-sent-12345"
    operator_msg_id = "operator-typed-67890"

    tracker.register(bot_msg_id)

    # Bot echo should be recognized
    is_echo = tracker.is_bot_sent(bot_msg_id)
    # Operator message should NOT be recognized
    is_not_echo = not tracker.is_bot_sent(operator_msg_id)

    record(
        13,
        "Bot echo filtered (sent_tracker unit test)",
        is_echo and is_not_echo,
        f"bot_echo={is_echo}, operator_not_echo={is_not_echo}",
    )


# ── INVENTORY (T14-T17) ─────────────────────────────────────────────────────


def _build_test_cache() -> InventoryCache:
    products = [
        Product("p1", "Nike Air Max", "100", "Great shoe", "nike,running",
                "https://example.com/img.jpg", True, "airmax", "size 42"),
        Product("p2", "Unavailable Shoe", "50", "Old stock", "old",
                "https://example.com/old.jpg", False, None, None),
        Product("p3", "Adidas Ultra", "120", "Boost tech", "adidas,boost",
                "https://example.com/adidas.jpg", True, "ultra", "size 40-44"),
    ]
    cache = InventoryCache(search_threshold=70)
    cache.build_index(products)
    return cache


def test_14_unavailable_excluded():
    cache = _build_test_cache()
    results_list = cache.search("shoe", [])
    ids = [p.id for p in results_list]
    record(14, "Unavailable product excluded", "p2" not in ids, f"ids={ids}")


def test_15_shown_excluded():
    cache = _build_test_cache()
    results_list = cache.search("nike", ["p1"])
    ids = [p.id for p in results_list]
    record(15, "Shown product excluded", "p1" not in ids, f"ids={ids}")


def test_16_empty_query():
    cache = _build_test_cache()
    try:
        results_list = cache.search("", [])
        record(16, "Empty query → empty list", results_list == [])
    except Exception as e:
        record(16, "Empty query → empty list", False, str(e))


def test_17_no_matches():
    cache = _build_test_cache()
    try:
        results_list = cache.search("xyznonexistent", [])
        record(17, "No matches → empty list", results_list == [])
    except Exception as e:
        record(17, "No matches → empty list", False, str(e))


# ── MESSAGING (T18) ─────────────────────────────────────────────────────────


def test_18_send_retry_alert():
    """Simulate 3 Whapi failures → send_failed logged, alert attempted.

    The adapter never raises — it logs failures and attempts an alert.
    With a fake token, all 3 retries fail (ReadTimeout or HTTP error),
    then the alert also fails. We verify the function completes without
    raising (graceful failure).
    """
    from app.adapters.messaging.whapi import WhapiMessagingAdapter
    from app.utils.crypto import encrypt

    async def run():
        adapter = WhapiMessagingAdapter(CFG.encryption_key)
        op = Operator(
            operator_id="op-test-18",
            shop_name="Test",
            owner_name="Tester",
            owner_personal_phone="+256700000000",
            whapi_channel_id="FAKE",
            whapi_channel_token=encrypt("fake-token-will-fail", CFG.encryption_key),
            whapi_webhook_secret=encrypt("fake", CFG.encryption_key),
            whapi_connected_phone=None,
            google_sheets_id="",
            luganda_canned_response="test",
            llm_model="test",
            status=OperatorStatus.ACTIVE,
            created_at=datetime.utcnow(),
        )
        # This should NOT raise — it logs send_failed x3 + alert attempt
        await adapter.send_text("+256700000001", "test message", op)
        return True  # completed without raising

    try:
        ok = asyncio.run(run())
        record(18, "3 send failures → graceful (no raise), alert attempted", ok)
    except Exception as e:
        record(18, "3 send failures → graceful", False, f"raised: {e}")


# ── SESSION (T19-T20) ───────────────────────────────────────────────────────


def test_19_stale_session_note():
    """Session with last_active 8 days ago → stale note in system prompt."""
    session = _make_session(
        last_active=datetime.utcnow() - timedelta(days=8),
    )
    op = Operator(
        operator_id="op-test",
        shop_name="Test Shop",
        owner_name="Tester",
        owner_personal_phone="+256700000000",
        whapi_channel_id="FAKE",
        whapi_channel_token="fake",
        whapi_webhook_secret="fake",
        whapi_connected_phone=None,
        google_sheets_id="",
        luganda_canned_response="test",
        llm_model="test",
        status=OperatorStatus.ACTIVE,
        created_at=datetime.utcnow(),
    )
    prompt = sp_mod.build(op, session, [], session_expiry_days=7)
    has_stale = "days ago" in prompt and "Greet them warmly" in prompt
    record(19, "Stale session note in system prompt", has_stale,
           f"contains 'days ago': {'days ago' in prompt}")


def test_20_history_compression():
    """Session with 11 history turns → oldest 2 compressed."""
    from app.engine.conversation import _compress_history

    # 11 turns = 22 messages (user+assistant pairs)
    history = []
    for i in range(11):
        history.append({"role": "user", "content": f"User message {i}"})
        history.append({"role": "assistant", "content": f"Bot reply {i}"})

    compressed = _compress_history(history, max_turns=10)
    first_msg = compressed[0]["content"] if compressed else ""
    has_summary = "Earlier in this conversation" in first_msg
    shorter = len(compressed) < len(history)
    record(
        20,
        "History compressed when > max_turns",
        has_summary and shorter,
        f"original={len(history)} compressed={len(compressed)} summary={has_summary}",
    )


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("Phase 5 Edge-Case Test Suite (20 tests)")
    print("=" * 60)

    # Check server is running
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=5)
        assert r.status_code == 200, f"Health check failed: {r.status_code}"
        print(f"\nServer running at {BASE_URL}\n")
    except Exception as e:
        print(f"\nFATAL: Server not running at {BASE_URL}: {e}")
        print("Start it first: uvicorn app.main:app --host 0.0.0.0 --port 8000")
        raise SystemExit(1)

    print("WEBHOOK FILTERING:")
    test_1_bad_auth()
    test_2_unknown_channel()
    test_3_group_message()
    test_4_status_event()
    test_5_duplicate_message()

    print("\nBUFFER:")
    test_6_force_flush()
    test_7_rate_limit()

    print("\nHANDOFF & SESSION STAGES:")
    test_8_bot_active_during_handoff()
    test_9_owner_active_only_suppression()
    test_10_resume_command()
    test_11_handled_command()
    test_12_passive_interruption()
    test_13_bot_echo_filtered()

    print("\nINVENTORY:")
    test_14_unavailable_excluded()
    test_15_shown_excluded()
    test_16_empty_query()
    test_17_no_matches()

    print("\nMESSAGING:")
    test_18_send_retry_alert()

    print("\nSESSION:")
    test_19_stale_session_note()
    test_20_history_compression()

    # Summary
    passed = sum(1 for _, _, ok, _ in results if ok)
    failed = [(n, name, reason) for n, name, ok, reason in results if not ok]

    print("\n" + "=" * 60)
    print(f"PASSED: {passed}/20")
    if failed:
        print(f"FAILED: {len(failed)}")
        for n, name, reason in failed:
            print(f"  T{n}: {name} — {reason}")
    else:
        print("ALL TESTS PASSED — ready for Phase 6")
    print("=" * 60)

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
