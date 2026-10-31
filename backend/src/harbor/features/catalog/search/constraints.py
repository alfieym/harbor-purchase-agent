"""Hard-constraint evaluation shared by the Qdrant path and the reference pipeline.

Every public function here is the single source of truth for one question:
"does this candidate satisfy the spec?" and "if not, which constraints did it
fail?". ``wrangle.py``-style duplication is what S2-2 is meant to remove: the
Qdrant filter, the reference pipeline and any future re-evaluation all route
through these functions.
"""

from decimal import Decimal

from harbor.features.catalog.models import Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.fx import convert
from harbor.features.catalog.search.schemas import CandidateFacts, RejectReason


def min_sku(product: Product) -> Sku:
    """The lowest-price SKU of a product."""
    return min(product.skus, key=lambda sku: sku.price.amount)


def facts_from_product(product: Product) -> CandidateFacts:
    """Project a product onto the fields hard constraints care about."""
    cheapest = min_sku(product)
    return CandidateFacts(
        product_id=product.product_id,
        title=product.title,
        min_price_major=cheapest.price.amount,
        currency=cheapest.price.currency,
        weight_kg=product.weight_kg,
        category=product.category,
        material_tags=product.material_tags,
    )


def matches_facts(facts: CandidateFacts, spec: ProductSearchSpec) -> bool:
    """True when every hard constraint in ``spec`` holds for ``facts``."""
    if spec.price_max_major is not None:
        converted = convert(facts.min_price_major, facts.currency, spec.currency)
        if converted > spec.price_max_major:
            return False
    if spec.weight_max_kg is not None and facts.weight_kg > spec.weight_max_kg:
        return False
    if spec.category is not None and facts.category != spec.category:
        return False
    if set(spec.excluded_material_tags).intersection(facts.material_tags):
        return False
    return True


def matches(product: Product, spec: ProductSearchSpec) -> bool:
    """Plain-Python mirror of ``build_filter`` — the oracle for hard constraints."""
    return matches_facts(facts_from_product(product), spec)


def _format_amount(value: Decimal) -> str:
    """Render a Decimal without trailing zeros ("4000.0" -> "4000")."""
    return f"{value.normalize():f}"


def reasons_for(facts: CandidateFacts, spec: ProductSearchSpec) -> list[RejectReason]:
    """Every hard constraint ``facts`` fails, in the order they should be reported.

    Contract:
    - Return an empty list when the candidate satisfies the whole spec.
    - Return one reason per failed constraint (a candidate can fail several).
    - ``code`` is a stable machine-readable slug; ``detail`` is a human sentence.
    - ``[reason.code for reason in reasons_for(...)]`` must be empty exactly when
      ``matches_facts(...)`` is true.

    Checks mirror ``matches_facts`` line for line, so the boolean oracle and the
    explanations can be read as one sequence.
    """
    reasons: list[RejectReason] = []

    if spec.price_max_major is not None:
        converted = convert(facts.min_price_major, facts.currency, spec.currency)
        if converted > spec.price_max_major:
            if facts.currency == spec.currency:
                price = f"{_format_amount(facts.min_price_major)} {spec.currency}"
            else:
                price = (
                    f"{_format_amount(converted)} {spec.currency}"
                    f"（换算自 {_format_amount(facts.min_price_major)} {facts.currency}）"
                )
            reasons.append(
                RejectReason(
                    code="price_over",
                    detail=(
                        f"价格 {price} 超过预算 "
                        f"{_format_amount(spec.price_max_major)} {spec.currency}"
                    ),
                )
            )
    if spec.weight_max_kg is not None and facts.weight_kg > spec.weight_max_kg:
        reasons.append(
            RejectReason(
                code="weight_over",
                detail=f"重量 {facts.weight_kg:g} kg 超过上限 {spec.weight_max_kg:g} kg",
            )
        )
    if spec.category is not None and facts.category != spec.category:
        reasons.append(
            RejectReason(
                code="category_mismatch",
                detail=f"类目「{facts.category}」不符合「{spec.category}」",
            )
        )
    matched_tags = [tag for tag in facts.material_tags if tag in spec.excluded_material_tags]
    if matched_tags:
        reasons.append(
            RejectReason(
                code="excluded_material",
                detail=f"材质标签命中排除项：{'、'.join(matched_tags)}",
            )
        )

    return reasons


def empty_reason_for(spec: ProductSearchSpec, category_exists: bool) -> str:
    """Explain an empty result: a missing category, or a generic no-match."""
    if spec.category is not None and not category_exists:
        return f"品类「{spec.category}」不存在"
    return "没有商品满足全部条件"
