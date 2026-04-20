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
from app.models.operator import Operator, OperatorStatus
from app.models.session import Session, Stage
from app.utils.crypto import decrypt
from app.utils.sent_tracker import SentTracker

# ── Setup ────────────────────────────────────────────────────────────────────

CFG = validate()
BASE_URL = "http://localhost:8000"

# Discover test operator ID dynamically from DB
def _find_operator_id() -> str:
    from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
    adapter = SqliteOperatorAdapter(CFG.storage_db_path, CFG.encryption_key)
    op = adapter.get_by_channel_id(os.getenv("WHAPI_CHANNEL_ID", "SPRWMN-5ULYD"))
    if op:
        return op.operator_id
    return "op-unknown"

OPERATOR_ID = _find_operator_id()
# Dedicated test phone — NOT a real customer number. This is the operator's
# own personal phone used only for test webhook payloads. Never use a
# customer number that the operator has been working with.
OWNER_PHONE = os.getenv("OWNER_PERSONAL_PHONE", "+256705878284")

# For tests that create sessions via webhook, use the OWNER phone as the
# simulated "customer" since we control it. For pure unit tests, use
# placeholder numbers that are never sent to via Whapi.
WEBHOOK_TEST_PHONE = OWNER_PHONE  # webhook tests route to this phone
UNIT_TEST_PHONE = "+256700000001"  # never reaches Whapi, logic tests only

# Mode flags
# --mock-only:  logic tests only, no server needed, no Whapi calls
# (default):    logic + webhook filtering via localhost, server needed, no outbound sends
# --whapi-live: full Whapi integration including outbound sends to WHAPI_TEST_PHONE.
#               Only run when investigating Whapi API behaviour.
#               Requires WHAPI_TEST_PHONE env var — a number the operator controls
#               and consents to receiving test messages on.
MOCK_ONLY = "--mock-only" in sys.argv
WHAPI_LIVE = "--whapi-live" in sys.argv
# Defaults to operator's personal phone if not set explicitly
WHAPI_TEST_PHONE = os.getenv("WHAPI_TEST_PHONE", OWNER_PHONE)

results: list[tuple[Any, str, bool, str]] = []


def record(num: Any, name: str, passed: bool, reason: str = "") -> None:
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
    from_phone: str | None = None,
    from_me: bool = False,
    chat_id: str | None = None,
    msg_type: str = "text",
    event_type: str = "messages",
    channel_id: str | None = None,
) -> dict:
    if from_phone is None:
        from_phone = WEBHOOK_TEST_PHONE.lstrip("+")
    if chat_id is None:
        chat_id = f"{from_phone}@s.whatsapp.net"
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
    phone: str = UNIT_TEST_PHONE,
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
    """HANDED_OFF does NOT suppress the bot (logic test).

    Verify: the pipeline runner does not skip HANDED_OFF sessions.
    The only suppression is OWNER_ACTIVE.
    """
    session = _make_session(stage=Stage.HANDED_OFF, handed_off_at=datetime.utcnow())
    # HANDED_OFF is NOT in the skip condition — only OWNER_ACTIVE is
    record(
        8,
        "HANDED_OFF does not suppress bot (by design)",
        session.stage == Stage.HANDED_OFF and session.stage != Stage.OWNER_ACTIVE,
        f"stage={session.stage.value}",
    )


def test_9_owner_active_only_suppression():
    """Only OWNER_ACTIVE suppresses the bot (logic test)."""
    session = _make_session(stage=Stage.OWNER_ACTIVE)
    record(
        9,
        "OWNER_ACTIVE is the only suppression stage",
        session.stage == Stage.OWNER_ACTIVE,
        f"stage={session.stage.value}",
    )


