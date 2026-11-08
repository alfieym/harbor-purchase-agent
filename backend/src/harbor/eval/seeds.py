"""Human seeds for S12, one block per failure mode.

Each seed maps an intent to a **catalog line** (brand + model code or the
Chinese product noun) and names the hard constraints the user would state.
The golden label is then derived from the catalog — never typed here.

Seeds are deliberately disjoint: every block targets different canonical
lines, so the canonical-isolation split can move a whole seed between dev and
test without leaking a product across the boundary.
"""

from decimal import Decimal

from harbor.eval.schema import EvalSeed

__all__ = ["BUCKET_QUOTAS", "SEEDS"]

# A hard minimum of seeds per bucket. A run that drops below is a golden-set
# regression, not a smaller run.
BUCKET_QUOTAS: dict[str, int] = {
    "budget_boundary": 4,
    "material_exclusion": 4,
    "delivery": 4,
    "cross_currency": 4,
    "duplicate_canonical": 4,
    "category_missing": 2,
    "constraint_empty": 3,
    "semantic_vague": 4,
}

SEEDS: list[EvalSeed] = [
    # --- budget boundary: the label set changes exactly at the budget -------
    EvalSeed(
        seed_id="budget-desk-lamp-cny",
        bucket="budget_boundary",
        category="办公学习",
        concept_terms=["Roamix", "GX-25"],
        queries=["两百元以内的阅读台灯", "书桌上看书用的台灯，预算 200 元"],
        price_max_major=Decimal("200"),
        notes="GX-25 阅读台灯线；189→336 CNY 档位之间切分",
    ),
    EvalSeed(
        seed_id="budget-file-folder-cny",
        bucket="budget_boundary",
        category="办公学习",
        concept_terms=["Roamix", "GX-28"],
        queries=["一百元以内的文件收纳夹", "办公桌上放 A4 文件的收纳夹，便宜点"],
        price_max_major=Decimal("100"),
        notes="GX-28 文件收纳夹线；预算落在第一、二档之间",
    ),
    EvalSeed(
        seed_id="budget-pet-carrier-cny",
        bucket="budget_boundary",
        category="母婴宠物",
        concept_terms=["Roamix", "GX-29"],
        queries=["四百元以内能带猫出门的宠物航空包", "宠物外出的航空包，预算 400 元"],
        price_max_major=Decimal("400"),
        notes="GX-29 宠物航空包线；377→658 CNY 档位之间切分",
    ),
    EvalSeed(
        seed_id="budget-utensils-cny",
        bucket="budget_boundary",
        category="厨房餐饮",
        concept_terms=["Roamix", "GX-22"],
        queries=["一百元以内的便携餐具", "上班带饭用的不锈钢便携餐具，不超过 100 元"],
        price_max_major=Decimal("100"),
        notes="GX-22 便携餐具线；预算落在第二档边缘",
    ),
    # --- material exclusion: the broad term matches mixed materials ---------
    EvalSeed(
        seed_id="material-sleeping-bag-no-natural",
        bucket="material_exclusion",
        category="户外运动",
        concept_terms=["羽绒睡袋"],
        queries=["羽绒睡袋，不要天然材料填充的", "露营用睡袋，排除天然材料"],
        excluded_material_tags=["天然材料"],
        notes="宽泛词命中 GX-15 与一条天然材料噪声品；排除后只剩 GX-15 的 4 个 canonical",
    ),
    EvalSeed(
        seed_id="material-camping-lantern-no-natural",
        bucket="material_exclusion",
        category="户外运动",
        concept_terms=["露营灯"],
        queries=["露营灯，不要天然材料外壳", "户外露营照明灯，不要天然材质"],
        excluded_material_tags=["天然材料"],
        notes="排除天然材料后剩 GX-13 的 4 个 canonical",
    ),
    EvalSeed(
        seed_id="material-trekking-pole-no-natural",
        bucket="material_exclusion",
        category="户外运动",
        concept_terms=["登山杖"],
        queries=["登山杖，不要天然材料手柄", "徒步登山杖，避开天然材料"],
        excluded_material_tags=["天然材料"],
        notes="排除天然材料噪声后剩 GX-14 的 4 个 canonical",
    ),
    EvalSeed(
        seed_id="material-earbuds-no-natural",
        bucket="material_exclusion",
        category="数码配件",
        concept_terms=["蓝牙耳机"],
        queries=["蓝牙耳机，不要天然材料的", "通勤用的蓝牙耳机，排除天然材质"],
        excluded_material_tags=["天然材料"],
        notes="排除天然材料噪声后剩 GX-06 的 4 个 canonical",
    ),
    # --- delivery: search has no shipping input; this is diagnostic only ----
    EvalSeed(
        seed_id="delivery-pet-fountain-us",
        bucket="delivery",
        category="母婴宠物",
        concept_terms=["宠物饮水器"],
        queries=["给猫买的自动饮水器，寄到美国", "宠物饮水器，收货地址在美国"],
        destination_country="US",
        notes="检索不接收收货地；美国不发货的命中只计入诊断违规",
    ),
    EvalSeed(
        seed_id="delivery-suitcase-jp",
        bucket="delivery",
        category="旅行装备",
        concept_terms=["行李箱"],
        queries=["出差用的行李箱，寄日本", "28 寸行李箱，发到日本"],
        destination_country="JP",
        notes="宽泛词含 LEGACY 噪声；日本不发货的命中只计入诊断",
    ),
    EvalSeed(
        seed_id="delivery-compression-sg",
        bucket="delivery",
        category="旅行装备",
        concept_terms=["压缩收纳袋"],
        queries=["旅行压缩收纳袋，寄新加坡", "出国用的压缩收纳袋，送到新加坡"],
        destination_country="SG",
        notes="含 LEGACY-013；新加坡不发货的命中只计入诊断",
    ),
    EvalSeed(
        seed_id="delivery-baby-towel-cn",
        bucket="delivery",
        category="母婴宠物",
        concept_terms=["婴儿纱布浴巾"],
        queries=["婴儿纱布浴巾，寄到国内", "新生儿洗澡用的纱布浴巾，发中国"],
        destination_country="CN",
        notes="中国不发货的命中只计入诊断",
    ),
    # --- cross currency: the budget is stated in the buyer's currency -------
    EvalSeed(
        seed_id="fx-laptop-stand-usd",
        bucket="cross_currency",
        category="办公学习",
        concept_terms=["Roamix", "GX-26"],
        queries=["笔记本支架，预算 35 美元", "35 美元以内的电脑支架"],
        price_max_major=Decimal("35"),
        currency="USD",
        notes="GX-26；USD 预算换算后落在前两档",
    ),
    EvalSeed(
        seed_id="fx-pour-over-eur",
        bucket="cross_currency",
        category="厨房餐饮",
        concept_terms=["Roamix", "GX-23"],
        queries=["手冲咖啡壶，30 欧元以内", "咖啡手冲壶，预算 30 欧"],
        price_max_major=Decimal("30"),
        currency="EUR",
        notes="GX-23；EUR 预算换算后落在前两档",
    ),
    EvalSeed(
        seed_id="fx-hair-dryer-jpy",
        bucket="cross_currency",
        category="美妆个护",
        concept_terms=["Roamix", "GX-19"],
        queries=["旅行吹风机，4000 日元以内", "出差用的小吹风机，预算 4000 日元"],
        price_max_major=Decimal("4000"),
        currency="JPY",
        notes="GX-19；JPY 预算换算后落在前三档",
    ),
    EvalSeed(
        seed_id="fx-toiletry-bottle-sgd",
        bucket="cross_currency",
        category="美妆个护",
        concept_terms=["Roamix", "GX-18"],
        queries=["洗漱分装瓶，35 新元以内", "旅行装洗发水分装瓶，预算 35 新币"],
        price_max_major=Decimal("35"),
        currency="SGD",
        notes="GX-18；SGD 预算换算后只剩第一档，正例标签数为 1",
    ),
    # --- duplicate canonical: every line has 27 localized/currency variants -
    EvalSeed(
        seed_id="dup-backpack-ml01",
        bucket="duplicate_canonical",
        category="旅行装备",
        concept_terms=["Lingua", "ML-01"],
        queries=["Lingua 登山背包 20L", "Lingua 徒步背包，30 升"],
        notes="ML-01 四个 canonical × 27 个本地化/币种变体；去重后每个 canonical 只出现一次",
    ),
    EvalSeed(
        seed_id="dup-bottle-ml08",
        bucket="duplicate_canonical",
        category="户外运动",
        concept_terms=["Lingua", "ML-08"],
        queries=["Lingua 保温瓶 500ml", "Lingua 户外保温水壶"],
        notes="ML-08 四个 canonical × 27 个变体",
    ),
    EvalSeed(
        seed_id="dup-sleeping-bag-ml10",
        bucket="duplicate_canonical",
        category="户外运动",
        concept_terms=["Lingua", "ML-10"],
        queries=["Lingua 露营睡袋", "Lingua 轻量睡袋"],
        notes="ML-10 四个 canonical × 27 个变体",
    ),
    EvalSeed(
        seed_id="dup-charger-ml04",
        bucket="duplicate_canonical",
        category="数码配件",
        concept_terms=["Lingua", "ML-04"],
        queries=["Lingua USB-C 充电器 65W", "Lingua 氮化镓快充头"],
        notes="ML-04 四个 canonical × 27 个变体",
    ),
    # --- category missing: a plausible category that is not in the catalog --
    EvalSeed(
        seed_id="missing-category-car",
        bucket="category_missing",
        category="汽车用品",
        queries=["车载吸尘器，吸力大一点的"],
        expected_empty=True,
        expected_empty_reason="品类「汽车用品」不存在",
        notes="目录只有 8 个品类，没有汽车用品",
    ),
    EvalSeed(
        seed_id="missing-category-sports",
        bucket="category_missing",
        category="运动户外",
        queries=["跑步鞋，缓震好的"],
        expected_empty=True,
        expected_empty_reason="品类「运动户外」不存在（正确名称是「户外运动」）",
        notes="近似品类名，验证不会误命中",
    ),
    # --- constraint empty: the product exists, the constraint is impossible -
    EvalSeed(
        seed_id="empty-bottle-budget",
        bucket="constraint_empty",
        category="户外运动",
        concept_terms=["Roamix", "GX-16"],
        queries=["十块钱以内的保温壶"],
        price_max_major=Decimal("10"),
        expected_empty=True,
        expected_empty_reason="GX-16 最低价约 59 CNY，10 CNY 预算无命中",
    ),
    EvalSeed(
        seed_id="empty-headphone-fx",
        bucket="constraint_empty",
        category="数码配件",
        concept_terms=["Lingua", "ML-05"],
        queries=["一美元以内的降噪耳机"],
        price_max_major=Decimal("1"),
        currency="USD",
        expected_empty=True,
        expected_empty_reason="ML-05 最低价约 121 CNY，1 USD 预算无命中",
    ),
    EvalSeed(
        seed_id="empty-suitcase-weight",
        bucket="constraint_empty",
        category="旅行装备",
        concept_terms=["Roamix", "GX-03"],
        queries=["50 克以内的行李箱"],
        weight_max_kg=0.05,
        expected_empty=True,
        expected_empty_reason="GX-03 最轻 0.37 kg，0.05 kg 上限无命中",
    ),
    # --- semantic vague: the query is the user's words, not the label ------
    EvalSeed(
        seed_id="vague-shaver",
        bucket="semantic_vague",
        category="美妆个护",
        concept_terms=["Roamix", "GX-17"],
        queries=["出差带着，早上刮胡子用，别太大", "旅行用的电动剃须刀，能水洗最好"],
        notes="标签来自 GX-17，不是查询里的词",
    ),
    EvalSeed(
        seed_id="vague-cleansing-towel",
        bucket="semantic_vague",
        category="美妆个护",
        concept_terms=["Roamix", "GX-20"],
        queries=["旅行时擦脸用的一次性小巾", "出门带着的洁面巾，薄一点"],
        notes="标签来自 GX-20",
    ),
    EvalSeed(
        seed_id="vague-drying-rack",
        bucket="semantic_vague",
        category="家居生活",
        concept_terms=["Roamix", "GX-12"],
        queries=["住酒店想晾点手洗的小衣服", "能折叠带走的晾衣架"],
        notes="标签来自 GX-12",
    ),
    EvalSeed(
        seed_id="vague-neck-pillow",
        bucket="semantic_vague",
        category="旅行装备",
        concept_terms=["Roamix", "GX-02"],
        queries=["坐飞机睡觉脖子不舒服，想买个靠枕", "长途飞行用的颈枕，轻一点"],
        notes="标签来自 GX-02",
    ),
]
