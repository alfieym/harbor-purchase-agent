from decimal import Decimal

from harbor.guardrails.output import GroundingFacts, OutputGuard


def test_ungrounded_price_is_replaced() -> None:
    facts = GroundingFacts(prices=frozenset({("CNY", Decimal("89.00"))}))
    out = OutputGuard().check("这款只要 ¥199，另外 ¥89 的那款也不错", facts)
    assert "¥199" not in out.text
    assert "¥89" in out.text
    assert "ungrounded_price" in out.hits


def test_completion_claim_is_rewritten_without_order_facts() -> None:
    out = OutputGuard().check("已下单，请查收", GroundingFacts(prices=frozenset()))
    assert "已下单" not in out.text
    assert "确认页" in out.text


def test_completion_claim_survives_when_order_exists() -> None:
    out = OutputGuard().check(
        "已下单，订单号 ord_1",
        GroundingFacts(prices=frozenset(), has_confirmed_order=True),
    )
    assert "已下单" in out.text


def test_empty_and_marker_replies() -> None:
    g = OutputGuard()
    assert "empty" in g.check("   ", GroundingFacts(prices=frozenset())).hits
    assert (
        "unrendered_marker"
        in g.check("<tool_call>x</tool_call>", GroundingFacts(prices=frozenset())).hits
    )


def test_internal_identifiers_are_removed() -> None:
    out = OutputGuard().check(
        "Traceback (most recent call last) 然后失败", GroundingFacts(prices=frozenset())
    )
    assert "Traceback" not in out.text


def test_malformed_json_block_is_replaced() -> None:
    out = OutputGuard().check('```json\n{"a": }\n```', GroundingFacts(prices=frozenset()))
    assert "json_block_invalid" in out.hits
    assert "```json" not in out.text


def test_valid_json_block_is_kept() -> None:
    out = OutputGuard().check('```json\n{"a": 1}\n```', GroundingFacts(prices=frozenset()))
    assert "```json" in out.text
    assert "json_block_invalid" not in out.hits