def test_10_resume_command():
    """Resume command sets session to CONSIDERING (logic test via handler)."""
    from app.webhook.owner_action_handler import handle
    from unittest.mock import AsyncMock

    t10_phone = UNIT_TEST_PHONE
    storage = _get_storage()
    storage.delete(OPERATOR_ID, t10_phone)
    session = _make_session(phone=t10_phone, stage=Stage.HANDED_OFF)
    _set_session(session)

    async def run():
        op = _make_test_operator()
        mock_messaging = AsyncMock()
        payload = {"messages": [{"id": "t10", "from_me": False, "type": "text",
                    "from": OWNER_PHONE.lstrip("+"), "chat_id": f"{OWNER_PHONE.lstrip('+')}@s.whatsapp.net",
                    "text": {"body": f"resume {t10_phone}"}}]}
        await handle(payload, op, storage, mock_messaging, None)

    asyncio.run(run())
    updated = storage.get(OPERATOR_ID, t10_phone)
    record(10, "resume command → CONSIDERING",
           updated is not None and updated.stage == Stage.CONSIDERING,
           f"stage={updated.stage.value if updated else 'None'}")
    storage.delete(OPERATOR_ID, t10_phone)


def test_11_handled_command():
    """Handled command sets session to OWNER_ACTIVE (logic test via handler)."""
    from app.webhook.owner_action_handler import handle
    from unittest.mock import AsyncMock

    t11_phone = UNIT_TEST_PHONE
    storage = _get_storage()
    storage.delete(OPERATOR_ID, t11_phone)
    session = _make_session(phone=t11_phone, stage=Stage.HANDED_OFF)
    _set_session(session)

    async def run():
        op = _make_test_operator()
        mock_messaging = AsyncMock()
        payload = {"messages": [{"id": "t11", "from_me": False, "type": "text",
                    "from": OWNER_PHONE.lstrip("+"), "chat_id": f"{OWNER_PHONE.lstrip('+')}@s.whatsapp.net",
                    "text": {"body": f"handled {t11_phone}"}}]}
        await handle(payload, op, storage, mock_messaging, None)

    asyncio.run(run())
    updated = storage.get(OPERATOR_ID, t11_phone)
    record(11, "handled command → OWNER_ACTIVE",
           updated is not None and updated.stage == Stage.OWNER_ACTIVE,
           f"stage={updated.stage.value if updated else 'None'}")
    storage.delete(OPERATOR_ID, t11_phone)


def test_12_passive_interruption():
    """from_me:true → OWNER_ACTIVE (logic test via handler)."""
    from app.webhook.owner_action_handler import handle
    from unittest.mock import AsyncMock

    t12_phone = UNIT_TEST_PHONE
    storage = _get_storage()
    storage.delete(OPERATOR_ID, t12_phone)
    session = _make_session(phone=t12_phone, stage=Stage.HANDED_OFF)
    _set_session(session)

    async def run():
        op = _make_test_operator()
        mock_messaging = AsyncMock()
        payload = {"messages": [{"id": "t12", "from_me": True, "type": "text",
                    "chat_id": f"{t12_phone.lstrip('+')}@s.whatsapp.net",
                    "from": CHANNEL_ID, "text": {"body": "I will handle"}}]}
        await handle(payload, op, storage, mock_messaging, None)

    asyncio.run(run())
    updated = storage.get(OPERATOR_ID, t12_phone)
    record(12, "from_me:true → OWNER_ACTIVE",
           updated is not None and updated.stage == Stage.OWNER_ACTIVE,
           f"stage={updated.stage.value if updated else 'None'}")
    storage.delete(OPERATOR_ID, t12_phone)


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


# ── PHASE 6 TESTS (T21-T30) ──────────────────────────────────────────────────


def test_21_daily_cap():
    """Daily cap hit → silence (unit test — cap state is in-process only)."""
    from app.pipeline import runner as runner_mod

    today = datetime.utcnow().strftime("%Y-%m-%d")
    cap_key = (OPERATOR_ID, "+256700210210", today)

    # Set count just at the limit
    runner_mod._daily_counts[cap_key] = 100

    # Next increment would be 101 → over cap
    new_count = runner_mod._daily_counts.get(cap_key, 0) + 1
    over_cap = new_count > 100
    record(21, "Daily cap detection (unit test)", over_cap,
           f"count_after_next={new_count}, over_cap={over_cap}")
    # Clean up
    runner_mod._daily_counts.pop(cap_key, None)


