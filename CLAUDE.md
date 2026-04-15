# CLAUDE.md — Salelular

## What this is

Salelular is a WhatsApp AI sales assistant for SME shop owners in Uganda.
It connects to the operator's existing WhatsApp number via Whapi.cloud
(linked-device, like WhatsApp Web). The operator keeps their number, app,
and personal conversations unchanged.

Customers message the operator's number. The bot handles enquiries, finds
matching products from a Google Sheet, and alerts the operator when a customer
is ready to buy. When the operator takes over, they type in a control thread
and Salelular forwards their reply to the customer from the shop number.
The customer always sees one number, one continuous conversation — whether
the bot or the operator is replying.

Full technical reference: SPEC.md
Read only the section you need. Never load the entire file at once.

---

## Current phase

PHASE 1 — Foundation
Read .claude/prompts/phase1.md before doing anything else.
Update this line when advancing phases.

---

## Tech stack

- Python 3.11+
- FastAPI + uvicorn
- Whapi.cloud — linked-device WhatsApp gateway (hosted, per-tenant tokens)
- Google Sheets API — inventory source, read-only, single service account
- OpenAI GPT-4o — conversation engine and vision (same API key)
- OpenAI GPT-4o-mini — language classifier (same API key, cheaper model)
- RapidFuzz — in-memory fuzzy product search
- SQLite — session and tenant storage for MVP
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
│       ├── phase5.md
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
│   │   │   ├── openai_adapter.py
│   │   │   └── factory.py
│   │   ├── vision/
│   │   │   ├── base.py
│   │   │   ├── openai_adapter.py
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
│   │   └── tenant/
│   │       ├── base.py
│   │       ├── sqlite_adapter.py
│   │       └── factory.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── tenant.py
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
│   ├── onboard_tenant.py
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
   files (openai_adapter.py, whapi.py, sqlite_adapter.py) directly.
3. Messaging adapter always receives tenant context. No global WhatsApp
   token. Per-tenant channel token on every outbound send.
4. Webhook receiver returns 200 OK within 5 seconds. All pipeline
   processing is async after the 200 via the queue.
5. Same (tenant_id, phone) pair never processed concurrently.
   Per-user asyncio.Lock in the queue worker.

SECURITY
6. Sensitive tenant fields (whapi_channel_token, whapi_webhook_secret)
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

class LLMAdapter(ABC):
    def chat(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse

class VisionAdapter(ABC):
    def describe(self, image_url: str) -> str

class MessagingAdapter(ABC):
    def send_text(self, phone: str, text: str, tenant: Tenant) -> None
    def send_image(self, phone: str, image_url: str, caption: str, tenant: Tenant) -> None

class InventoryAdapter(ABC):
    def search(self, query: str, shown_ids: list[str]) -> list[Product]
    def get_all(self) -> list[Product]

class StorageAdapter(ABC):
    def get(self, tenant_id: str, phone: str) -> Session | None
    def set(self, tenant_id: str, phone: str, session: Session) -> None
    def delete(self, tenant_id: str, phone: str) -> None

class TenantAdapter(ABC):
    def get_by_channel_id(self, channel_id: str) -> Tenant | None
    def get_all_active(self) -> list[Tenant]
    def update_status(self, tenant_id: str, status: TenantStatus) -> None

---

## LLM tools — names and signatures fixed

search_products(query: str) -> list[Product]
update_session(fields: dict) -> None
trigger_handoff(summary: str) -> None

---

## Session stages

EXPLORING    = 'exploring'     active bot conversation
CONSIDERING  = 'considering'   products shown, customer deciding
HANDED_OFF   = 'handed_off'    operator alerted, relay mode active
OWNER_ACTIVE = 'owner_active'  operator typing directly in customer thread

---

## Whapi key facts

Webhook auth: custom header X-Salelular-Token compared via hmac.compare_digest()
channel_id in payload: identifies which tenant
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
