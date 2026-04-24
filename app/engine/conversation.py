from __future__ import annotations

import asyncio
import copy
from datetime import datetime

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.llm.base import LLMAdapter, LLMResponse, LLMTimeoutError, ToolResult
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.utils.cost_tracker import get_tracker
from app.engine import system_prompt as system_prompt_mod
from app.engine import tools as tools_mod
from app.models.operator import Operator
from app.models.product import Product
from app.models.session import Session, Stage
from app.utils.log import log

MAX_TOOL_ROUNDS = 5
TIMEOUT_RETRY_WAITS = [3, 5]  # seconds to wait between attempts 1→2, 2→3
FALLBACK_GENERIC_REPLY = (
    "Sorry — I had trouble processing that. Could you try rephrasing?"
)


def _looks_malformed(text: str) -> bool:
    """Catch obvious LLM output corruption like 'Let's let … we … ... …'.

    Only flag clearly-broken output — fragmented ellipses, mostly-
    punctuation, or impossibly short. Valid short human replies like
    "Sure thing!" or "Got it — sending now." must pass.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 4:
        return True
    # Multiple ellipsis or trailing-off patterns strongly suggest a
    # fragmented generation that was abandoned mid-thought.
    ellipsis_count = stripped.count("…") + stripped.count("...")
    if ellipsis_count >= 2:
        return True
    # Ratio of letters to non-letters — if a reply is mostly dots,
    # ellipses, asterisks, it's malformed.
    letters = sum(1 for c in stripped if c.isalpha())
    if letters == 0 or letters / max(len(stripped), 1) < 0.4:
        return True
    return False


async def run(
    operator: Operator,
    session: Session,
    unified_text: str,
    llm: LLMAdapter,
    inventory: InventoryAdapter,
    messaging: MessagingAdapter,
    storage: StorageAdapter,
    max_history_turns: int,
    session_expiry_days: int,
) -> tuple[str, list[Product]]:
    """Run a single turn of the conversation engine.

    Returns (reply_text, products_to_show). Persists the session before
    returning. Never raises — on LLM failure returns a safe fallback reply.
    """
    # products_to_present is populated ONLY by the LLM's present_products
    # tool call after it semantically reviews search results.
    products_to_present: list[Product] = []
    # Candidates from the most recent search_products call — used to
    # validate that present_products only references real search results.
    last_search_candidates: list[Product] = []

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
        response = await _llm_call_with_retry(
            llm, messages, tools_mod.ALL_TOOLS, system,
            operator, session, storage, messaging, unified_text,
            products_to_present, max_history_turns, round_idx,
        )
        if response is None:
            # All retries exhausted — _llm_call_with_retry already
            # handled failure (silence + HANDED_OFF + operator alert).
            # Return empty to signal no reply should be sent.
            return ("", [])
        last_response = response

        if not response.tool_calls:
            reply = response.text or FALLBACK_GENERIC_REPLY
            if _looks_malformed(reply):
                log(
                    "llm_malformed_reply",
                    operator_id=operator.operator_id,
                    preview=reply[:80],
                )
                reply = FALLBACK_GENERIC_REPLY
            return _persist_and_return(
                session, storage, operator, unified_text,
                reply, products_to_present, max_history_turns,
            )

        # Append the assistant message verbatim — adapter-shaped, preserves
        # tool_use blocks (Anthropic) or tool_calls list (OpenAI/Groq)
        messages.append(response.assistant_message)

        # Execute each tool call and collect provider-neutral results
        tool_results: list[ToolResult] = []
        for tc in response.tool_calls:
            log(
                "tool_called",
                operator_id=operator.operator_id,
                tool_name=tc["name"],
            )
            is_error = False
            try:
                result_str = await _dispatch_tool(
                    tc, session, operator, inventory, unified_text,
                    products_to_present, last_search_candidates,
                    messaging, storage,
                )
            except Exception as e:
                log(
                    "error",
                    component="tool_dispatch",
                    tool_name=tc["name"],
                    error_type=type(e).__name__,
                )
                result_str = f"Tool error: {type(e).__name__}"
                is_error = True

            tool_results.append(ToolResult(
                tool_use_id=tc["id"],
                content=result_str,
                is_error=is_error,
            ))

        # Adapter knows how to shape these into the next-turn message(s)
        messages.extend(llm.make_tool_result_messages(tool_results))

    # Loop exhausted without a text reply
    has_text = bool(last_response and last_response.text)
    log(
        "tool_loop_limit_exceeded",
        operator_id=operator.operator_id,
        rounds=MAX_TOOL_ROUNDS,
        had_text=has_text,
    )
    if has_text:
        return _persist_and_return(
            session, storage, operator, unified_text,
            last_response.text, products_to_present, max_history_turns,
        )
    # No text at all — silence + HANDED_OFF + operator alert
    await _handle_fatal_failure(
        session, storage, operator, messaging, "tool loop exhausted without text"
    )
    return ("", [])


# ── LLM retry + failure handling ─────────────────────────────────────────────


async def _llm_call_with_retry(
    llm: LLMAdapter,
    messages: list[dict],
    tools: list[dict],
    system: str,
    operator: Operator,
    session: Session,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
    unified_text: str,
    products_shown: list[Product],
    max_history_turns: int,
    round_idx: int,
) -> LLMResponse | None:
    """Call LLM with 3-attempt timeout retry. Returns None on fatal failure.

    Attempt 1: full call, 30s timeout
    Attempt 2: same, after 3s wait
    Attempt 3: simplified prompt (no tools, no rules), after 5s wait
    All 3 fail: silence + HANDED_OFF + operator alert → returns None
    """
    for attempt in range(3):
        use_tools = tools if attempt < 2 else []
        use_system = system if attempt < 2 else _build_simplified_system(operator, session)

        try:
            response = await llm.chat(
                messages=messages,
                tools=use_tools,
                system=use_system,
            )
            # Record tokens for cost tracking
            tracker = get_tracker()
            if tracker:
                tracker.record(
                    operator.operator_id,
                    response.input_tokens,
                    response.output_tokens,
                )
            return response
        except LLMTimeoutError:
            log(
                "llm_timeout_attempt",
                operator_id=operator.operator_id,
                attempt=attempt + 1,
                round=round_idx,
            )
            if attempt < 2:
                await asyncio.sleep(TIMEOUT_RETRY_WAITS[attempt])
                continue
            # Attempt 3 timed out — fall through to fatal
        except Exception as e:
            err_msg = str(e)[:300]
            log(
                "error",
                component="conversation",
                error_type=type(e).__name__,
                message=err_msg,
                operator_id=operator.operator_id,
                round=round_idx,
                attempt=attempt + 1,
            )
            # Tool-use failure recovery (Groq + Llama)
            if _is_tool_use_failure(err_msg) and attempt < 2:
                try:
                    response = await llm.chat(
                        messages=messages, tools=[], system=system,
                    )
                    log("llm_recovered_no_tools", operator_id=operator.operator_id)
                    return response
                except Exception:
                    pass
            # Non-timeout error on first attempt → don't retry
            if attempt == 0 and not isinstance(e, LLMTimeoutError):
                break

    # All attempts exhausted
    await _handle_fatal_failure(
        session, storage, operator, messaging, "all LLM attempts failed"
    )
    return None


def _build_simplified_system(operator: Operator, session: Session) -> str:
    """Minimal system prompt for attempt 3 — persona + context only."""
    name = session.name or "the customer"
    return (
        f"You are a friendly sales assistant for {operator.shop_name}. "
        f"You are chatting with {name}. "
        f"Reply briefly and naturally. You cannot search for products right now."
    )


async def _handle_fatal_failure(
    session: Session,
    storage: StorageAdapter,
    operator: Operator,
    messaging: MessagingAdapter,
    reason: str,
) -> None:
    """Silence to customer, HANDED_OFF, operator alert."""
    session.stage = Stage.HANDED_OFF
    session.handed_off_at = datetime.utcnow()
    storage.set(operator.operator_id, session.phone, session)

    customer_name = session.name or "a customer"
    alert = (
        f"Hi {operator.owner_name}, I'm having trouble responding to "
        f"{customer_name} right now. You may want to step in — "
        f"find them in your shop's WhatsApp and pick up the conversation."
    )
    asyncio.create_task(
        messaging.send_text(operator.owner_personal_phone, alert, operator)
    )

    log(
        "llm_timeout_fatal",
        operator_id=operator.operator_id,
        reason=reason,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

# Substrings that indicate the provider rejected the request because the
# model emitted a malformed tool call (Groq + Llama is the common case).
_TOOL_USE_FAILURE_HINTS = (
    "tool_use_failed",
    "tool call validation failed",
    "did not match schema",
)


def _is_tool_use_failure(error_msg: str) -> bool:
    lowered = error_msg.lower()
    return any(hint in lowered for hint in _TOOL_USE_FAILURE_HINTS)


async def _dispatch_tool(
    tc: dict,
    session: Session,
    operator: Operator,
    inventory: InventoryAdapter,
    unified_text: str,
    products_to_present: list[Product],
    last_search_candidates: list[Product],
    messaging: MessagingAdapter,
    storage: StorageAdapter,
) -> str:
    name = tc["name"]
    args = tc.get("input") or {}

    if name == "search_products":
        result_str, products = tools_mod.handle_search_products(
            args.get("query", ""), session, inventory
        )
        # Replace candidate pool — only the latest search's results are
        # valid targets for present_products.
        last_search_candidates.clear()
        last_search_candidates.extend(products)
        return result_str

    if name == "present_products":
        result_str, selected = tools_mod.handle_present_products(
            args.get("product_ids", []), session, last_search_candidates,
        )
        # Replace presentation list. If the LLM calls present_products
        # more than once in a turn, each call overrides the previous.
        products_to_present.clear()
        products_to_present.extend(selected)
        return result_str

    if name == "update_session":
        return tools_mod.handle_update_session(args.get("fields", {}), session)

    if name == "trigger_handoff":
        return await tools_mod.handle_trigger_handoff(
            args.get("summary", ""), session, operator, messaging,
            storage, inventory, unified_text
        )

    if name == "request_haggle_approval":
        return await tools_mod.handle_request_haggle_approval(
            args.get("customer_ask", ""),
            args.get("product_ids", []),
            session, operator, messaging, storage, inventory, unified_text,
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