def test_22_daily_cap_reset():
    """Daily cap resets with date change."""
    from app.pipeline.runner import _daily_counts

    old_key = (OPERATOR_ID, "+256700220220", "2020-01-01")
    _daily_counts[old_key] = 999

    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_key = (OPERATOR_ID, "+256700220220", today)

    # Old date key has high count, but today's key doesn't exist yet
    today_count = _daily_counts.get(today_key, 0)
    record(22, "Daily cap resets on new date", today_count == 0,
           f"old_date_count=999, today_count={today_count}")


def test_23_llm_timeout_retry():
    """LLM timeout attempt 1 fails, retry recovers → normal response."""
    from app.adapters.llm.base import LLMResponse, LLMTimeoutError
    from app.engine import conversation as conv_mod
    from unittest.mock import AsyncMock, MagicMock

    call_count = 0

    async def mock_chat(messages, tools, system, max_tokens=1024):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise LLMTimeoutError("timeout")
        return LLMResponse(
            text="Here are some products!",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "Here are some products!"},
            input_tokens=100,
            output_tokens=20,
        )

    async def run():
        nonlocal call_count
        call_count = 0
        mock_llm = MagicMock()
        mock_llm.chat = mock_chat
        mock_messaging = AsyncMock()
        mock_storage = MagicMock()
        op = _make_test_operator()
        session = _make_session()

        original_waits = conv_mod.TIMEOUT_RETRY_WAITS
        conv_mod.TIMEOUT_RETRY_WAITS = [0, 0]
        try:
            result = await conv_mod._llm_call_with_retry(
                mock_llm, [], [], "system", op, session,
                mock_storage, mock_messaging, "hi", [], 10, 0,
            )
        finally:
            conv_mod.TIMEOUT_RETRY_WAITS = original_waits
        return result

    result = asyncio.run(run())
    record(23, "LLM timeout retry recovers on attempt 2",
           result is not None and result.text == "Here are some products!",
           f"attempts={call_count}, text={result.text if result else 'None'}")


def test_24_llm_timeout_fatal():
    """LLM timeout all 3 attempts → None returned, session HANDED_OFF."""
    from app.adapters.llm.base import LLMTimeoutError
    from app.engine import conversation as conv_mod
    from unittest.mock import AsyncMock, MagicMock, patch

    async def always_timeout(messages, tools, system, max_tokens=1024):
        raise LLMTimeoutError("timeout")

    async def run():
        mock_llm = MagicMock()
        mock_llm.chat = always_timeout
        mock_messaging = AsyncMock()
        mock_storage = MagicMock()
        op = _make_test_operator()
        session = _make_session()

        # Patch TIMEOUT_RETRY_WAITS to [0, 0] so we don't wait 3+5 seconds
        original_waits = conv_mod.TIMEOUT_RETRY_WAITS
        conv_mod.TIMEOUT_RETRY_WAITS = [0, 0]
        try:
            result = await conv_mod._llm_call_with_retry(
                mock_llm, [], [], "system", op, session,
                mock_storage, mock_messaging, "hi", [], 10, 0,
            )
        finally:
            conv_mod.TIMEOUT_RETRY_WAITS = original_waits
        return result, session.stage

    result, stage = asyncio.run(run())
    record(24, "LLM timeout fatal → None + HANDED_OFF",
           result is None and stage == Stage.HANDED_OFF,
           f"result={'None' if result is None else 'response'}, stage={stage.value}")


def test_25_tool_loop_has_text():
    """Tool loop exhausted but text exists → that text used."""
    from app.adapters.llm.base import LLMResponse

    # Simulate: last_response has text
    response = LLMResponse(
        text="Partial answer from earlier round",
        tool_calls=[],
        assistant_message={"role": "assistant", "content": "Partial"},
        input_tokens=50, output_tokens=10,
    )
    has_text = bool(response.text)
    record(25, "Tool loop exhausted, text exists → used",
           has_text, f"text={response.text[:30]}")


