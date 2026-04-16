from __future__ import annotations

import copy
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.llm.base import LLMAdapter, LLMResponse, LLMTimeoutError
from app.adapters.storage.base import StorageAdapter
from app.engine import system_prompt as system_prompt_mod
from app.engine import tools as tools_mod
from app.models.operator import Operator
from app.models.product import Product
from app.models.session import Session
from app.utils.log import log

MAX_TOOL_ROUNDS = 5
FALLBACK_TIMEOUT_REPLY = (
    "Sorry — I'm taking longer than expected. Please try again in a moment."
)
FALLBACK_GENERIC_REPLY = (
    "Sorry — I had trouble processing that. Could you try rephrasing?"
)


async def run(
    operator: Operator,
    session: Session,
    unified_text: str,
    llm: LLMAdapter,
    inventory: InventoryAdapter,
    storage: StorageAdapter,
    max_history_turns: int,
    session_expiry_days: int,
) -> tuple[str, list[Product]]:
    """Run a single turn of the conversation engine.

    Returns (reply_text, products_to_show). Persists the session before
    returning. Never raises — on LLM failure returns a safe fallback reply.
    """
    products_shown_this_turn: list[Product] = []

    # Build system prompt — persona + rules + session context (no customer text)
    products_shown_names = _shown_names_from_session(session, inventory)
    system = system_prompt_mod.build(
        operator, session, products_shown_names, session_expiry_days
    )

    # Build messages: prior history + new wrapped user turn
    messages: list[dict] = copy.deepcopy(session.history) if session.history else []
    user_content = system_prompt_mod.wrap_customer_message(unified_text)
    messages.append({"role": "user", "content": user_content})

    last_response: LLMResponse | None = None

    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            response = await llm.chat(
                messages=messages,
                tools=tools_mod.ALL_TOOLS,
                system=system,
            )
            last_response = response
        except LLMTimeoutError:
            log("llm_timeout", operator_id=operator.operator_id, round=round_idx)
            return _persist_and_return(
                session, storage, operator, unified_text,
                FALLBACK_TIMEOUT_REPLY, products_shown_this_turn, max_history_turns,
            )
        except Exception as e:
            log(
                "error",
                component="conversation",
                error_type=type(e).__name__,
                message=str(e)[:200],
                operator_id=operator.operator_id,
            )
            return _persist_and_return(
                session, storage, operator, unified_text,
                FALLBACK_GENERIC_REPLY, products_shown_this_turn, max_history_turns,
            )

        if not response.tool_calls:
            reply = response.text or FALLBACK_GENERIC_REPLY
            return _persist_and_return(
                session, storage, operator, unified_text,
                reply, products_shown_this_turn, max_history_turns,
            )

        # Append the assistant message verbatim — preserves tool_use blocks
        messages.append({"role": "assistant", "content": response.raw_content})

        # Execute each tool call and collect results
        tool_results = []
        for tc in response.tool_calls:
            log(
                "tool_called",
                operator_id=operator.operator_id,
                tool_name=tc["name"],
            )
            try:
                result_str = await _dispatch_tool(
                    tc, session, operator, inventory, unified_text,
                    products_shown_this_turn,
                )
            except Exception as e:
                log(
                    "error",
                    component="tool_dispatch",
                    tool_name=tc["name"],
                    error_type=type(e).__name__,
                )
                result_str = f"Tool error: {type(e).__name__}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    # Loop exhausted without a text reply
    log(
        "tool_loop_exhausted",
        operator_id=operator.operator_id,
        rounds=MAX_TOOL_ROUNDS,
    )
    final_text = (last_response.text if last_response else None) or FALLBACK_GENERIC_REPLY
    return _persist_and_return(
        session, storage, operator, unified_text,
        final_text, products_shown_this_turn, max_history_turns,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _dispatch_tool(
    tc: dict,
    session: Session,
    operator: Operator,
    inventory: InventoryAdapter,
    unified_text: str,
    products_shown_this_turn: list[Product],
) -> str:
    name = tc["name"]
    args = tc.get("input") or {}

    if name == "search_products":
        result_str, products = tools_mod.handle_search_products(
            args.get("query", ""), session, inventory
        )
        # Track products for response_builder (max 3 shown to customer)
        for p in products:
            if p.id not in [x.id for x in products_shown_this_turn]:
                products_shown_this_turn.append(p)
        return result_str

    if name == "update_session":
        return tools_mod.handle_update_session(args.get("fields", {}), session)

    if name == "trigger_handoff":
        return await tools_mod.handle_trigger_handoff(
            args.get("summary", ""), session, operator, unified_text
        )

    return f"Unknown tool: {name}"


def _shown_names_from_session(
    session: Session, inventory: InventoryAdapter
) -> list[str]:
    """Look up product names for ids in session.shown_product_ids."""
    if not session.shown_product_ids:
        return []
    by_id = {p.id: p for p in inventory.get_all()}
    names = []
    for pid in session.shown_product_ids:
        if pid in by_id:
            names.append(by_id[pid].name)
    return names


def _persist_and_return(
    session: Session,
    storage: StorageAdapter,
    operator: Operator,
    unified_text: str,
    reply_text: str,
    products: list[Product],
    max_history_turns: int,
) -> tuple[str, list[Product]]:
    """Append this turn to history, compress if too long, persist, return."""
    new_history = list(session.history) if session.history else []
    new_history.append({"role": "user", "content": unified_text})
    new_history.append({"role": "assistant", "content": reply_text})

    if len(new_history) > max_history_turns * 2:
        new_history = _compress_history(new_history, max_history_turns)

    session.history = new_history
    session.last_active = datetime.utcnow()
    storage.set(operator.operator_id, session.phone, session)
    return reply_text, products


def _compress_history(
    history: list[dict], max_turns: int
) -> list[dict]:
    """Code-only summariser per S9. Drops oldest turns and prepends a note.

    A "turn" = one user + one assistant message pair.
    """
    keep_pairs = max_turns - 1  # leave room for the summary note
    keep_msgs = keep_pairs * 2
    dropped = history[:-keep_msgs] if keep_msgs > 0 else history

    if not dropped:
        return history

    note = (
        f"[Earlier in this conversation: {len(dropped) // 2} turns were "
        "summarised away to save context. Continue the conversation naturally.]"
    )
    return [{"role": "user", "content": note}] + history[-keep_msgs:]
