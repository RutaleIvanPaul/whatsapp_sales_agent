# Phase 7 Change Log

Every notable change made since the Phase 6 hardening merge (`225de9c`),
through the Phase 7 human-feel evaluation.

---

## Schema / model changes

### Operator dataclass — new fields
File: [app/models/operator.py](app/models/operator.py)

- `shop_category: str` — e.g. "Clothing & Fashion", "Phone Accessories".
  Feeds the system prompt to ground the LLM in what the shop sells.
- `shop_description: str` — free-form sentence(s) about target customers,
  product organisation, gender/size tagging conventions. Used by the
  LLM to craft semantically appropriate searches.
- Both optional for backward compatibility in the SQLite adapter via
  `d.setdefault(...)` in `_deserialise`.

### Session dataclass — intent gate state
File: [app/models/session.py](app/models/session.py)

- `intent_gate_state: str = "unclassified"` — replaces the former
  one-shot intent gate. Values: `"unclassified"`, `"passed"`,
  `"silenced"`. See "3-turn grace period" below.

---

## Intent gate — 3-turn grace period

Files:
- [app/pipeline/runner.py](app/pipeline/runner.py)
- [app/input/intent.py](app/input/intent.py)

**Before:** Intent gate ran once on the first message from a new contact.
A misclassification silenced the entire session. Bare greetings like
"Hello" were often classified NOT_SALES and filtered out.

**Now:** For new contacts (no session or empty history):
- Turns 1–3: classifier runs but result is informational; bot replies
  regardless.
- If classifier returns SALES at any turn → `intent_gate_state` is
  locked to `"passed"`, gate never runs again for this session.
- If turn count reaches 3 without SALES → `intent_gate_state` set to
  `"silenced"`, future messages are dropped without LLM calls.
- Existing customers (session has history with `intent_gate_state`
  other than `"unclassified"`) skip the gate entirely.

**LLM classifier prompt** was also rewritten to bias toward SALES in
ambiguous shop contexts: "Reply SALES for anyone who might plausibly
be a customer; NOT_SALES only for clear wrong numbers or spam."

---

## Search + presentation redesign (major)

Files:
- [app/adapters/inventory/cache.py](app/adapters/inventory/cache.py)
- [app/engine/tools.py](app/engine/tools.py)
- [app/engine/conversation.py](app/engine/conversation.py)
- [app/engine/system_prompt.py](app/engine/system_prompt.py)

### Three-step flow
1. **Enrich** — LLM crafts a query using shop category, description,
   and conversation context (e.g. "women's yellow dress" not just
   "dress").
2. **Search** — `search_products` runs fuzzy matching with a lowered
   threshold (55) and returns up to **10 loose candidates** with full
   fields (name, price, description, keywords, attributes, image_url).
   No side effects on `shown_product_ids`.
3. **Review + present** — LLM reads candidates, rejects mismatches
   (wrong colour, gender, category, etc.), calls the new
   `present_products(product_ids)` tool with only the actually-matching
   ids. Only presented items become visible: added to
   `shown_product_ids`, sent as images, and used in handoff alert
   context. If nothing matches, LLM passes an empty list and explains
   in the reply text.

### New tool: `present_products`
Schema in [app/engine/tools.py](app/engine/tools.py:64). Handler
validates that each id was in the most recent `search_products` result
(drops hallucinated ids, logs). Replaces (doesn't accumulate) the
presentation list, so the latest call wins.

### Search scoring
`InventoryCache.search()` now uses dual scoring:
- `word_coverage` = min(`partial_ratio(word, index)` for each query
  word ≥3 chars)
- `full_score` = `partial_ratio(query, index)`
- Qualifies if `word_coverage ≥ threshold` OR `full_score ≥ threshold+15`
- Returns up to `MAX_CANDIDATES = 10`

The "min-of-words" rule fixes the class of bugs where e.g. "yellow
dress" returned "Men's Dress Shirt" (because "dress" scored 100 on
its own).

### System prompt updates
- Shop category + description injected into the prompt.
- Product data-structure explainer (what each field means + how to
  read attributes like `sizes: S M L | colour: yellow`).
- Explicit three-step flow instructions (enrich → search → review &
  present).
- Human-toned intro guidance ("Sure, let me share a few options")
  without listing product details in text — images carry that.
- Broad-category handling: for "men's clothes", run several narrow
  per-type searches rather than a category word that won't match
  product keywords.

---

## Handoff alert content

File: [app/engine/handoff.py](app/engine/handoff.py)

- Now shows **all** products shown in the session (not just the last
  one in a bulk-appended list).
