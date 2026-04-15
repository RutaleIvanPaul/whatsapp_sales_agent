# Phase 4 — Conversation Engine

## Goal

Real customer enquiry gets a real AI reply with product images.
End-to-end: customer messages → buffer → input processor → language check
→ LLM with tools → search → session saved → reply sent with images.
This is the moment the system feels real.

## Read these SPEC sections first

  S7  — Input processor (all handlers)
  S8  — Language classifier
  S10 — Conversation engine and system prompt (full detail)
  S12 — Response builder
  S9  — Session store (expiry, history compression)

## Prerequisites

  Phases 1-3 complete and passing.
  OPENAI_API_KEY in .env.
  At least 5 products in your Google Sheet with image_url values.

## What to build

### 1. app/adapters/llm/base.py
  @dataclass
  class LLMResponse:
      text: str | None
      tool_calls: list[dict]   # [{name, arguments}]
      input_tokens: int
      output_tokens: int

  Abstract LLMAdapter with chat() method (signature from CLAUDE.md).

### 2. app/adapters/llm/openai_adapter.py
  OpenAILLMAdapter implements LLMAdapter.
  Uses openai Python SDK (openai>=1.0.0).
  chat() maps messages, tools, system to OpenAI chat.completions.create().
  Extracts tool_calls from response.
  Timeout: 30 seconds (raise LLMTimeoutError on timeout).
  Log llm_called event with token counts and latency.

### 3. app/adapters/llm/factory.py
  from_env() -> LLMAdapter: reads LLM_MODEL, returns OpenAILLMAdapter.

### 4. app/adapters/vision/base.py + openai_adapter.py + factory.py
  VisionAdapter with describe(image_url: str) -> str.
  OpenAI vision: chat.completions.create with image_url content block.
  Timeout: 8 seconds.
  On failure: return "[image received, could not be described]".

### 5. app/input/text.py
  clean(text: str) -> str: strip, normalise spaces.

### 6. app/input/image.py
  describe(payload: dict, vision_adapter: VisionAdapter) -> str
  Reads payload["image"]["link"] (Whapi auto-downloaded, stable URL).
  Calls vision_adapter.describe(link).
  On any exception: return "[image received, could not be described]".

### 7. app/input/voice.py
  transcribe(payload: dict) -> str
  Returns "[voice note received — please type your message]".
  (Deferred — placeholder only.)

### 8. app/input/link.py
  extract(payload: dict, inventory: InventoryAdapter) -> str
  Detect URL in text payload or link_preview type.
  Extract slug from URL path (last segment before query string).
  Check inventory.get_all() for product with matching slug.
  Return appropriate placeholder string (see S7).

### 9. app/input/language.py
  classify(text: str, api_key: str, model: str) -> str
  Single OpenAI call with CLASSIFIER_MODEL.
  Returns: "ENGLISH" | "LUGANDA" | "MIXED" | "UNKNOWN".
  On API failure: return "ENGLISH" and log warning.

### 10. app/input/processor.py
  async process(payloads: list[dict], vision, inventory) -> str
  Runs all handlers via asyncio.gather().
  Assembles unified_text in arrival order.
  Truncates if > 3000 chars (oldest text first).
  Returns unified_text.

### 11. app/engine/system_prompt.py
  build(tenant: Tenant, session: Session, unified_text: str,
        products_shown_names: list[str]) -> str
  Returns the full system prompt string per S10 template.
  Wraps unified_text in === CUSTOMER MESSAGE === delimiters.
  Includes stale session note if applicable.

### 12. app/engine/tools.py
  Tool schemas (for OpenAI function calling format):
    search_products: { name, description, parameters: {query: string} }
    update_session:  { name, description, parameters: {fields: object} }
    trigger_handoff: { name, description, parameters: {summary: string} }

  Tool handlers:
    handle_search_products(query, session, inventory) -> list[Product]
    handle_update_session(fields, session, storage) -> None
    handle_trigger_handoff(summary, session, tenant, messaging, triggering_msg)
      -> None  (stub in Phase 4 — full implementation in Phase 5)

### 13. app/engine/conversation.py
  async run(tenant, session, unified_text, adapters) -> (str, list[Product])
  Builds system prompt via system_prompt.py.
  Assembles messages list from session.history.
  Calls LLM with tool definitions.
  Tool execution loop (max 5 rounds):
    For each tool_call in response:
      Dispatch to correct handler in tools.py
      Append tool result to messages
    Call LLM again with updated messages
    If no tool_calls: break loop
  On LLMTimeoutError: return fallback text, log timeout event.
  After loop: return (reply_text, products_shown_this_turn).
  Call update_session to save session after each turn.

### 14. app/pipeline/response_builder.py
  async send_response(phone, reply_text, products, tenant, messaging):
  Split reply if > 1500 chars at "\n\n" boundary.
  Send each chunk via messaging.send_text (typing_time=2).
  Send each product image (max 3) via messaging.send_image.
  Caption: "{name}\n{price}\n{description}".

### 15. app/pipeline/runner.py (replace Phase 3 stub)
  async run(payloads, tenant):
    unified = await input.processor.process(payloads, vision, inventory)
    language = input.language.classify(unified, ...)
    if language in ("LUGANDA", "UNKNOWN"):
      send canned response + alert operator + return
    session = storage.get(tenant.tenant_id, sender_phone) or Session(...)
    reply_text, products = await engine.conversation.run(
      tenant, session, unified, adapters)
    await response_builder.send_response(
      sender_phone, reply_text, products, tenant, messaging)

## Success criteria

Phase 4 passes when:
  1. Send "do you have Nike shoes?" → AI reply about relevant products
  2. Product images appear after the text reply (if products found)
  3. Send image of a shoe → bot describes it and searches for similar
  4. Send message in Luganda → canned response received, no AI call made
  5. Session persists: ask about size, bot remembers in next message
  6. Console shows: llm_called, tool_called (search_products), message_sent logs

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase4.md.
  Read SPEC.md sections S7, S8, S10, S12, S9.
  Invoke @architect before writing code.
  Invoke @security on conversation.py (prompt injection check).
  Use Plan Mode.
