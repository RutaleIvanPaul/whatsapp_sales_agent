from __future__ import annotations

from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.llm.base import LLMAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.adapters.vision.base import VisionAdapter
from app.engine import conversation
from app.input import language as language_mod
from app.input import processor
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.pipeline import response_builder
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log, to_whapi

MAX_ALERT_CHARS = 200


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

    # 1. Input processor — text/image/voice/link → unified_text
    unified = await processor.process(payloads, vision, inventory)
    if not unified.strip():
        log("pipeline_skipped", reason="empty_unified_text", phone_hash=phone_hash)
        return

    # 2. Language gate (provider-agnostic — uses classifier_llm)
    lang = await language_mod.classify(unified, classifier_llm)
    if lang in ("LUGANDA", "UNKNOWN"):
        await _send_canned_and_alert(
            sender_phone, unified, lang, operator, messaging, phone_hash
        )
        return

    # 3. Load or create session
    session = storage.get(operator.operator_id, sender_phone)
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
            last_holding_sent=None,
            last_active=now,
            created_at=now,
        )

    # 4. Run the conversation engine
    reply_text, products = await conversation.run(
        operator=operator,
        session=session,
        unified_text=unified,
        llm=llm,
        inventory=inventory,
        storage=storage,
        max_history_turns=max_history_turns,
        session_expiry_days=session_expiry_days,
    )

    # 5. Send the reply (text + up to 3 product images)
    await response_builder.send_response(
        sender_phone, reply_text, products, operator, messaging
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
        f"Your canned response was sent automatically. Here's what "
        f"they said:\n\"{snippet}\"\n\n"
        f"Tap this link to reply directly: "
        f"https://wa.me/{to_whapi(sender_phone)}"
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
