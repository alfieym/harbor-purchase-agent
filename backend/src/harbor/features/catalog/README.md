# Catalog feature (Phase 2+)

Implemented domain models: `Money`, `Sku`, `Product`, `ProductSearchSpec`, one
per file under `models/`, with tests in `backend/tests/test_catalog_models.py`.
`models/__init__.py` re-exports all of them, so `from
harbor.features.catalog.models import Money` keeps working.

Model notes:

- `Money.amount` is `Decimal` in major units (189.0 = 189.00 CNY), not cents.
  `Money` is frozen; `currency` is a three-letter upper-case ISO-4217-shaped
  string, but membership in the official list is not checked.
- `Money.from_major_units(amount, currency)` constructs from major units;
  `amount_in_minor_units` converts using the currency exponent (JPY=0,
  CNY/USD/EUR/SGD=2; unknown currencies default to 2). Values with fractional
  minor units raise `ValueError`.
- Invalid amounts or currencies raise Pydantic `ValidationError`, which is a
  `ValueError` subclass.
- `Sku.price` is `Money`; `stock` is non-negative; zero-stock SKUs are kept.
  `deduct_stock(quantity)` rejects insufficient stock without mutating;
  `restock(quantity)` adds units back. Assignment validation is enabled, so a
  direct negative stock assignment also fails.
- `Product` keeps `dimensions_cm`, `highlights`, `rating_summary` as raw
  dict / list[dict] by decision. `skus` has at least one entry; `updated_at`
  is a `date`.
- `Product` deliberately ignores evaluation/localization metadata
  (`evaluation_*`, `language_group`, `source_*`, `localized_*`, `model_spec`).
- `ProductSearchSpec` is a filter description, not an API schema.
  `price_max_major` is in major units of `currency` (default CNY); there is no
  FX conversion, so a future service must only compare same-currency SKUs.
- `canonical_product_id` groups variants/translations but is not unique; do not
  use it as a key.

Persistence layer:

- `repo.py`: `ProductRepository`, a read-only `Protocol` with `find_by_id`,
  `find_by_ids` and `list_all`. The port stays at the feature root because it
  is an application-facing contract, not a storage adapter.
- `persistence/seed_products.py`: `load_products(path)` reads a versioned
  catalog JSONL file (for example `catalog-v1.jsonl`) and maps raw SKUs
  (`price_major` + `currency`) into `Sku.price: Money`. The loader itself is
  version-agnostic; the version lives in the path.
- `persistence/in_memory_repositories.py`: `InMemoryProductRepository(products)`
  implements the port purely in process memory. It does not persist anything and
  is meant for tests and the early MVP. Duplicate `product_id` values are
  rejected.

Search layer (`search/`):

- `embedder.py`: `Embedder`, a `Protocol` turning texts into fixed-size vectors;
  `product_text(product)` folds title, brand, category, description, highlights
  and material tags into the searchable string; `OpenAIEmbedder` posts to an
  OpenAI-compatible `/embeddings` endpoint and reuses `openai_base_url` /
  `openai_api_key` / `openai_embedding_model` (no separate provider config).
- `schemas.py`: `SearchHit(product, score)`, the retrieval result DTO.
- `qdrant_indexer.py`: `QdrantProductIndexer.recreate(products)` embeds and
  upserts products into a Qdrant collection. It precomputes hard-constraint
  payload — `category`, `weight_kg`, `material_tags`, `min_price_major` (the
  cheapest SKU in the product's single currency) — plus a full `product`
  snapshot so retrieval rebuilds `Product` without a repo round-trip (the
  catalog is read-only). Point ids are `uuid5(product_id)`, because local Qdrant
  accepts only UUID point ids.
- `qdrant_search.py`: `QdrantProductSearch.search(query_text, spec, limit)`
  turns `ProductSearchSpec` into a Qdrant `Filter` (every constraint under
  `must` / `must_not`) and runs a vector query with that filter. Hard
  constraints are enforced by Qdrant at retrieval time, so a hit satisfies all
  of them; vector similarity only orders the survivors.
- `pipeline.py`: `SearchPipeline`, a transparent reference pipeline that spells
  the same logic out in five inspectable stages — 打分 `score` → 过滤 `filter`
  → 排序 `sort` → 截断 `truncate` → 组装 `assemble` — plus a `matches(product,
  spec)` oracle that mirrors `build_filter` 1:1. It is a teaching aid and a
  verification oracle, not the production hot path; `test_search_pipeline.py`
  checks that it and `QdrantProductSearch` agree on the same inputs.

Retrieval semantics: semantic relevance (query vector) and hard constraints
(Qdrant filter) are separated — the filter decides *whether* a product can be
returned, the vector decides *in what order*. This is the same shape a future
`service.py` orchestration layer would expose; `search/` stays a pure-function
layer without AgentScope/LLM tool registration for now.

Still absent:

- `service.py`: orchestration over search + repository (budget/destination/
  material as application concerns beyond raw filtering).
- router/endpoints.
- A file-backed repository that loads or watches the catalog as its own
  lifecycle concern; seed loading is explicit.
- Currency conversion: `min_price_major` and `currency` are stored as-is; a
  multi-currency comparison must happen before filtering.

If these models are exposed through FastAPI later, note that Pydantic serializes
`Money.amount` as a string by default; add an explicit serializer if clients
expect a number.

The original README DDD layout (`harbor/domain/...`) is replaced by this vertical
slice; see `docs/adr/0001-vertical-slice-deferred-agentscope.md`.
