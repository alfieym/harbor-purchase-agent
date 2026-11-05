"""S9: task_dispatch contracts, evidence verification, and batch execution."""

from harbor.agents.dispatch import Claim, SubAgentOutput, TaskBrief, verify_claims


def test_verify_marks_fabricated_product_unverified() -> None:
    output = SubAgentOutput(
        summary="找到一款", claims=[Claim(product_id="P404", statement="很合适")]
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False
    assert verified_ids == ["P1"]


def test_verify_marks_wrong_field_value_unverified() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[Claim(product_id="P1", field="price_major", value=99.0)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_verify_accepts_matching_value_and_int_float_equivalence() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[
            Claim(product_id="P1", field="price_major", value=89),
            Claim(product_id="P1", statement="真实存在"),
        ],
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert [ref.verified for ref in refs] == [True, True]
    assert verified_ids == ["P1"]


def test_verify_marks_unknown_field_unverified() -> None:
    output = SubAgentOutput(
        summary="…",
        claims=[Claim(product_id="P1", field="stock", value=5)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_brief_renders_all_constraints_and_known_ids() -> None:
    brief = TaskBrief(
        task_id="t1",
        goal="母婴宠物类目找 500 元以内的饮水器",
        category="母婴宠物",
        price_max_major=500.0,
        currency="CNY",
        weight_max_kg=1.0,
        excluded_material_tags=("塑料",),
        known_product_ids=("P1",),
    )

    text = brief.render()

    assert "目标：母婴宠物类目找 500 元以内的饮水器" in text
    assert "- category: 母婴宠物" in text
    assert "- price_max_major: 500.0" in text
    assert "- excluded_material_tags: 塑料" in text
    assert "- known_product_ids（用户已见过，仅供参考）: P1" in text


def test_brief_marks_unspecified_fields() -> None:
    text = TaskBrief(task_id="t1", goal="找杯子").render()

    assert "- category: 未指定" in text
    assert "- excluded_material_tags: 无" in text
