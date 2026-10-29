# Catalog feature (Phase 2+)

Implemented domain models: `Money`, `Sku`, `Product`, `ProductSearchSpec` in
`models.py`, with tests in `backend/tests/test_catalog_models.py`.

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

Still absent:

- `service.py`: filtering by query, budget, category, destination and material.
- router/endpoints.
- A file-backed repository that loads or watches the catalog as its own
  lifecycle concern; seed loading is explicit.

If these models are exposed through FastAPI later, note that Pydantic serializes
`Money.amount` as a string by default; add an explicit serializer if clients
expect a number.

The original README DDD layout (`harbor/domain/...`) is replaced by this vertical
slice; see `docs/adr/0001-vertical-slice-deferred-agentscope.md`.
