from __future__ import annotations

from app.adapters.messaging.base import MessagingAdapter
from app.adapters.operator.base import OperatorAdapter
from app.models.operator import Operator, OperatorStatus
from app.utils.log import log


async def handle_disconnect(
    operator: Operator,
    channel_id: str,
    operator_adapter: OperatorAdapter,
    messaging_adapter: MessagingAdapter,
) -> None:
    operator_adapter.update_status(operator.operator_id, OperatorStatus.DISCONNECTED)
    log(
        "session_disconnect",
        operator_id=operator.operator_id,
        channel_id=channel_id,
    )
    # Attempt alert. Channel may be dead so this may fail — messaging adapter
    # will log send_failed and try to alert via the same dead channel (also
    # fails). In MVP we accept this: nothing to do without OOB channel.
    try:
        await messaging_adapter.send_text(
            operator.owner_personal_phone,
            "Your Salelular bot has disconnected. Please open WhatsApp > "
            "Linked Devices and re-scan the QR code in the Whapi dashboard.",
            operator,
        )
    except Exception as e:
        log(
            "error",
            component="session_disconnect",
            error_type="alert_failed",
            message=type(e).__name__,
            operator_id=operator.operator_id,
        )


async def handle_reconnect(
    operator: Operator,
    operator_adapter: OperatorAdapter,
    messaging_adapter: MessagingAdapter,
) -> None:
    operator_adapter.update_status(operator.operator_id, OperatorStatus.ACTIVE)
    log(
        "session_reconnect",
        operator_id=operator.operator_id,
        channel_id=operator.whapi_channel_id,
    )
    try:
        await messaging_adapter.send_text(
            operator.owner_personal_phone,
            "Your Salelular bot is back online.",
            operator,
        )
    except Exception as e:
        log(
            "error",
            component="session_reconnect",
            error_type="confirmation_failed",
            message=type(e).__name__,
            operator_id=operator.operator_id,
        )
