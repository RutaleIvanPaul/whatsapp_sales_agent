from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Request, Response

from app.models.operator import OperatorStatus
from app.utils.crypto import decrypt
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log
from app.webhook import owner_action_handler, session_disconnect_handler

router = APIRouter()


@router.get("/webhook")
async def liveness() -> Response:
    return Response(content="OK", status_code=200)


@router.post("/webhook")
async def receive(request: Request) -> Response:
    """Full S4 processing order.

    Returns within 5 seconds or Whapi retries. All heavy work is spawned
    onto the async queue / asyncio tasks before the 200 is returned.
    """
    state = request.app.state

    try:
        # Step 1: extract auth header (do not log the value itself)
        received_token = request.headers.get("X-Salelular-Token", "")

        # Step 2: parse body, extract channel_id
        try:
            payload = await request.json()
        except Exception:
            log(
                "error",
                component="receiver",
                error_type="malformed_json",
                client_ip=_client_ip(request),
            )
            return Response(status_code=200)

        channel_id = payload.get("channel_id", "")
        if not channel_id:
            return Response(status_code=200)

        # Step 3: look up operator by channel_id
        operator = state.operators_by_channel_id.get(channel_id)
        if operator is None:
            # Do NOT reveal channel existence
            return Response(status_code=200)

        # Step 4: constant-time token comparison
        try:
            expected = decrypt(operator.whapi_webhook_secret, state.encryption_key)
        except Exception as e:
            log(
                "error",
                component="receiver",
                error_type="webhook_secret_decrypt_failed",
                operator_id=operator.operator_id,
                message=type(e).__name__,
            )
            return Response(status_code=200)

        if not hmac.compare_digest(received_token, expected):
            log(
                "webhook_auth_failed",
                operator_id=operator.operator_id,
                client_ip=_client_ip(request),
            )
            return Response(status_code=403)

        # Step 5: status check
        if operator.status == OperatorStatus.SUSPENDED:
            return Response(status_code=200)

        # Step 6: event type routing
        event = payload.get("event") or {}
        event_type = event.get("type", "")
        event_action = event.get("event", "")

        if event_type == "users":
            if event_action == "delete":
                asyncio.create_task(
                    session_disconnect_handler.handle_disconnect(
                        operator,
                        channel_id,
                        state.operator_adapter,
                        state.messaging_adapter,
                    )
                )
            elif event_action == "post":
                asyncio.create_task(
                    session_disconnect_handler.handle_reconnect(
                        operator,
                        state.operator_adapter,
                        state.messaging_adapter,
                    )
                )
            return Response(status_code=200)

        if event_type != "messages":
            return Response(status_code=200)

        # Step 7+: iterate messages and filter
        for msg in payload.get("messages", []):
            msg_id = msg.get("id", "")

            if msg.get("from_me"):
                asyncio.create_task(owner_action_handler.handle(payload, operator))
                continue

            chat_id = msg.get("chat_id", "")
            if chat_id.endswith("@g.us"):
                log(
                    "message_discarded",
                    reason="group",
                    message_id=msg_id,
                    operator_id=operator.operator_id,
                )
                continue

            raw_phone = msg.get("from", "")
            if not raw_phone:
                log(
                    "message_discarded",
                    reason="missing_from",
                    message_id=msg_id,
                    operator_id=operator.operator_id,
                )
                continue

            try:
                sender_phone = from_whapi(raw_phone)
            except ValueError:
                log(
                    "message_discarded",
                    reason="invalid_phone",
                    message_id=msg_id,
                    operator_id=operator.operator_id,
                )
                continue

            if state.contacts_cache.is_contact(operator.operator_id, sender_phone):
                log(
                    "message_discarded",
                    reason="saved_contact",
                    phone_hash=hash_for_log(sender_phone),
                    message_id=msg_id,
                    operator_id=operator.operator_id,
                )
                continue

            # Fire-and-forget enqueue. queue.put_nowait is O(1) non-blocking.
            state.message_queue.put_nowait((payload, operator))

        return Response(status_code=200)

    except Exception as e:
        # S18 fallback — on any unexpected failure, return 200 to avoid info
        # disclosure and let Whapi think it was delivered. The error is logged.
        log(
            "error",
            component="receiver",
            error_type=type(e).__name__,
            message=str(e)[:200],
        )
        return Response(status_code=200)


def _client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host
