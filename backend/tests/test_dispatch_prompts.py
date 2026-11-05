"""S9: the two prompt sets and the restart plan block."""

from harbor.agents.prompts import (
    dispatch_ledger_prompt_block,
    subagent_system_prompt,
    system_prompt_for,
)


def test_dispatch_guidance_is_opt_in() -> None:
    base = system_prompt_for(["家用电器"])
    enabled = system_prompt_for(["家用电器"], dispatch_enabled=True)

    assert "task_dispatch" not in base
    assert "task_dispatch" in enabled
    for phrase in ("可并行", "上下文隔离", "链路够深", "任一不成立"):
        assert phrase in enabled


def test_ledger_block_names_unfinished_tasks() -> None:
    ledger = {
        "revision": 2,
        "batches": {
            "b2": {
                "t1": {"goal": "找饮水器", "status": "failed"},
                "t2": {"goal": "找猫砂", "status": "ok"},
            }
        },
    }

    block = dispatch_ledger_prompt_block(ledger)

    assert "上次派发计划" in block
    assert "找饮水器" in block and "failed" in block
    assert "找猫砂" not in block


def test_subagent_prompt_has_no_user_and_a_fixed_output_shape() -> None:
    prompt = subagent_system_prompt(
        brief_block="目标：找杯子", categories=["家用电器"], memory_block=""
    )

    assert "没有用户" in prompt
    assert '"summary"' in prompt and '"claims"' in prompt
    assert "家用电器" in prompt
    assert "add_preference" not in prompt


def test_subagent_prompt_carries_memory_and_brief() -> None:
    prompt = subagent_system_prompt(
        brief_block="目标：找杯子",
        categories=["家用电器"],
        memory_block="[长期·不喜欢] 不要塑料",
    )

    assert "目标：找杯子" in prompt
    assert "不要塑料" in prompt
