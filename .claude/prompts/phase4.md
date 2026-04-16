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
  ANTHROPIC_API_KEY in .env.
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

### 2. app/adapters/llm/anthropic_adapter.py
  AnthropicLLMAdapter implements LLMAdapter.
  Uses the anthropic Python SDK (anthropic>=0.40.0). Use AsyncAnthropic.
  chat() maps messages + tools + system to client.messages.create():
    - system prompt → top-level `system` param (string)
    - messages → list of {role: "user"|"assistant", content: ...}
    - tools → list of {name, description, input_schema (JSON Schema)}
    - max_tokens: required by Anthropic API (use 1024 for replies)
    - model: from LLM_MODEL env var (default: claude-sonnet-4-6)
  Parse the response:
    - response.content is a list of content blocks
    - For each block:
      - type == "text"     → append to text reply
      - type == "tool_use" → record {id, name, input} as tool call
    - response.usage.input_tokens / output_tokens for cost logging
  Timeout: 30 seconds (raise LLMTimeoutError on timeout).
  Log llm_called event with token counts and latency_ms.

### 3. app/adapters/llm/factory.py
  from_env() -> LLMAdapter: reads ANTHROPIC_API_KEY + LLM_MODEL,
  returns AnthropicLLMAdapter.

### 4. app/adapters/vision/base.py + anthropic_adapter.py + factory.py
  VisionAdapter with describe(image_url: str) -> str.
  Anthropic vision: client.messages.create with content block:
    {"type": "image", "source": {"type": "url", "url": image_url}}
  followed by a text block: "Describe this image briefly. Focus on
  product details: type, colour, brand, key features."
  Model: VISION_MODEL env var (default: claude-sonnet-4-6).
  Timeout: 8 seconds.
  On failure: return "[image received, could not be described]".

  NOTE: Anthropic supports image URLs directly. If a future provider
  swap requires base64, convert at this adapter boundary only.

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
  async classify(text: str, api_key: str, model: str) -> str
  Single Anthropic call with CLASSIFIER_MODEL (claude-haiku-4-5-20251001).
  Use AsyncAnthropic, max_tokens=10, system="You are a language
  classifier. Reply with exactly one word."
  User prompt per S8.
  Returns: "ENGLISH" | "LUGANDA" | "MIXED" | "UNKNOWN".
  On API failure: return "ENGLISH" and log warning.

### 10. app/input/processor.py
  async process(payloads: list[dict], vision, inventory) -> str
  Runs all handlers via asyncio.gather().
  Assembles unified_text in arrival order.
  Truncates if > 3000 chars (oldest text first).
  Returns unified_text.

### 11. app/engine/system_prompt.py
  build(operator: Operator, session: Session, unified_text: str,
        products_shown_names: list[str]) -> str
  Returns the full system prompt string per S10 template.
  Wraps unified_text in === CUSTOMER MESSAGE === delimiters.
  Includes stale session note if applicable.

### 12. app/engine/tools.py
  Tool schemas (Anthropic format — `input_schema` is JSON Schema):
    search_products:
      name: "search_products"
      description: "Search the product inventory by natural-language query."
      input_schema: {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
    update_session:
      name: "update_session"
      description: "Persist what you have learned about this customer."
      input_schema: {"type":"object","properties":{"fields":{"type":"object"}},"required":["fields"]}
    trigger_handoff:
      name: "trigger_handoff"
      description: "Alert the operator that the customer is ready to buy."
      input_schema: {"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"]}

  Tool handlers:
    handle_search_products(query, session, inventory) -> list[Product]
    handle_update_session(fields, session, storage) -> None
    handle_trigger_handoff(summary, session, operator, messaging, triggering_msg)
      -> None  (stub in Phase 4 — full implementation in Phase 5)

### 13. app/engine/conversation.py
  async run(operator, session, unified_text, adapters) -> (str, list[Product])
  Builds system prompt via system_prompt.py (string passed as `system`).
  Assembles messages list from session.history.
  Calls LLM with tool definitions.

  Tool execution loop (max 5 rounds), Anthropic-shaped:
    Send context to LLM → parse response.content blocks
    If any tool_use blocks present:
      Append the assistant message (full content list, including tool_use)
        to messages
      For each tool_use block:
        Dispatch to correct handler in tools.py
        Append a user message with content:
          [{"type": "tool_result", "tool_use_id": <id>, "content": <result_str>}]
      Call LLM again with updated messages
    Else (only text blocks, or empty content):
      Concatenate text blocks → reply_text
      Break loop.

  On LLMTimeoutError: return fallback text, log timeout event.
  After loop: return (reply_text, products_shown_this_turn).
  Call update_session to save session after each turn.

### 14. app/pipeline/response_builder.py
  async send_response(phone, reply_text, products, operator, messaging):
  Split reply if > 1500 chars at "\n\n" boundary.
  Send each chunk via messaging.send_text (typing_time=2).
  Send each product image (max 3) via messaging.send_image.
  Caption: "{name}\n{price}\n{description}".

### 15. app/pipeline/runner.py (replace Phase 3 stub)
  async run(payloads, operator):
    unified = await input.processor.process(payloads, vision, inventory)
    language = input.language.classify(unified, ...)
    if language in ("LUGANDA", "UNKNOWN"):
      send canned response + alert operator + return
    session = storage.get(operator.operator_id, sender_phone) or Session(...)
    reply_text, products = await engine.conversation.run(
      operator, session, unified, adapters)
    await response_builder.send_response(
      sender_phone, reply_text, products, operator, messaging)

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
