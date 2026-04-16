from __future__ import annotations

import asyncio

import httpx

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.utils.crypto import decrypt
from app.utils.log import log
from app.utils.phone import hash_for_log, to_whapi

WHAPI_BASE = "https://gate.whapi.cloud"
RETRY_BACKOFF_S = [1, 2]  # Attempt 1 immediate, 2 after 1s, 3 after 2s
HTTP_TIMEOUT = 15.0


class WhapiMessagingAdapter(MessagingAdapter):
    def __init__(self, encryption_key: bytes) -> None:
        self._key = encryption_key

    async def send_text(self, phone: str, text: str, operator: Operator) -> None:
        token = decrypt(operator.whapi_channel_token, self._key)
        url = f"{WHAPI_BASE}/messages/text?token={token}"
        body = {
            "to": f"{to_whapi(phone)}@s.whatsapp.net",
            "body": text,
            "typing_time": 2,
        }
        await self._send_with_retry(
            url=url, json=body, operator=operator, phone=phone, kind="text"
        )

    async def send_image(
        self, phone: str, image_url: str, caption: str, operator: Operator
    ) -> None:
        token = decrypt(operator.whapi_channel_token, self._key)
        url = f"{WHAPI_BASE}/messages/image?token={token}"
        # Whapi /messages/image expects `media` (URL or base64), not `image`.
        # Per Whapi 400 error: "/body/media must have required property 'media'".
        body = {
            "to": f"{to_whapi(phone)}@s.whatsapp.net",
            "media": image_url,
            "caption": caption,
        }
        await self._send_with_retry(
            url=url, json=body, operator=operator, phone=phone, kind="image"
        )

    async def _send_with_retry(
        self, url: str, json: dict, operator: Operator, phone: str, kind: str
    ) -> None:
        phone_hash = hash_for_log(phone)
        last_status: int | None = None
        last_error: str | None = None

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(RETRY_BACKOFF_S[attempt - 1])
                try:
                    resp = await client.post(url, json=json)
                    if resp.status_code < 400:
                        log(
                            "message_sent",
                            operator_id=operator.operator_id,
                            phone_hash=phone_hash,
                            type=kind,
                            typing_time_ms=2000 if kind == "text" else 0,
                        )
                        return
                    last_status = resp.status_code
                    last_error = f"http_{resp.status_code}"
                    # Capture body for debugging non-200 responses
                    try:
                        last_error = f"http_{resp.status_code}:{resp.text[:200]}"
                    except Exception:
                        pass
                except (httpx.RequestError, httpx.TimeoutException) as e:
                    last_error = type(e).__name__

                log(
                    "send_failed",
                    operator_id=operator.operator_id,
                    phone_hash=phone_hash,
                    attempt=attempt + 1,
                    error_code=last_error or "unknown",
                )

        # All attempts failed — alert operator's personal phone, no retry on alert
        await self._send_alert_noretry(
            operator=operator,
            message=(
                f"Salelular: failed to send {kind} message to a customer after "
                f"3 attempts. Last error: {last_error}"
            ),
        )

    async def _send_alert_noretry(self, operator: Operator, message: str) -> None:
        try:
            token = decrypt(operator.whapi_channel_token, self._key)
            url = f"{WHAPI_BASE}/messages/text?token={token}"
            body = {
                "to": f"{to_whapi(operator.owner_personal_phone)}@s.whatsapp.net",
                "body": message,
                "typing_time": 1,
            }
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                await client.post(url, json=body)
        except Exception as e:
            log(
                "error",
                component="messaging",
                error_type="alert_send_failed",
                message=type(e).__name__,
                operator_id=operator.operator_id,
            )
