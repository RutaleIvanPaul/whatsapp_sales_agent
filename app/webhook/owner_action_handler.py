from __future__ import annotations

import asyncio

from app.adapters.messaging.base import MessagingAdapter
from app.adapters.storage.base import StorageAdapter
from app.models.operator import Operator
from app.models.session import Session, Stage
from app.utils.log import log
from app.utils.phone import from_whapi, hash_for_log, normalise

# Per-session lock to prevent races with pipeline/runner.py mutating
# the same session concurrently (architect fix 1).
_session_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock_for(operator_id: str, phone: str) -> asyncio.Lock:
    key = (operator_id, phone)
    return _session_locks.setdefault(key, asyncio.Lock())


async def handle(
    payload: dict,
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
) -> None:
    """Route owner actions: control-thread commands OR passive interruption."""

    msgs = payload.get("messages", [])
    if not msgs:
        return
    msg = msgs[0]
    is_from_me = msg.get("from_me", False)

    if is_from_me:
        await _handle_passive_interruption(msg, operator, storage)
    else:
        await _handle_control_command(msg, operator, storage, messaging)


# ── CASE A: control thread ───────────────────────────────────────────────────

async def _handle_control_command(
    msg: dict,
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
) -> None:
    raw_from = msg.get("from", "")
    try:
        sender = from_whapi(raw_from)
    except ValueError:
        return

    if sender != operator.owner_personal_phone:
        return

    text = ((msg.get("text") or {}).get("body") or "").strip().lower()
    if not text:
        return

    if text.startswith("resume"):
        target_phone = _parse_phone_arg(text, "resume")
        session = await _resolve_handoff(
            operator, storage, messaging, target_phone
        )
        if session is None:
            return

        async with _lock_for(operator.operator_id, session.phone):
            session.stage = Stage.CONSIDERING
            storage.set(operator.operator_id, session.phone, session)

        await messaging.send_text(
            session.phone,
            "I'm still here if you'd like to continue browsing!",
            operator,
        )
        await messaging.send_text(
            operator.owner_personal_phone, "Handed back to bot.", operator
        )
        log(
            "owner_command",
            operator_id=operator.operator_id,
            command_type="resume",
            phone_hash=hash_for_log(session.phone),
        )

    elif text.startswith("handled"):
        target_phone = _parse_phone_arg(text, "handled")
        session = await _resolve_handoff(
            operator, storage, messaging, target_phone
        )
        if session is None:
            return

        async with _lock_for(operator.operator_id, session.phone):
            session.stage = Stage.OWNER_ACTIVE
            storage.set(operator.operator_id, session.phone, session)

        await messaging.send_text(
            operator.owner_personal_phone,
            "Got it. Bot suppressed for that conversation.",
            operator,
        )
        log(
            "owner_command",
            operator_id=operator.operator_id,
            command_type="handled",
            phone_hash=hash_for_log(session.phone),
        )

    else:
        await messaging.send_text(
            operator.owner_personal_phone,
            "Unrecognised command. Available:\n"
            "  resume {phone} — hand back to bot\n"
            "  handled {phone} — you are dealing with it",
            operator,
        )
        log(
            "owner_command",
            operator_id=operator.operator_id,
            command_type="unrecognised",
        )


# ── CASE B: passive interruption ─────────────────────────────────────────────

async def _handle_passive_interruption(
    msg: dict,
    operator: Operator,
    storage: StorageAdapter,
) -> None:
    """Operator typed in a customer thread (from_me=true). Set OWNER_ACTIVE."""
    chat_id = msg.get("chat_id", "")
    if not chat_id or chat_id.endswith("@g.us"):
        return

    raw_phone = chat_id.split("@")[0]
    try:
        customer_phone = from_whapi(raw_phone)
    except ValueError:
        return

    session = storage.get(operator.operator_id, customer_phone)
    if session is None:
        return

    # Bot-sent echoes are filtered in receiver.py via sent_tracker before
    # we reach here. If we're here, a human typed in the customer thread.
    async with _lock_for(operator.operator_id, customer_phone):
        session.stage = Stage.OWNER_ACTIVE
        storage.set(operator.operator_id, customer_phone, session)

    log(
        "owner_typed_in_customer_thread",
        operator_id=operator.operator_id,
        phone_hash=hash_for_log(customer_phone),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_handoff(
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
    target_phone: str | None,
) -> Session | None:
    """Find the HANDED_OFF session to act on. Sends operator reply and
    returns None if the target can't be determined."""

    # Look for sessions in HANDED_OFF (bot-triggered handoff) or
    # OWNER_ACTIVE (operator manually typed in customer thread).
    # Both represent "operator took over, bot is paused".
    handed_off = storage.get_by_stage(
        operator.operator_id, Stage.HANDED_OFF.value
    )
    owner_active = storage.get_by_stage(
        operator.operator_id, Stage.OWNER_ACTIVE.value
    )
    paused_sessions = handed_off + owner_active

    if not paused_sessions:
        await messaging.send_text(
            operator.owner_personal_phone, "No active handoff.", operator
        )
        return None

    if target_phone:
        for s in paused_sessions:
            if s.phone == target_phone:
                return s
        await messaging.send_text(
            operator.owner_personal_phone,
            f"No paused conversation for {target_phone}.",
            operator,
        )
        return None

    if len(paused_sessions) == 1:
        return paused_sessions[0]

    # Multiple paused sessions, no phone specified — disambiguation
    lines = [f"  {s.phone} — {s.name or 'unknown'}" for s in paused_sessions]
    await messaging.send_text(
        operator.owner_personal_phone,
        "Multiple paused conversations. Please specify:\n"
        + "\n".join(lines)
        + "\n\nExample: resume +256700123456",
        operator,
    )
    return None


def _parse_phone_arg(text: str, command: str) -> str | None:
    """Extract the optional phone argument after a command word.

    'resume +256700123456' → '+256700123456'
    'resume 256700123456'  → '+256700123456'
    'resume'               → None
    """
    rest = text[len(command) :].strip()
    if not rest:
        return None
    try:
        return normalise(rest if rest.startswith("+") else "+" + rest)
    except ValueError:
        return None
