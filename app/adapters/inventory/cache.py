from __future__ import annotations

import asyncio
import threading

from rapidfuzz import fuzz

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.models.product import Product
from app.utils.log import log


class InventoryCache(InventoryAdapter):
    def __init__(self, search_threshold: int = 70) -> None:
        self._threshold = search_threshold
        self._index: list[tuple[str, Product]] = []
        self._lock = threading.Lock()

    def build_index(self, products: list[Product]) -> None:
        new_index: list[tuple[str, Product]] = []
        for p in products:
            attrs = p.attributes or ""
            index_str = f"{p.name} {p.keywords} {p.description} {attrs}".lower()
            new_index.append((index_str, p))

        with self._lock:
            self._index = new_index

    def search(self, query: str, shown_ids: list[str]) -> list[Product]:
        if not query:
            return []

        # Build candidate queries: the full query plus progressively shorter
        # sub-queries (bigrams of the words). Long vision descriptions produce
        # verbose queries that score low on partial_ratio; shorter fragments
        # match much better against index strings.
        words = query.lower().split()
        sub_queries = [query.lower()]
        if len(words) >= 3:
            for i in range(len(words) - 1):
                sub_queries.append(f"{words[i]} {words[i + 1]}")

        shown_set = set(shown_ids)
        candidates: dict[str, tuple[float, Product]] = {}

        with self._lock:
            for sq in sub_queries:
                for index_str, product in self._index:
                    if not product.available:
                        continue
                    if product.id in shown_set:
                        continue
                    score = fuzz.partial_ratio(sq, index_str)
                    if score >= self._threshold:
                        existing = candidates.get(product.id)
                        if existing is None or score > existing[0]:
                            candidates[product.id] = (score, product)

        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
        return [p for _, p in ranked[:5]]

    def get_all(self) -> list[Product]:
        with self._lock:
            return [p for _, p in self._index]

    async def start_refresh(
        self, loader: GoogleSheetsLoader, interval_s: int
    ) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                products = await loader.load()
                self.build_index(products)
                log("inventory_refreshed", product_count=len(products))
            except SheetsLoadError as e:
                log("error", component="inventory", error_type="refresh_failed", message=str(e))
