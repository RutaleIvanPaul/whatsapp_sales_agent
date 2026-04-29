from __future__ import annotations

import asyncio

from app.adapters.llm.base import LLMAdapter
from app.adapters.messaging.base import MessagingAdapter
from app.adapters.operator.base import OperatorAdapter
from app.adapters.storage.base import StorageAdapter
from app.engine import haggling as haggling_mod
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
    operator_adapter: OperatorAdapter | None = None,
    rephrase_llm: LLMAdapter | None = None,
) -> None:
    """Route owner actions: control-thread commands OR passive interruption."""

    msgs = payload.get("messages", [])
    if not msgs:
        return
    msg = msgs[0]
    is_from_me = msg.get("from_me", False)

    if is_from_me:
        await _handle_passive_interruption(msg, operator, storage, messaging)
    else:
        await _handle_control_command(
            msg, operator, storage, messaging, operator_adapter, rephrase_llm
        )


# ── CASE A: control thread ───────────────────────────────────────────────────

async def _handle_control_command(
    msg: dict,
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
    operator_adapter: OperatorAdapter | None = None,
    rephrase_llm: LLMAdapter | None = None,
) -> None:
    raw_from = msg.get("from", "")
    try:
        sender = from_whapi(raw_from)
    except ValueError:
        return

    if sender != operator.owner_personal_phone:
        return

    original_text = ((msg.get("text") or {}).get("body") or "").strip()
    text = original_text.lower()
    if not text:
        return

    if text.startswith("resume"):
        target_phone = _parse_phone_arg(text, "resume")
        if target_phone == _INVALID_PHONE:
            raw = text[len("resume"):].strip()
            await messaging.send_text(
                operator.owner_personal_phone,
                f"'{raw}' doesn't look like a valid phone number. "
                f"Use the full number with country code, e.g. resume +256700123456",
                operator,
            )
            return
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
        if target_phone == _INVALID_PHONE:
            raw = text[len("handled"):].strip()
            await messaging.send_text(
                operator.owner_personal_phone,
                f"'{raw}' doesn't look like a valid phone number. "
                f"Use the full number with country code, e.g. handled +256700123456",
                operator,
            )
            return
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

    elif text.startswith("exclude"):
        await _cmd_exclude(text, operator, messaging, operator_adapter)

    elif text.startswith("include"):
        await _cmd_include(text, operator, messaging, operator_adapter)

    elif text.startswith("remove"):
        await _cmd_remove(text, operator, messaging, operator_adapter)

    elif text.startswith("reply "):
        await _cmd_reply(
            original_text, operator, storage, messaging, rephrase_llm,
        )

    elif text == "list excluded":
        phones = operator.excluded_phones or []
        msg_text = "Excluded numbers:\n" + "\n".join(f"  {p}" for p in phones) if phones else "No excluded numbers."
        await messaging.send_text(operator.owner_personal_phone, msg_text, operator)

    elif text == "list included":
        phones = operator.included_phones or []
        msg_text = "Included numbers (whitelisted contacts):\n" + "\n".join(f"  {p}" for p in phones) if phones else "No included numbers."
        await messaging.send_text(operator.owner_personal_phone, msg_text, operator)

    else:
        await messaging.send_text(
            operator.owner_personal_phone,
            "Unrecognised command. Available:\n"
            "  resume {phone} — let the assistant continue\n"
            "  handled {phone} — you've taken care of it\n"
            "  reply {phone} {text} — relay your response to the customer\n"
            "  exclude {phone} — block a number\n"
            "  include {phone} — allow a saved contact through\n"
            "  remove {phone} — undo exclude or include\n"
            "  list excluded / list included",
            operator,
        )
        log(
            "owner_command",
            operator_id=operator.operator_id,
            command_type="unrecognised",
        )


# ── Exclusion / inclusion commands ────────────────────────────────────────────

