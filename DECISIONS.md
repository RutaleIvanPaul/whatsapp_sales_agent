# DECISIONS.md — Salelular

Permanent record of why things are built the way they are.
Each entry documents a deviation from the original spec or a non-obvious
design choice, with the context that forced the decision.

---

## 1. Async adapter interfaces

**Decision:** All I/O adapter interfaces (LLMAdapter, VisionAdapter,
MessagingAdapter) use `async def` methods.

**Original spec:** CLAUDE.md showed sync signatures.

**Why async was required:** FastAPI runs on a single-threaded async event
loop. All external I/O — Whapi HTTP calls (httpx.AsyncClient), Anthropic
API (AsyncAnthropic), Groq API (AsyncGroq) — is natively async. Sync
methods would block the event loop, preventing concurrent handling of
webhook requests and background tasks (inventory refresh, contacts
refresh, buffer timers). Using `asyncio.to_thread()` to wrap sync calls
adds thread-pool overhead and negates the benefit of async FastAPI.

**Impact:** Every caller of these adapters must `await`. The conversation
engine, pipeline runner, webhook handlers, and buffer flush callback are
all async. StorageAdapter and OperatorAdapter remain sync (SQLite is
local I/O, not network-bound, and the calls are sub-millisecond).

**Date:** Phase 3 (April 2026)

---

## 2. Bigram sub-query search enhancement

**Decision:** When a search query has 3+ words, InventoryCache.search()
also tries all consecutive word pairs (bigrams) and uses the best score
per product across all sub-queries.

**Original spec:** S11 described a single `partial_ratio(query, index_str)`
per product. Threshold: 70.

**Why it was needed:** Vision descriptions from image inputs produce
queries like `"black Adidas sneakers leather upper white stripes black
laces rubber sole"`. The `partial_ratio` of this 11-word string against
a product's index string scores 50-64 — below the 70 threshold. But the
bigram `"adidas sneakers"` from the same query scores 78.6, which is a
strong match.

