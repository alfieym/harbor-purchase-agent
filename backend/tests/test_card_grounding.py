"""Per-turn card grounding: a card must be backed by this turn's tool results.

The model keeps the previous ``GenerateStructuredOutput`` call in its context,
so it can copy stale cards into a new answer without searching again. These
tests pin the rule that fixes it: only products a tool returned *during this
turn* may appear as cards.
"""

import asyncio
import json

from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState

from harbor.agents.cards import ProductCard
from harbor.agents.grounding import ground_cards, tool_results_since


def _card(*, title: str = "猫饮水器", sku: str = "P1-S1") -> ProductCard:
    return ProductCard(
        title=title,
        brand="PetGo",
        category="母婴宠物",
        price_major=200.0,
        currency="CNY",
        weight_kg=1.0,
        material_tags=[],
        highlights=[],
        sku_id=sku,
        score=None,
    )


def _result(
    name: str,
    payload: dict,
    *,
    block_id: str = "call-1",
    state: ToolResultState = ToolResultState.SUCCESS,
) -> Msg:
    return Msg(
        name="purchase_agent",
        role="assistant",
        content=[
            ToolResultBlock(
                id=block_id,
                name=name,
                output=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
                state=state,
            )
        ],
    )


def _search(*hits: dict, block_id: str = "call-1") -> Msg:
    return _result("product_search", {"hits": list(hits)}, block_id=block_id)


def _hit(pid: str = "P1", sku: str = "P1-S1", title: str = "猫饮水器") -> dict:
    return {"product_id": pid, "title": title, "selected_sku_id": sku}


def _blocks(*messages: Msg) -> list:
    return [block for message in messages for block in message.get_content_blocks("tool_result")]


def test_card_backed_by_this_turns_search_is_kept() -> None:
    cards = [_card()]

    kept = ground_cards(cards, _blocks(_search(_hit())))

    assert kept == cards


def test_card_without_any_evidence_is_dropped() -> None:
    cards = [_card(sku="P404-S1", title="不存在的杯子")]

    kept = ground_cards(cards, _blocks(_search(_hit())))

    assert kept == []


def test_no_tool_result_means_no_cards() -> None:
    """A chit-chat turn must not reprint the previous turn's products."""
    cards = [_card()]

    kept = ground_cards(cards, [])

    assert kept == []


def test_a_title_match_is_enough_when_the_sku_shape_differs() -> None:
    cards = [_card(title="猫 饮水器", sku="other-id")]

    kept = ground_cards(cards, _blocks(_search(_hit(title="猫饮水器"))))

    assert kept == cards


def test_dispatch_verified_ids_are_evidence() -> None:
    """S9 parent cards may come from a dispatched child, not a direct search."""
    dispatch = _result(
        "task_dispatch",
        {
            "results": [
                {
                    "ok": True,
                    "verified_product_ids": ["P1"],
                    "evidence_refs": [
                        {
                            "product_id": "P1",
                            "field": "title",
                            "value": "猫饮水器",
                            "verified": True,
                        }
                    ],
                }
            ]
        },
    )

    kept = ground_cards([_card()], _blocks(dispatch))

    assert kept != []


def test_tool_results_since_excludes_an_earlier_turn() -> None:
    old = _search(_hit(), block_id="old")
    new = _search(_hit(pid="P2", sku="P2-S1"), block_id="new")

    blocks = tool_results_since([old, new], known_ids={"old"})

    assert [block.id for block in blocks] == ["new"]


def test_an_error_result_contributes_no_evidence() -> None:
    error = _result("product_search", {"hits": [_hit()]}, state=ToolResultState.ERROR)

    assert ground_cards([_card()], _blocks(error)) == []


class _ReplAgent:
    """Minimal REPL agent: one canned answer, nothing searched this turn."""

    def __init__(self, output: dict) -> None:
        self.state = AgentState()
        self._output = output

    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context.append(msg)
        result = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(text="命中 1 条")],
            structured_output=self._output,
        )
        self.state.context.append(result)
        return result


def test_cli_repl_does_not_print_a_stale_card(monkeypatch, capsys) -> None:
    from harbor.agents.cli import run_repl

    output = {
        "cards": [_card().model_dump()],
        "summary": "命中 1 条",
    }
    answers = iter(["你好", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    asyncio.run(run_repl(_ReplAgent(output)))

    printed = capsys.readouterr().out
    assert "命中 1 条" in printed
    assert "猫饮水器" not in printed
