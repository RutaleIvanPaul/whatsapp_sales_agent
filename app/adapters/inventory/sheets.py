from __future__ import annotations

import asyncio
import base64
import json

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.models.product import Product

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
EXPECTED_HEADERS = [
    "id", "name", "price", "description", "keywords",
    "image_url", "available", "slug", "attributes",
]
MAX_RETRIES = 3
BACKOFF_SECONDS = [1, 2, 4]


class SheetsLoadError(Exception):
    """Raised when Google Sheets load fails after all retries.

    Expected to be caught by the inventory cache refresh loop,
    which keeps serving the stale index on failure.
    """


class GoogleSheetsLoader:
    def __init__(self, google_credentials_json_b64: str, sheets_id: str, sheet_name: str = "Sheet1") -> None:
        creds_json = json.loads(base64.b64decode(google_credentials_json_b64))
        self._creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
        self._sheets_id = sheets_id
        self._sheet_name = sheet_name

    async def load(self) -> list[Product]:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                return await asyncio.to_thread(self._fetch_and_parse)
            except HttpError as e:
                status = e.resp.status if e.resp else 0
                if status == 429 or status >= 500:
                    last_error = e
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(BACKOFF_SECONDS[attempt])
                    continue
                raise SheetsLoadError(f"Sheets API error: {e}") from e
            except Exception as e:
                raise SheetsLoadError(f"Unexpected error loading sheet: {e}") from e

        raise SheetsLoadError(
            f"Failed after {MAX_RETRIES} retries: {last_error}"
        )

    def _fetch_and_parse(self) -> list[Product]:
        service = build("sheets", "v4", credentials=self._creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._sheets_id, range=f"{self._sheet_name}!A:I")
            .execute()
        )

        rows = result.get("values", [])
        if not rows:
            return []

        headers = [h.strip().lower() for h in rows[0]]
        if headers != EXPECTED_HEADERS:
            raise SheetsLoadError(
                f"Header mismatch: expected {EXPECTED_HEADERS}, got {headers}"
            )

        products: list[Product] = []
        for row in rows[1:]:
            # Pad short rows with empty strings
            padded = row + [""] * (9 - len(row))

            row_id = padded[0].strip()
            name = padded[1].strip()
            if not row_id or not name:
                continue

            available_raw = padded[6].strip().upper()
            slug_raw = padded[7].strip()
            attrs_raw = padded[8].strip()

            products.append(
                Product(
                    id=row_id,
                    name=name,
                    price=padded[2].strip(),
                    description=padded[3].strip(),
                    keywords=padded[4].strip(),
                    image_url=padded[5].strip(),
                    available=available_raw in ("TRUE", "1"),
                    slug=slug_raw or None,
                    attributes=attrs_raw or None,
                )
            )

        return products
