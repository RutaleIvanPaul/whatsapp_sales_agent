from __future__ import annotations

import asyncio
import threading

from rapidfuzz import fuzz

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.models.product import Product
from app.utils.log import log

# Wide-net retrieval — the LLM filters semantically via present_products.
MAX_CANDIDATES = 10


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
        """Wide-net fuzzy retrieval. The LLM is expected to semantically
        review results and pick matches via present_products. So we return
        more candidates at a lower threshold than a strict retrieval.

        Ranking: prefer products where all query words match individually
        (word_coverage), then fall back to full-query partial_ratio.
        """
        if not query:
            return []

        q_lower = query.lower()
        words = [w for w in q_lower.split() if len(w) >= 3]
        if not words:
            words = q_lower.split()

        shown_set = set(shown_ids)
        candidates: list[tuple[float, float, Product]] = []

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

                # Widen the net: keep anything loosely relevant. The LLM
                # will filter semantically via present_products.
                if word_coverage >= self._threshold or full_score >= (self._threshold + 15):
                    candidates.append((word_coverage, full_score, product))

        # Rank by (word_coverage desc, full_score desc)
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [p for _, _, p in candidates[:MAX_CANDIDATES]]

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
