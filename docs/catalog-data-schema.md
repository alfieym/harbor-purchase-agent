# Catalog 数据 Schema（`data/catalog-v1.jsonl`）

- 日期：2026-10-31（当日修订：CROSS 88 组拆成 440 单件，见第四节/第七节）
- 数据：`data/catalog-v1.jsonl`，共 3700 条
- 性质：本文 = **权威语义契约** + **从 3700 条数据反推的实测结构** 两部分。
  权威契约来自主数据设计（见第一节）；实测结构因仓库里没有生成脚本、没有上游字段文档
  （数据在一次提交 `14b64e4 S1 构建完成` 整份落盘）而只能靠反推，所有数字均实测得出。

## 一、语义契约与字段归属

### 语义契约（权威定义）

> 同 `canonical_product_id` 组内的商品，**必须是同一款**：品牌、品类、材质、型号
> （`model_spec`）相同；价格、库存、币种、配送国可以不同。

即：`canonical_product_id` 是「跨平台同一个真实商品」的**等价类**，组内成员的
不变部分是品牌/品类/材质/型号，可变部分是价格/库存/币种/配送国。

### 字段层级与归属

| 字段 | 层级 | 语义 | 谁维护 |
|---|---|---|---|
| `canonical_product_id` | 等价类 | 跨平台同一个真实商品 | 主数据 / MDM |
| `product_id` | SPU | 某平台一条商品记录 | 平台 |
| `external_product_id` | 外部引用 | 平台侧原始 ID（如 `amazon-P1001`） | 平台 |
| `sku_id` | SKU | 颜色/尺码等具体规格 | 商家 |

层级关系：**一个等价类（canonical）⊇ 多个 SPU（product）⊇ 多个 SKU**。检索发生在
SPU（`Product`）层；SKU 只存在于单个 `Product.skus` 内、从不跨 `Product`。

### 契约核对：实测数据 vs 语义契约

| 族 | 组数 | 组内 4 个平台 | 品牌/品类/材质/型号 | 是否符合契约 |
|---|---|---|---|---|
| LEGACY | 60 | —（单件） | — | 契约空转（无同组成员） |
| CROSS | 88 → 440 单件（已修复） | ✅ 跨 4 平台 | ❌ 全不同 | 违反 → **2026-10-31 拆成单件** |
| PLAIN | 125 | ✅ 跨 4 平台 | ✅ 全相同 | 符合 |
| ML | 100 | ✅ 跨 4 平台 | ✅ 全相同 | 符合 |

**结论**：契约对 225 组（PLAIN + ML）成立；CROSS 的 88 组原违反契约（组内品牌/品类/材质
全不同），已于 2026-10-31 拆成 440 个单件等价类——修复后 225 个多件组**零违反**。

## 二、总览：四条「数据族」

`canonical_product_id` 的取值按 id 模式和 `product_id` 数值区间切成互不相交的四个族。
**同一字段，在四个族里表达的是不同的东西**——理解这一点是正确去重的前提。

| 族 | id 模式 | 组数 | 每组成员 | 商品数 | `product_id` 区间 | 组内真实含义 |
|---|---|---|---|---|---|---|
| LEGACY | `CAN-LEGACY-\d{3}`（000–059） | 60 | 1 | 60 | P1001–P1060 | 单件，无分组 |
| CROSS | `CAN-\d{3}`（000–087）→ 已拆 | 88 → 440 单件 | 5 → 1 | 440 | P2000–P2439 | 原 5 件无关商品，**已拆成单件** |
| PLAIN | `CAN-GX-\d{2}-\d`（族 1–32，变体 1–4） | 125 | 4 | 500 | P3000–P3499 | 同一商品 **跨 4 平台 × 4 币种** |
| ML | `CAN-ML-\d{2}-\d`（族 1–25，变体 1–4） | 100 | 27 | 2700 | P10000–P43099 | 同一商品 **跨 4 平台 × 10 语言 × 5 币种** |

合计 60 + 440 + 500 + 2700 = 3700；组数（修复后）500 单件 + 125 + 100 = 725（修复前 373）。

## 三、记录级 schema（原始 JSONL 全字段）

`Product` 模型只投影了其中一部分（见第六节）；下表是原始记录的完整字段。

