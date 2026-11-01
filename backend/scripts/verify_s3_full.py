"""Walk the complete S3 flow end-to-end, stage by stage, and self-check.

Usage:
    uv run --directory backend python scripts/verify_s3_full.py

Covers the whole S3 surface:

    召回决策（exact_id / embedding / keyword_2gram） → 过滤 → 精排 → 去重 → 截断 → 组装
    降级（embedding 挂 → keyword_2gram；精排挂 → rerank_status=failed）
    可观测（recall_strategy / diagnostics.rerank_status / latency_ms）
    bootstrap（ensure_indexed）

The demo query「打扫猫毛的机器」is a paraphrase of「吸尘器」with zero lexical overlap —
only the dual-tower embedding can recall it, which is exactly the S3 problem.
Embedding / rerank scores are deterministic hand-set stand-ins so the flow is
reproducible; the real cross-encoder is smoked separately by verify_s3.py.
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.lexical import LexicalVectorizer
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

QUERY = "打扫猫毛的机器"  # 同义改写 —— 与「吸尘器」字面零重叠
SPEC = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
)
LIMIT = 3

# 每个商品在双塔 embedding 下的相似分（按标题子串映射）。
EMBED_SCORES: Sequence[tuple[str, float]] = [
    ("标准版", 0.90),
    ("促销版", 0.88),
    ("便携", 0.80),
    ("旗舰", 0.75),
    ("工业", 0.70),
    ("宠物", 0.55),
    ("台灯", 0.50),
    ("加湿器", 0.45),
]
# cross-encoder 精排分 —— 把「促销版」A2 反超「标准版」A1（0.92 > 0.85）。
RERANK_SCORES: Sequence[tuple[str, float]] = [
    ("标准版", 0.85),
    ("促销版", 0.92),
    ("便携", 0.70),
    ("台灯", 0.30),
    ("加湿器", 0.25),
]


class ScoreEmbedder:
    """2-d embedder whose cosine against the query equals a hand-set score."""

    def __init__(self, query: str, scoring: Sequence[tuple[str, float]]) -> None:
        self._query = query
        self._scoring = scoring

    def _score(self, text: str) -> float:
        if text == self._query:
            return 1.0
        return next((s for sub, s in self._scoring if sub in text), 0.0)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            score = self._score(text)
            out.append([score, math.sqrt(max(0.0, 1.0 - score * score))])
        return out


class DictReranker:
    """Returns a fixed relevance score per document, matched by substring."""

    def __init__(self, scoring: Sequence[tuple[str, float]]) -> None:
        self._scoring = scoring

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        return [next((s for sub, s in self._scoring if sub in doc), 0.0) for doc in documents]


class FailingEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")


class ExplodingReranker:
    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        raise RuntimeError("reranker down")


class CountingEmbedder:
    """Returns a fixed vector but records every embed call, to prove it is never invoked."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0, 0.0] for _ in texts]


