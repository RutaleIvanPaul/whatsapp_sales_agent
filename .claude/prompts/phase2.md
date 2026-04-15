# Phase 2 — Inventory

## Goal

Google Sheets loads. In-memory index builds. Search returns correct results.
Tested against your real product sheet before any web server work.

## Read these SPEC sections first

  S11 — Inventory adapter (sheets.py, cache.py, search logic)
  S3  — Product data model

## Prerequisites

  Google Cloud project created.
  Google Sheets API enabled.
  Service account created, credentials JSON downloaded.
  Base64-encode the credentials JSON:
    python -c "import base64,json; print(base64.b64encode(open('creds.json','rb').read()).decode())"
  Add to .env: GOOGLE_CREDENTIALS_JSON=<base64 string>
  Create a test Google Sheet with the 9 columns from S3.
  Share it with your service account email (client_email in creds JSON).
  Add GOOGLE_SHEETS_ID=<sheet id> to .env.

## What to build (in this order)

### 1. app/adapters/inventory/base.py
  Abstract base class InventoryAdapter:
    search(query: str, shown_ids: list[str]) -> list[Product]
    get_all() -> list[Product]
    async start_refresh() -> None   (starts background refresh task)

### 2. app/adapters/inventory/sheets.py
  GoogleSheetsLoader class (not the adapter — just the loader).
  __init__(google_credentials_json_b64: str, sheets_id: str)
  async load() -> list[Product]
    Decode and parse GOOGLE_CREDENTIALS_JSON env var.
    Authenticate with google.oauth2.service_account.Credentials.
    Fetch range Sheet1!A:I via Google Sheets API v4.
    Parse header row to confirm column positions.
    Parse each data row into a Product dataclass.
    Parse available: "TRUE"/"true"/"1"/"yes" → True, else False.
    Skip rows with empty id or name.
    Return list of Product.
  Retry on 429 and 5xx: asyncio.sleep(1), sleep(2), sleep(4). Max 3 retries.
  On persistent failure: raise SheetsLoadError.

### 3. app/adapters/inventory/cache.py
  InventoryCache class that implements InventoryAdapter.
  Holds: list of (index_str, Product) tuples + asyncio.Lock.

  async build_index(products: list[Product]) -> None:
    Acquire lock.
    For each product:
      index_str = f"{name} {keywords} {description} {attributes or ''}".lower()
      Append (index_str, product) to internal list.
    Release lock.

  search(query: str, shown_ids: list[str]) -> list[Product]:
    Acquire lock.
    For each (index_str, product):
      score = rapidfuzz.fuzz.partial_ratio(query.lower(), index_str)
      if score >= SEARCH_THRESHOLD and product.available
         and product.id not in shown_ids:
        add to candidates
    Sort candidates by score descending.
    Release lock.
    Return top 5.

  async start_refresh(loader: GoogleSheetsLoader, interval_s: int) -> None:
    asyncio background task.
    Every interval_s seconds: reload from Sheets, rebuild index.
    On SheetsLoadError: log warning, keep serving stale index.

### 4. scripts/test_search.py
  Usage: python scripts/test_search.py "search query here"
  Loads Google Sheet using credentials from .env.
  Builds search index.
  Runs search with provided query.
  Prints top 5 results with match scores.

## Success criteria

Phase 2 passes when:
  python scripts/test_search.py "black nike running shoes"
  Returns relevant products from your actual sheet.
  Unavailable products do not appear.
  Already-shown products (pass test IDs as a second arg) do not appear.
  No crashes on empty query or query with no matches.

## How to use this prompt

Paste into Claude Code:

  Read CLAUDE.md.
  Read .claude/prompts/phase2.md.
  Read SPEC.md sections S11 and S3.
  Invoke @architect with your plan before writing code.
  Use Plan Mode.
