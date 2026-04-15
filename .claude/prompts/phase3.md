# Phase 3 — Webhook and Messaging

## Goal

Full FastAPI server running. Real WhatsApp message arrives. All filtering,
deduplication, and routing logic in place. Server sends a hardcoded reply
(not LLM yet) from the operator's number. No sessions written yet — that
comes in Phase 4 with the conversation engine.

## Read these SPEC sections first

  S4  — Webhook receiver (full processing order)
  S5  — Async queue and worker
  S6  — Message buffer
  S13 — Messaging adapter (Whapi send)
  S15 — Saved contacts filter
  S19 — Startup sequence (focus on steps 1, 3, 8, 9, 11, 12)
  S2  — Whapi integration (webhook auth, payload structure)

## Prerequisites

  Phase 1 and Phase 2 complete and passing.
  Whapi account set up (from POC phase).
  ngrok running: ngrok http 8000
  Whapi channel webhook URL updated to ngrok HTTPS URL + /webhook
  Additional env vars needed:
    WEBHOOK_SECRET is set per-tenant in DB.
    For Phase 3 single-tenant test: manually insert a tenant record
    using scripts/check_session.py extended to also create a test tenant.

## What to build

### 1. app/utils/contacts.py
  ContactsCache class.
  Per-tenant set of phone numbers loaded from Whapi GET /contacts.
  is_contact(tenant_id, phone) -> bool
  async load_for_tenant(tenant) -> None
  async start_refresh(tenants, interval_s=3600) -> None (hourly background task)
  On Whapi API failure: log warning, serve stale cache.

### 2. app/adapters/messaging/base.py
  Abstract MessagingAdapter with send_text and send_image (signatures from CLAUDE.md).

### 3. app/adapters/messaging/whapi.py
  WhapiMessagingAdapter implements MessagingAdapter.
  send_text: POST /messages/text with typing_time=2
  send_image: POST /messages/image
  Both: decrypt(tenant.whapi_channel_token) per call.
  Both: 3-attempt retry with 1s, 2s backoff.
  On final failure: log send_failed event, send alert to operator personal phone.
    (Alert send has no retry — if it fails, log and stop.)
  Use httpx.AsyncClient.

### 4. app/buffer/buffer.py
  MessageBuffer class (per-tenant-user).
  Keys: (tenant_id, phone).
  Stores raw Whapi payload dicts.
  add(tenant_id, phone, payload, on_flush_callback)
  handle_deletion(tenant_id, phone, message_id)
  Debounce: asyncio task, 3 seconds, cancelled and restarted on each add.
  Force-flush at 10 messages.
  Rate limit: track last_flush_time per user, delay flush if < 8 seconds.
  on_flush: call the callback with (tenant_id, phone, payloads).

### 5. app/queue/queue.py + app/queue/worker.py
  queue.py: module-level asyncio.Queue instance.
  worker.py: consumes queue in continuous loop.
    Per-user asyncio.Lock (dict keyed by (tenant_id, phone)).
    Deduplication: _seen dict, message_id → timestamp, TTL 600s.
    On consume: check dedup → add to buffer → (buffer handles timer and flush).

### 6. app/webhook/session_disconnect_handler.py
  handle_disconnect(tenant, channel_id):
    Update tenant.status = DISCONNECTED via tenant adapter.
    Send alert to tenant.owner_personal_phone via messaging adapter.
    Log session_disconnect event.
  handle_reconnect(tenant):
    Update tenant.status = ACTIVE.
    Send confirmation to operator.
    Log session_reconnect event.

### 7. app/webhook/owner_action_handler.py
  handle(payload, tenant):
    Stub for Phase 3 — log that owner action received, do nothing else.
    Full implementation in Phase 5.

### 8. app/webhook/receiver.py
  Full implementation per S4 processing order.
  Step-by-step as specified, no shortcuts.
  Calls owner_action_handler for from_me: true.
  Places payload on queue for customer messages.
  Handles users.delete/post events for disconnect/reconnect.

### 9. app/config.py (extend from Phase 1)
  Add validation for all Phase 3 env vars.

### 10. app/main.py
  Full startup sequence for Phase 3:
    config.validate()
    crypto.init()
    tenant_adapter + storage_adapter connected
    contacts_cache loaded and refresh started
    inventory loaded (from Phase 2) and refresh started
    messaging_adapter initialised
    buffer + queue + worker started
    FastAPI app built with webhook routes
    uvicorn started

### 11. Dockerfile + .env.example
  Dockerfile: FROM python:3.11-slim, COPY requirements.txt, pip install,
              COPY app, CMD uvicorn app.main:app --host 0.0.0.0 --port 8000
  .env.example: all env vars from S20 with placeholder values and comments.

### 12. Hardcoded reply (temporary — replaced in Phase 4)
  In pipeline/runner.py: placeholder function.
  After buffer flush, call messaging_adapter.send_text with:
    "Salelular is listening. Full AI responses coming soon."
  This will be replaced by the full pipeline in Phase 4.

## Success criteria

Phase 3 passes when ALL of these work:
  1. Server starts without errors: uvicorn app.main:app
  2. ngrok forwards to port 8000
  3. Send a WhatsApp message to your connected number
  4. Console shows: message_received log event
  5. Console shows: buffer_flushed log event
  6. You receive: "Salelular is listening. Full AI responses coming soon."
  7. Send the same message twice rapidly: second is deduplicated (log shows)
  8. Send a group message: discarded (log shows message_discarded, reason=group)
  9. From_me message (reply from your phone): log shows owner action routing

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase3.md.
  Read SPEC.md sections S4, S5, S6, S13, S15, S19 (steps 1,3,8,9,11,12), S2.
  Invoke @architect with your plan before writing code.
  Invoke @security before implementing receiver.py and whapi.py.
  Use Plan Mode.
