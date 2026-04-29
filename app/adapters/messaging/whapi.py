from __future__ import annotations

import asyncio

import httpx

from app.adapters.messaging.base import MessagingAdapter
from app.models.operator import Operator
from app.utils.crypto import decrypt
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log, to_whapi
from app.utils.sent_tracker import sent_tracker

WHAPI_BASE = "https://gate.whapi.cloud"
RETRY_BACKOFF_S = [1, 2]  # Attempt 1 immediate, 2 after 1s, 3 after 2s
HTTP_TIMEOUT = 15.0


class WhapiMessagingAdapter(MessagingAdapter):
    def __init__(self, encryption_key: bytes) -> None:
        self._key = encryption_key

    async def send_text(self, phone: str, text: str, operator: Operator) -> None:
        token = decrypt(operator.whapi_channel_token, self._key)
        url = f"{WHAPI_BASE}/messages/text?token={token}"
        typing_time = 2 if len(text) > 200 else 1
        body = {
            "to": f"{to_whapi(phone)}@s.whatsapp.net",
            "body": text,
            "typing_time": typing_time,
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
        # Extract product name from caption (first line) for error context
        product_hint = caption.split("\n")[0] if caption else "unknown product"
        await self._send_with_retry(
            url=url, json=body, operator=operator, phone=phone, kind="image",
            context=f"Product: {product_hint}."
        )

    async def _send_with_retry(
        self,
        url: str,
        json: dict,
        operator: Operator,
        phone: str,
        kind: str,
        context: str = "",
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
                        # Capture the message ID so we can distinguish
                        # bot-sent echoes from operator typing in receiver.
                        try:
                            resp_data = resp.json()
                            # Whapi response: {"sent": true, "message": {"id": "..."}}
                            msg_obj = resp_data.get("message") or {}
                            sent_id = msg_obj.get("id", "")
                            if sent_id:
                                sent_tracker.register(sent_id)
                        except Exception:
                            pass  # See TECH DEBT in sent_tracker.py
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
                    try:
                        last_error = f"http_{resp.status_code}:{resp.text[:200]}"
                    except Exception:
                        pass
                    # Don't retry 400 errors — the request is malformed
                    # (e.g. broken image URL). Retrying won't help.
                    if resp.status_code == 400:
                        break
                except (httpx.RequestError, httpx.TimeoutException) as e:
                    last_error = type(e).__name__

                log(
                    "send_failed",
                    operator_id=operator.operator_id,
                    phone_hash=phone_hash,
                    attempt=attempt + 1,
                    error_code=last_error or "unknown",
                )

        # All attempts failed — alert operator with human-friendly message
        if kind == "image" and "media link is not available" in (last_error or ""):
            alert = (
                f"Hi {operator.owner_name}, I couldn't send a product "
                f"image to a customer.\n\n"
                f"The image link in your product sheet seems to be broken. "
                f"{context}\n\n"
                f"What to do: Open your Google Sheet, find the product, and "
                f"replace the image link with a fresh one."
            )
        elif kind == "image":
            alert = (
                f"Hi {operator.owner_name}, I couldn't send a product "
                f"image to a customer. {context}\n\n"
                f"This might be a temporary issue. If it keeps happening, "
                f"check that the image link in your product sheet still works."
            )
        else:
            alert = (
                f"Hi {operator.owner_name}, I couldn't get a reply through "
                f"to a customer just now.\n\n"
                f"This is usually temporary. Their message was received "
                f"but my reply didn't go through — they may try again."
            )

        await self._send_alert_noretry(operator=operator, message=alert)

    async def check_health(self, operator: Operator) -> dict:
        token = decrypt(operator.whapi_channel_token, self._key)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(f"{WHAPI_BASE}/health?token={token}")
                data = resp.json()
                status_info = data.get("status", {})
                status_code = status_info.get("code")
                return {
                    "connected": status_code == 4,
                    "status_text": status_info.get("text", "unknown"),
                    "status_code": status_code,
                    "user": data.get("user", {}),
                }
        except Exception as e:
            return {
                "connected": False,
                "status_text": f"error: {type(e).__name__}",
                "status_code": None,
            }

    async def get_contacts(self, operator: Operator) -> set[str]:
        token = decrypt(operator.whapi_channel_token, self._key)
        base_url = f"{WHAPI_BASE}/contacts?token={token}"
        phones: set[str] = set()
        page_size = 500
        offset = 0

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            while True:
                url = f"{base_url}&count={page_size}&offset={offset}"
                resp = await client.get(url)
                if resp.status_code >= 400:
                    log(
                        "error",
                        component="contacts",
                        error_type="fetch_failed",
                        message=f"http_{resp.status_code}",
                        operator_id=operator.operator_id,
                    )
                    raise RuntimeError(f"Contacts API returned HTTP {resp.status_code}")

                data = resp.json()
                batch = data.get("contacts", [])
                for c in batch:
                    raw = c.get("id") or c.get("phone") or ""
                    if not raw:
                        continue
                    try:
                        phones.add(from_whapi(raw))
                    except ValueError:
                        continue

                if len(batch) < page_size:
                    break
                offset += page_size

        return phones

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