def _product(
    pid: str, title: str, canonical: str, price: float, weight: float, category: str
) -> Product:
    return Product(
        product_id=pid,
        canonical_product_id=canonical,
        external_product_id=f"ext-{pid}",
        title=title,
        brand="TestBrand",
        category=category,
        tax_category=category,
        description="",
        highlights=[],
        material_tags=[],
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=weight,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{pid}-S1",
                spec="标准",
                price=Money.from_major_units(price, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


PRODUCTS = [
    _product("A1", "无线吸尘器 标准版", "CAN-X", 1200, 1.2, "家用电器"),
    _product("A2", "无线吸尘器 促销版", "CAN-X", 800, 1.2, "家用电器"),
    _product("B1", "便携无线吸尘器", "CAN-B", 1500, 1.0, "家用电器"),
    _product("C1", "旗舰吸尘器 大吸力", "CAN-C", 4000, 1.0, "家用电器"),  # 超预算
    _product("D1", "强力工业吸尘器", "CAN-D", 900, 6.0, "家用电器"),  # 超重
    _product("E1", "宠物出行包 猫包", "CAN-E", 500, 0.8, "母婴宠物"),  # 类目不符
    _product("F1", "北欧台灯", "CAN-F", 300, 1.0, "家用电器"),
    _product("G1", "静音加湿器", "CAN-G", 200, 0.5, "家用电器"),
]


def _ids(items: Sequence[tuple[Product, float]]) -> str:
    return " ".join(f"{p.product_id}({s:.2f})" for p, s in items)


def main() -> None:
    problems: list[str] = []
    print(f"query = 「{QUERY}」  spec = 家用电器 / ≤2000 / ≤1.5kg  limit = {LIMIT}\n")

    embedder = ScoreEmbedder(QUERY, EMBED_SCORES)
    reranker = DictReranker(RERANK_SCORES)
    pipeline = SearchPipeline(embedder, reranker=reranker)

    # ---- ① 召回（双塔 embedding）----
    recalled = pipeline.sort(pipeline.score(QUERY, PRODUCTS))
    print(f"① 召回（embedding · {len(recalled)} 条）: {_ids(recalled)}")

    # ---- ② 过滤（硬约束）----
    filtered = pipeline.filter(SPEC, recalled)
    survivors = {p.product_id for p, _ in filtered}
    rejected = [(p, s) for p, s in recalled if p.product_id not in survivors]
    print(f"② 过滤（filter · {len(filtered)} 幸存）: {_ids(filtered)}")
    for p, _s in rejected:
        print(
            f"     ✗ {p.product_id} {p.title}  "
            f"（价格 {p.skus[0].price.amount} / 重量 {p.weight_kg} / 类目 {p.category}）"
        )

    # ---- ③ 精排（cross-encoder）----
    reranked, status = pipeline.rerank(QUERY, filtered)
    print(f"③ 精排（rerank · status={status}）: {_ids(reranked)}")
    print("     ↳ 促销版 A2 反超标准版 A1（0.92 > 0.85），只重排不掉候选")

    # ---- ④ 去重（canonical 最低价）----
    deduped = pipeline.dedupe(SPEC, reranked)
    print(f"④ 去重（dedupe · {len(deduped)} 条）: {_ids(deduped)}")
    print("     ↳ CAN-X 同款留最低价 A2（¥800 < ¥1200），丢 A1")

    # ---- ⑤ 截断 ----
    truncated = pipeline.truncate(deduped, LIMIT)
    print(f"⑤ 截断（truncate · limit={LIMIT}）: {_ids(truncated)}")

    # ---- ⑥ 组装 ----
    hits = pipeline.assemble(truncated)
    pipeline_ids = [h.product.product_id for h in hits]
    print(f"⑥ 组装（assemble · {len(hits)} 条）: {pipeline_ids}")

    # ---- Qdrant 生产路径 + 可观测 ----
    client = QdrantClient(":memory:")
    vectorizer = LexicalVectorizer()
    indexer = QdrantProductIndexer(client, embedder, vectorizer=vectorizer)
    bootstrapped = indexer.ensure_indexed(PRODUCTS)
    print(f"\n[bootstrap] ensure_indexed 建库 = {bootstrapped}（空索引自动建，非空幂等跳过）")

    search = QdrantProductSearch(client, embedder, vectorizer=vectorizer, reranker=reranker)
    result = search.search(QUERY, SPEC, limit=LIMIT)
    print(
        f"[Qdrant] recall_strategy={result.recall_strategy}  "
        f"rerank_status={result.diagnostics.rerank_status}  "
        f"latency={result.diagnostics.latency_ms:.1f}ms"
    )
    print(
        f"[Qdrant] hits = {[h.product.product_id for h in result.hits]}  "
        f"filtered_out = {[c.product_id for c in result.filtered_out]}"
    )

    # ---- 召回决策：exact_id_lookup（零 embedding）----
    counter = CountingEmbedder()
    exact_search = QdrantProductSearch(client, counter, vectorizer=vectorizer)
    exact = exact_search.search("A2-S1", SPEC, limit=10)
    print(
        f"\n[exact_id_lookup] 查 sku_id「A2-S1」→ strategy={exact.recall_strategy}  "
        f"hits={[h.product.product_id for h in exact.hits]}  embedding_calls={counter.calls}"
    )

    # ---- 召回决策：embedding 挂 → keyword_2gram ----
    kw = QdrantProductSearch(client, FailingEmbedder(), vectorizer=vectorizer).search(
        "吸尘器", SPEC, limit=10
    )
    print(
        f"[keyword_2gram] embedding 挂 → strategy={kw.recall_strategy}  "
        f"hits={[h.product.product_id for h in kw.hits]}"
    )

    # ---- 降级：精排挂 → rerank_status=failed，保留召回序 ----
    deg = QdrantProductSearch(
        client, embedder, vectorizer=vectorizer, reranker=ExplodingReranker()
    ).search(QUERY, SPEC, limit=LIMIT)
    print(
        f"[rerank 降级] 精排挂 → rerank_status={deg.diagnostics.rerank_status}  "
        f"hits={[h.product.product_id for h in deg.hits]}（保留召回序）"
    )

    # ---- 自检 ----
    if pipeline_ids != ["A2", "B1", "F1"]:
        problems.append(f"最终 hits 应为 [A2, B1, F1]，实际 {pipeline_ids}")
    if result.recall_strategy != "embedding":
        problems.append("recall_strategy 应为 embedding")
    if result.diagnostics.rerank_status != "success":
        problems.append("rerank_status 应为 success")
    if [h.product.product_id for h in result.hits] != pipeline_ids:
        problems.append("Qdrant 与 pipeline 结果不一致")
    if not bootstrapped:
        problems.append("bootstrap 应首次建库")
    if exact.recall_strategy != "exact_id_lookup" or [h.product.product_id for h in exact.hits] != [
        "A2"
    ]:
        problems.append("exact_id_lookup 应返回 A2")
    if counter.calls != 0:
        problems.append(f"exact_id_lookup 不应调用 embedding，实际调了 {counter.calls} 次")
    if kw.recall_strategy != "keyword_2gram" or not kw.hits:
        problems.append("embedding 挂应降级 keyword_2gram 且有结果")
    if deg.diagnostics.rerank_status != "failed":
        problems.append("精排挂应 rerank_status=failed")

    print(f"\n{'=' * 60}\nS3 完整流程: {'OK' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  - {problem}")


if __name__ == "__main__":
    main()
