# Phase 5 — Handoff and Owner Control

## Goal

Full handoff flow end-to-end. Customer expresses buying intent →
bot alerts operator on personal number → operator types in control thread →
message forwarded to customer from shop number → operator resumes bot.

## Read these SPEC sections first

  S14 — Handoff manager (full detail including concurrent handoff edge case)
  S2  — Whapi health monitoring section

## Prerequisites

  Phases 1-4 complete and passing.
  Two WhatsApp numbers available for testing:
    Number A: the shop number (connected to Whapi)
    Number B: the operator's personal number (OWNER_PERSONAL_PHONE in operator)
  Test a handoff by sending a strong buying signal to Number A from a
  third number (or a friend's phone).

## What to build

### 1. app/engine/handoff.py (full implementation)
  async trigger(session, summary, operator, messaging, triggering_message):

  a. Update session: stage=HANDED_OFF, handed_off_at=now(),
     active_handoff_phone=session.phone
  b. Persist session immediately
  c. Send notification to operator.owner_personal_phone:

     "Ready to close:
      Customer: {session.name or session.phone}
      Looking for: {session.intent}
      Interested in: {last shown product name and price}
      What they said: "{triggering_message}"

      Reply here to message them from your shop number.
      Commands: reply=resume, reply=handled"

  d. Return. (LLM writes closing message to customer after this returns.)

  CONCURRENT HANDOFF EDGE CASE — must be implemented:
    When trigger() is called and session.active_handoff_phone is already set
    for another session belonging to this operator:
      Log warning: concurrent_handoff_attempt
      Send notification to operator (they can see both)
      Set new session.stage = HANDED_OFF
      Do NOT overwrite existing active_handoff_phone on operator
      Operator must clear existing relay before new one activates

### 2. app/webhook/owner_action_handler.py (full implementation)

  handle(payload, operator):
    sender = normalise(payload from)
    text = payload message text (lowercased, stripped)

    CASE 1: sender == operator.owner_personal_phone (control thread)
      If text == "resume" or text.startswith("resume "):
        Find session in HANDED_OFF stage for this operator
        If specific phone in command: use that session
        If not: use session with active_handoff_phone set
        If none found: send "No active handoff." to operator. Return.
        Set session.stage = CONSIDERING
        Clear session.active_handoff_phone
        Persist session
        Start 10-minute context window (asyncio task):
          If operator sends more context within 10 min: prepend to session
          After 10 min or on "done" signal: bot resumes

      Elif text == "handled":
        Find HANDED_OFF session for this operator
        Set session.stage = OWNER_ACTIVE
        Clear session.active_handoff_phone
        Persist session
        Reply to operator: "Got it. Bot suppressed for that conversation."

      Else (unrecognised text, relay mode):
        Find session with active_handoff_phone set for this operator
        If found:
          Forward operator's message to active_handoff_phone via messaging
          Append to session.history as role='assistant'
          Persist session
          Log owner_relay_sent event
        Else:
          No active relay. Log warning. Do nothing.

    CASE 2: from_me == true in a customer chat (operator typed in customer thread)
      Extract customer phone from chat_id
      Find session for (operator_id, customer_phone)
      If found: set session.stage = OWNER_ACTIVE. Persist. Log.

### 3. Holding message logic (in pipeline/runner.py)
  When session.stage == HANDED_OFF and customer sends a new message:
    Check session.last_holding_sent
    If None or (now() - last_holding_sent) > 3600 seconds (1 hour):
      Send: "The team has been notified and will be with you shortly!"
      Set session.last_holding_sent = now()
      Persist session
    Else: do nothing

### 4. 24-hour inactivity revert (in pipeline/runner.py)
  When session.stage == HANDED_OFF:
    If (now() - session.handed_off_at) > 86400 seconds (24 hours):
      Set session.stage = CONSIDERING
      Bot responds normally
      Prepend to bot message: "I'm still here if you'd like to keep browsing!"

### 5. Background health monitor (app/main.py extension)
  asyncio task, runs every WHAPI_HEALTH_CHECK_INTERVAL_S seconds.
  For each active operator:
    GET https://gate.whapi.cloud/health?token={decrypted_channel_token}
    If response does not indicate CONNECTED:
      If operator.status != DISCONNECTED:
        Call session_disconnect_handler.handle_disconnect(operator)

### 6. session_disconnect_handler.py (complete)
  handle_disconnect(operator):
    Update operator.status = DISCONNECTED via operator adapter
    Send alert to operator.owner_personal_phone:
      "Your Salelular bot has disconnected from WhatsApp.
       Open WhatsApp > Linked Devices to reconnect,
       or contact support."
    Log session_disconnect event

  handle_reconnect(operator):
    Update operator.status = ACTIVE
    Send to operator: "Your Salelular bot is back online."
    Log session_reconnect event

## Success criteria

Phase 5 passes when:
  1. Customer sends "I'll take it" → operator personal number receives alert
  2. Operator types reply in personal WhatsApp chat with bot number
  3. Customer receives that reply from the SHOP number (not operator's personal)
  4. Operator types "resume" → customer gets "I'm still here..."
  5. Bot resumes normal conversation after resume
  6. Customer messages while HANDED_OFF → holding message once per hour max
  7. Operator types in customer thread → passive detection, stage=OWNER_ACTIVE
  8. Whapi health endpoint checked — disconnect alert fires if session drops

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase5.md.
  Read SPEC.md sections S14 and the health monitoring part of S2.
  Invoke @architect before writing code.
  Invoke @security on owner_action_handler.py.
  Use Plan Mode.
  Pay special attention to the concurrent handoff edge case in S14.
  This must be explicitly handled — it is not an optional edge case.
