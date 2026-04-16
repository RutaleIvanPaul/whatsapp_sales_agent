from __future__ import annotations

import json
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.models.operator import Operator
from app.models.product import Product
from app.models.session import Session, Stage
from app.utils.log import log

# ── Tool schemas (provider-neutral) ──────────────────────────────────────────
#
# Stored as {name, description, parameters (JSON Schema)}. Each LLM adapter
# translates this into its provider-native shape:
#   - Anthropic: rename `parameters` → `input_schema`
#   - Groq/OpenAI: wrap in {"type": "function", "function": {...}}
#
# This keeps engine/tools.py free of provider-specific formatting.

SEARCH_PRODUCTS_SCHEMA = {
    "name": "search_products",
    "description": (
        "Search the product inventory by natural-language query. Returns up to "
        "5 matching products with name, price, description, and image URL. "
        "Excludes products already shown to this customer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of what the customer wants.",
            }
        },
        "required": ["query"],
    },
}

UPDATE_SESSION_SCHEMA = {
    "name": "update_session",
    "description": (
        "Persist what you have learned about this customer. Only call this "
        "when you actually learn a new fact. Provide the `fields` object with "
        "any subset of: name, language, intent, constraints, shown_product_ids."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": (
                    "Subset of: name, language, intent, constraints, "
                    "shown_product_ids. Example: {\"name\": \"Sarah\"}."
                ),
            }
        },
        # NOTE: `fields` intentionally NOT required. Some models (e.g. Llama
        # via Groq) sometimes call this tool with no args; making it required
        # produces a hard 400 from Groq. We accept the call and no-op.
    },
}

TRIGGER_HANDOFF_SCHEMA = {
    "name": "trigger_handoff",
    "description": (
        "Alert the operator that this customer is ready to buy. Call when you "
        "detect strong buying intent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Plain-English brief for the operator: what the customer "
                    "wants, what was shown, what they said."
                ),
            }
        },
        "required": ["summary"],
    },
}

ALL_TOOLS = [SEARCH_PRODUCTS_SCHEMA, UPDATE_SESSION_SCHEMA, TRIGGER_HANDOFF_SCHEMA]

# ── Tool handlers ────────────────────────────────────────────────────────────

# S16 prompt-injection defence — only these Session fields can be set via
# the LLM-callable update_session tool. Anything else is silently dropped.
ALLOWED_SESSION_FIELDS = {
    "name",
    "language",
    "intent",
    "constraints",
    "shown_product_ids",
    "stage",
}

MAX_QUERY_CHARS = 500


def handle_search_products(
    query: str, session: Session, inventory: InventoryAdapter
) -> tuple[str, list[Product]]:
    """Returns (string for LLM, products for response builder)."""
    safe_query = (query or "")[:MAX_QUERY_CHARS]
    products = inventory.search(safe_query, session.shown_product_ids)

    # Track shown ids on the session so future searches exclude them
    new_ids = [p.id for p in products if p.id not in session.shown_product_ids]
    session.shown_product_ids = list(session.shown_product_ids) + new_ids

    if not products:
        result_str = json.dumps({"products": [], "note": "No matching products found."})
    else:
        result_str = json.dumps(
            {
                "products": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "price": p.price,
                        "description": p.description,
                        "attributes": p.attributes,
                        "image_url": p.image_url,
                    }
                    for p in products
                ]
            }
        )

    log(
        "tool_result",
        tool_name="search_products",
        result_count=len(products),
    )
    return result_str, products


def handle_update_session(fields: dict, session: Session) -> str:
    if not fields:
        return json.dumps({"applied": [], "rejected": [], "note": "no fields provided"})
    if not isinstance(fields, dict):
        return "Error: fields must be an object."

    applied = {}
    rejected = []
    for k, v in fields.items():
        if k not in ALLOWED_SESSION_FIELDS:
            rejected.append(k)
            continue
        if k == "stage":
            try:
                v = Stage(v) if isinstance(v, str) else v
            except ValueError:
                rejected.append(k)
                continue
        setattr(session, k, v)
        applied[k] = True

    if rejected:
        log(
            "session_update_rejected",
            rejected_fields=",".join(rejected),
        )

    log(
        "session_updated",
        fields_changed=",".join(applied.keys()) if applied else "none",
    )
    return json.dumps({"applied": list(applied.keys()), "rejected": rejected})


async def handle_trigger_handoff(
    summary: str,
    session: Session,
    operator: Operator,
    triggering_message: str,
) -> str:
    """Phase 4 stub — flips the session to HANDED_OFF and logs.

    Full handoff (alert + wa.me link to operator) lives in Phase 5.
    """
    session.stage = Stage.HANDED_OFF
    session.handed_off_at = datetime.utcnow()

    log(
        "handoff_triggered",
        operator_id=operator.operator_id,
        summary=(summary or "")[:200],
    )
    return json.dumps({"status": "handoff_triggered_phase4_stub"})
