from __future__ import annotations

import json
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.utils.log import log
from app.utils.phone import to_whapi


async def trigger(
    session: Session,
    summary: str,
    operator: Operator,
    messaging: MessagingAdapter,
    storage: StorageAdapter,
    inventory: InventoryAdapter,
    triggering_message: str,
) -> str:
    """Full handoff: flip session, persist, alert operator with wa.me link."""

    # 1. Update session state
    session.stage = Stage.HANDED_OFF
    session.handed_off_at = datetime.utcnow()

    # 2. Persist immediately
    storage.set(operator.operator_id, session.phone, session)

    # 3. Look up last shown product
    last_product_line = "none shown yet"
    if session.shown_product_ids:
        by_id = {p.id: p for p in inventory.get_all()}
        last_id = session.shown_product_ids[-1]
        product = by_id.get(last_id)
        if product:
            last_product_line = f"{product.name} — {product.price}"

    # 4. Build wa.me link
    customer_digits = to_whapi(session.phone)
    wa_link = f"https://wa.me/{customer_digits}"

    # 5. Build alert per S14 template + OPERATOR ALERT CONTENT RULES
    customer_name = session.name or "Unknown"
    intent = session.intent or "not specified"
    snippet = (triggering_message or "")[:120]

    alert = (
        f"🛎 Customer ready to close:\n"
        f"   Name: {customer_name}\n"
        f"   Looking for: {intent}\n"
        f"   Last shown: {last_product_line}\n"
        f'   What they said: "{snippet}"\n'
        f"\n"
        f"To reply: open your shop's WhatsApp and find this "
        f"customer's chat ({customer_name}, {session.phone}).\n"
        f"\n"
        f"When done, reply HERE:\n"
        f"  resume {session.phone} — hand back to bot\n"
        f"  handled {session.phone} — you are dealing with it"
    )

    # 6. Send alert to operator
    try:
        await messaging.send_text(
            operator.owner_personal_phone, alert, operator
        )
    except Exception as e:
        log(
            "error",
            component="handoff",
            error_type="alert_send_failed",
            message=type(e).__name__,
            operator_id=operator.operator_id,
        )

    # 7. Log
    log(
        "handoff_triggered",
        operator_id=operator.operator_id,
        summary=(summary or "")[:200],
    )

    return json.dumps({"status": "handoff_triggered"})
