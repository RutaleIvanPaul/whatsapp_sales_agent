# Salelular — Project Context for GitHub Copilot

You are taking over development of Salelular from Claude Code.
Read this entire document before making any suggestions or edits.

---

## What Salelular is

Salelular is a WhatsApp AI sales assistant for SME shop owners in Uganda.
It connects to the operator's existing WhatsApp number via Whapi.cloud
(linked-device, like WhatsApp Web). The operator keeps their number and
their WhatsApp app unchanged.

Customers message the shop number. The bot handles enquiries, finds
matching products from a Google Sheet inventory, conducts a natural
sales conversation, and alerts the operator when a customer is ready
to buy. The operator takes over by typing directly in the customer's
WhatsApp thread. The customer always sees one number, one continuous
conversation — they never know they are talking to a bot.

---

## Where to find everything

All project documentation lives in the repository root:

  CLAUDE.md       — architecture rules, adapter interfaces, non-negotiable
                    constraints. Read this before touching any code.
  SPEC.md         — full technical specification, section by section.
                    Read only the section relevant to what you are working on.
  DECISIONS.md    — every architectural decision and its rationale.
                    Before deviating from a pattern, check if it is documented here.
  CHANGELOG_PHASE7.md — all changes made during Phase 7 human feel evaluation.

Phase build prompts (history of what was built and when):
  .claude/prompts/poc.md through phase7.md

Agent definitions (still valid as review criteria):
  .claude/agents/architect.md
  .claude/agents/reviewer.md
  .claude/agents/security.md
  .claude/agents/tester.md
  .claude/agents/debugger.md

---

## Current system state (as of Phase 7, April 2026)

All build phases 0-7 are complete or in progress:

  Phase 0  POC               Complete. poc_working_proof.py kept as reference.
  Phase 1  Foundation        Complete. Data models, crypto, storage, logging.
  Phase 2  Inventory         Complete. Google Sheets loader, RapidFuzz search.
  Phase 3  Webhook+Messaging Complete. FastAPI server, Whapi integration, queue.
  Phase 4  Conversation      Complete. LLM pipeline, tools, session management.
  Phase 5  Handoff+Control   Complete. Handoff flow, owner actions, exclusion list.
  Phase 6  Resilience        Complete. Rate limits, retries, onboarding script.
  Phase 7  Human Feel        In progress. Real WhatsApp testing driving prompt
                             and pipeline refinements.

Current LLM provider: Groq (temporary — Anthropic Claude is the target).
Current storage: SQLite (Supabase/Postgres is the documented upgrade path).
Current search: RapidFuzz fuzzy matching (semantic search is next work item).

---

## Tech stack

- Python 3.11+ (local env may be 3.9 — all files use `from __future__ import annotations`)
- FastAPI + uvicorn
- Whapi.cloud — linked-device WhatsApp gateway
- Google Sheets API — inventory source, read-only, service account
- OpenAI GPT-4o or Anthropic Claude — conversation engine (configurable)
- RapidFuzz — in-memory fuzzy product search
- SQLite — session and operator storage
- asyncio.Queue — message queue

---

## Non-negotiable architecture rules

These are defined in CLAUDE.md. Violating any of these is a blocker.

1. Never send the full product inventory to the LLM. Max 10 candidates
   from search(), then the LLM filters via present_products tool.

2. Every external dependency is behind a swappable adapter interface.
   Business logic only imports from base classes or factories.
   Never import from implementation files directly
   (openai_adapter.py, whapi.py, sqlite_adapter.py, etc.).

3. Messaging adapter always receives tenant/operator context.
   No global WhatsApp token. Per-operator tokens on every send.

4. Webhook receiver returns 200 OK within 5 seconds.
   All pipeline processing is async after the 200 via queue.

5. Same (operator_id, phone) pair never processed concurrently.
   Per-user asyncio.Lock in the queue worker.

6. Sensitive operator fields (whapi_channel_token, whapi_webhook_secret)
   encrypted at rest using AES-256-GCM via utils/crypto.py.

7. Webhook token comparison always uses hmac.compare_digest(). Never ==.

8. Customer phone numbers never logged plain text.
   Always utils/phone.py hash_for_log() before logging.

9. Customer input always wrapped in delimiters in system prompt:
   === CUSTOMER MESSAGE === / === END CUSTOMER MESSAGE ===

10. All logging via app/utils/log.py structured JSON logger only.

---

## Key adapter interfaces (signatures are fixed — do not change)

