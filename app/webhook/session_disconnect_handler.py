from __future__ import annotations

import time

from app.adapters.messaging.base import MessagingAdapter
from app.adapters.operator.base import OperatorAdapter
from app.models.operator import Operator, OperatorStatus
from app.utils.log import log

# Debounce: don't send the same alert type to the same operator within
# this window. Prevents alert spam during server restarts, health monitor
# flapping, or Whapi transient disconnects.
ALERT_DEBOUNCE_S = 600  # 10 minutes

_last_alert: dict[tuple[str, str], float] = {}  # (operator_id, alert_type) → timestamp


def _should_alert(operator_id: str, alert_type: str) -> bool:
    key = (operator_id, alert_type)
    now = time.monotonic()
    last = _last_alert.get(key)
    if last is not None and now - last < ALERT_DEBOUNCE_S:
        return False
    _last_alert[key] = now
    return True


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

    if not _should_alert(operator.operator_id, "disconnect"):
        return

    try:
        await messaging_adapter.send_text(
            operator.owner_personal_phone,
            f"Hi {operator.owner_name}, your Salelular bot has disconnected "
            f"from WhatsApp.\n\n"
            f"What to do: Open WhatsApp > Settings > Linked Devices and "
            f"re-scan the QR code in the Whapi dashboard.\n\n"
            f"If you need help, contact support.",
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

    if not _should_alert(operator.operator_id, "reconnect"):
        return

    try:
        await messaging_adapter.send_text(
            operator.owner_personal_phone,
            f"Hi {operator.owner_name}, your Salelular bot is back online.",
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
