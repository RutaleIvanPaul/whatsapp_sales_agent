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

Accepted as known risk.

**Production fix options (in order of robustness):**
1. Whapi webhook delivery confirmation — if Whapi offers a way to mark
   outbound-API messages distinctly in the webhook payload (e.g. a
   `source: "api"` field), filter on that instead of tracking IDs. The
   raw payload already shows `"source": "api"` — but this field was
   discovered late and needs validation that it's consistently present.
2. Zero-width Unicode marker — embed a zero-width space (U+200B) at
   the end of every bot-sent message body. If the echo arrives with
   the marker, it's a bot echo. Risk: WhatsApp may strip it.
3. Timing heuristic — if a from_me:true message arrives within 3
   seconds of a message_sent log for the same chat_id, treat as echo.
   Fragile under load.

**Date:** Phase 5 (April 2026)

---

## 7. Quoted message extraction for reply-to context

**Decision:** When a customer replies to a specific message (long-press
+ Reply in WhatsApp), `input/text.py` extracts the quoted content from
`msg.context.quoted_content` and prepends it as
`[replying to: "..."] customer's text`. This gives the LLM context for
what "this", "that one", or "I want it" refers to.

**Whapi payload structure:** `msg.context.quoted_content.body` for text
replies, `msg.context.quoted_content.caption` for image/media replies.
This was discovered by inspecting the raw webhook payload during Phase 5
testing — the Whapi documentation doesn't clearly document this field.

**Date:** Phase 5 (April 2026)

---

## 8. Resume/handled commands find both HANDED_OFF and OWNER_ACTIVE sessions

**Decision:** The `resume` and `handled` owner commands search for
sessions in BOTH `Stage.HANDED_OFF` and `Stage.OWNER_ACTIVE` when
looking for the target session to act on.

**Why:** When the operator manually types in a customer thread (passive
interruption), the session stage becomes `OWNER_ACTIVE`. If the operator
then sends `resume` in the control thread, the command needs to find
that session. Originally it only searched for `HANDED_OFF`, which meant
there was no way to resume after a manual interruption — the operator
got "No active handoff."

**Date:** Phase 5 (April 2026)

---

## 9. Single image URL per product (MVP simplification)

**Decision:** Each Product has a single `image_url` field. Multi-image
support is deferred.

**Why:** Google Sheets as inventory source makes multi-image storage
awkward. MVP catalogue sizes (15-200 products) do not require vector
search to return good results.

**Future path:** `product_images` table with CLIP embeddings when moving
to Supabase. Documented in SPEC.md S25 and S26.

**Date:** Phase 2 (April 2026)

---

## 10. RapidFuzz fuzzy search over vector search (MVP)

**Decision:** Text-based fuzzy search using RapidFuzz with bigram
decomposition, rather than vector embedding similarity search.

**Why:** Adequate for text-based queries on small catalogues. No
infrastructure dependency. Bigram enhancement handles verbose vision
descriptions adequately. Threshold 70 preserves precision.

**Future path:** Replace search() internals in cache.py with pgvector
cosine similarity. The search() interface is unchanged — the swap is
invisible to business logic.

**Date:** Phase 2 (April 2026)

---

## 11. Voice notes return placeholder (MVP)

**Decision:** `voice.py` returns a fixed placeholder rather than
transcribing audio.

**Why:** Whisper integration adds infrastructure complexity and cost that
is not justified before real operator usage data exists. The placeholder
degrades gracefully — the LLM asks the customer to type.

**Future path:** TranscriptionAdapter ABC defined in SPEC.md S7.
Implement whisper.py when operators report voice note usage is
significant.

**Date:** Phase 4 (April 2026)

---

## 12. Social media export over scraping

**Decision:** Build inventory from operators' own Instagram/TikTok data
exports rather than scraping their profiles.

**Why:** Instagram and TikTok scraping violates their ToS and is
technically unreliable (auth walls, rate limits, layout changes).
Platform data exports are legitimate, stable, and give the same result.

**Trade-off:** Semi-automated — operator does one export step — rather
than fully automated. This is acceptable because inventory building is
a one-time setup task, not a daily operation.

**Date:** Post-MVP design (April 2026)

---

## 13. Manual Whapi channel creation for MVP onboarding

**Decision:** Channels are created manually in the Whapi dashboard for
MVP. Programmatic creation via Whapi Partner API is documented but not
implemented.

**Why:** Programmatic channel creation requires Whapi partner plan. MVP
operator count does not justify the cost.

**Future path:** `--auto-channel` flag in `onboard_operator.py` when
Whapi partner plan is activated. Documented in SPEC.md S23.

**Date:** Phase 3 (April 2026)

---

## 14. Phase 7 human feel evaluation as mandatory gate

**Decision:** A dedicated Phase 7 consisting entirely of human evaluation
is required before MVP sign-off. No code deliverables.

**Why:** Technical correctness is necessary but not sufficient. The
product's core promise is that customers cannot tell they are talking to
a bot. This requires deliberate human evaluation, not automated testing.
10 conversation types, 10 evaluation criteria, and an independent
reviewer test (give the log to someone who didn't know — they should not
notice).

