from __future__ import annotations

"""
InventoryCache — hybrid semantic + fuzzy product search.

Search pipeline
───────────────
1. RapidFuzz (always runs, instant, no model needed)
   Word-coverage min-partial-ratio + full-query fallback.
   Threshold from SEARCH_THRESHOLD env var (default 55).

2. Semantic embedding (runs when EmbeddingModels.is_ready())
   - Text queries  → all-MiniLM-L6-v2 (384-dim)
   - Image queries → CLIP clip-ViT-B-32 text encoder (512-dim)
     (detected by VISION_QUERY_PREFIX "[image:" prefix)
   Cosine similarity via vectorised numpy dot product.

3. Score fusion
   combined = (semantic_weight * semantic_score_normalised)
            + ((1 - semantic_weight) * fuzzy_score_normalised)
   SEMANTIC_WEIGHT env var, default 0.6.
   Set to 0.0 for pure RapidFuzz. Set to 1.0 for pure semantic.

4. Filter + rank
   Filter: available=True, id not in shown_ids.
   Sort: combined score descending.
   Return: top MAX_CANDIDATES (default 10).

The LLM then semantically reviews candidates via present_products
and picks truly matching ids — this layer does wide-net retrieval,
not precision filtering.
"""

import asyncio
import threading

import numpy as np
from rapidfuzz import fuzz

from app.adapters.inventory.base import InventoryAdapter
from app.adapters.inventory.embeddings import (
    VISION_QUERY_PREFIX,
    EmbeddingModels,
    cosine_similarity_batch,
)
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.models.product import Product
from app.utils.log import log

MAX_CANDIDATES = 10

# Cosine similarity is in [-1, 1]. Clamp to [0, 1] for scoring.
_COSINE_FLOOR = 0.0


