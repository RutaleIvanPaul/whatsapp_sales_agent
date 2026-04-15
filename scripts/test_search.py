"""Phase 2 verification script. Run with: python scripts/test_search.py "query" [shown_id1,shown_id2]"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import validate
from app.adapters.inventory.sheets import GoogleSheetsLoader
from app.adapters.inventory.cache import InventoryCache


async def load_products(cfg):
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
    loader = GoogleSheetsLoader(cfg.google_credentials_json_b64, cfg.google_sheets_id, sheet_name)
    return await loader.load()


def main():
    print("=" * 60)
    print("Phase 2 — Inventory Search Test")
    print("=" * 60)

    cfg = validate()

    if not cfg.google_credentials_json_b64 or not cfg.google_sheets_id:
        print("FATAL: GOOGLE_CREDENTIALS_JSON and GOOGLE_SHEETS_ID must be set in .env")
        raise SystemExit(1)

    # Load products from Google Sheet
    print("\n[1] Loading products from Google Sheets...")
    products = asyncio.run(load_products(cfg))
    print(f"    Loaded {len(products)} products")

    if not products:
        print("    WARNING: No products found. Check your sheet.")
        return

    # Build search index
    print("\n[2] Building search index...")
    cache = InventoryCache(search_threshold=cfg.search_threshold)
    cache.build_index(products)
    print(f"    Index built with {len(products)} entries (threshold={cfg.search_threshold})")

    # Parse CLI args
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    shown_ids = sys.argv[2].split(",") if len(sys.argv) > 2 else []

    # Run search
    print(f"\n[3] Searching: {query!r}  (excluding: {shown_ids})")
    results = cache.search(query, shown_ids)

    if results:
        for i, p in enumerate(results, 1):
            print(f"\n    #{i}: {p.name}")
            print(f"        ID:          {p.id}")
            print(f"        Price:       {p.price}")
            print(f"        Available:   {p.available}")
            print(f"        Description: {p.description[:80]}...")
    else:
        print("    No matches found.")

    # Test empty query
    print(f"\n[4] Testing empty query...")
    empty_results = cache.search("", [])
    assert empty_results == [], f"Expected empty list, got {len(empty_results)} results"
    print("    Empty query returns [] — PASS")

    # Test get_all
    print(f"\n[5] Testing get_all()...")
    all_products = cache.get_all()
    print(f"    get_all() returned {len(all_products)} products")
    assert len(all_products) == len(products)
    print("    PASS")

    print("\n" + "=" * 60)
    print("Phase 2 — inventory search verified.")
    print("=" * 60)


if __name__ == "__main__":
    main()
