# CLAUDE.md — Salelular

## What this is

Salelular is a WhatsApp AI sales assistant for SME shop owners in Uganda.
It connects to the operator's existing WhatsApp number via Whapi.cloud
(linked-device, like WhatsApp Web). The operator keeps their number, app,
and personal conversations unchanged.

Customers message the operator's number. The bot handles enquiries, finds
matching products from a Google Sheet, and alerts the operator when a customer
is ready to buy. The operator then opens the customer's chat in the shop's
WhatsApp and types directly. The bot detects this and steps aside. The
customer always sees one number, one continuous conversation — whether
the bot or the operator is replying.

Full technical reference: SPEC.md
Read only the section you need. Never load the entire file at once.

---

## Current phase

PHASE 7 — Human Feel Evaluation
Read .claude/prompts/phase6.md for prior context and CHANGELOG_PHASE7.md
for every change made since the Phase 6 hardening merge.
Update this line when advancing phases.

---

## Tech stack

- Python 3.11+
- FastAPI + uvicorn
- Whapi.cloud — linked-device WhatsApp gateway (hosted, per-operator tokens)
- Google Sheets API — inventory source, read-only, single service account
- LLM provider is configurable via LLM_PROVIDER env var (see DECISIONS.md)
  Default: Anthropic Claude Sonnet 4.6 (conversation + vision)
  Alternative: Groq (Llama models) — switching is a config change, no code changes
- Language classifier uses the same provider, cheaper model (Haiku 4.5 / llama-3.1-8b)
- RapidFuzz — in-memory fuzzy product search
- SQLite — session and operator storage for MVP
- asyncio.Queue — message queue for MVP

---

## Target file structure

salelular/
├── CLAUDE.md
├── SPEC.md
├── .claude/
│   ├── agents/
│   │   ├── architect.md
│   │   ├── reviewer.md
│   │   ├── tester.md
│   │   ├── security.md
│   │   └── debugger.md
│   └── prompts/
│       ├── poc.md
│       ├── phase1.md
│       ├── phase2.md
│       ├── phase3.md
│       ├── phase4.md
│       ├── phase6.md
│       └── phase6.md
├── poc.py               (Phase 0 only — deleted after POC passes)
├── app/
│   ├── main.py
│   ├── config.py
│   ├── webhook/
│   │   ├── __init__.py
│   │   ├── receiver.py
│   │   ├── owner_action_handler.py
│   │   └── session_disconnect_handler.py
│   ├── queue/
│   │   ├── __init__.py
│   │   ├── queue.py
│   │   └── worker.py
│   ├── buffer/
│   │   ├── __init__.py
│   │   └── buffer.py
│   ├── input/
│   │   ├── __init__.py
│   │   ├── processor.py
│   │   ├── text.py
│   │   ├── image.py
│   │   ├── voice.py
│   │   ├── link.py
│   │   └── language.py
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── conversation.py
│   │   ├── system_prompt.py
│   │   ├── tools.py
│   │   └── handoff.py
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── runner.py
│   │   └── response_builder.py
│   ├── adapters/
│   │   ├── llm/
│   │   │   ├── base.py
│   │   │   ├── anthropic_adapter.py
│   │   │   └── factory.py
│   │   ├── vision/
│   │   │   ├── base.py
│   │   │   ├── anthropic_adapter.py
│   │   │   └── factory.py
│   │   ├── messaging/
│   │   │   ├── base.py
│   │   │   └── whapi.py
│   │   ├── inventory/
│   │   │   ├── base.py
│   │   │   ├── sheets.py
│   │   │   └── cache.py
│   │   ├── storage/
│   │   │   ├── base.py
│   │   │   ├── sqlite_adapter.py
│   │   │   └── redis_adapter.py
│   │   └── operator/
│   │       ├── base.py
│   │       ├── sqlite_adapter.py
│   │       └── factory.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── operator.py
│   │   ├── session.py
│   │   ├── product.py
│   │   └── message.py
│   └── utils/
│       ├── __init__.py
│       ├── logging.py
│       ├── phone.py
│       └── crypto.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── scripts/
│   ├── onboard_operator.py
│   ├── test_search.py
│   └── check_session.py
├── .env
├── .env.example
├── requirements.txt
├── Dockerfile
├── .gitignore
└── README.md

---

## Non-negotiable rules

Checked by @reviewer every phase. Violation is a blocker.

ARCHITECTURE
1. Never send the full product inventory to the LLM. Filter before any AI
   call. Maximum 5 products ever passed to the LLM at once.
