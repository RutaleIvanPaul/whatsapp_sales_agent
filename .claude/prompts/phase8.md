# Phase 8 — Semantic Search

## Goal

Add semantic embedding search to the inventory layer as a hybrid
alongside the existing RapidFuzz fuzzy search. The result is better
recall for natural language queries ("something casual in blue"),
image-derived queries from vision descriptions, and queries where
the customer's words do not exactly match the product's keywords.

The search() interface is unchanged. Nothing above the inventory
adapter layer is affected. This is an internal improvement only.

---

## Background — why this is needed

RapidFuzz works well when the customer's words closely match the
product's name, keywords, or description. It breaks down in two cases:

1. Image queries: vision descriptions are verbose
   ("A blue running shoe with white sole and orange laces")
   and often score below threshold against product index strings.
   The bigram fix partially addresses this but misses cases where
   no individual word pair matches well.

2. Semantic gap: a customer asking for "something for the office"
   or "a gift for my mum" has intent but no keyword match.
   RapidFuzz returns nothing. A semantic model understands these.

The hybrid approach keeps RapidFuzz for its strengths (speed,
exact keyword matching, zero RAM cost) and adds semantic search
for the cases where fuzzy matching falls short.

---

## What is being added

### New file: app/adapters/inventory/embeddings.py

Wrapper for two local embedding models (no API cost, no rate limits):

  all-MiniLM-L6-v2   Text queries → 384-dimensional embeddings (~90MB)
  clip-ViT-B-32       Image queries → 512-dimensional embeddings (~350MB)

Both models run in-process via sentence-transformers library.
Models download once (~440MB total) and cache locally.
Loading is non-blocking — a background thread handles it.
Until models are ready, system falls back to RapidFuzz-only (zero disruption).

### Updated file: app/adapters/inventory/cache.py

Hybrid search pipeline inside the existing InventoryCache class:

  Step 1: RapidFuzz scores (always runs, instant)
  Step 2: Semantic scores (runs when models ready)
           Text queries  → MiniLM cosine similarity
           Image queries → CLIP cosine similarity
           (Image queries detected by "[image:" prefix)
  Step 3: Score fusion
           combined = (SEMANTIC_WEIGHT × semantic) + ((1-SEMANTIC_WEIGHT) × fuzzy)
  Step 4: Filter (available=True, not in shown_ids) and rank

The InventoryAdapter interface is unchanged. search() and get_all()
signatures are identical. No caller changes needed anywhere.

### Updated file: .env.example

Three new variables:
  SEMANTIC_SEARCH_ENABLED=true   (false → pure RapidFuzz, no models loaded)
  SEMANTIC_WEIGHT=0.6            (0.0=pure fuzzy, 1.0=pure semantic)
  SENTENCE_TRANSFORMERS_HOME=    (optional: path for model cache)

---

## Prerequisites

All of these must be done before running:
  - embeddings.py placed at app/adapters/inventory/embeddings.py
  - cache.py replaced at app/adapters/inventory/cache.py
  - .env.example updated
  - requirements.txt updated (sentence-transformers, numpy)
  - app/config.py updated with new fields
  - app/main.py updated to initialise EmbeddingModels and pass to cache

Full integration steps are in SEMANTIC_SEARCH_INTEGRATION.md.

---

## Integration steps (summary — full detail in SEMANTIC_SEARCH_INTEGRATION.md)

### requirements.txt — add:
  sentence-transformers>=2.7.0
  numpy>=1.24.0

### app/config.py — add to Config dataclass:
  semantic_search_enabled: bool = True
  semantic_weight: float = 0.6

  Load in validation:
  semantic_search_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "true").lower() != "false"
  semantic_weight = float(os.getenv("SEMANTIC_WEIGHT", "0.6"))
  Validate: must be float in [0.0, 1.0]

### app/main.py — add after config, before inventory:
  from app.adapters.inventory.embeddings import EmbeddingModels

  embedding_models = None
  if config.semantic_search_enabled:
      embedding_models = EmbeddingModels()
      embedding_models.load_async()

  Then pass to InventoryCache:
  cache = InventoryCache(
      search_threshold=config.search_threshold,
      semantic_weight=config.semantic_weight,
      embedding_models=embedding_models,
  )

---

## Verification steps

### Step 1 — Smoke test (run after server starts)

  python -c "
  from app.adapters.inventory.embeddings import EmbeddingModels
  import time
  m = EmbeddingModels()
  m.load_async()
  print('Waiting for models...')
  for i in range(120):
      if m.is_ready(): break
      time.sleep(1)
  assert m.is_ready(), 'Models did not load'
  v = m.embed_text('blue nike shoes')
  assert v.shape == (384,), f'Wrong shape: {v.shape}'
  v2 = m.embed_image_query('[image: blue running shoe white sole]')
  assert v2.shape == (512,), f'Wrong shape: {v2.shape}'
  print('PASS — semantic search ready')
  "

### Step 2 — Search quality test

Run scripts/test_search.py with queries that previously returned
poor or no results:

  python scripts/test_search.py "something casual and comfortable for everyday"
  python scripts/test_search.py "gift for a woman who likes cooking"
  python scripts/test_search.py "[image: blue running shoe with white sole and orange laces]"
  python scripts/test_search.py "office wear"
  python scripts/test_search.py "hot weather clothes"

