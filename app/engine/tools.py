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
                    "Plain-English brief for the shop owner: what specific "
                    "product the customer wants, the price, and any details "
                    "they mentioned (size, colour, quantity). Be specific — "
                    "e.g. 'Wants the Samsung Watch Charger at 35,000 UGX'."
                ),
            }
        },
        "required": ["summary"],
    },
}

PRESENT_PRODUCTS_SCHEMA = {
    "name": "present_products",
    "description": (
        "After reviewing search_products results, explicitly pick which "
        "products to show the customer as images. Call this with only the "
        "product ids that ACTUALLY match the customer's request. Products "
        "not listed here will not be shown. If none match, call with an "
        "empty list and explain in your text reply. Up to 3 images are "
        "sent to the customer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "product_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of product ids from the most recent search_products "
                    "result. Only include ids that truly match what the "
                    "customer asked for — not just loose fuzzy matches."
                ),
            }
        },
        "required": ["product_ids"],
    },
}

REQUEST_HAGGLE_APPROVAL_SCHEMA = {
    "name": "request_haggle_approval",
    "description": (
        "Used ONLY in notify-first haggling mode. Escalate a discount or "
        "price-negotiation request to the shop owner for approval. Do NOT "
        "call trigger_handoff for haggling — this tool is the right one. "
        "After calling, write a brief holding message to the customer "
        "(e.g. 'Let me check with my boss and get back to you')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "customer_ask": {
                "type": "string",
                "description": (
                    "Verbatim or near-verbatim: what the customer said they "
                    "want. e.g. '10% off the navy dress', 'both shoes for "
                    "140k instead of 170k'."
                ),
            },
            "product_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ids of the product(s) the customer is haggling on, "
                    "from the most recent search result."
                ),
            },
        },
        "required": ["customer_ask"],
    },
}

ALL_TOOLS = [
    SEARCH_PRODUCTS_SCHEMA,
    PRESENT_PRODUCTS_SCHEMA,
    UPDATE_SESSION_SCHEMA,
    TRIGGER_HANDOFF_SCHEMA,
    REQUEST_HAGGLE_APPROVAL_SCHEMA,
]

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
    """Returns (string for LLM, products for response builder).

    Search returns loose fuzzy matches — the LLM must semantically
    review and call present_products to select matches. Nothing is
    added to shown_product_ids from search alone.
    """
    safe_query = (query or "")[:MAX_QUERY_CHARS]
    log("search_query", query=safe_query[:100])
    products = inventory.search(safe_query, session.shown_product_ids)

    # If search returns nothing with exclusions, retry without — the customer
    # may be explicitly asking to see products again (S10: "never show again
    # *unprompted*", but an explicit ask overrides the exclusion).
    if not products:
        products = inventory.search(safe_query, [])

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
                        "keywords": p.keywords,
                        "attributes": p.attributes,
                        "image_url": p.image_url,
                        "haggling_notes": p.haggling_notes,
                    }
                    for p in products
                ],
                "note": (
                    "These are fuzzy matches — review each carefully. Use "
                    "name, description, keywords, and attributes to judge "
                    "whether each truly matches the customer's request. "
                    "Then call present_products with only the matching ids. "
                    "If haggling_notes is non-empty, follow it if the "
                    "customer haggles on that item."
                ),
            }
        )

    log(
        "tool_result",
        tool_name="search_products",
        result_count=len(products),
    )
    return result_str, products


def handle_present_products(
    product_ids: list[str],
    session: Session,
    last_search_candidates: list[Product],
) -> tuple[str, list[Product]]:
    """Mark which products the LLM wants to display.

    Returns (tool_result_string, products_to_show). Adds the selected
    ids to session.shown_product_ids so future searches exclude them.
    Drops any ids not in the most recent search candidates (anti-
    hallucination guard).
    """
    if not isinstance(product_ids, list):
        return (
            json.dumps({"error": "product_ids must be a list"}),
            [],
        )

    by_id = {p.id: p for p in last_search_candidates}
    selected: list[Product] = []
    dropped: list[str] = []
    for pid in product_ids:
        if pid in by_id:
            selected.append(by_id[pid])
        else:
            dropped.append(pid)

    new_ids = [p.id for p in selected if p.id not in session.shown_product_ids]
    session.shown_product_ids = list(session.shown_product_ids) + new_ids

    log(
        "products_presented",
        selected=len(selected),
        requested=len(product_ids),
        dropped=len(dropped),
    )

    result = {
        "accepted": [p.id for p in selected],
        "dropped_invalid_ids": dropped,
        "note": (
            f"Will send up to 3 images for the {len(selected)} selected "
            f"product(s). Write your short text reply next."
            if selected
            else "No products will be shown. Write a text-only reply."
        ),
    }
    return json.dumps(result), selected


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


async def handle_request_haggle_approval(
    customer_ask: str,
    product_ids: list[str],
    session: Session,
    operator: Operator,
    messaging: "MessagingAdapter",
    storage: "StorageAdapter",
    inventory: "InventoryAdapter",
    triggering_message: str,
) -> str:
    """Notify-first haggling: pause the bot, alert the owner, set the
    session to HANDED_OFF with reason='haggling'.
    """
    import asyncio as _asyncio
    from datetime import datetime as _datetime
    from app.engine import haggling as _haggling

    by_id = {p.id: p for p in inventory.get_all()}
    referenced = [by_id[pid] for pid in (product_ids or []) if pid in by_id]

    items_context = "Items: " + (
        ", ".join(f"{p.name} ({p.price})" for p in referenced)
        if referenced
        else "(not specified)"
    )
    per_product_notes = "\n  ".join(
        f"{p.name}: {p.haggling_notes}"
        for p in referenced
        if p.haggling_notes
    )

    # Flip session state before sending messages — any retry will see
    # we're already in a haggle handoff.
    session.stage = Stage.HANDED_OFF
    session.handoff_reason = "haggling"
    session.handed_off_at = _datetime.utcnow()
    storage.set(operator.operator_id, session.phone, session)

    alert = _haggling.build_haggle_alert(
        operator=operator,
        session=session,
        customer_ask=customer_ask or triggering_message[:200],
        items_context=items_context,
        per_product_notes=per_product_notes,
    )
    _asyncio.create_task(
        messaging.send_text(operator.owner_personal_phone, alert, operator)
    )

    log(
        "haggling_approval_requested",
        operator_id=operator.operator_id,
        product_count=len(referenced),
        notify_first=operator.haggling_notify_first,
    )

    return json.dumps(
        {
            "status": "awaiting_owner",
            "note": (
                "The owner will decide how to respond. Write a brief, "
                "natural holding message to the customer — do not quote "
                "prices or concede anything."
            ),
        }
    )


async def handle_trigger_handoff(
    summary: str,
    session: Session,
    operator: Operator,
    messaging: "MessagingAdapter",
    storage: "StorageAdapter",
    inventory: "InventoryAdapter",
    triggering_message: str,
) -> str:
    """Full handoff — alert operator with wa.me link. See engine/handoff.py."""
    from app.engine import handoff

    return await handoff.trigger(
        session=session,
        summary=summary,
        operator=operator,
        messaging=messaging,
        storage=storage,
        inventory=inventory,
        triggering_message=triggering_message,
    )