async def _cmd_exclude(
    text: str, operator: Operator, messaging: MessagingAdapter,
    operator_adapter: OperatorAdapter | None,
) -> None:
    phone = _parse_phone_arg(text, "exclude")
    if phone == _INVALID_PHONE:
        raw = text[len("exclude"):].strip()
        await messaging.send_text(
            operator.owner_personal_phone,
            f"'{raw}' doesn't look like a valid phone number. "
            f"Use the full number with country code, e.g. exclude +256700123456",
            operator,
        )
        return
    if not phone:
        await messaging.send_text(operator.owner_personal_phone, "Usage: exclude {phone}", operator)
        return
    # Mutually exclusive: remove from included if present
    if phone in operator.included_phones:
        operator.included_phones.remove(phone)
    if phone not in operator.excluded_phones:
        operator.excluded_phones.append(phone)
    if operator_adapter:
        operator_adapter.save(operator)
    await messaging.send_text(
        operator.owner_personal_phone,
        f"Excluded {phone}. They will no longer receive replies.",
        operator,
    )
    log("exclusion_added", operator_id=operator.operator_id, phone_hash=hash_for_log(phone))


async def _cmd_include(
    text: str, operator: Operator, messaging: MessagingAdapter,
    operator_adapter: OperatorAdapter | None,
) -> None:
    phone = _parse_phone_arg(text, "include")
    if phone == _INVALID_PHONE:
        raw = text[len("include"):].strip()
        await messaging.send_text(
            operator.owner_personal_phone,
            f"'{raw}' doesn't look like a valid phone number. "
            f"Use the full number with country code, e.g. include +256700123456",
            operator,
        )
        return
    if not phone:
        await messaging.send_text(operator.owner_personal_phone, "Usage: include {phone}", operator)
        return
    # Mutually exclusive: remove from excluded if present
    if phone in operator.excluded_phones:
        operator.excluded_phones.remove(phone)
    if phone not in operator.included_phones:
        operator.included_phones.append(phone)
    if operator_adapter:
        operator_adapter.save(operator)
    await messaging.send_text(
        operator.owner_personal_phone,
        f"Included {phone}. They can now reach the assistant even if they are a saved contact.",
        operator,
    )
    log("inclusion_added", operator_id=operator.operator_id, phone_hash=hash_for_log(phone))


async def _cmd_remove(
    text: str, operator: Operator, messaging: MessagingAdapter,
    operator_adapter: OperatorAdapter | None,
) -> None:
    phone = _parse_phone_arg(text, "remove")
    if phone == _INVALID_PHONE:
        raw = text[len("remove"):].strip()
        await messaging.send_text(
            operator.owner_personal_phone,
            f"'{raw}' doesn't look like a valid phone number. "
            f"Use the full number with country code, e.g. remove +256700123456",
            operator,
        )
        return
    if not phone:
        await messaging.send_text(operator.owner_personal_phone, "Usage: remove {phone}", operator)
        return
    removed_from = []
    if phone in operator.excluded_phones:
        operator.excluded_phones.remove(phone)
        removed_from.append("excluded")
    if phone in operator.included_phones:
        operator.included_phones.remove(phone)
        removed_from.append("included")
    if removed_from and operator_adapter:
        operator_adapter.save(operator)
    if removed_from:
        await messaging.send_text(
            operator.owner_personal_phone,
            f"Removed {phone} from {' and '.join(removed_from)} list.",
            operator,
        )
    else:
        await messaging.send_text(
            operator.owner_personal_phone,
            f"{phone} was not on any list.",
            operator,
        )
    log("exclusion_removed", operator_id=operator.operator_id, phone_hash=hash_for_log(phone))


