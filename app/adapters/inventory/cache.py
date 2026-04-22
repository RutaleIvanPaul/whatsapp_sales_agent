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

        # Scoring strategy:
        #   1. word_coverage = min(partial_ratio(w, index) for each query word)
        #      A product qualifies only if EVERY significant query word matches
        #      something. "yellow dress" on "Men's Dress Shirt" fails because
        #      "yellow" doesn't match anything in that product.
        #   2. full_score = partial_ratio(full_query, index) — fallback for
        #      verbose queries (image descriptions) where word_coverage is
        #      too strict.
        #   A product passes if word_coverage >= threshold OR
        #   full_score >= strong_threshold.
        q_lower = query.lower()
        words = [w for w in q_lower.split() if len(w) >= 3]
        if not words:
            words = q_lower.split()

        strong_full_threshold = max(self._threshold + 10, 80)
        shown_set = set(shown_ids)
        candidates: dict[str, tuple[float, Product]] = {}

        with self._lock:
            for index_str, product in self._index:
                if not product.available:
                    continue
                if product.id in shown_set:
                    continue

                full_score = fuzz.partial_ratio(q_lower, index_str)
                if words:
                    word_scores = [fuzz.partial_ratio(w, index_str) for w in words]
                    word_coverage = min(word_scores)
                else:
                    word_coverage = full_score

                passes_coverage = word_coverage >= self._threshold
                passes_full = full_score >= strong_full_threshold
                if passes_coverage or passes_full:
                    # Rank by word_coverage primarily (more precise), full as fallback
                    ranking_score = max(word_coverage, full_score if passes_full else 0)
                    candidates[product.id] = (ranking_score, product)

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
