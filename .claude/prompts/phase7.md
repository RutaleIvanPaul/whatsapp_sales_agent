# Phase 7 — Human Feel Evaluation

## Goal

Every change in this phase was driven by live WhatsApp testing with a real
Whapi channel and real inventory. The shift from prior phases: stop proving
the system works in synthetic tests and start proving it *feels human* to
an actual customer. Every fix below came from a specific real-chat
failure, not a planned feature list.

The full per-file changelog is in [CHANGELOG_PHASE7.md](../../CHANGELOG_PHASE7.md).
This document is a narrative summary.

## Read these SPEC sections first

  S4  — Receiver filtering order (how webhooks reach the pipeline)
  S6  — Session storage
  S10 — Conversation engine flow (tools + rounds)
  S11 — System prompt (what the LLM sees)
  S17 — Operator onboarding + control thread commands
  S18 — Edge cases (each one cross-checked against live behaviour)
  S22 — Haggling (new section, Phase 7)

## What changed

### 1. Business context on the Operator
New required fields at onboarding:
  - `shop_category` — e.g. "Clothing & Fashion"
  - `shop_description` — free-form, 1–3 sentences about what the shop
    sells and how the product data is organised.
Injected into the system prompt so the LLM grounds searches in the
shop's actual domain. Without this, the LLM defaulted to generic
interpretations (e.g. treating "dress" as any clothing item).

### 2. Intent gate — 3-turn grace period
Replaces the former single-shot gate that silenced a session after one
NOT_SALES classification. Silent is now only triggered after 3 turns
pass with no SALES signal — once SALES is detected the gate locks to
`"passed"` and never runs again for that session. Rationale: bare
greetings ("Hello") were being filtered as NOT_SALES, killing legitimate
customers before they could say what they wanted.

### 3. Search + presentation redesign (biggest change)

New three-step contract:
  1. **Enrich**: LLM crafts a query using shop category + conversation
     context.
  2. **Search**: `search_products` returns up to 10 loose fuzzy candidates
     with full fields. **No side effects on `shown_product_ids`.**
  3. **Review + present**: LLM semantically reviews each candidate and
     calls the new `present_products(product_ids)` tool with only the
     truly-matching ids. Only presented items are added to
     `shown_product_ids`, sent as images, and used in handoff alerts.

Search scoring reworked to min-of-per-word `partial_ratio` (each query
word ≥3 chars must match individually, with full-query fallback for
verbose queries). Fixes "yellow dress" pulling in "Men's Dress Shirt".

### 4. Haggling strategy (new, hybrid 3-lever)

Three levers, strict precedence, prompt branches once at build time:

  1. **`operator.haggling_policy`** (free-form, ≤3 sentences) — shop-wide.
  2. **`product.haggling_notes`** (optional column J in the Google
     Sheet) — per-item override.
  3. **`operator.haggling_notify_first`** (bool) — when True, every
     haggling attempt escalates to the owner. LLM never negotiates
     autonomously.

All haggling business logic lives in `app/engine/haggling.py`,
transport-agnostic, so the same functions will back the future owner
web dashboard.

New tool `request_haggle_approval` (notify-first mode). New control-
thread command `reply {phone} {instruction}` — owner's free-form
instruction is rephrased into bot voice via a small LLM call and
delivered to the customer. Session flips back to EXPLORING.

### 5. Owner-takeover notification
When the operator types in a customer thread (passive interruption),
they now receive a confirmation on their control thread the first
time per takeover: "Noted — you've taken over the chat with X. Send
`resume +phone` here when you'd like me back."

### 6. Link + media + language hardening
  - Gibberish no longer gets the Luganda canned response (classifier
    now returns UNKNOWN for nonsense; pipeline drops UNKNOWN silently).
  - Links not in catalogue don't trigger blind searches — LLM asks
    the customer to describe what they want.
  - Quoted-reply extractor walks every known Whapi schema variant
    (`quoted_content`, `quoted`, `quoted_message`, nested `text.body`,
    nested `image/video/document.caption`); truncation raised from
    200 → 600 chars. Prompt explicitly teaches the LLM to treat
    `[replying to: "..."]` as the authoritative referent for "this",
    "that one", etc.

### 7. Response framing
The LLM no longer lists products in text; it writes a short human
intro ("Sure, let me share a few options") and the images with rich
captions (bold name, price, description, attributes) carry the
details. "Sure, let me share" is strictly gated on actually having
called `present_products` with a non-empty list.

### 8. Robustness
  - Malformed-reply detector (`_looks_malformed`): catches fragmented
    LLM output (multiple ellipses, <40% letters, <4 chars) and swaps
    in the fallback text so customers don't receive "Let's let … we …
    ..." type gibberish.
  - Per-turn repeat-query guard in the engine: if the LLM tries
    `search_products` with the same query it already ran, the tool
    returns a `duplicate_query` directive forcing progress.
  - `MAX_TOOL_ROUNDS` raised 5 → 8 to give legitimate multi-step
    flows room.

### 9. Lazy operator registration
New operators onboarded while the server is running are served
without a restart. On webhook miss for a channel_id, the registry
looks up the DB, initialises per-operator caches (inventory,
contacts), pins in memory. Necessary for production multi-tenant.

### 10. Provider decoupling
`check_health` and `get_contacts` moved into the `MessagingAdapter`
interface. All `gate.whapi.cloud` references now contained to
`app/adapters/messaging/whapi.py`. Swapping providers is one adapter
file.

### 11. Onboarding hardening
Every input field validated with actionable error messages (channel
ID format, token length, phone country-code, Sheets ID whitespace,
sheet tab name verified against actual tabs with case-sensitivity
warning, Google Sheets 403 vs 404 differentiated, webhook HTTPS
requirement). `SERVER_URL` moved from onboarding prompt to `.env`
(deployment config, not per-operator).

## Testing approach

No synthetic tests for this phase — each fix was validated against the
specific live conversation that surfaced it. The test operator was
re-onboarded multiple times; real customers were the test suite.

Smoke-test coverage for the haggling feature (6 unit tests) lives
alongside the haggling commit and is deleted after verification —
these test implementation details that will change, not contracts.

## Status

Phase 7 is still open. We are in continuous iteration mode until the
system feels reliably human to an independent reviewer. No exit
criteria beyond "enough live conversations pass without a pain point
surfacing that a normal customer would hit".