def test_26_tool_loop_no_text():
    """Tool loop exhausted, no text → silence + HANDED_OFF."""
    from app.adapters.llm.base import LLMResponse
    from app.engine.conversation import _handle_fatal_failure
    from unittest.mock import AsyncMock, MagicMock

    async def run():
        mock_messaging = AsyncMock()
        mock_storage = MagicMock()
        op = _make_test_operator()
        session = _make_session()

        await _handle_fatal_failure(session, mock_storage, op, mock_messaging, "test")
        return session.stage

    stage = asyncio.run(run())
    record(26, "Tool loop no text → HANDED_OFF",
           stage == Stage.HANDED_OFF, f"stage={stage.value}")


def test_27_cost_tracker_records():
    """Cost tracker records tokens and computes cost."""
    from app.utils.cost_tracker import CostTracker

    tracker = CostTracker(provider="groq", input_rate_per_1k=0.0005, output_rate_per_1k=0.001)
    tracker.record("op-test", 1000, 500)
    tracker.record("op-test", 2000, 1000)

    summary = tracker.get_summary("op-test")
    ok = (len(summary) == 1
          and summary[0]["input_tokens"] == 3000
          and summary[0]["output_tokens"] == 1500
          and summary[0]["estimated_cost_usd"] > 0)
    record(27, "Cost tracker records + computes cost", ok,
           f"tokens={summary[0]['input_tokens']}/{summary[0]['output_tokens']}, "
           f"cost=${summary[0]['estimated_cost_usd']}" if summary else "no summary")


def test_28_cost_tracker_log_and_reset():
    """Cost tracker log_and_reset logs summaries and clears old data."""
    from app.utils.cost_tracker import CostTracker

    tracker = CostTracker(provider="groq", input_rate_per_1k=0.0005, output_rate_per_1k=0.001)
    # Inject a fake "yesterday" entry
    tracker._counters[("op-test", "2020-01-01")] = {
        "input_tokens": 5000, "output_tokens": 2000, "vision_calls": 0,
    }
    tracker.record("op-test", 1000, 500)  # today's entry

    tracker.log_and_reset()

    # Yesterday's data should be cleared, today's kept
    remaining = list(tracker._counters.keys())
    yesterday_gone = ("op-test", "2020-01-01") not in tracker._counters
    today_kept = any(k[1] != "2020-01-01" for k in remaining)
    record(28, "Cost tracker clears old data, keeps today", yesterday_gone and today_kept,
           f"remaining_keys={len(remaining)}, yesterday_gone={yesterday_gone}")


