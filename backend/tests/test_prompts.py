"""The base system prompt's turn-scoping rules (offline, no model)."""

from harbor.agents.prompts import system_prompt_for


def test_summary_is_scoped_to_the_current_turn() -> None:
    """The model must not narrate last turn's finds as this turn's answer.

    Cards are grounded in code (``grounding.py``); the prompt is the
    cooperative half of the same rule, so a chit-chat turn does not produce a
    summary like "找到 1 款饮水器" with an empty card list.
    """
    prompt = system_prompt_for(["母婴宠物"])

    assert "summary 只描述本轮" in prompt
    assert "不要复述上一轮" in prompt


def test_turn_scoping_survives_the_dispatch_prompt() -> None:
    prompt = system_prompt_for(["母婴宠物"], dispatch_enabled=True)

    assert "summary 只描述本轮" in prompt
