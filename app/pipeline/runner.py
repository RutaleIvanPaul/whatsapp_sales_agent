from __future__ import annotations

import asyncio
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.llm.base import LLMAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.adapters.vision.base import VisionAdapter
from app.engine import conversation
from app.input import intent as intent_mod
from app.input import language as language_mod
from app.input import processor
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.pipeline import response_builder
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log
import time as _time

MAX_ALERT_CHARS = 200

# Daily message cap — in-memory, resets at midnight UTC via date key
_daily_counts: dict[tuple[str, str, str], int] = {}
_daily_alerted: set[tuple[str, str, str]] = set()
_cap_lock: asyncio.Lock | None = None


def _get_cap_lock() -> asyncio.Lock:
    global _cap_lock
    if _cap_lock is None:
        _cap_lock = asyncio.Lock()
    return _cap_lock


async def run(
    payloads: list[dict],
    operator: Operator,
    *,
    llm: LLMAdapter,
    classifier_llm: LLMAdapter,
    vision: VisionAdapter,
    inventory: InventoryAdapter,
    messaging: MessagingAdapter,
    storage: StorageAdapter,
    max_history_turns: int,
    session_expiry_days: int,
    max_messages_per_user_day: int = 100,
) -> None:
    """Phase 4 full pipeline.

    payloads → unified_text → language gate → conversation engine →
    response builder → outbound WhatsApp messages.
    """
    if not payloads:
        return

    raw_phone = (payloads[0].get("messages") or [{}])[0].get("from", "")
    if not raw_phone:
        log("error", component="runner", error_type="missing_from")
        return

    try:
        sender_phone = from_whapi(raw_phone)
    except ValueError:
        log("error", component="runner", error_type="invalid_phone")
        return

    phone_hash = hash_for_log(sender_phone)
    t_start = _time.monotonic()

    # 1. Input processor — text/image/voice/link → unified_text
    t0 = _time.monotonic()
    unified = await processor.process(payloads, vision, inventory)
    input_ms = int((_time.monotonic() - t0) * 1000)
    if not unified.strip():
        log("pipeline_skipped", reason="empty_unified_text", phone_hash=phone_hash)
        return

    # 2. Load existing session (needed for stage checks + intent gate)
    session = storage.get(operator.operator_id, sender_phone)

    # 3. OWNER_ACTIVE — skip pipeline entirely (operator is handling directly)
    if session and session.stage == Stage.OWNER_ACTIVE:
        log("pipeline_skipped", reason="owner_active", phone_hash=phone_hash)
        return

    # NOTE: HANDED_OFF does NOT suppress the bot.

    # 3.5 Daily message cap
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cap_key = (operator.operator_id, sender_phone, today)
    async with _get_cap_lock():
        # Purge stale date keys (prevents unbounded growth)
        stale = [k for k in _daily_counts if k[2] != today]
        for k in stale:
            _daily_counts.pop(k, None)
            _daily_alerted.discard(k)
        count = _daily_counts.get(cap_key, 0) + 1
        _daily_counts[cap_key] = count
    if count > max_messages_per_user_day:
        if cap_key not in _daily_alerted:
            _daily_alerted.add(cap_key)
            customer_name = session.name if session else "a customer"
            alert = (
                f"Hi {operator.owner_name}, {customer_name} has sent "
                f"{count} messages today. I've stepped back from this "
                f"conversation for now — you may want to check in with "
                f"them directly."
            )
            asyncio.create_task(
                messaging.send_text(operator.owner_personal_phone, alert, operator)
            )
        log(
            "daily_cap_reached",
            operator_id=operator.operator_id,
            phone_hash=phone_hash,
            count=count,
        )
        return  # Silent — no reply to customer

    # 4. Language gate
    t0 = _time.monotonic()
    lang = await language_mod.classify(unified, classifier_llm)
    language_ms = int((_time.monotonic() - t0) * 1000)
    if lang in ("LUGANDA", "UNKNOWN"):
        await _send_canned_and_alert(
            sender_phone, unified, lang, operator, messaging, phone_hash
        )
        return

    # 5. Intent gate — only for new/unknown contacts (no session or empty history)
    intent_ms = 0
    should_classify_intent = (session is None or not session.history)
    if should_classify_intent:
        t0 = _time.monotonic()
        intent = await intent_mod.classify_intent(unified, classifier_llm)
        intent_ms = int((_time.monotonic() - t0) * 1000)
        if intent == "NOT_SALES":
            log(
                "intent_gate_silent",
                phone_hash=phone_hash,
                operator_id=operator.operator_id,
                chars=len(unified),
            )
            # TODO: operator alert for high-signal non-sales messages
            # Deferred — needs real usage data to define "important"
            return  # Silent. No reply. No session update.

    # 6. Create session if new
    if session is None:
        now = datetime.utcnow()
        session = Session(
            operator_id=operator.operator_id,
            phone=sender_phone,
            name=None,
            language=lang.lower() if lang in ("ENGLISH", "MIXED") else None,
            history=[],
            intent=None,
            constraints={},
            shown_product_ids=[],
            stage=Stage.EXPLORING,
            handed_off_at=None,
            last_active=now,
            created_at=now,
        )

    # 7. Run the conversation engine
    t0 = _time.monotonic()
    reply_text, products = await conversation.run(
        operator=operator,
        session=session,
        unified_text=unified,
        llm=llm,
        inventory=inventory,
        messaging=messaging,
        storage=storage,
        max_history_turns=max_history_turns,
        session_expiry_days=session_expiry_days,
    )
    llm_ms = int((_time.monotonic() - t0) * 1000)

    # 8. Send the reply (text + up to 3 product images)
    # Empty reply_text means fatal failure (silence to customer — handled in conversation.py)
    if not reply_text:
        return
    t0 = _time.monotonic()
    await response_builder.send_response(
        sender_phone, reply_text, products, operator, messaging
    )
    send_ms = int((_time.monotonic() - t0) * 1000)

    total_ms = int((_time.monotonic() - t_start) * 1000)
    log(
        "pipeline_timing",
        phone_hash=phone_hash,
        operator_id=operator.operator_id,
        input_ms=input_ms,
        language_ms=language_ms,
        intent_ms=intent_ms,
        llm_ms=llm_ms,
        send_ms=send_ms,
        total_ms=total_ms,
    )


async def _send_canned_and_alert(
    sender_phone: str,
    unified_text: str,
    detected_language: str,
    operator: Operator,
    messaging: MessagingAdapter,
    phone_hash: str,
) -> None:
    """For LUGANDA / UNKNOWN: reply with operator's canned response,
    then alert the operator with a snippet of the original message."""
    canned = (
        operator.luganda_canned_response
        or "Webale okutuwa obubaka! / Thank you for your message — we will be in touch shortly."
    )
    await messaging.send_text(sender_phone, canned, operator)

    snippet = unified_text[:MAX_ALERT_CHARS]
    alert = (
        f"Hi {operator.owner_name}, a customer sent a message that "
        f"appears to be in {detected_language.lower()}.\n\n"
        f"I replied with your standard response. Here's what "
        f"they said:\n\"{snippet}\"\n\n"
        f"To reply directly, find them in your shop's WhatsApp."
    )
    try:
        await messaging.send_text(operator.owner_personal_phone, alert, operator)
    except Exception as e:
        log(
            "error",
            component="runner",
            error_type="alert_send_failed",
            message=type(e).__name__,
        )

    log(
        "language_escalated",
        operator_id=operator.operator_id,
        phone_hash=phone_hash,
        language=detected_language,
    )