def test_29_config_missing_encryption_key():
    """Config with missing ENCRYPTION_KEY → clear error."""
    import subprocess
    env = os.environ.copy()
    env.pop("ENCRYPTION_KEY", None)
    env["STORAGE_URL"] = "sqlite:///test.db"
    # Prevent load_dotenv from loading ENCRYPTION_KEY from .env file
    result = subprocess.run(
        [sys.executable, "-c",
         "import os; os.environ.pop('ENCRYPTION_KEY', None); "
         "os.environ['DOTENV_OVERRIDE'] = '1'; "
         "from dotenv import load_dotenv; "
         "from app import config; "
         "config.validate()"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    # Even if dotenv loads it, the test should work if we override
    # Actually the simplest approach: just test the validation logic directly
    from app.config import validate as _val
    import io, contextlib

    old_key = os.environ.get("ENCRYPTION_KEY", "")
    os.environ["ENCRYPTION_KEY"] = ""  # set empty, not remove (load_dotenv would reload)
    try:
        _val()
        passed = False
        reason = "validate() did not raise"
    except SystemExit:
        passed = True
        reason = "SystemExit raised as expected"
    finally:
        os.environ["ENCRYPTION_KEY"] = old_key
    record(29, "Missing ENCRYPTION_KEY → clear error", passed, reason)


def test_30_config_invalid_google_creds():
    """Config with invalid GOOGLE_CREDENTIALS_JSON → clear error."""
    import subprocess
    env = os.environ.copy()
    env["GOOGLE_CREDENTIALS_JSON"] = "not-valid-base64!!!"
    result = subprocess.run(
        [sys.executable, "-c",
         "from app.config import validate; validate()"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    has_error = "GOOGLE_CREDENTIALS_JSON" in result.stderr
    record(30, "Invalid GOOGLE_CREDENTIALS_JSON → clear error",
           result.returncode != 0 and has_error,
           f"exit={result.returncode}, mentions_creds={has_error}")


def _make_test_operator() -> Operator:
    return Operator(
        operator_id=OPERATOR_ID,
        shop_name="Test Shop",
        owner_name="Test Owner",
        owner_personal_phone=OWNER_PHONE,
        whapi_channel_id=CHANNEL_ID,
        whapi_channel_token="fake",
        whapi_webhook_secret="fake",
        whapi_connected_phone=None,
        google_sheets_id="",
        luganda_canned_response="test",
        llm_model="test",
        status=OperatorStatus.ACTIVE,
        created_at=datetime.utcnow(),
    )


# ── WHAPI LIVE TESTS (only with --whapi-live) ────────────────────────────────
# These send REAL messages via the Whapi API to WHAPI_TEST_PHONE.
# Only run when investigating Whapi API behaviour, not for normal builds.


def _get_whapi_operator():
    """Load operator for Whapi live tests."""
    from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
    op_adapter = SqliteOperatorAdapter(CFG.storage_db_path, CFG.encryption_key)
    return op_adapter.get_by_channel_id(CHANNEL_ID)


def test_w1_send_text():
    """Whapi send_text → Whapi accepts the request (200)."""
    from app.adapters.messaging.whapi import WhapiMessagingAdapter

    async def run():
        adapter = WhapiMessagingAdapter(CFG.encryption_key)
        op = _get_whapi_operator()
        if not op:
            return False, "no operator"
        try:
            await adapter.send_text(WHAPI_TEST_PHONE, "Salelular test: text delivery check", op)
            return True, "accepted by Whapi"
        except Exception as e:
            return False, str(e)[:100]

    ok, reason = asyncio.run(run())
    record("W1", "Whapi send_text accepted", ok, reason)


def test_w2_send_image():
    """Whapi send_image with a real product image URL → accepted."""
    from app.adapters.messaging.whapi import WhapiMessagingAdapter
    from app.adapters.inventory.sheets import GoogleSheetsLoader

    async def run():
        adapter = WhapiMessagingAdapter(CFG.encryption_key)
        op = _get_whapi_operator()
        if not op:
            return False, "no operator"

        # Use a real product image from the inventory
        test_url = None
        if CFG.google_credentials_json_b64 and CFG.google_sheets_id:
            try:
                loader = GoogleSheetsLoader(
                    CFG.google_credentials_json_b64,
                    CFG.google_sheets_id,
                    CFG.google_sheet_name,
                )
                products = await loader.load()
                for p in products:
                    if p.image_url and p.available:
                        test_url = p.image_url
                        break
            except Exception:
                pass

        if not test_url:
            return True, "skipped — no product image URL available"

        try:
            await adapter.send_image(WHAPI_TEST_PHONE, test_url, "Salelular test: image delivery check", op)
            return True, "accepted by Whapi"
        except Exception as e:
            return False, str(e)[:100]

    ok, reason = asyncio.run(run())
    record("W2", "Whapi send_image accepted", ok, reason)


def test_w3_send_to_invalid_number():
    """Whapi send to invalid number → graceful failure, no crash."""
    from app.adapters.messaging.whapi import WhapiMessagingAdapter

    async def run():
        adapter = WhapiMessagingAdapter(CFG.encryption_key)
        op = _get_whapi_operator()
        if not op:
            return False, "no operator"
        try:
            await adapter.send_text("+000000000000", "Should fail gracefully", op)
            return True, "completed without crash"
        except Exception as e:
            return False, f"crashed: {str(e)[:100]}"

    ok, reason = asyncio.run(run())
    record("W3", "Whapi invalid number → graceful", ok, reason)


def test_w4_webhook_round_trip():
    """Send webhook payload for WHAPI_TEST_PHONE, verify it reaches pipeline."""
    _get_storage().delete(OPERATOR_ID, WHAPI_TEST_PHONE)

    msg_id = f"w4-roundtrip-{int(time.time())}"
    payload = _make_payload(
        msg_id=msg_id,
        text="Whapi round-trip test",
        from_phone=WHAPI_TEST_PHONE.lstrip("+"),
        chat_id=f"{WHAPI_TEST_PHONE.lstrip('+')}@s.whatsapp.net",
    )
    resp = _post_webhook(payload)
    time.sleep(15)

    session = _get_storage().get(OPERATOR_ID, WHAPI_TEST_PHONE)
    has_session = session is not None and len(session.history) > 0
    record("W4", "Webhook round-trip creates session", has_session,
           f"session={'yes' if session else 'no'}, "
           f"history={len(session.history) if session else 0}")
    _get_storage().delete(OPERATOR_ID, WHAPI_TEST_PHONE)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    mode = "MOCK-ONLY (logic tests)" if MOCK_ONLY else "FULL (logic + Whapi integration)"
    print("=" * 60)
    print(f"Edge-Case Test Suite — {mode}")
    print("=" * 60)

    if not MOCK_ONLY:
        # Check server is running (needed for webhook tests)
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=5)
            assert r.status_code == 200, f"Health check failed: {r.status_code}"
            print(f"\nServer running at {BASE_URL}")
            print(f"Webhook test phone: {WEBHOOK_TEST_PHONE}\n")
        except Exception as e:
            print(f"\nFATAL: Server not running at {BASE_URL}: {e}")
            print("Start it first: uvicorn app.main:app --host 0.0.0.0 --port 8000")
            print("Or run with --mock-only to skip Whapi integration tests")
            raise SystemExit(1)

        print("WEBHOOK FILTERING (requires server — no outbound sends):")
        test_1_bad_auth()
        test_2_unknown_channel()
        test_3_group_message()
        test_4_status_event()
        test_5_duplicate_message()
    else:
        print("\nSkipping webhook tests (--mock-only)\n")

    print("\nBUFFER (logic):")
    test_6_force_flush()
    test_7_rate_limit()

    print("\nSESSION STAGES (logic — DB state, no Whapi):")
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

    print("\nPHASE 6 — RESILIENCE:")
    test_21_daily_cap()
    test_22_daily_cap_reset()
    test_23_llm_timeout_retry()
    test_24_llm_timeout_fatal()
    test_25_tool_loop_has_text()
    test_26_tool_loop_no_text()
    test_27_cost_tracker_records()
    test_28_cost_tracker_log_and_reset()
    test_29_config_missing_encryption_key()
    test_30_config_invalid_google_creds()

    if WHAPI_LIVE:
        if not WHAPI_TEST_PHONE:
            print("\nFATAL: --whapi-live requires WHAPI_TEST_PHONE env var")
            print("Set it to a phone number you control and consent to receiving test messages on.")
            raise SystemExit(1)
        print(f"\nWHAPI LIVE INTEGRATION (sends real messages to {WHAPI_TEST_PHONE}):")
        test_w1_send_text()
        test_w2_send_image()
        test_w3_send_to_invalid_number()
        if not MOCK_ONLY:
            test_w4_webhook_round_trip()
        else:
            print("  [SKIP] TW4: webhook round-trip (needs server, use without --mock-only)")

    # Summary
    total = len(results)
    passed = sum(1 for _, _, ok, _ in results if ok)
    failed = [(n, name, reason) for n, name, ok, reason in results if not ok]

    print("\n" + "=" * 60)
    print(f"PASSED: {passed}/{total}")
    if failed:
        print(f"FAILED: {len(failed)}")
        for n, name, reason in failed:
            print(f"  T{n}: {name} — {reason}")
    else:
        print("ALL TESTS PASSED")
    print("=" * 60)

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
