from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InboundMessage:
    message_id: str
    sender_phone: str
    sender_name: str | None
    type: str
    text: str | None
    image_link: str | None
    voice_link: str | None
    from_me: bool
    chat_id: str
    timestamp: int
    channel_id: str
    operator_id: str
