"""S10: REPL slash commands for the human gate (offline, no model)."""

import asyncio
from decimal import Decimal
from pathlib import Path

from harbor.agents.cli import handle_command
from harbor.features.orders.evidence import SqliteEvidenceStore
from harbor.features.orders.models import SearchEvidence
from harbor.features.orders.service import DraftRequestItem, OrderService
from harbor.features.orders.store import SqliteOrderStore


def _service(tmp_path: Path) -> OrderService:
    path = tmp_path / "orders.sqlite3"
    return OrderService(SqliteOrderStore(path), SqliteEvidenceStore(path))


def _evidence() -> SearchEvidence:
    return SearchEvidence(
        product_id="P1",
        sku_id="P1-S1",
        title="猫饮水器",
        unit_price=Decimal("89.00"),
        currency="CNY",
    )


def _draft(service: OrderService):
    return asyncio.run(
        service.create_draft(
            buyer_id="cli",
            shopping_session_id="cli",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
    ).draft


def test_confirm_command_reports_the_order(tmp_path) -> None:
    service = _service(tmp_path)
    draft = _draft(service)

    output = asyncio.run(
        handle_command(
            f"/confirm {draft.draft_id}",
            service,
            buyer_id="cli",
            shopping_session_id="cli",
        )
    )

    assert "订单" in output
    assert "ord_" in output


def test_non_command_text_is_left_to_the_model() -> None:
    result = asyncio.run(
        handle_command("买个饮水器", None, buyer_id="cli", shopping_session_id="cli")
    )
    assert result is None


def test_draft_command_says_when_nothing_is_pending(tmp_path) -> None:
    output = asyncio.run(
        handle_command("/draft", _service(tmp_path), buyer_id="cli", shopping_session_id="cli")
    )
    assert "没有待确认" in output


def test_draft_command_shows_the_pending_draft(tmp_path) -> None:
    service = _service(tmp_path)
    draft = _draft(service)

    output = asyncio.run(
        handle_command("/draft", service, buyer_id="cli", shopping_session_id="cli")
    )

    assert draft.draft_id in output
    assert "89.00" in output


def test_unknown_command_returns_usage() -> None:
    output = asyncio.run(handle_command("/nope", None, buyer_id="cli", shopping_session_id="cli"))
    assert output is not None
    assert "/confirm" in output


def test_bare_command_returns_usage_instead_of_crashing() -> None:
    output = asyncio.run(
        handle_command("/confirm", None, buyer_id="cli", shopping_session_id="cli")
    )
    assert output is not None
    assert "用法" in output


def test_commands_report_when_the_service_is_missing() -> None:
    output = asyncio.run(handle_command("/draft", None, buyer_id="cli", shopping_session_id="cli"))
    assert "订单服务未配置" in output


def test_conflict_is_reported_not_raised(tmp_path) -> None:
    service = _service(tmp_path)
    draft = _draft(service)
    asyncio.run(service.confirm_draft(draft_id=draft.draft_id, buyer_id="cli"))

    output = asyncio.run(
        handle_command(
            f"/cancel-draft {draft.draft_id}", service, buyer_id="cli", shopping_session_id="cli"
        )
    )

    assert "confirmed" in output