Without bigram decomposition, image-based product search returned 0
results in the first 3 live tests during Phase 4. Lowering the threshold
to 55 was tried but rejected — it produced false positives (e.g. "green
neon sneaker" matched non-green, non-neon products).

The bigram approach keeps the threshold at 70 (preserving precision)
while recovering recall for long, compound queries.

**Impact:** Search is slightly slower (N * (W-1) comparisons instead of
N, where W is word count). At 15 products this is negligible. At 10K+
products, profile before worrying — the lock hold time is still short.

**Date:** Phase 4 edge-case sweep (April 2026)

---

## 3. Provider-neutral LLM architecture

**Decision:** LLM and vision providers are selected at runtime via
`LLM_PROVIDER` / `VISION_PROVIDER` env vars. Adapters are instantiated
by factory functions that dispatch on the provider name. Business logic
never imports from a specific adapter implementation.

**Original spec:** Assumed a single provider (initially OpenAI, then
changed to Anthropic Claude during Phase 4 planning).

**Why LLM_PROVIDER was added:** Phase 4 was built with Anthropic Claude
as the LLM, but the Anthropic account had zero credit balance. Groq was
added as a temporary alternative. Rather than maintain two codepaths or
wait for billing, the adapter contract was refactored to be
provider-neutral:

- Tool schemas use a neutral `{name, description, parameters}` format.
  Each adapter translates to its native shape:
  - Anthropic: `parameters` → `input_schema`
  - Groq/OpenAI: wrapped in `{type: "function", function: {...}}`

- LLMResponse.assistant_message is a provider-shaped dict that the
  conversation engine appends verbatim. It preserves tool_use blocks
  (Anthropic) or tool_calls lists (OpenAI/Groq) without the engine
  knowing which shape it's handling.

- LLMAdapter.make_tool_result_messages() encapsulates the incompatible
  tool-result shapes:
  - Anthropic: one user message with tool_result content blocks
  - OpenAI/Groq: separate "tool" role messages per tool_call_id

**How to switch providers:** Change 3 env vars (LLM_PROVIDER, LLM_API_KEY,
LLM_MODEL). No code changes. Vision and classifier can use different
providers/models independently.

**How to add a new provider:** Create `app/adapters/llm/<name>_adapter.py`
implementing LLMAdapter, add an `elif` branch in `factory.py`. OpenAI
would be nearly a copy of the Groq adapter (same API shape).

**Date:** Phase 4 Groq branch (April 2026)

---

## 4. Whapi phone number format normalisation (from_whapi / to_whapi)

**Decision:** Two named helper functions in app/utils/phone.py handle
conversion between Whapi's wire format and the canonical +E.164 format
used throughout the business logic:
  - `from_whapi(raw)` → `+E.164` (strips @s.whatsapp.net, prepends +)
  - `to_whapi(e164)` → bare digits (strips +)

**Why this was needed:** Whapi uses bare international digits with no `+`
prefix in both directions:
  - Inbound: `"from": "256705878284"` (no `+`, no `@s.whatsapp.net`)
  - Outbound: the `to` field must match `^[\d-]{9,31}(@[\w\.]+)?$`.
    Including a `+` produces HTTP 400 ("wrong request parameters").

The canonical format used by all business logic, logging, session keys,
and operator records is +E.164 (`"+256705878284"`). This is the ITU-T
standard, required by most messaging APIs, and unambiguous.

The initial implementation scattered `"+" + raw_phone` and
`phone.lstrip("+")` conversions across 5 files (receiver.py, worker.py,
runner.py, contacts.py, whapi.py). This was error-prone — any new file
touching phone numbers could introduce the same bug. The named functions
`from_whapi()` / `to_whapi()` centralise the conversion at the adapter
boundary.

**Design principle:** Business logic deals only in +E.164. Provider wire
formats live behind named adapter functions called only at the boundary.
When a future provider is added (e.g. Twilio, which uses +E.164
natively), `from_twilio()` / `to_twilio()` would be identity functions.

**Date:** Phase 3 live testing (April 2026)

---

## 5. Operator-facing alert content rules

**Decision:** All WhatsApp alerts sent to the operator's personal phone
must address them by name, explain the issue in plain language, provide
actionable next steps, and include wa.me links instead of raw phone
numbers.

**Why this was needed:** During Phase 4 edge-case testing, send-failure
alerts arrived as `"Salelular: failed to send image message to a customer
after 3 attempts. Last error: media link is not available"`. The
operator receiving this has no idea which product, which customer, or
what to do about it. Similarly, language-escalation alerts exposed raw
phone numbers (`"Customer message in unknown from +256705878284"`),
which is both unhelpful and a privacy concern.

**Rules (documented in SPEC.md S2):**
1. Address operator by name
2. Plain non-technical language
3. Specific actionable instructions (not just "an error occurred")
4. wa.me links for customer context (not raw phone numbers)
5. Product names when product-specific (e.g. broken image URL)
6. No raw error codes, stack traces, or technical identifiers

**Date:** Phase 4 edge-case sweep (April 2026)

---

## 6. Bot-sent echo filtering (sent_tracker)

**Decision:** Track every message ID returned by the Whapi send API in a
TTL-expiring set (`app/utils/sent_tracker.py`). When a `from_me: true`
webhook arrives, check the set: if the ID is present, it's a bot echo
(ignore). If absent, it's genuine operator typing (route to
`owner_action_handler`).

**Why this was needed:** Whapi fires `from_me: true` for ALL messages
sent from the linked WhatsApp number — whether sent by the bot via
the API or by the operator physically typing on their phone. Without
filtering, every bot reply triggered `owner_typed_in_customer_thread`,
which set `session.stage = OWNER_ACTIVE`, permanently silencing the bot
after the first reply.

**Why not a stage-based filter:** The original fix was to only trigger
passive interruption when `session.stage == HANDED_OFF`. This was
rejected because the operator should be able to interrupt at any time
(e.g., typing in a customer thread during EXPLORING to correct the bot),
not just after a handoff alert.

**TECH DEBT — missed ID capture:** If the Whapi API accepts our POST
but we fail to read the response body (network drop after server-side
accept, response parsing error), the message ID won't be captured. The
echo will then be treated as operator typing, causing a false
OWNER_ACTIVE flip. This is:
- Extremely rare (HTTP responses arrive atomically, and we retry 3x)
- Self-healing (operator can type `resume` to un-pause the bot)
- Not worth adding a secondary signal for now (Whapi has no custom
  metadata field that survives the echo; zero-width Unicode in message
  body risks being stripped by WhatsApp)

Accepted as known risk. If it becomes a real problem in production,
revisit with a zero-width marker or a timing heuristic as a fallback.

**Date:** Phase 5 (April 2026)
