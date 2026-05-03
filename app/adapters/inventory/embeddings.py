from __future__ import annotations

"""
Embedding model wrapper for Salelular semantic search.

Models used (both free, local, no API cost):
  - all-MiniLM-L6-v2   : 384-dim text embeddings (~90MB)
  - clip-ViT-B-32       : 512-dim image+text embeddings (~350MB)

Models download once on first use and are cached in
~/.cache/torch/sentence_transformers/. Subsequent starts
are instant.

Both models run in-process on CPU. No external service,
no API keys, no rate limits.
"""

import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from app.utils.log import log

_TEXT_MODEL_NAME = "all-MiniLM-L6-v2"
_CLIP_MODEL_NAME = "clip-ViT-B-32"

# Prefix that vision handlers prepend so we can detect image queries
VISION_QUERY_PREFIX = "[image:"


class EmbeddingModels:
    """
    Lazy-loading wrapper for text and CLIP embedding models.

    Models are loaded in a background thread so server startup
    is not blocked. While loading, is_ready() returns False and
    callers fall back to RapidFuzz-only search.
    """

    def __init__(self) -> None:
        self._text_model: SentenceTransformer | None = None
        self._clip_model: SentenceTransformer | None = None
        self._ready = False
        self._lock = threading.Lock()

    def load_async(self) -> None:
        """Start loading models in a background daemon thread."""
        t = threading.Thread(target=self._load, daemon=True, name="embedding-loader")
        t.start()

    def _load(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer

            log("embedding_models_loading", text_model=_TEXT_MODEL_NAME, clip_model=_CLIP_MODEL_NAME)

            text_model = SentenceTransformer(_TEXT_MODEL_NAME)
            clip_model = SentenceTransformer(_CLIP_MODEL_NAME)

            with self._lock:
                self._text_model = text_model
                self._clip_model = clip_model
                self._ready = True

            log("embedding_models_ready", text_model=_TEXT_MODEL_NAME, clip_model=_CLIP_MODEL_NAME)
        except Exception as e:
            # Non-fatal: system falls back to RapidFuzz-only search
            log(
                "error",
                component="embeddings",
                error_type="model_load_failed",
                message=str(e),
            )

    def is_ready(self) -> bool:
        return self._ready

    def embed_text(self, text: str) -> np.ndarray | None:
        """
        Embed a text string using all-MiniLM-L6-v2.
        Returns a normalised 384-dim float32 array, or None if not ready.
        """
        with self._lock:
            model = self._text_model
        if model is None:
            return None
        try:
            vec = model.encode(text, normalize_embeddings=True, show_progress_bar=False)
            return np.array(vec, dtype=np.float32)
        except Exception as e:
            log("error", component="embeddings", error_type="embed_text_failed", message=str(e))
            return None

    def embed_image_query(self, query: str) -> np.ndarray | None:
        """
        Embed a vision-derived query string using CLIP's text encoder.
        CLIP text and image embeddings share the same vector space, so
        a text description of an image is comparable to image embeddings
        built from actual product photos (when we add those later).

        Returns a normalised 512-dim float32 array, or None if not ready.
        """
        with self._lock:
            model = self._clip_model
        if model is None:
            return None
        try:
            # Strip the "[image: ...]" prefix if present so only the
            # descriptive content is embedded
            clean = query
            if clean.startswith(VISION_QUERY_PREFIX):
                clean = clean[len(VISION_QUERY_PREFIX):].rstrip("]").strip()

            vec = model.encode(clean, normalize_embeddings=True, show_progress_bar=False)
            return np.array(vec, dtype=np.float32)
        except Exception as e:
            log("error", component="embeddings", error_type="embed_image_failed", message=str(e))
            return None

    def embed_products_text(self, texts: list[str]) -> np.ndarray | None:
        """
        Batch-embed a list of product index strings using all-MiniLM-L6-v2.
        Returns a (N, 384) float32 array, or None if not ready.
        Batch encoding is significantly faster than one-by-one calls.
        """
        with self._lock:
            model = self._text_model
        if model is None:
            return None
        if not texts:
            return np.empty((0, 384), dtype=np.float32)
        try:
            vecs = model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=64,
            )
            return np.array(vecs, dtype=np.float32)
        except Exception as e:
            log("error", component="embeddings", error_type="batch_embed_failed", message=str(e))
            return None

    def embed_products_clip(self, texts: list[str]) -> np.ndarray | None:
        """
        Batch-embed product index strings using CLIP's text encoder.
        Returns a (N, 512) float32 array, or None if not ready.
        Used to build product embeddings that are comparable to
        future image embeddings from product photos.
        """
        with self._lock:
            model = self._clip_model
        if model is None:
            return None
        if not texts:
            return np.empty((0, 512), dtype=np.float32)
        try:
            vecs = model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=32,
            )
            return np.array(vecs, dtype=np.float32)
        except Exception as e:
            log("error", component="embeddings", error_type="batch_clip_failed", message=str(e))
            return None


def cosine_similarity_batch(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """
    Compute cosine similarity between one query vector and a matrix of
    product vectors.

    Both inputs must be L2-normalised (which encode(..., normalize_embeddings=True)
    guarantees). Under that condition cosine similarity reduces to a dot product,
    which numpy computes in a single vectorised operation — no loops, no overhead.

    Args:
        query_vec : shape (D,)         — single query embedding
        matrix    : shape (N, D)       — N product embeddings

    Returns:
        scores    : shape (N,) float32 — similarity in [-1, 1], higher = more similar
    """
    if matrix.shape[0] == 0:
        return np.empty(0, dtype=np.float32)
    return (matrix @ query_vec).astype(np.float32)