- Uses the LLM's `trigger_handoff(summary)` text as the primary
  context line, falling back to `session.intent` if summary is empty.

---

## Provider decoupling

Files:
- [app/adapters/messaging/base.py](app/adapters/messaging/base.py)
- [app/adapters/messaging/whapi.py](app/adapters/messaging/whapi.py)
- [app/utils/contacts.py](app/utils/contacts.py)
- [app/main.py](app/main.py)

Moved `check_health(operator) -> dict` and
`get_contacts(operator) -> set[str]` into the `MessagingAdapter`
interface. The health monitor and `ContactsCache` now go through the
adapter — previously both called Whapi URLs directly. All
`https://gate.whapi.cloud` references are now contained in
`app/adapters/messaging/whapi.py`.

---

## Onboarding hardening

File: [scripts/onboard_operator.py](scripts/onboard_operator.py)

- Every input field now has specific validation with actionable error
  messages:
  - Channel ID: format check (alphanumeric + hyphens), length check
  - Channel token: length check, HTTP 401/403/404 differentiated in
    health response, non-JSON body handled
  - Owner phone: missing-country-code vs invalid-format distinguished
  - Sheets ID: whitespace and length checks
  - Sheet tab: verified against actual Google Sheet tabs; warns on
    case sensitivity; auto-selects when only one tab exists
  - Server URL: must be HTTPS; `http://` rejected with tunnel hint
  - Webhook config: HTTP 401/400/generic differentiated
  - Google Sheets API: 403 (permission) vs 404 (wrong ID) vs other
    errors
- Asks for `shop_category` and `shop_description` (new required
  fields).
- Removed `Server URL` prompt — it's deployment-level config now
  (must be in `.env` as `SERVER_URL`).

---

## Config / env

Files: `.env`, `.env.example`, [app/config.py](app/config.py)

- `SERVER_URL` documented in `.env.example` as a deployment setting.
- `SEARCH_THRESHOLD` lowered from 70 → 55 (wider retrieval net; LLM
  filters semantically).

---

## Phone-argument validation in owner commands

File: [app/webhook/owner_action_handler.py](app/webhook/owner_action_handler.py)

Commands (`resume`, `handled`, `exclude`, `include`, `remove`) now
return a specific error if the phone arg is malformed:
"'xyz' doesn't look like a valid phone number. Use the full number
with country code, e.g. exclude +256700123456."

Previously, malformed input yielded generic "Usage: …" or, worse,
silently tried to match the bad string against sessions.

---

## Google Sheets per-operator tab name

Files:
- [app/models/operator.py](app/models/operator.py) — `google_sheet_name`
- [app/adapters/operator/sqlite_adapter.py](app/adapters/operator/sqlite_adapter.py) — backward-compat default `"Sheet1"`
- [app/main.py](app/main.py) — uses `op.google_sheet_name`
- [app/adapters/inventory/sheets.py](app/adapters/inventory/sheets.py) — HTTP 400/403/404 differentiated

`google_sheet_name` moved out of `.env` (where it was global) into
the Operator record, so each operator's sheet can have a different
tab. Onboarding verifies the tab exists before proceeding.

---

## Other notable fixes

- **Intro tone**: LLM reply text no longer lists product names; images
  with rich captions carry everything.
- **No image accumulation across searches in a turn**: presentation
  list is now driven only by `present_products`, not by accumulation
  in `products_shown_this_turn`.
- **Image caption**: now includes bold name, price, description, and
  attributes.
- **Handoff closing message**: prompt requires naming the specific
  product (no more vague "Got it!").

---

## Removed

- Keyword-based intent allowlist for greetings (brittle). LLM
  classifier handles it with a shop-context-aware prompt.
- Name-matching filter in response builder (we now trust
  `present_products` explicit selection).

---

## Summary of tool changes

| Tool               | Before                                 | After |
|--------------------|----------------------------------------|-------|
| `search_products`  | Returns ≤5, auto-adds to `shown_ids` | Returns ≤10 candidates; no side effects |
| `present_products` | did not exist                          | LLM picks which candidates to show; adds to `shown_ids`; drives images |
| `update_session`   | unchanged                              | Prompt now emphasises calling it BEFORE search/handoff |
| `trigger_handoff`  | unchanged                              | Summary description now asks for specific product+price; alert uses it |

---

## Migration note

The DB was wiped and re-onboarding is required because the Operator
record now includes required `shop_category` and `shop_description`
fields. Existing sessions also gain `intent_gate_state` with a
backward-compatible default.
