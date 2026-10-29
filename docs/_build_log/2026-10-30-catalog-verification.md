# Catalog 领域模型与持久化验收记录

- 日期：2026-10-30
- 范围：Catalog 领域模型、`ProductRepository` 端口、seed 加载、内存仓库
- 数据：`data/catalog-v1.jsonl`，共 3700 条

## 结论

| ID | 验收项 | 结果 |
|---|---|---|
| S1-1 | `len(repo.list_all()) == 3700`，且 `find_by_id()` 可取回任意一条 | PASS |
| S1-2 | `Money.from_major_units(189.0, "CNY").amount_in_minor_units == 18900` | PASS |
| S1-3 | 负数金额 / 非法币种抛出 `ValueError` | PASS |
| S1-4 | 构造没有 Sku 的 `Product` 抛出错误（`model_post_init` 校验） | PASS |
| S1-5 | 库存扣到负数抛错；回补后恢复 | PASS |
| S1-6 | 冷启动加载 3700 条 < 2s | PASS |
| S1-7 | `uv run pytest tests/test_domain.py -q` 全绿（≥6 个断言） | PASS |

质量门禁：

```text
make test       72 passed
make lint       All checks passed
make typecheck  mypy Success；tsc 无错误
```

## S1-1：全量加载与按 ID 查询

```python
from pathlib import Path

from harbor.features.catalog.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from harbor.features.catalog.persistence.seed_products import load_products

repo = InMemoryProductRepository(load_products(Path("../data/catalog-v1.jsonl")))
products = repo.list_all()

assert len(products) == 3700

sample = products[0]
assert repo.find_by_id(sample.product_id) == sample

# 全量校验：每一条都能按 product_id 取回
for product in products:
    assert repo.find_by_id(product.product_id) == product
```

实际输出：

```text
PASS
count: 3700
sample: P1001 | Nomadica 旅行三件套（收纳袋+颈枕+眼罩） | amount=Decimal('189.0') currency='CNY'
```

## S1-2：主单位金额转最小单位

```python
from harbor.features.catalog.models import Money

money = Money.from_major_units(189.0, "CNY")
assert money.amount_in_minor_units == 18900
```

实际输出：

```text
PASS
amount=Decimal('189.0') currency='CNY'
18900
```

说明：CNY/USD/EUR/SGD 按 2 位小数，JPY 按 0 位小数；未知币种默认 2 位。
不做任何汇率换算。

## S1-3：负数金额与非法币种

验证了 `Money.from_major_units(...)` 和直接构造 `Money(...)` 两种入口：

```python
from harbor.features.catalog.models import Money

cases = [
    lambda: Money.from_major_units(-1, "CNY"),
    lambda: Money.from_major_units(1, "cny"),
    lambda: Money(amount=-1, currency="CNY"),
    lambda: Money(amount=1, currency="cny"),
]

for case in cases:
    try:
        case()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
```

实际输出：

```text
PASS negative amount: ValidationError
PASS invalid currency: ValidationError
PASS negative amount (direct): ValidationError
PASS invalid currency (direct): ValidationError
```

说明：Pydantic 抛出的 `ValidationError` 是 `ValueError` 的子类，因此
`pytest.raises(ValueError)` / `except ValueError` 都能捕获。

## S1-4：没有 SKU 的 Product

`Product` 使用 Pydantic v2；Pydantic 不会调用 dataclass 风格的
`__post_init__`，等价钩子是 `model_post_init`。当前实现：

```python
class Product(BaseModel):
    ...
    skus: list[Sku]

    def model_post_init(self, __context: Any) -> None:
        if not self.skus:
            raise ValueError("Product must contain at least one Sku")
```

验证命令：

```bash
uv run --directory backend pytest -q tests/test_catalog_models.py -k empty_skus
```

实际结果：

```text
1 passed, 43 deselected
```

完整错误信息：

```text
1 validation error for Product
  Value error, Product must contain at least one Sku [type=value_error, ...]
```

## S1-5：库存扣减与回补

`Sku.deduct_stock(quantity)` 在库存不足时抛 `ValueError`，且不修改库存；
`Sku.restock(quantity)` 回补库存。`Sku` 开启了 `validate_assignment`，直接给
`stock` 赋负值也会被拒绝。

```python
from decimal import Decimal

from harbor.features.catalog.models import Money, Sku

sku = Sku(
    sku_id="P1-S1",
    spec="标准",
    price=Money(amount=Decimal("189.00"), currency="CNY"),
    stock=5,
)

sku.deduct_stock(3)
assert sku.stock == 2
sku.restock(3)
assert sku.stock == 5

try:
    sku.deduct_stock(6)
except ValueError as exc:
    print("PASS", type(exc).__name__)
    print(str(exc))
else:
    raise AssertionError("expected ValueError")

assert sku.stock == 5
```

实际输出：

```text
PASS ValueError
insufficient stock: requested 6, available 5
final stock: 5
```

## S1-6：冷启动加载 3700 条 < 2s

当前应用尚未在启动时自动加载 catalog；这里测量的是实际冷加载路径：
新进程导入模块后，从读取 JSONL 到构建 `InMemoryProductRepository` 完成。

```python
import time
from pathlib import Path

from harbor.features.catalog.persistence.in_memory_repositories import (
    InMemoryProductRepository,
)
from harbor.features.catalog.persistence.seed_products import load_products

start = time.perf_counter()
products = load_products(Path("../data/catalog-v1.jsonl"))
repo = InMemoryProductRepository(products)
elapsed = time.perf_counter() - start

print(f"loaded={len(products)} elapsed={elapsed:.4f}s")
```

实际输出：

```text
loaded=3700 elapsed=0.0440s
```

补充端到端测量（包含 `uv` 和 Python 进程启动）：

```text
/usr/bin/time -p ...   real 0.12
```

结论：0.044s / 0.12s 均远低于 2s，PASS。性能检查不放进单元测试，
避免不同机器和 CI 环境下的计时抖动。

## S1-7：领域测试入口

在 `backend/` 下执行：

```bash
uv run pytest tests/test_domain.py -q
```

实际输出：

```text
5 passed
```

`tests/test_domain.py` 包含 10 个 `assert` 语句，覆盖 Money 主/最小单位、
负数与非法币种、Sku 库存扣减/回补、空 SKU Product、内存仓库查询。

## 补充校验

- `find_by_id("missing")` 返回 `None`。
- `find_by_ids` 按输入顺序返回命中项，缺失 ID 跳过。
- `InMemoryProductRepository` 构造时拒绝重复 `product_id`。
- seed 加载跳过空行；非法 JSON 或非法记录会抛出带文件路径和行号的
  `ValueError`。
- 可选评测/语言元数据（`evaluation_*`、`language_*`、`localized_*`、
  `model_spec`）不会进入 `Product.model_dump()`。

## 后续

本文件按验收顺序追加；当前记录到 S1-7。后续新增验收项继续在本文件下方补充。
