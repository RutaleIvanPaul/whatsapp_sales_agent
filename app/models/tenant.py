from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TenantStatus(Enum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
    SUSPENDED = "suspended"


@dataclass
class Tenant:
    tenant_id: str
    shop_name: str
    owner_name: str
    owner_personal_phone: str
    whapi_channel_id: str
    whapi_channel_token: str
    whapi_webhook_secret: str
    whapi_connected_phone: str | None
    google_sheets_id: str
    luganda_canned_response: str
    llm_model: str
    status: TenantStatus
    created_at: datetime