```python
class LLMAdapter(ABC):
    async def chat(self, messages, tools, system, max_tokens=1024) -> LLMResponse
    def make_tool_result_messages(self, results) -> list[dict]

class VisionAdapter(ABC):
    async def describe(self, image_url: str) -> str

class MessagingAdapter(ABC):
    async def send_text(self, phone, text, operator) -> None
    async def send_image(self, phone, image_url, caption, operator) -> None
    async def check_health(self, operator) -> bool
    async def get_contacts(self, operator) -> list[str]

class InventoryAdapter(ABC):
    def search(self, query: str, shown_ids: list[str]) -> list[Product]
    def get_all(self) -> list[Product]

class StorageAdapter(ABC):
    def get(self, operator_id, phone) -> Session | None
    def set(self, operator_id, phone, session) -> None
    def delete(self, operator_id, phone) -> None
    def get_by_stage(self, operator_id, stage) -> list[Session]

class TenantAdapter(ABC):  # named OperatorAdapter in code
    def get_by_channel_id(self, channel_id) -> Operator | None
    def get_all_active(self) -> list[Operator]
    def update_status(self, operator_id, status) -> None
    def save(self, operator) -> None
```

---

## LLM tools (names and signatures fixed)

```python
search_products(query: str)              # returns candidates, no side effects
present_products(product_ids: list[str]) # commits to showing specific products
update_session(fields: dict)             # optional fields parameter
trigger_handoff(summary: str)            # alert operator, set HANDED_OFF
request_haggle_approval(...)             # escalate haggling to operator
```

---

## Session stages

```python
EXPLORING    = 'exploring'
CONSIDERING  = 'considering'
HANDED_OFF   = 'handed_off'
OWNER_ACTIVE = 'owner_active'
```

---

## Operator model (key fields)

```python
@dataclass
class Operator:
    operator_id: str
    shop_name: str
    shop_category: str           # e.g. "Clothing & Fashion"
    shop_description: str        # 1-3 sentences about the shop
    owner_name: str
    owner_personal_phone: str    # E.164, control thread
    whapi_channel_id: str
    whapi_channel_token: str     # ENCRYPTED at rest
    whapi_webhook_secret: str    # ENCRYPTED at rest
    whapi_connected_phone: str | None
    google_sheets_id: str
    google_sheet_name: str       # tab name within the sheet
    luganda_canned_response: str
    llm_model: str
    excluded_phones: list[str]   # silent discard list
    included_phones: list[str]   # override contacts cache
    haggling_policy: str         # shop-wide haggling guidance
    haggling_notify_first: bool  # escalate before accepting
    status: OperatorStatus
```

---

## Important Phase 7 changes (not yet in SPEC.md — apply these)

1. Search is now two steps:
   - search_products returns up to 10 loose candidates (no side effects)
   - present_products(ids) commits to showing — only then are
     shown_product_ids updated and images sent

2. Search scoring uses word-coverage minimum:
   word_coverage = min(partial_ratio(word, index_str) for each word)
   Qualifies if word_coverage >= threshold OR full_score >= threshold+15

3. Intent gate has 3-turn grace period before silencing.
   Classifier biased toward SALES for shop-context ambiguity.
   Session field: intent_gate_state ("unclassified"|"passed"|"silenced")

4. Haggling feature added (app/engine/haggling.py):
   Precedence: product.haggling_notes > operator.haggling_policy
   operator.haggling_notify_first → escalates via request_haggle_approval
   Control thread command: reply {phone} {instruction}

5. Bot stays active during HANDED_OFF (no holding message, no 24h revert).
   OWNER_ACTIVE is the only bot-suppression trigger.

6. Lazy operator registration: new operators served without server restart.

7. All Whapi URLs contained in app/adapters/messaging/whapi.py only.

8. Quoted reply handling: reads multiple Whapi schema variants.
   Prefix: [replying to: "..."] prepended to input.

---

## What is being built next — Semantic Search (Phase 8)

The current search uses RapidFuzz fuzzy string matching. We are adding
semantic embedding search as a hybrid alongside it.

Three files have been prepared and are ready to integrate:

  embeddings.py                  → app/adapters/inventory/embeddings.py  (NEW)
  cache.py (updated)             → app/adapters/inventory/cache.py       (REPLACE)
  .env.example (updated)         → .env.example                          (REPLACE)

Integration instructions are in: SEMANTIC_SEARCH_INTEGRATION.md

Read that file and follow it exactly. It specifies:
  - What to add to requirements.txt
  - What to add to app/config.py
  - What to change in app/main.py
  - How to verify the integration works

The phase prompt with full context is in: .claude/prompts/phase8.md

---

## Development workflow

For every task:
1. Read the relevant SPEC.md section before making changes
2. Check DECISIONS.md to understand why things are built the way they are
3. Never change an adapter interface signature
4. All new logging must use app/utils/log.py structured JSON
5. All new env vars must be added to .env.example with comments
6. Update DECISIONS.md if you make a design decision that deviates from spec

---

## Running the project locally

```bash
# Activate virtual environment
source venv/bin/activate

# Start ngrok tunnel (separate terminal)
ngrok http 8000

# Start server
uvicorn app.main:app --reload --port 8000

# Test search (no server needed)
python scripts/test_search.py "blue nike shoes"

# Check a session
python scripts/check_session.py --list {operator_id}

# Onboard a new operator
python scripts/onboard_operator.py
```
