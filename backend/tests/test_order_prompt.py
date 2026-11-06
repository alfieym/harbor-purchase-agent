"""S10: prompt guidance for drafting orders without claiming to have placed one."""

from harbor.agents.prompts import system_prompt_for


def test_order_guidance_only_when_enabled() -> None:
    assert "create_order_draft" not in system_prompt_for(["母婴宠物"])
    assert "create_order_draft" in system_prompt_for(["母婴宠物"], orders_enabled=True)


def test_order_guidance_forbids_claiming_an_order_was_created() -> None:
    prompt = system_prompt_for(["母婴宠物"], orders_enabled=True)
    assert "你没有确认能力" in prompt
    assert "已下单" in prompt


def test_dispatch_guidance_stops_saying_orders_are_unwired() -> None:
    prompt = system_prompt_for(["母婴宠物"], dispatch_enabled=True, orders_enabled=True)
    assert "尚未接入" not in prompt
    assert "子 Agent 没有买家身份" in prompt


def test_dispatch_note_keeps_the_old_wording_when_orders_are_off() -> None:
    prompt = system_prompt_for(["母婴宠物"], dispatch_enabled=True)
    assert "尚未接入" in prompt
    assert "子 Agent 没有买家身份" not in prompt
