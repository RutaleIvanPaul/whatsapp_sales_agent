from __future__ import annotations

from app.models.operator import Operator
from app.utils.log import log


async def handle(payload: dict, operator: Operator) -> None:
    """Phase 3 stub. Full operator-command handling lands in Phase 5.

    For now we log the event so we can verify the receiver routes from_me
    messages here correctly.
    """
    msgs = payload.get("messages", [])
    msg_id = msgs[0].get("id", "") if msgs else ""
    log(
        "owner_command",
        operator_id=operator.operator_id,
        command_type="unparsed_phase3_stub",
        message_id=msg_id,
    )
