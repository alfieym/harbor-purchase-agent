"""S10: OrderService — draft validation and server-side pricing."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import (
    DraftRequestItem,
    OrderService,
    OrderValidationError,
)
from harbor.features.orders.store import SqliteOrderStore


def _service(tmp_path: Path) -> OrderService:
    path = tmp_path / "orders.sqlite3"
    return OrderService(SqliteOrderStore(path), SqliteEvidenceStore(path))


def _evidence(price="89.00", currency="CNY", product_id="P1", sku="P1-S1"):
    return SearchEvidence(
        product_id=product_id,
        sku_id=sku,
        title="猫饮水器",
        unit_price=Decimal(price),
        currency=currency,
    )


def test_draft_is_priced_from_evidence_not_from_model(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 2)],
            context_evidence=[_evidence()],
        )

    creation = asyncio.run(scenario())
    assert creation.replayed is False
    assert creation.draft.total == Decimal("178.00")
    assert creation.draft.items[0].unit_price == Decimal("89.00")
    assert creation.draft.items[0].evidence_currency == "CNY"


def test_jpy_draft_has_no_fractional_yen(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            currency="JPY",
            items=[DraftRequestItem("P1", "P1-S1", 2)],
            context_evidence=[_evidence("7.20", "CNY")],
        )

    creation = asyncio.run(scenario())
    assert creation.draft.items[0].unit_price == Decimal("150")
    assert creation.draft.total == Decimal("300")


def test_draft_uses_durable_evidence_when_context_is_empty(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        await service.record_search_hits(
            buyer_id="b1", shopping_session_id="s1", evidence=[_evidence()]
        )
        return await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[],
        )

    assert asyncio.run(scenario()).draft.total == Decimal("89.00")


def test_durable_evidence_wins_over_context(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        await service.record_search_hits(
            buyer_id="b1", shopping_session_id="s1", evidence=[_evidence("99.00")]
        )
        return await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence("89.00")],
        )

    assert asyncio.run(scenario()).draft.total == Decimal("99.00")


def test_identical_draft_is_replayed(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        kwargs = dict(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
        first = await service.create_draft(**kwargs)
        second = await service.create_draft(**kwargs)
        return first, second

    first, second = asyncio.run(scenario())
    assert second.replayed is True
    assert second.draft.draft_id == first.draft.draft_id


def test_appendix_evidence_without_product_id_is_accepted(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            appendix_evidence=[_evidence(product_id=None)],
        )

    assert asyncio.run(scenario()).draft.total == Decimal("89.00")


def test_pending_draft_is_returned(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        creation = await service.create_draft(
            buyer_id="b1",
            shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
        return creation.draft, await service.get_pending_draft(
            buyer_id="b1", shopping_session_id="s1"
        )

    created, pending = asyncio.run(scenario())
    assert pending is not None and pending.draft_id == created.draft_id


def _build(**overrides):
    values = {
        "buyer_id": "b1",
        "shopping_session_id": "s1",
        "items": [DraftRequestItem("P1", "P1-S1", 1)],
        "context_evidence": [_evidence()],
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"buyer_id": ""}, "missing_identity"),
        ({"shopping_session_id": ""}, "missing_identity"),
        ({"items": []}, "empty_items"),
        (
            {
                "items": [DraftRequestItem(f"P{i}", f"P{i}-S1", 1) for i in range(21)],
                "context_evidence": [],
            },
            "too_many_items",
        ),
        ({"items": [DraftRequestItem("P1", "", 1)]}, "invalid_item"),
        ({"items": [DraftRequestItem("", "P1-S1", 1)]}, "invalid_item"),
        ({"items": [DraftRequestItem("P1", "P1-S1", 0)]}, "invalid_quantity"),
        ({"items": [DraftRequestItem("P1", "P1-S1", 100)]}, "invalid_quantity"),
        (
            {
                "items": [
                    DraftRequestItem("P1", "P1-S1", 1),
                    DraftRequestItem("P1", "P1-S1", 2),
                ]
            },
            "duplicate_sku",
        ),
        ({"currency": "XYZ"}, "invalid_currency"),
        ({"context_evidence": []}, "evidence_missing"),
        (
            {"context_evidence": [_evidence(product_id="P2")]},
            "evidence_mismatch",
        ),
    ],
)
def test_validation_codes(tmp_path, overrides, code) -> None:
    async def scenario():
        service = _service(tmp_path)
        with pytest.raises(OrderValidationError) as excinfo:
            await service.create_draft(**_build(**overrides))
        return excinfo.value

    assert asyncio.run(scenario()).code == code
