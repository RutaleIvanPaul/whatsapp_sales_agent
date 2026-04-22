from __future__ import annotations

import asyncio
import json
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.utils.log import log


async def trigger(
    session: Session,
    summary: str,
    operator: Operator,
    messaging: MessagingAdapter,
    storage: StorageAdapter,
    inventory: InventoryAdapter,
    triggering_message: str,
) -> str:
    """Full handoff: flip session, persist, fire-and-forget operator alert."""

    # 1. Check for concurrent handoffs (log warning, do not block)
    existing_handoffs = storage.get_by_stage(
        operator.operator_id, Stage.HANDED_OFF.value
    )
    if existing_handoffs:
        log(
            "concurrent_handoff",
            operator_id=operator.operator_id,
            existing_count=len(existing_handoffs),
            new_phone=session.phone,
        )

    # 2. Update session state
    session.stage = Stage.HANDED_OFF
    session.handed_off_at = datetime.utcnow()

    # 3. Persist immediately
    storage.set(operator.operator_id, session.phone, session)

    # 4. Build alert context
    products_shown_line = "none shown yet"
    if session.shown_product_ids:
        by_id = {p.id: p for p in inventory.get_all()}
        shown = []
        for pid in session.shown_product_ids:
            product = by_id.get(pid)
            if product:
                shown.append(f"{product.name} — {product.price}")
        if shown:
            products_shown_line = "\n  ".join(shown)

    customer_name = session.name or "Unknown"
    snippet = (triggering_message or "")[:200]

    # The summary from the LLM is the best description of what happened,
    # especially when session.intent wasn't populated via update_session.
    summary_line = summary or session.intent or "not specified"

    alert = (
        f"New sale opportunity:\n"
        f"\n"
        f"Customer: {customer_name}\n"
        f"Summary: {summary_line}\n"
        f"Products shown:\n  {products_shown_line}\n"
        f'They said: "{snippet}"\n'
        f"\n"
        f"To respond, open your shop's WhatsApp and find "
        f"{customer_name}'s chat.\n"
        f"\n"
        f"When you're done, come back here and type:\n"
        f"  resume {session.phone}\n"
        f"    (to let the assistant continue helping them)\n"
        f"  handled {session.phone}\n"
        f"    (if you've taken care of it yourself)"
    )

    # 5. Fire-and-forget alert — do not block the pipeline
    asyncio.create_task(_send_alert(messaging, operator, alert))

    # 6. Log (returns immediately, alert sends in background)
    log(
        "handoff_triggered",
        operator_id=operator.operator_id,
        summary=(summary or "")[:200],
    )

    return json.dumps({"status": "handoff_triggered"})


async def _send_alert(
    messaging: MessagingAdapter, operator: Operator, alert: str
) -> None:
    """Background task — sends operator alert, handles own exceptions."""
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