**Date:** Post-MVP design (April 2026)

---

## 15. Two-list exclusion/inclusion system

**Decision:** Operator manages two lists on their Operator record:
`excluded_phones` (manually blocked) and `included_phones` (whitelisted
saved contacts). The receiver checks included → contacts cache → excluded
in that order.

**Why not simpler (exclude-only):** The contacts cache already blocks all
saved contacts. But some saved contacts ARE customers (e.g., repeat buyer
the operator added). The `include` list overrides the contacts cache for
specific numbers. Without it, there's no way to let a saved contact
through.

**Why not smarter (auto-detect):** Auto-detecting "who should be excluded"
requires ML intent classification, which is a separate feature (intent
gate). The exclusion list handles the explicit case where the operator
knows a specific person should not receive bot responses.

**Date:** Phase 5 (April 2026)

---

## 16. Two-stage intent gate (keyword first, LLM fallback)

**Decision:** New contacts are classified as SALES or NOT_SALES before
entering the conversation engine. Stage 1 is a fast keyword check (no
API). Stage 2 is an LLM call (cheap model) only for ambiguous messages.

**Why session-state-aware:** The intent gate only fires when there is no
session or the session history is empty. Existing customers (history has
turns) skip it entirely. This means <10% of messages ever reach the
classifier.

**Why keyword first:** Most first messages contain obvious signals. "do
you have Nike shoes?" contains "do you have" and "shoes". "wrong number"
contains "wrong number". These can be caught in microseconds without an
API call. The LLM is only needed for genuinely ambiguous messages like
"hello" (which could be either a customer greeting or a friend saying
hi).

**Why fail open:** On any error or ambiguity, the gate returns SALES.
Silencing a real customer is worse than engaging with a non-customer.

**Date:** Phase 5 (April 2026)

---

## 17. Customer text delimiters in all classifiers

**Decision:** Both the language classifier (`language.py`) and intent
classifier (`intent.py`) wrap customer text in `=== CUSTOMER MESSAGE ===`
delimiters per CLAUDE.md rule 10.

**Why:** Rule 10 is non-negotiable: "Customer input always wrapped in
delimiters." The language classifier was missing them (pre-existing tech
debt from Phase 4). Fixed alongside the intent gate implementation.

**Date:** Phase 5 (April 2026)

---

## 18. Bot stays active during HANDED_OFF (no holding message)

**Decision:** When a handoff is triggered, the bot continues responding
normally. The session stage is HANDED_OFF but the pipeline does NOT skip
or send a holding message. The bot only stops when the operator
physically types in the customer thread (from_me: true → OWNER_ACTIVE).

**Previous design:** HANDED_OFF suppressed the bot and sent a holding
message ("Still here! Just sorting a few things out for you.") once per
hour, with a 24-hour inactivity revert back to CONSIDERING.

**Why it was changed:** During live testing, the holding message felt
impersonal and broke the illusion of talking to a human. Customers may
want to keep browsing even after expressing buying intent — they don't
know a handoff happened, so they expect the conversation to continue.
The operator takes over when ready; the bot fills the gap naturally
until then.

**What was removed:**
- Holding message logic in pipeline/runner.py
- 24-hour inactivity revert logic
- last_holding_sent field removed from Session model and SQLite
  adapter (_deserialise drops it from existing DB rows via d.pop)

**What remains:**
- OWNER_ACTIVE is the sole bot-suppression trigger
- The operator alerts still fire on handoff
- resume/handled commands still work

**Date:** Phase 5 (April 2026)

---

## 19. Whapi source field as secondary echo filter

**Decision:** In addition to sent_tracker ID matching, the receiver
also checks `msg.source == "api"` to identify bot-sent echoes. This
is a secondary signal that does not depend on capturing the message
ID from the Whapi send response.

**Why:** During live testing, some image sends failed to return a
parseable message ID (network timing, response format variations).
The sent_tracker missed these IDs, and the echoes were treated as
operator typing — flipping the session to OWNER_ACTIVE and silencing
the bot. The `source: "api"` field is set by Whapi on all API-sent
messages and is always present in the webhook payload.

**Date:** Phase 5 (April 2026)

---

## 20. Response time optimisation before Phase 6

**Decision:** Optimise response latency before Phase 6 so Phase 7 human
feel evaluation is not blocked by perceptible delays.

**Why:** 10-35 second response times during Phase 5 testing. No human
salesperson takes 35 seconds to reply on WhatsApp. The biggest
contributor was the handoff alert blocking the pipeline (22s).

**Changes made:**
- Handoff alert: fire-and-forget via asyncio.create_task — pipeline no
  longer waits for the alert to send
- Language classifier: two-stage with keyword pre-filter — obvious
  English/Luganda caught in microseconds, LLM only for ambiguous
- Typing time: adaptive — 1s for short messages, 2s for >200 chars
- Pipeline timing: pipeline_timing log event with per-stage breakdown

**Buffer debounce kept at 3s** — user decision.

**Date:** Phase 5 (April 2026)

---

## 21. LLM timeout retry strategy (3 attempts with degradation)