2. Every external dependency behind a swappable adapter. Business logic
   only imports from base classes or factories — never from implementation
   files (anthropic_adapter.py, whapi.py, sqlite_adapter.py) directly.
3. Messaging adapter always receives operator context. No global WhatsApp
   token. Per-operator channel token on every outbound send.
4. Webhook receiver returns 200 OK within 5 seconds. All pipeline
   processing is async after the 200 via the queue.
5. Same (operator_id, phone) pair never processed concurrently.
   Per-user asyncio.Lock in the queue worker.

SECURITY
6. Sensitive operator fields (whapi_channel_token, whapi_webhook_secret)
   encrypted at rest using AES-256-GCM via utils/crypto.py. Never plain text.
7. Webhook token comparison always uses hmac.compare_digest(). Never ==.
8. Customer phone numbers never logged plain text. Always
   utils/phone.py hash_for_log() before logging.
9. Customer message content never logged in full. Log type + char count only.
10. Customer input always wrapped in delimiters in the system prompt:
    === CUSTOMER MESSAGE === and === END CUSTOMER MESSAGE ===

FILTERING (in receiver, before queue)
11. Group messages (chat_id ending @g.us) discarded silently.
12. Operator's saved WhatsApp contacts discarded silently.
13. Non-message events (event.type != 'messages') discarded silently.

CODE QUALITY
14. All input handlers catch all exceptions internally. Never raise.
    Return a placeholder string on any failure.
15. All logging uses app/utils/logging.py structured JSON logger only.
    No direct use of Python's logging module elsewhere.
16. No private function imports across module boundaries.

---

## Adapter interfaces — signatures never change

All I/O adapters are async (FastAPI event loop requirement — see DECISIONS.md).

class LLMAdapter(ABC):
    async def chat(self, messages: list[dict], tools: list[dict], system: str, max_tokens: int = 1024) -> LLMResponse
    def make_tool_result_messages(self, results: list[ToolResult]) -> list[dict]

class VisionAdapter(ABC):
    async def describe(self, image_url: str) -> str

class MessagingAdapter(ABC):
    async def send_text(self, phone: str, text: str, operator: Operator) -> None
    async def send_image(self, phone: str, image_url: str, caption: str, operator: Operator) -> None

class InventoryAdapter(ABC):
    def search(self, query: str, shown_ids: list[str]) -> list[Product]
    def get_all(self) -> list[Product]

class StorageAdapter(ABC):
    def get(self, operator_id: str, phone: str) -> Session | None
    def set(self, operator_id: str, phone: str, session: Session) -> None
    def delete(self, operator_id: str, phone: str) -> None
    def get_by_stage(self, operator_id: str, stage: str) -> list[Session]

class OperatorAdapter(ABC):
    def get_by_channel_id(self, channel_id: str) -> Operator | None
    def get_all_active(self) -> list[Operator]
    def update_status(self, operator_id: str, status: OperatorStatus) -> None
    def save(self, operator: Operator) -> None

---

## LLM tools — names and signatures fixed

search_products(query: str) -> list[Product]
update_session(fields: dict) -> None
trigger_handoff(summary: str) -> None

---

## Session stages

EXPLORING    = 'exploring'     active bot conversation
CONSIDERING  = 'considering'   products shown, customer deciding
HANDED_OFF   = 'handed_off'    operator alerted; bot holding the line
OWNER_ACTIVE = 'owner_active'  operator typing directly in customer thread

---

## Whapi key facts

Webhook auth: custom header X-Salelular-Token compared via hmac.compare_digest()
channel_id in payload: identifies which operator
from_me: true: operator typed from phone → owner_action_handler
from_me: false: customer message → pipeline
chat_id ending @g.us: group message → discard
image.link: stable Whapi-hosted URL (auto_download enabled, no expiry)
typing_time: int seconds — Whapi shows typing indicator before sending

Send text:  POST https://gate.whapi.cloud/messages/text?token={channel_token}
Send image: POST https://gate.whapi.cloud/messages/image?token={channel_token}
Health:     GET  https://gate.whapi.cloud/health?token={channel_token}
Contacts:   GET  https://gate.whapi.cloud/contacts?token={channel_token}

---

## Development loop — every phase without exception

1. Read .claude/prompts/phaseN.md
2. Read the SPEC sections listed in the prompt
3. Invoke @architect — present plan, wait for go/no-go
4. Enter Plan Mode — review full file plan before approving
5. Build
6. Invoke @reviewer — fix all flagged issues before continuing
7. Invoke @tester — generate test suite
8. Invoke @security on any file touching auth, tokens, phones, crypto
9. Phase complete only when all reviews pass