| 字段 | 类型 | 必填范围 | 语义 |
|---|---|---|---|
| `product_id` | str | 全部 | SPU 主键，`P` + 数字 |
| `canonical_product_id` | str | 全部 | 等价类键，**非唯一**（见第二节） |
| `external_product_id` | str | 全部 | `<平台>-<product_id>`；平台为 `amazon/ebay/etsy/walmart` 或 `demo` |
| `source_platform` | str | 全部 | 上架平台 `amazon/ebay/etsy/walmart` |
| `title` / `brand` / `description` / `highlights` | str/str/str/list | 全部 | 商品文本；PLAIN/ML 的 title 尾部内嵌 canonical id |
| `category` / `tax_category` | str | 全部 | 类目；8 类（办公学习/厨房餐饮/家居生活/户外运动/数码配件/旅行装备/母婴宠物/美妆个护） |
| `material_tags` | list[str] | 全部 | 材质标签（不是场景标签，「养猫」不在其中） |
| `weight_kg` | float | 全部 | 重量 |
| `dimensions_cm` | dict | 全部 | 长宽高 |
| `origin_country` / `ships_to` | str / list[str] | 全部 | 产地 / 可发国家 |
| `rating_summary` | dict | 全部 | 评分与评论数 |
| `skus` | list[dict] | 全部 | `{sku_id, spec, price_major, currency, stock}` |
| `updated_at` | date(str) | 全部 | 更新日期 |
| `data_provenance` | str | PLAIN(500) + ML(2700) | 固定 `"synthetic"` |
| `model_spec` | str \| dict | PLAIN(500) + ML(2700) | 型号规格：PLAIN 为字符串（如 `"15L 无防雨罩"`），ML 为 dict（如 `{"feature":"Capacity","value":20,"unit":"L"}`） |
| `evaluation_family` | int | PLAIN + ML | 0–31，推测为家族编号（0 索引） |
| `evaluation_tier` | int | PLAIN + ML | 0–3，推测为变体档位（对应变体 1–4） |
| `evaluation_tags` | list[str] | 部分 | `hard_negative` / `high_spec` / `multilingual`，见第五节 |
| `language_group` | str | ML(2700) | `common` / `long_tail` |
| `source_language` | str | ML(2700) | 10 种：`de/en/es/fr/it/ja/nl/pl/pt/sv` |
| `source_locale` | str | ML(2700) | 12 种：上表 10 语言 + `es-MX`、`fr-CA`（长尾） |
| `localized_category` / `localized_material` | str | ML(2700) | 类目/材质的英文版 |

字段缺失规律是**按族整体出现**的，不是逐条随机：LEGACY 和 CROSS 完全没有
`data_provenance`/`model_spec`/`evaluation_*`/`language_*`；这些生成元数据只出现在
PLAIN 与 ML 上，`language_*` 更是 ML 独占。

## 四、四个族的详细语义

### LEGACY — `CAN-LEGACY-000…059`（60 条）

- 每 id 只有 1 条，`external_product_id` 用真实平台前缀（amazon/ebay/etsy/walmart 各 15 条）。
- 币种几乎全 CNY（57），仅 3 条 USD。
- 无任何生成元数据。像是「第一版手工 seed」，后三族是模板化批量生成的。

### CROSS — `CAN-000…087`（88 组 × 5 = 440 条）— **已修复**

> 2026-10-31 已把 88 组拆成 440 个单件等价类（`canonical_product_id = CAN-<product_id>`，
> 脚本 `scripts/fix_cross_canonical_ids.py`）。下面描述的是**修复前的原始状态**，保留作记录。

- 每组 5 条**互不相干**的商品：5 个类目 × 5 个币种各一件，品牌/标题/材质/重量全不同。
- 样例 `CAN-000`：P2000 旅行收纳包(旅行装备/CNY)、P2001 数码扩展坞(数码配件/USD)、
  P2002 家居收纳盒(家居生活/EUR)、P2003 户外保温杯(户外运动/JPY)、
  P2004 旅行洗护套装(美妆个护/SGD)。
- 其中 15 组（75 条）带 `evaluation_tags=["hard_negative"]`。
- **契约核对：违反语义契约**——组内品牌（`Atlas-00…04`）、品类、材质、型号全部不同，
  却共享一个 `canonical_product_id`。这是检索评测构造的「必须被硬约束过滤掉」的负样本桶；
  把它们当同款去重会错得离谱。

### PLAIN — `CAN-GX-<A>-<B>`（125 组 × 4 = 500 条）

- `A`（族）1–32，`B`（变体）1–4；`CAN-GX-32-2/3/4` 三个 id 缺失，所以是 125 而非 128。
- **`B` 编码型号规格**：`-1`=15L 无防雨罩、`-2`=25L、`-3`=35L 配防雨罩、`-4`=45L 配防雨罩及水袋仓；
  `model_spec` 是字符串，`title` 尾段内嵌 id 与规格。
