"""Haggling strategy — policy resolution + owner relay.

All haggling business logic lives here, transport-agnostic so the CLI,
the WhatsApp control thread, and a future owner web dashboard can call
the same functions.

Resolution precedence (most specific wins):
  1. operator.haggling_notify_first  → escalate-only mode (owner approves)
  2. product.haggling_notes           → per-item rule
  3. operator.haggling_policy          → shop-wide rule
  4. built-in default                  → "prices are fixed, decline politely"
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from app.adapters.llm.base import LLMAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.utils.log import log

DEFAULT_POLICY = "Prices are fixed. Decline discount requests politely."
HOLDING_MESSAGE_TEMPLATE = (
    "Let me check with my boss quickly and get back to you."
)


def render_prompt_section(operator: Operator) -> str:
    """Build the haggling block of the system prompt for this operator.

    Called from system_prompt.build(). Returns a complete section ready
    to concatenate.
    """
    policy = operator.haggling_policy.strip() or DEFAULT_POLICY

    if operator.haggling_notify_first:
        return (
            "HAGGLING (notify-first mode):\n"
            "  Every discount or price-negotiation request MUST be escalated\n"
            "  to the owner via request_haggle_approval. Do NOT accept,\n"
            "  decline, or negotiate on your own.\n"
            "  Background context (for your understanding only, do not act\n"
            "  on without the owner's real-time instruction):\n"
            f"    Shop-wide stance: {policy}\n"
            "    Per-product notes: check the haggling_notes field on the\n"
            "    product(s) being discussed in the search_products result.\n"
            "  When you call request_haggle_approval, include: the item(s)\n"
            "  the customer is haggling on, their price ask, and their\n"
            "  wording. Then write a short, natural holding message to the\n"
            "  customer (e.g. \"Let me check with my boss and get back to\n"
            "  you\") — do NOT quote a price or concede anything yet.\n"
        )

    return (
        "HAGGLING (autonomous mode):\n"
        "  Handle discount and price-negotiation requests using these\n"
        "  rules. Follow strict precedence:\n"
        "    1. If the product has a non-empty haggling_notes, follow it\n"
        "       for that specific product.\n"
        f"    2. Otherwise, follow the shop-wide stance: {policy}\n"
        "  Haggling is NOT buying intent — do NOT call trigger_handoff\n"
        "  just because the customer asks for a discount. Only escalate\n"
        "  via trigger_handoff if they then commit to buying a specific\n"
        "  item (\"I'll take it\").\n"
    )


# ── Owner alert builder ─────────────────────────────────────────────────────

def build_haggle_alert(
    operator: Operator,
    session: Session,
    customer_ask: str,
    items_context: str,
    per_product_notes: str,
) -> str:
    """Rich haggling alert with a copy-paste reply command.

    Kept as a pure function so the CLI, control thread, or a future
    web dashboard can share the same wording.
    """
    policy = operator.haggling_policy.strip() or DEFAULT_POLICY
    customer_label = session.name or session.phone
    copy_paste = f"reply {session.phone} "
    return (
        f"Customer wants a discount:\n"
        f"\n"
        f"Customer: {customer_label} ({session.phone})\n"
        f"Their ask: \"{customer_ask}\"\n"
        f"{items_context}\n"
        f"\n"
        f"Your shop policy: {policy}\n"
        f"Product notes: {per_product_notes or 'none'}\n"
        f"\n"
        f"To respond, copy this and type your offer after it:\n"
        f"  {copy_paste}<your instruction to the customer>\n"
        f"\n"
        f"Example: {copy_paste}Sure, I can do 5% off that one.\n"
        f"\n"
        f"Or take over the chat directly in your shop's WhatsApp.\n"
        f"When you're done, type: resume {session.phone}"
    )


# ── Relay: owner instruction → customer-voice message ────────────────────────

REPHRASE_SYSTEM = (
    "You are a friendly WhatsApp sales assistant. The shop owner has\n"
    "told you how to respond to a haggling customer. Rephrase the\n"
    "owner's decision as if YOU are telling the customer directly —\n"
    "brief, natural WhatsApp tone. Do not reveal you are relaying\n"
    "anything or that an owner is involved. 1-3 short sentences max."
)


async def relay_owner_instruction(
    operator: Operator,
    session: Session,
    owner_instruction: str,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
    rephrase_llm: LLMAdapter,
) -> None:
    """Take the owner's free-form instruction, rephrase into bot voice,
    send to the customer, update the session, resume normal flow.
    Shared across transports (WhatsApp control thread, future web UI).
    """
    # Find the customer's last ask for context
    customer_ask = ""
    for h in reversed(session.history):
        if h.get("role") == "user":
            content = h.get("content", "")
            if isinstance(content, str):
                customer_ask = content
                break

    prompt = (
        f"Customer's last message:\n"
        f'"{customer_ask[:400]}"\n\n'
        f"Owner says (do not send verbatim):\n"
        f'"{owner_instruction[:400]}"\n\n'
        f"Write the reply to the customer (1-3 short sentences):"
    )

    try:
        response = await rephrase_llm.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=REPHRASE_SYSTEM,
            max_tokens=200,
        )
        reply = (response.text or owner_instruction).strip()
    except Exception as e:
        log(
            "error",
            component="haggling",
            error_type="rephrase_failed",
            message=type(e).__name__,
        )
        # Fallback: forward verbatim rather than fail silently
        reply = owner_instruction.strip()

    # Send the reply to the customer
    try:
        await messaging.send_text(session.phone, reply, operator)
    except Exception as e:
        log(
            "error",
            component="haggling",
            error_type="customer_send_failed",
            message=type(e).__name__,
        )
        return

    # Append to history and clear the haggling handoff state
    new_history = list(session.history) if session.history else []
    new_history.append(
        {"role": "user", "content": f"[owner instruction: {owner_instruction[:300]}]"}
    )
    new_history.append({"role": "assistant", "content": reply})
    session.history = new_history
    session.stage = Stage.EXPLORING
    session.handoff_reason = None
    session.handed_off_at = None
    session.last_active = datetime.utcnow()
    storage.set(operator.operator_id, session.phone, session)

    # Confirm to the owner
    asyncio.create_task(
        messaging.send_text(
            operator.owner_personal_phone,
            f"Relayed your reply to {session.name or session.phone}. "
            f"I'm back on the conversation.",
            operator,
        )
    )

    log(
        "haggling_relay_completed",
        operator_id=operator.operator_id,
        phone_hash_suffix=session.phone[-4:],
    )
