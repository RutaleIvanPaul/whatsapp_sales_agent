# Semantic Search Integration — Instructions for Claude Code

Three files need to be placed and two existing files need small updates.
No other files in the codebase change.

## Step 1 — Place new files

Copy these files into the project:

  embeddings.py  →  app/adapters/inventory/embeddings.py
  cache.py       →  app/adapters/inventory/cache.py   (replaces existing)
  env.example    →  .env.example                      (replaces existing)

## Step 2 — Update requirements.txt

Add these two lines:

  sentence-transformers>=2.7.0
  numpy>=1.24.0

numpy is likely already present as a transitive dependency.
sentence-transformers installs torch (CPU-only by default) as a dependency.
Total new disk space: ~440MB for model weights (downloaded on first run).

## Step 3 — Update app/config.py

Add these two fields to the Config dataclass:

  semantic_search_enabled: bool = True
  semantic_weight: float = 0.6

Load them in the validation/loading section:

  semantic_search_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "true").lower() != "false"
  semantic_weight_raw = os.getenv("SEMANTIC_WEIGHT", "0.6")
  try:
      semantic_weight = float(semantic_weight_raw)
      if not 0.0 <= semantic_weight <= 1.0:
          raise ValueError
  except ValueError:
      errors.append(f"SEMANTIC_WEIGHT must be a float between 0.0 and 1.0, got: {semantic_weight_raw}")
      semantic_weight = 0.6

## Step 4 — Update app/main.py startup sequence

After config is loaded and before inventory is initialised, add:

  from app.adapters.inventory.embeddings import EmbeddingModels

  embedding_models: EmbeddingModels | None = None
  if config.semantic_search_enabled:
      embedding_models = EmbeddingModels()
      embedding_models.load_async()   # non-blocking background thread
      log("semantic_search_enabled", weight=config.semantic_weight)
  else:
      log("semantic_search_disabled")

Then when constructing InventoryCache, pass the new arguments:

  # Before (existing):
  cache = InventoryCache(search_threshold=config.search_threshold)

  # After:
  cache = InventoryCache(
      search_threshold=config.search_threshold,
      semantic_weight=config.semantic_weight,
      embedding_models=embedding_models,   # None if disabled
  )

That is the entire integration. InventoryCache handles the rest internally.

## Step 5 — Add SENTENCE_TRANSFORMERS_HOME to environment (optional)

If deploying to Railway or Render, add this env var to point model storage
at a persistent volume or a fixed path:

  SENTENCE_TRANSFORMERS_HOME=/app/.model_cache

Without this, models re-download on every cold start (~440MB, adds ~2-3 min).
With a persistent volume, models are cached across deploys.

## Step 6 — Verify the integration

Run this quick smoke test after starting the server:

  python -c "
  from app.adapters.inventory.embeddings import EmbeddingModels
  import time
  m = EmbeddingModels()
  m.load_async()
  print('Loading models...')
  for i in range(60):
      if m.is_ready():
          break
      time.sleep(1)
  if m.is_ready():
      v = m.embed_text('blue nike shoes')
      print(f'Text embedding: shape={v.shape}, dtype={v.dtype}')
      v2 = m.embed_image_query('[image: blue running shoe with white sole]')
      print(f'CLIP embedding: shape={v2.shape}, dtype={v2.dtype}')
      print('OK — semantic search is ready')
  else:
      print('Models still loading or failed — check logs')
  "

Expected output:
  Loading models...
  Text embedding: shape=(384,), dtype=float32
  CLIP embedding: shape=(512,), dtype=float32
  OK — semantic search is ready

## What changes at runtime

SEMANTIC_SEARCH_ENABLED=true (default):
  - Server starts normally
  - Embedding models load in background thread (non-blocking)
  - Until models ready: RapidFuzz-only search (same as before)
  - Once models ready: hybrid search activates automatically
  - Next inventory refresh also rebuilds embedding matrices
  - Log event: embedding_models_ready

SEMANTIC_SEARCH_ENABLED=false:
  - No models downloaded or loaded
  - Pure RapidFuzz search (identical to previous behaviour)
  - No performance or memory impact

## Memory footprint

  all-MiniLM-L6-v2 model in memory: ~90MB
  clip-ViT-B-32 model in memory:    ~350MB
  Embedding matrices for 100 products:
    Text (384-dim float32): 100 × 384 × 4 bytes = ~150KB
    CLIP (512-dim float32): 100 × 512 × 4 bytes = ~200KB
  Total additional RAM: ~440MB for models + negligible for product matrices

  Minimum recommended server RAM with semantic search: 1GB
  Minimum without (SEMANTIC_SEARCH_ENABLED=false): 256MB (unchanged)

## No other files change

The search() interface on InventoryAdapter is unchanged.
The LLM tools (search_products, present_products) are unchanged.
The pipeline runner is unchanged.
The response builder is unchanged.
Nothing above the inventory adapter layer is affected.
