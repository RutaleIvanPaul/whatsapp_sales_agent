from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OperatorStatus(Enum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
    SUSPENDED = "suspended"


@dataclass
class Operator:
    operator_id: str
    shop_name: str
    owner_name: str
    owner_personal_phone: str
    whapi_channel_id: str
    whapi_channel_token: str
    whapi_webhook_secret: str
    whapi_connected_phone: str | None
    google_sheets_id: str
    google_sheet_name: str
    luganda_canned_response: str
    llm_model: str
    status: OperatorStatus
    created_at: datetime
    excluded_phones: list[str] = field(default_factory=list)
    included_phones: list[str] = field(default_factory=list)
    # Business context — feeds the system prompt so the LLM can craft
    # semantically relevant searches and review results knowing the
    # domain (e.g. "Clothing & Fashion", "Phone accessories").
    shop_category: str = ""
    # Free-form. Examples: "Sells women's and men's casual wear.
    # Products tagged by gender, colour, size." Helps the LLM know
    # what's in scope and how the data is typically organised.
    shop_description: str = ""