Compare results against RapidFuzz-only (set SEMANTIC_WEIGHT=0.0 temporarily).
Semantic results should show more relevant products for vague queries.

### Step 3 — Log verification

After a search, logs should contain:
  embedding_models_ready        (once, at startup after models load)
  inventory_search              (every search, with semantic_active=true)
  embedding_index_built         (after each inventory refresh)

### Step 4 — Existing search regression check

Verify that specific keyword searches still work well:
  python scripts/test_search.py "nike air force"
  python scripts/test_search.py "samsung s24 case"
  python scripts/test_search.py "non-stick frying pan"

These should still return the correct products (RapidFuzz handles these
well and the hybrid should not degrade them).

### Step 5 — Fallback verification

Set SEMANTIC_SEARCH_ENABLED=false and restart server.
Verify search still works (pure RapidFuzz mode).
No embedding_models_ready log should appear.
inventory_search log should show semantic_active=false.

---

## Tuning SEMANTIC_WEIGHT

Start with SEMANTIC_WEIGHT=0.6 (default).

If semantic search is pulling in irrelevant results:
  Lower to 0.4 or 0.3

If exact keyword searches are degrading:
  Lower to 0.5 or raise SEARCH_THRESHOLD slightly

If image queries are still returning poor results:
  Raise to 0.7 or 0.8

Log each test query and compare results at different weight values
before settling on a production default.

---

## Memory and performance

  Model loading time: 30-90 seconds (first run, downloading ~440MB)
                      2-5 seconds (subsequent runs from cache)
  RAM overhead:       ~440MB (both models in memory)
  Index build time:   ~0.5s for 100 products, ~5s for 1000 products
  Search latency:     <5ms for 100 products (vectorised numpy dot product)

  Minimum server RAM with semantic search:    1GB
  Minimum without (SEMANTIC_SEARCH_ENABLED=false): 256MB (unchanged)

For Railway or Render deployment: add SENTENCE_TRANSFORMERS_HOME
pointing to a persistent volume so models are not re-downloaded
on every deploy.

---

## Future extension: real image embeddings

The current implementation embeds product text descriptions using CLIP's
text encoder. This means image queries (from vision descriptions) and
product embeddings are in the same CLIP vector space — they are comparable.

When the product_images table is added (SPEC.md S25/S26), the upgrade path is:

  1. At inventory import time: embed each product image using CLIP's image
     encoder (model.encode() with an image URL or PIL Image object)
  2. Store the 512-dim embedding alongside the image record
  3. At search time when customer sends a photo: embed the photo directly
     using CLIP image encoder (not the text encoder)
  4. Compare photo embedding against stored image embeddings

The embeddings.py file already uses clip-ViT-B-32 and the vector space
is shared between text and image encodings. Adding real image embeddings
requires only:
  - Loading image bytes and passing to CLIP image encoder
  - Storing per-image embeddings (in Supabase with pgvector when that
    migration happens, or in memory for SQLite MVP)

No changes to cache.py's search() interface or any caller.

---

## Phase 8 complete when

  [ ] embeddings.py in place and importing correctly
  [ ] cache.py replaced, InventoryCache accepting EmbeddingModels
  [ ] config.py updated with semantic_search_enabled and semantic_weight
  [ ] main.py initialises EmbeddingModels and passes to InventoryCache
  [ ] requirements.txt updated
  [ ] .env.example updated
  [ ] Smoke test passes (Step 1 above)
  [ ] Search quality test shows improvement for vague queries (Step 2)
  [ ] No regression for keyword searches (Step 4)
  [ ] Fallback verified (Step 5)
  [ ] Log events confirmed (Step 3)
  [ ] DECISIONS.md updated with semantic search decision entry
  [ ] SEMANTIC_WEIGHT default confirmed or tuned from testing

---

## DECISIONS.md entry to add on completion

  DECISION: Hybrid semantic + fuzzy search (Phase 8)

  Why hybrid not pure semantic:
    RapidFuzz is instant, has zero RAM cost, and handles exact keyword
    matches (model names, product codes, specific terms) better than
    semantic models. Pure semantic search would regress on these cases.
    The hybrid preserves RapidFuzz strengths while adding semantic recall
    for vague queries and image-derived descriptions.

  Why sentence-transformers / local models not an API:
    No API cost, no rate limits, no latency from external calls,
    no dependency on third-party uptime. The models are small enough
    to run on any VPS with 1GB RAM. First-run download (~440MB) is
    a one-time cost.

  Why all-MiniLM-L6-v2 for text:
    Best-in-class performance/size ratio for semantic similarity tasks.
    384 dimensions is compact (fast dot products), 90MB fits comfortably
    in a 1GB server. Outperforms larger models on short text matching
    tasks (product names and descriptions are short text).

  Why clip-ViT-B-32 for image queries:
    CLIP shares a vector space between text and image encodings.
    A text description of an image (from vision API) and an actual
    product image embedding are directly comparable. This means the
    same model serves both current text-described-image queries and
    future real image-to-image matching without any architecture change.

  Fallback:
    SEMANTIC_SEARCH_ENABLED=false disables entirely with no performance
    impact. Models load async so server startup is never blocked.
    Until models ready, system runs pure RapidFuzz (identical to Phase 2-7).