- 每个 id 的 4 个成员 = 同一商品**跨 4 个平台（amazon/ebay/etsy/walmart）× 4 币种**
  （5 币种取 4，整份数据每个币种恰 100 次）。
- **价格是按汇率换算过的等价价**：`CAN-GX-01-1` = CNY 59.0 ≈ USD 9.3 ≈ EUR 9.36 ≈ JPY 1666.67。
  即生成端做过汇率换算，代码端（S2-6）缺的是运行时换算。
- **契约核对：符合**——品牌/品类/材质/型号组内全相同，价格/币种不同。
- `external_product_id` 前缀为 `demo`（与 `source_platform` 的真实平台名不一致）。

### ML — `CAN-ML-<A>-<B>`（100 组 × 27 = 2700 条）

- `A`（族）1–25，`B`（变体）1–4，25 × 4 = 100 完整。
- **`B` 编码容量档位**：`model_spec` 是 dict，`value` 依次为 20/30/40/50（L）。
- 每个 id 的 27 个成员 = 同一商品 **跨 4 平台 × 10 语言 × 5 币种的 27 个 (语言, 币种) 组合**，
  不是完整的 10 × 5 网格；同语言在组内出现 1–4 次（如 `en` 落在 CNY/EUR/JPY/USD 四个币种）。
- 每组成员拆成 `language_group`：`common` 18 条 + `long_tail` 9 条。
- **契约核对：符合**——品牌/品类/材质/型号组内全相同，币种/语言不同。
- `external_product_id` 前缀为 `demo`。

## 五、`evaluation_tags` 取值与覆盖

| 取值 | 覆盖 | 含义（推测） |
|---|---|---|
| `multilingual` | ML 全部 2700 | 属于多语言重复样本 |
| `hard_negative` | CROSS 75 + PLAIN 165 + ML 1350 | 检索评测里「必须被过滤/不应召回」的负样本 |
| `high_spec` | ML 1350 | 高规格正样本 |

`evaluation_*`、`language_*`、`data_provenance`、`model_spec`、`localized_*` 都是
**评测/生成期元数据**，`Product` 模型用 `ConfigDict(extra="ignore")` 主动丢弃，
不会进入 `model_dump()`，也不该影响检索业务逻辑。

## 六、`Product` 模型的投影

`Product`（`models/product.py`）保留业务字段，丢弃生成期元数据：

- **保留**：`product_id`、`canonical_product_id`、`external_product_id`、`title`、
  `brand`、`category`、`tax_category`、`description`、`highlights`、`material_tags`、
  `origin_country`、`ships_to`、`source_platform`、`dimensions_cm`、`weight_kg`、
  `rating_summary`、`skus`（映射为 `Sku(price: Money)`）、`updated_at`。
- **丢弃**：`data_provenance`、`model_spec`、`evaluation_family/tier/tags`、
  `language_group`、`source_language/locale`、`localized_category/material`。

## 七、对检索与去重的影响（S2-4 落点）

> **方向（已定）**：采用 (a)——把 CROSS 当作**数据质量问题修数据**：88 组的 440 条商品
> 拆成单件等价类（`CAN-<product_id>`），语义契约在整份数据上成立。检索层**不**做契约
> 校验式去重（那是已否的 (b)），改为在数据干净后正常按 `canonical_product_id` 去重。
>
> 遗留子项：
> 1. **契约「型号（`model_spec`）相同」子句**：`model_spec` 仍被 `Product` 丢弃；数据修复后
>    该子句已由「数据本身保证组内同款」覆盖，暂不校验（要字面校验需回挂 `model_spec`）。
> 2. **「保留最低价 SKU」**：S2-6 汇率换算已实现（`search/fx.py`），跨币种比价已可行；
>    S2-4 的最低价选择可在此基础上实现。

1. **去重键是 `canonical_product_id`**：CROSS 已拆成单件，整份数据 225 个多件组全部满足
   契约，可安全按 canonical 去重（修复前 CROSS 的 88 组会把 440 件无关商品压成 88 件）。
2. **真正的 SKU 变体在单个 `Product.skus` 内**（同币种，如「石墨黑/雾灰」），SKU 从不跨
   `Product` 存在；canonical 组是**跨 `Product` 的整条记录重复**（跨平台/币种/语言副本）。
3. **「保留最低价」必须先有汇率（S2-6）**：225 个多件组里 **0 组是同币种**（每组都跨
   4–5 币种），没有运行时换算就无法比较 CNY 59 与 USD 9.3 谁更低。
4. **PLAIN 组内的价格已是汇率换算后的等价价**，可为 S2-6 的换算正确性提供参考基准。