**Decision:** Never send a fallback message on first timeout. Retry up
to 3 times with a simplified prompt on the third attempt. Only after all
3 fail: silence to customer, operator alert, session → HANDED_OFF.

**Why:** A retry costs 30 seconds maximum. A confused fallback message
("Sorry, I'm having trouble") costs customer trust and breaks the human
illusion. Retrying with a simplified prompt (no tools, brief reply) on
the third attempt gives the model a better chance of responding.

**Date:** Phase 6 (April 2026)

---

## 22. Daily cap as silence plus single operator alert

**Decision:** When a customer exceeds MAX_MESSAGES_PER_USER_DAY, stop
responding silently. Alert the operator once only — not on every
subsequent message.

**Why:** Silence is less disruptive than a cutoff notice ("you've sent
too many messages today"). The operator is informed once so they are
aware of unusual activity. Not alerted on every subsequent message to
avoid alert fatigue. Resets automatically at midnight UTC.

**Date:** Phase 6 (April 2026)

---

## 23. Onboarding script handles registration only

**Decision:** scripts/onboard_operator.py handles system registration.
QR scanning happens in the Whapi dashboard — the tool designed for it.
The script receives already-connected channel credentials and handles
everything else (webhook config, DB record, inventory load).

**Why:** Separation of concerns. QR delivery adds complexity (HTML
generation, email, image sending) with no reliability gain. The Whapi
dashboard is purpose-built for this. The script focuses on what only
the system can do: webhook configuration, encryption, DB writes.

**Date:** Phase 6 (April 2026)

---

## 24. Phase 6 hardening — crash protection + memory cleanup

**Decision:** Added try/except around all JSON deserialization in storage
and operator adapters. Corrupt DB rows return None (sessions) or are
skipped (get_by_stage, get_all_active) instead of crashing the server.

**Why:** A single corrupted JSON row in SQLite would crash the entire
server process — affecting ALL operators, not just the one with bad data.
Returning None for a corrupt session means the pipeline creates a fresh
session (losing history, but the customer can still chat). Skipping
corrupt operators in get_all_active means the server starts with N-1
operators instead of crashing.

**Additional fixes:**
- Message deletion events (Whapi event=delete) now routed to
  buffer.handle_deletion() instead of being processed as regular messages
- Daily counts dict purges stale date keys on each access (prevents
  unbounded growth)
- Contacts cache preserves stale data on partial load failure (HTTP error
  mid-pagination) instead of replacing with incomplete set
- Config validation output uses structured logger instead of print()

**Known accepted risks (not fixed, documented):**
- _session_locks, _operator_refs, _last_flush dicts grow unbounded with
  unique customer count. At MVP scale (<10K customers) this is ~100KB.
  Monitor in production; add periodic cleanup if needed.
- Operator deleted from DB while server running: in-memory cache stale
  until restart. Acceptable for MVP (operators rarely deleted).
- Queue lost on process restart. Documented in S24. Redis swap planned.

**Date:** Phase 6 hardening (April 2026)

---

## 9. Hybrid semantic + fuzzy search (Phase 8)

**Decision:** InventoryCache.search() uses a two-model hybrid:
- RapidFuzz partial ratio matching (instant, zero RAM cost)
- Sentence-transformers embeddings (all-MiniLM-L6-v2 for text,
  CLIP clip-ViT-B-32 for image-derived queries)
- Score fusion: combined = (semantic_weight × semantic) + ((1-semantic_weight) × fuzzy)
- SEMANTIC_WEIGHT env var (default 0.6) controls the blend
- Models load async on startup; system falls back to fuzzy-only until ready

**Why hybrid, not pure semantic:**
RapidFuzz is instant (no model inference), has zero RAM cost, and handles
exact product name / model number matches better than semantic models
(which look for conceptual similarity). Pure semantic search would regress
on specific keyword queries like "iPhone 15 case" or "Adidas Air Force 1".
The hybrid preserves RapidFuzz strengths while adding semantic recall for
vague queries ("something casual for the office") and image descriptions.

**Why local models, not an API:**
- No API cost (free models, CPU inference)
- No rate limits
- No latency from external service calls
- No dependency on third-party uptime
- Models fit on any VPS with 1GB RAM (total: ~440MB for both models)
- First download ~440MB; subsequent starts use local cache (~2-5 seconds)

**Model choices:**

all-MiniLM-L6-v2 (90MB, 384-dim):
- Best-in-class performance/size ratio for short text similarity
- Outperforms larger models on product name and description matching
- Fast inference (<1ms per query on CPU)

clip-ViT-B-32 (350MB, 512-dim):
- CLIP shares a vector space between text and image encodings
- A text description of an image and an actual product photo embedding
  are directly comparable
- Foundation for Phase 9 (real image-to-image search without architecture change)

**Fallback:** SEMANTIC_SEARCH_ENABLED=false disables entirely. Models load
async so server startup is never blocked. Until models ready, system runs
pure RapidFuzz (identical to Phase 2-7).

**Memory:** Minimum 1GB RAM with semantic search. Minimum 256MB without.
Both fit on free-tier VPS instances (Railway, Render, etc.).

**Date:** Phase 8 semantic search (April 2026)