async def _cmd_reply(
    original_text: str,
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter,
    rephrase_llm: LLMAdapter | None,
) -> None:
    """Owner sends `reply {phone} {instruction}` on the control thread.
    We find the target session, pass the instruction through the
    haggling-relay function, which rephrases into customer voice and
    sends it on the customer's thread.
    """
    # Strip the literal "reply " prefix (6 chars, case-insensitive)
    remainder = original_text[6:].strip()
    if not remainder:
        await messaging.send_text(
            operator.owner_personal_phone,
            "Usage: reply {phone} {your instruction to the customer}",
            operator,
        )
        return

    # Split phone from instruction. The phone token is the first whitespace-
    # delimited piece; the rest is the free-form instruction.
    parts = remainder.split(None, 1)
    if len(parts) < 2:
        await messaging.send_text(
            operator.owner_personal_phone,
            "Usage: reply {phone} {your instruction}\n"
            "Example: reply +256700123456 Sure, I can do 5% off that one.",
            operator,
        )
        return

    raw_phone, instruction = parts[0], parts[1].strip()
    try:
        target_phone = normalise(
            raw_phone if raw_phone.startswith("+") else "+" + raw_phone
        )
    except ValueError:
        await messaging.send_text(
            operator.owner_personal_phone,
            f"'{raw_phone}' doesn't look like a valid phone number. "
            f"Use the full number with country code, e.g. "
            f"reply +256700123456 Your message here",
            operator,
        )
        return

    if not instruction:
        await messaging.send_text(
            operator.owner_personal_phone,
            "Please include your instruction after the phone number.",
            operator,
        )
        return

    session = storage.get(operator.operator_id, target_phone)
    if session is None:
        await messaging.send_text(
            operator.owner_personal_phone,
            f"No conversation with {target_phone}.",
            operator,
        )
        return

    if rephrase_llm is None:
        # Degraded path: forward verbatim rather than failing silently
        try:
            await messaging.send_text(target_phone, instruction, operator)
        except Exception:
            pass
        await messaging.send_text(
            operator.owner_personal_phone,
            f"Sent your message to {target_phone} (verbatim — rephrase "
            f"unavailable).",
            operator,
        )
        return

    async with _lock_for(operator.operator_id, target_phone):
        await haggling_mod.relay_owner_instruction(
            operator=operator,
            session=session,
            owner_instruction=instruction,
            storage=storage,
            messaging=messaging,
            rephrase_llm=rephrase_llm,
        )

    log(
        "owner_command",
        operator_id=operator.operator_id,
        command_type="reply",
        phone_hash=hash_for_log(target_phone),
    )


# ── CASE B: passive interruption ─────────────────────────────────────────────

async def _handle_passive_interruption(
    msg: dict,
    operator: Operator,
    storage: StorageAdapter,
    messaging: MessagingAdapter | None = None,
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
    already_active = session.stage == Stage.OWNER_ACTIVE
    async with _lock_for(operator.operator_id, customer_phone):
        session.stage = Stage.OWNER_ACTIVE
        storage.set(operator.operator_id, customer_phone, session)

    # Notify the owner on their control thread the first time they take
    # over a given conversation. Don't re-notify on every message.
    if messaging is not None and not already_active:
        customer_label = session.name or customer_phone
        note = (
            f"Noted — you've taken over the chat with {customer_label}. "
            f"I'll stay out of this one. Send 'resume {customer_phone}' "
            f"here when you'd like me to pick it back up."
        )
        asyncio.create_task(
            messaging.send_text(operator.owner_personal_phone, note, operator)
        )

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


_INVALID_PHONE = "__INVALID__"


def _parse_phone_arg(text: str, command: str) -> str | None:
    """Extract the optional phone argument after a command word.

    'resume +256700123456' → '+256700123456'
    'resume 256700123456'  → '+256700123456'
    'resume'               → None
    'resume abc'            → _INVALID_PHONE sentinel
    """
    rest = text[len(command) :].strip()
    if not rest:
        return None
    try:
        return normalise(rest if rest.startswith("+") else "+" + rest)
    except ValueError:
        return _INVALID_PHONE
