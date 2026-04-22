from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Stage(Enum):
    EXPLORING = "exploring"
    CONSIDERING = "considering"
    HANDED_OFF = "handed_off"
    OWNER_ACTIVE = "owner_active"


@dataclass
class Session:
    operator_id: str
    phone: str
    name: str | None
    language: str | None
    history: list[dict]
    intent: str | None
    constraints: dict
    shown_product_ids: list[str]
    stage: Stage
    handed_off_at: datetime | None
    last_active: datetime
    created_at: datetime
    # Intent gate state: "unclassified", "passed" (SALES detected → gate off),
    # or "silenced" (3 turns without sales intent → drop future messages).
    intent_gate_state: str = "unclassified"
