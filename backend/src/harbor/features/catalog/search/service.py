"""Orchestration over the S3 search: bootstrap the index and expose search().

S3 kept the search a pure function (no wiring); this service is the missing
wiring — it builds the Qdrant index (``ensure_indexed``), holds the fitted
vectorizer, and hands out ``SearchResult`` for a query + spec. It is the single
entry point the agent layer (``harbor.agents``) depends on.
"""

from collections.abc import Sequence

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.embedder import Embedder
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch
from harbor.features.catalog.search.rerank import Reranker
from harbor.features.catalog.search.schemas import SearchResult


class CatalogSearchService:
    """Bootstraps the product index once, then serves search requests."""

    def __init__(
        self,
        products: Sequence[Product],
        embedder: Embedder,
        reranker: Reranker | None = None,
        collection_name: str = "products",
        client: QdrantClient | None = None,
        index_embedder: Embedder | None = None,
    ) -> None:
        """Wire an index and a search over ``products``.

        ``index_embedder`` is used only for building the index. Pass a local
        stand-in when the configured endpoint has no ``/embeddings``: the build
        succeeds, query-time embedding still uses ``embedder`` (and fails), so
        the S3 chain degrades to ``keyword_2gram`` instead of crashing.
        """
        self._client = client or QdrantClient(":memory:")
        self._vectorizer = LexicalVectorizer()
        self._indexer = QdrantProductIndexer(
            self._client,
            index_embedder if index_embedder is not None else embedder,
            collection_name=collection_name,
            vectorizer=self._vectorizer,
        )
        self._indexer.ensure_indexed(products)
        self._search = QdrantProductSearch(
            self._client,
            embedder,
            collection_name=collection_name,
            vectorizer=self._vectorizer,
            reranker=reranker,
        )

    def search(self, query_text: str, spec: ProductSearchSpec, limit: int = 10) -> SearchResult:
        """Run the S3 fallback-chain recall + rerank for ``query_text`` under ``spec``."""
        return self._search.search(query_text, spec, limit=limit)
