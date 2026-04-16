# Phase 5 — Handoff and Owner Control

## Goal

Full handoff flow end-to-end. Customer expresses buying intent →
bot alerts operator on personal number with a wa.me link →
operator taps the link, types in the shop's customer thread →
bot detects the takeover (from_me: true) and stays quiet →
operator types `resume {phone}` or `handled {phone}` in the control
thread when done.

Design principle (see SPEC.md S14): no relay. The operator's personal
thread is for ALERTS and CONTROL COMMANDS only.

## Read these SPEC sections first

  S14 — Handoff manager (full detail — note the no-relay design)
  S2  — Whapi health monitoring section

## Prerequisites

  Phases 1-4 complete and passing.
  Two WhatsApp numbers available for testing:
    Number A: the shop number (connected to Whapi)
    Number B: the operator's personal number (OWNER_PERSONAL_PHONE on operator)
  Test a handoff by sending a strong buying signal to Number A from a
  third number (or a friend's phone).

## What to build

### 1. app/engine/handoff.py (full implementation)
  async trigger(session, summary, operator, messaging, triggering_message):

  a. Update session: stage=HANDED_OFF, handed_off_at=now()
  b. Persist session immediately via storage adapter
  c. Build the wa.me link from the customer phone:
       link = f"https://wa.me/{to_whapi(session.phone)}"
     (to_whapi strips the leading '+' — wa.me uses bare digits)
  d. Send alert to operator.owner_personal_phone with:
     - Customer name / phone
     - session.intent
     - Last shown product name and price (from session.shown_product_ids)
     - triggering_message (truncated to ~120 chars)
     - The wa.me link
     - Command hint: "resume {phone}" / "handled {phone}"
     Include interactive buttons where the messaging adapter supports them:
       [Resume AI]        → "resume {phone}"
       [I'll handle this] → "handled {phone}"
  e. Log handoff_triggered event.
  f. Return. (LLM writes closing message to customer after this returns.)

  NO RELAY STATE:
    Multiple customers can be in HANDED_OFF simultaneously. There is no
    shared "active relay" pointer on Session or Operator; the operator
    disambiguates by providing {phone} in the command.

### 2. app/webhook/owner_action_handler.py (full implementation)

  Dispatch rules (in receiver.py, before calling this handler):
    Control thread  = from_me: false AND normalise(msg.from) == operator.owner_personal_phone
    Customer thread = from_me: true  AND chat_id is a customer chat

  IMPORTANT: the receiver must NOT apply the contacts-cache filter to
  messages from the owner's personal phone. Those are control commands,
  not casual chatter from a saved contact.

  async handle(payload, operator, storage, messaging):
    msg = payload["messages"][0]

    CASE A — control thread (from_me: false, from == operator.owner_personal_phone):
      text = (msg.text.body or "").strip().lower()

      If text starts with "resume":
        target_phone = parse_phone_arg(text)  # None if no arg
        session = find_handoff_session(operator, target_phone, storage)
          - If target_phone given: look up session for that customer.
          - If not: if exactly one HANDED_OFF session for this operator, use it;
            else reply "Please specify: resume {phone}. Active handoffs: {list}."
          - If no HANDED_OFF session: reply "No active handoff." Return.
        session.stage = CONSIDERING
        persist(session)
        await messaging.send_text(session.phone,
          "I'm still here if you'd like to continue browsing!", operator)
        await messaging.send_text(operator.owner_personal_phone,
          "Handed back to bot.", operator)
        Log owner_command{command_type=resume}

      Elif text starts with "handled":
        Same lookup rules.
        session.stage = OWNER_ACTIVE
        persist(session)
        await messaging.send_text(operator.owner_personal_phone,
          "Got it. Bot suppressed for that conversation.", operator)
        Log owner_command{command_type=handled}

      Else:
        await messaging.send_text(operator.owner_personal_phone,
          "Unrecognised command. Available: resume {phone}, handled {phone}.",
          operator)

    CASE B — customer thread (from_me: true, chat_id is a customer):
      # Passive interruption (Mode 2) — operator typed in customer thread
      customer_phone = normalise(chat_id phone)
      session = storage.get(operator.operator_id, customer_phone)
      if session:
        session.stage = OWNER_ACTIVE
        persist(session)
        Log owner_typed_in_customer_thread

### 3. Holding message logic (in pipeline/runner.py)
  When session.stage == HANDED_OFF and customer sends a new message:
    Check session.last_holding_sent.
    If None or (now() - last_holding_sent) > 3600 seconds (1 hour):
      Send: "The team has been notified and will be with you shortly!"
      Set session.last_holding_sent = now().
      Persist session.
    Else: do nothing.

### 4. 24-hour inactivity revert (in pipeline/runner.py)
  When session.stage == HANDED_OFF:
    If (now() - session.handed_off_at) > 86400 seconds (24 hours):
      Set session.stage = CONSIDERING.
      Bot responds normally.
      Prepend to bot message: "I'm still here if you'd like to keep browsing!"

### 5. Background health monitor (app/main.py extension)
  asyncio task, runs every WHAPI_HEALTH_CHECK_INTERVAL_S seconds.
  For each active operator:
    GET https://gate.whapi.cloud/health?token={decrypted_channel_token}
    If response does not indicate CONNECTED:
      If operator.status != DISCONNECTED:
        Call session_disconnect_handler.handle_disconnect(operator)

### 6. session_disconnect_handler.py (already stubbed in Phase 3 — just verify)
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
  1. Customer sends strong buying signal → operator gets alert with wa.me link
  2. Alert includes [Resume AI] / [I'll handle this] buttons (where supported)
  3. Operator taps wa.me link → lands in the customer's thread on the shop's WhatsApp
  4. Operator types in that customer thread → stage becomes OWNER_ACTIVE,
     bot stops responding to that customer (passive interruption)
  5. Operator types "resume {customer_phone}" in control thread →
     customer stage becomes CONSIDERING; bot resumes on next message
  6. Operator types "handled {customer_phone}" → stage becomes OWNER_ACTIVE
  7. Customer messages while HANDED_OFF → holding message sent once per hour max
  8. Two customers in HANDED_OFF concurrently → `resume` without arg replies
     with disambiguation prompt; `resume {phone}` targets the right one
  9. Whapi health endpoint checked — disconnect alert fires if session drops

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase5.md.
  Read SPEC.md sections S14 and the health monitoring part of S2.
  Invoke @architect before writing code.
  Invoke @security on owner_action_handler.py (operator-command auth,
    E.164 phone comparison with normalise()).
  Use Plan Mode.
