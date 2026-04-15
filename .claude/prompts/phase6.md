# Phase 6 — Resilience

## Goal

Every edge case in S18 is handled. System survives bad input, API failures,
concurrent users, expired sessions. Cost tracking is visible.
Operator onboarding is scriptable.

## Read these SPEC sections first

  S18 — Edge cases (complete table)
  S20 — Environment variables (verify all are validated)
  S23 — Operator onboarding

## What to build

### 1. Per-user daily message cap (app/buffer/buffer.py or app/pipeline/runner.py)
  Track message count per (tenant_id, phone) per calendar day (UTC).
  Use a simple in-memory dict: { (tenant_id, phone, date): count }
  When count exceeds MAX_MESSAGES_PER_USER_DAY:
    Send to customer: "Thank you for your interest! Please reach out again
                       tomorrow or contact us directly."
    Do not call conversation engine.
    Reset count at midnight UTC (or on first message of new day).

### 2. LLM timeout fallback (app/engine/conversation.py)
  LLMTimeoutError is already raised on timeout in Phase 4.
  Ensure it is caught in conversation.py.
  On catch: send to customer: "Sorry, I'm having trouble right now.
             Please try again in a moment."
  Log timeout event.

### 3. Tool loop limit enforcement (app/engine/conversation.py)
  Max 5 rounds is spec'd. Verify it is implemented.
  On limit reached: log warning, use last text response if available.
  If no text response obtained: send fallback message to customer.

### 4. Session expiry with stale note (app/engine/conversation.py)
  On load: check if session.last_active > SESSION_EXPIRY_DAYS ago.
  If so: inject stale_session_note into system_prompt.build() call.
  stale_session_note = f"This customer last spoke with you {N} days ago.
                         Greet them warmly and re-establish what they need."

### 5. History compression (app/engine/conversation.py or session store)
  When session.history length > MAX_HISTORY_TURNS:
    Take the oldest 2 turns (1 user + 1 assistant pair).
    Extract key information: what customer asked, what was shown.
    Create a compression note:
      "[Earlier: customer asked about {intent_from_those_turns},
        was shown {product_names_from_those_turns}]"
    Prepend note to remaining history as a system message.
    Remove the 2 oldest turns.
  This is done by your code, not the LLM.

### 6. Cost tracking log (app/utils/logging.py extension)
  asyncio task that fires at midnight UTC daily.
  Logs: llm_cost_summary event with per-tenant:
    total input_tokens, total output_tokens, vision_calls, estimated_cost_usd.
  Track cumulative tokens in memory (reset at midnight).
  Estimated cost calculated from known OpenAI rates for configured models.

### 7. Full config validation (app/config.py)
  Validate ALL environment variables from S20.
  For each: check presence, check format where applicable.
  ENCRYPTION_KEY: must decode to exactly 32 bytes.
  GOOGLE_CREDENTIALS_JSON: must be valid base64 that decodes to valid JSON
    with required service account fields.
  STORAGE_URL: must be parseable as a SQLite path or valid DB URL.
  All numeric env vars: must parse as integers.
  Exit with descriptive error message listing ALL missing/invalid vars at once
  (not just the first one found).

### 8. scripts/onboard_tenant.py
  Interactive CLI script for adding a new operator.
  Prompts for: shop_name, owner_name, owner_personal_phone, google_sheets_id,
               luganda_canned_response.
  Then:
    Generate 32-byte webhook secret
    Create Whapi channel via Partner API
    Configure channel (webhook, events, auto_download, secret header)
    Insert tenant record into DB (encrypt sensitive fields)
    Print QR code URL for operator to scan
    Wait for users.post webhook or prompt user to confirm scan manually
    Load inventory from Google Sheet
    Print product count
    Print "Tenant {shop_name} is live."

### 9. Edge case verification
  Write integration tests or manual test scripts for:
  - Duplicate webhook (same message_id twice) → second discarded
  - Group message → discarded
  - Empty message text → handled without crash
  - Vision API timeout → placeholder returned, bot asks to type
  - Zero search results → LLM asks clarifying question
  - Unavailable product → not returned in search
  - Message > 1500 chars from LLM → split correctly
  - Session > 10 turns → history compressed, older turns summarised
  - Second handoff while first relay active → warning logged, alert sent

## Success criteria

Phase 6 passes when:
  1. All items in S18 edge case table are verified (manually or by test)
  2. python scripts/onboard_tenant.py completes without errors
  3. Daily cost summary logs at midnight (or on demand via test)
  4. config.validate() catches and reports ALL missing env vars at once
  5. No uncaught exceptions under any tested bad input scenario
  6. Load test: 10 concurrent test users, no session corruption

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase6.md.
  Read SPEC.md sections S18, S20, S23.
  Invoke @architect before writing code.
  Invoke @reviewer on the completed runner.py and conversation.py.
  Invoke @tester to generate a full test suite.
  Use Plan Mode.