class InventoryCache(InventoryAdapter):
    """
    In-memory product index with hybrid fuzzy + semantic search.

    Thread-safety: a single threading.Lock protects both the RapidFuzz
    string index and the numpy embedding matrices. Index rebuilds are
    atomic — the lock is held only while swapping in the new index,
    not during the (slow) build phase.
    """

    def __init__(
        self,
        search_threshold: int = 55,
        semantic_weight: float = 0.6,
        embedding_models: EmbeddingModels | None = None,
    ) -> None:
        self._threshold = search_threshold
        self._semantic_weight = max(0.0, min(1.0, semantic_weight))
        self._models = embedding_models  # None → RapidFuzz-only mode

        # Protected by _lock:
        self._index: list[tuple[str, Product]] = []          # (index_str, product)
        self._text_embeddings: np.ndarray | None = None      # shape (N, 384)
        self._clip_embeddings: np.ndarray | None = None      # shape (N, 512)
        self._lock = threading.Lock()

    # ── Index management ──────────────────────────────────────────────────────

    def build_index(self, products: list[Product]) -> None:
        """
        Build the RapidFuzz string index and (if models are ready)
        the embedding matrices.

        The heavy embedding work is done OUTSIDE the lock. The lock is
        acquired only for the final swap, keeping contention minimal.
        """
        # Build RapidFuzz index strings (fast, always)
        new_index: list[tuple[str, Product]] = []
        index_strings: list[str] = []
        for p in products:
            attrs = p.attributes or ""
            s = f"{p.name} {p.name} {p.keywords} {p.keywords} {p.description} {attrs}".lower()
            new_index.append((s, p))
            index_strings.append(s)

        # Build embedding matrices (slower, only when models ready)
        new_text_emb: np.ndarray | None = None
        new_clip_emb: np.ndarray | None = None

        if self._models is not None and self._models.is_ready() and index_strings:
            new_text_emb = self._models.embed_products_text(index_strings)
            new_clip_emb = self._models.embed_products_clip(index_strings)
            if new_text_emb is not None:
                log("embedding_index_built", product_count=len(index_strings),
                    text_dims=new_text_emb.shape[1] if new_text_emb.ndim == 2 else 0)

        # Atomic swap
        with self._lock:
            self._index = new_index
            self._text_embeddings = new_text_emb
            self._clip_embeddings = new_clip_emb

    # ── Public interface (InventoryAdapter) ───────────────────────────────────

    def search(self, query: str, shown_ids: list[str]) -> list[Product]:
        """
        Hybrid search. Returns up to MAX_CANDIDATES products.
        The LLM reviews these via present_products and picks matches.
        """
        if not query:
            return []

        shown_set = set(shown_ids)
        is_image_query = query.startswith(VISION_QUERY_PREFIX)

        with self._lock:
            index_snapshot = self._index
            text_emb_matrix = self._text_embeddings
            clip_emb_matrix = self._clip_embeddings

        if not index_snapshot:
            return []

        # ── 1. RapidFuzz scores ───────────────────────────────────────────────
        fuzzy_scores = self._compute_fuzzy_scores(query, index_snapshot)

        # ── 2. Semantic scores ────────────────────────────────────────────────
        semantic_scores = self._compute_semantic_scores(
            query,
            is_image_query,
            text_emb_matrix,
            clip_emb_matrix,
            len(index_snapshot),
        )

        # ── 3. Fuse scores ────────────────────────────────────────────────────
        combined = self._fuse_scores(fuzzy_scores, semantic_scores)

        # ── 4. Filter and rank ────────────────────────────────────────────────
        candidates: list[tuple[float, Product]] = []
        for i, (_, product) in enumerate(index_snapshot):
            if not product.available:
                continue
            if product.id in shown_set:
                continue

            score = combined[i]

            # Qualify: either the fuzzy score on its own would have passed,
            # OR the combined semantic+fuzzy score is meaningful.
            fuzzy_ok = self._fuzzy_qualifies(fuzzy_scores[i], query)
            semantic_ok = (
                semantic_scores is not None
                and semantic_scores[i] >= 0.25  # cosine ≥ 0.25 is a real signal
            )

            if fuzzy_ok or semantic_ok:
                candidates.append((score, product))

        candidates.sort(key=lambda t: t[0], reverse=True)

        result = [p for _, p in candidates[:MAX_CANDIDATES]]

        log(
            "inventory_search",
            query_len=len(query),
            is_image=is_image_query,
            semantic_active=semantic_scores is not None,
            candidates_before_filter=len(index_snapshot),
            candidates_returned=len(result),
        )
        return result

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
                log(
                    "error",
                    component="inventory",
                    error_type="refresh_failed",
                    message=str(e),
                )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _compute_fuzzy_scores(
        self,
        query: str,
        index: list[tuple[str, Product]],
    ) -> list[float]:
        """
        Per-product RapidFuzz scores in [0, 100].
        Uses the same word-coverage + full-score logic as before.
        """
        q_lower = query.lower()
        words = [w for w in q_lower.split() if len(w) >= 3] or q_lower.split()

        scores: list[float] = []
        for index_str, _ in index:
            full_score = fuzz.partial_ratio(q_lower, index_str)
            if words:
                word_scores = [fuzz.partial_ratio(w, index_str) for w in words]
                word_coverage = min(word_scores)
            else:
                word_coverage = full_score
            # Use the better of the two — max rather than picking one
            scores.append(max(word_coverage, full_score))
        return scores

    def _fuzzy_qualifies(self, fuzzy_score: float, query: str) -> bool:
        """
        Replicates the original qualification logic:
        word_coverage >= threshold OR full_score >= threshold+15.
        Since we now store max(word_coverage, full_score), we apply
        a slightly relaxed single threshold check here.
        """
        return fuzzy_score >= self._threshold

    def _compute_semantic_scores(
        self,
        query: str,
        is_image_query: bool,
        text_matrix: np.ndarray | None,
        clip_matrix: np.ndarray | None,
        n: int,
    ) -> np.ndarray | None:
        """
        Compute cosine similarity scores for all products.
        Returns a float32 array of shape (N,) in [−1, 1],
        or None if models are not ready or embedding fails.
        """
        if self._models is None or not self._models.is_ready():
            return None

        if is_image_query:
            # Use CLIP for image-derived queries
            if clip_matrix is None or clip_matrix.shape[0] != n:
                return None
            q_vec = self._models.embed_image_query(query)
            if q_vec is None:
                return None
            return cosine_similarity_batch(q_vec, clip_matrix)
        else:
            # Use MiniLM for plain text queries
            if text_matrix is None or text_matrix.shape[0] != n:
                return None
            q_vec = self._models.embed_text(query)
            if q_vec is None:
                return None
            return cosine_similarity_batch(q_vec, text_matrix)

    def _fuse_scores(
        self,
        fuzzy_scores: list[float],
        semantic_scores: np.ndarray | None,
    ) -> list[float]:
        """
        Weighted combination of normalised fuzzy and semantic scores.

        Fuzzy scores are in [0, 100] → normalise to [0, 1].
        Semantic scores are cosine similarity in [−1, 1] → clamp to [0, 1].

        combined = (w * semantic) + ((1 - w) * fuzzy)

        When semantic_scores is None (models not ready), combined = fuzzy only.
        """
        n = len(fuzzy_scores)
        fuzzy_norm = [s / 100.0 for s in fuzzy_scores]

        if semantic_scores is None or len(semantic_scores) != n:
            return fuzzy_norm

        w = self._semantic_weight
        combined: list[float] = []
        for i in range(n):
            sem = float(max(_COSINE_FLOOR, semantic_scores[i]))
            combined.append(w * sem + (1.0 - w) * fuzzy_norm[i])
        return combined
