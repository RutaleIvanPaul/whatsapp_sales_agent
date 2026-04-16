from __future__ import annotations

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log

STUB_REPLY = "Salelular is listening. Full AI responses coming soon."


async def run(
    payloads: list[dict],
    operator: Operator,
    messaging_adapter: MessagingAdapter,
) -> None:
    """Phase 3 placeholder. Full pipeline (input processor, LLM, tools,
    response builder) lands in Phase 4."""

    if not payloads:
        return

    first_msg = payloads[0].get("messages", [])
    if not first_msg:
        return
    raw_phone = first_msg[0].get("from", "")
    if not raw_phone:
        return

    try:
        sender_phone = from_whapi(raw_phone)
    except ValueError:
        return

    log(
        "pipeline_run_stub",
        operator_id=operator.operator_id,
        phone_hash=hash_for_log(sender_phone),
        message_count=len(payloads),
    )

    await messaging_adapter.send_text(sender_phone, STUB_REPLY, operator)
