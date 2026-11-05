"""S9: dispatch contracts and evidence verification.

A sub-agent never reports ``verified`` itself. It reports *claims*; this module
turns each claim into an ``EvidenceRef`` by comparing it against what the
sub-agent's own ``product_search`` calls actually returned. That asymmetry is
the whole point: the parent may trust the evidence, not the narrator.
"""

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.message import TextBlock, ToolResultState, UserMsg
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk, Toolkit
from pydantic import BaseModel, Field

from harbor.agents.permissions import allow_tool
from harbor.agents.prompts import subagent_system_prompt
from harbor.agents.tools import (
    FixedSearchConstraints,
    _search_result_to_dict,
    make_product_search_tool,
)

DISPATCH_STATE_KEY = "harbor.dispatch"
_LEDGER_HISTORY = 5


@dataclass(frozen=True)
class TaskBrief:
    """The only thing a sub-agent gets: a self-contained, structured task."""

    task_id: str
    goal: str
    category: str | None = None
    price_max_major: float | None = None
    currency: str = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: tuple[str, ...] = ()
    known_product_ids: tuple[str, ...] = ()

    def fixed_constraints(self) -> FixedSearchConstraints:
        """Pin this task's hard constraints at the sub-agent's tool layer."""
        return FixedSearchConstraints(
            category=self.category,
            price_max_major=self.price_max_major,
            currency=self.currency,
            weight_max_kg=self.weight_max_kg,
            excluded_material_tags=self.excluded_material_tags,
        )

    def render(self) -> str:
        """Render the brief the sub-agent's system prompt carries."""
        return "\n".join(
            [
                f"目标：{self.goal}",
                "以下字段都是硬约束，不得自行放宽：",
                f"- category: {self.category or '未指定'}",
                f"- price_max_major: "
                f"{self.price_max_major if self.price_max_major is not None else '未指定'}",
                f"- currency: {self.currency}",
                f"- weight_max_kg: "
                f"{self.weight_max_kg if self.weight_max_kg is not None else '未指定'}",
                f"- excluded_material_tags: {'、'.join(self.excluded_material_tags) or '无'}",
                f"- known_product_ids（用户已见过，仅供参考）: "
                f"{'、'.join(self.known_product_ids) or '无'}",
            ]
        )


class Claim(BaseModel):
    """A structured statement a sub-agent makes about a product."""

    product_id: str
    field: str | None = None
    value: Any = None
    statement: str = ""


class SubAgentOutput(BaseModel):
    """The fixed hand-off format a sub-agent must return."""

    summary: str
    claims: list[Claim] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """A claim plus the verdict the dispatch layer computed for it."""

    product_id: str
    field: str | None = None
    value: Any = None
    statement: str = ""
    source: str = "product_search"
    verified: bool


class DispatchResult(BaseModel):
    """One task's receipt — all the parent agent ever sees of that task."""

    task_id: str
    ok: bool
    summary: str
    evidence_refs: list[EvidenceRef]
    verified_product_ids: list[str]


@dataclass(frozen=True)
class SubAgentRun:
    """What a sub-agent runner hands back: its words plus what tools saw."""

    output: SubAgentOutput
    observed: dict[str, dict[str, Any]]


def _values_match(claimed: Any, observed: Any) -> bool:
    # JSON turns 89 into int and 89.0 into float; they are the same price.
    if isinstance(claimed, int | float) and isinstance(observed, int | float):
        return float(claimed) == float(observed)
    return claimed == observed


def verify_claims(
    output: SubAgentOutput,
    observed: Mapping[str, Mapping[str, Any]],
) -> tuple[list[EvidenceRef], list[str]]:
    """Cross-check claims against real tool returns.

    Returns the evidence refs (one per claim) and the ids the tools actually
    returned, sorted. A fabricated id or a wrong field value stays visible as
    ``verified=False`` rather than being silently dropped.
    """
    refs: list[EvidenceRef] = []
    for claim in output.claims:
        fields = observed.get(claim.product_id)
        if fields is None or claim.field is None:
            verified = fields is not None
        else:
            verified = claim.field in fields and _values_match(claim.value, fields[claim.field])
        refs.append(
            EvidenceRef(
                product_id=claim.product_id,
                field=claim.field,
                value=claim.value,
                statement=claim.statement,
                verified=verified,
            )
        )
    return refs, sorted(observed)


class DispatchTaskArgs(BaseModel):
    """One self-contained task inside a dispatch batch."""

    goal: str = Field(description="一句话任务说明，必须自包含：子 Agent 看不到对话历史")
    category: str | None = None
    price_max_major: float | None = None
    currency: str = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: list[str] = Field(default_factory=list)
    known_product_ids: list[str] = Field(default_factory=list)


class TaskDispatchArgs(BaseModel):
    """Arguments the model supplies when it calls ``task_dispatch``."""

    tasks: list[DispatchTaskArgs] = Field(default_factory=list)


def build_briefs(
    tasks: Sequence[DispatchTaskArgs],
    blacklist: Sequence[str],
) -> list[TaskBrief]:
    """Turn model-supplied tasks into briefs, merging the buyer's blacklist."""
    briefs: list[TaskBrief] = []
    for index, task in enumerate(tasks, start=1):
        excluded = list(task.excluded_material_tags)
        excluded.extend(tag for tag in blacklist if tag not in excluded)
        briefs.append(
            TaskBrief(
                task_id=f"t{index}",
                goal=task.goal,
                category=task.category,
                price_max_major=task.price_max_major,
                currency=task.currency,
                weight_max_kg=task.weight_max_kg,
                excluded_material_tags=tuple(excluded),
                known_product_ids=tuple(task.known_product_ids),
            )
        )
    return briefs


def read_dispatch_ledger(state: AgentState | None) -> dict[str, Any]:
    """Return the dispatch plan stored in the session's middle context."""
    if state is None:
        return {}
    return dict(state.middle_context.get(DISPATCH_STATE_KEY, {}) or {})


def _batch_order(key: str) -> int:
    try:
        return int(key[1:])
    except ValueError:
        return 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _failed(task_id: str, summary: str) -> DispatchResult:
    return DispatchResult(
        task_id=task_id,
        ok=False,
        summary=summary,
        evidence_refs=[],
        verified_product_ids=[],
    )


def _status_of(result: DispatchResult) -> str:
    if result.ok:
        return "ok"
    return "timeout" if "超时" in result.summary else "failed"


def make_task_dispatch_tool(
    memory,
    runner,
    *,
    max_concurrency: int = 3,
    task_timeout_seconds: float = 90.0,
    batch_timeout_seconds: float = 75.0,
    max_tasks: int = 6,
) -> FunctionTool:
    """Build the ``task_dispatch`` tool.

    ``runner`` has ``async run(brief) -> SubAgentRun``. Tasks run concurrently,
    bounded by a semaphore; every failure mode (exception, task timeout, batch
    deadline) becomes a ``ok=false`` *success* result so the parent turn keeps
    going (S9-4).
    """
    semaphore = asyncio.Semaphore(max_concurrency)
    # A module-level lock would bind to the first contending event loop; tests
    # (and any process embedding more than one loop) would then break. Each
    # agent builds its own dispatch tool, so a per-tool lock is the right scope.
    ledger_lock = asyncio.Lock()

    async def _record(state: AgentState | None, batch_key: str, entries: dict[str, Any]) -> None:
        if state is None:
            return
        async with ledger_lock:
            current = read_dispatch_ledger(state)
            batches = dict(current.get("batches", {}))
            batches[batch_key] = entries
            ordered = sorted(batches.items(), key=lambda item: _batch_order(item[0]))
            state.middle_context[DISPATCH_STATE_KEY] = {
                "revision": max(int(current.get("revision", 0)), int(batch_key[1:])),
                "batches": dict(ordered[-_LEDGER_HISTORY:]),
            }

    async def task_dispatch(
        tasks: list[DispatchTaskArgs] | None = None,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        def reply(payload: dict[str, Any]) -> ToolChunk:
            return ToolChunk(
                content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
            )

        raw = [_coerce_task(task) for task in (tasks or [])]
        if not raw:
            return reply({"ok": False, "code": "empty_tasks", "results": []})
        if len(raw) > max_tasks:
            return reply(
                {"ok": False, "code": "too_many_tasks", "max_tasks": max_tasks, "results": []}
            )

        blacklist = (
            memory.blacklisted_material_tags(_agent_state)
            if memory is not None and _agent_state is not None
            else ()
        )
        briefs = build_briefs(raw, blacklist)

        started_at = {brief.task_id: _now() for brief in briefs}
        # Claim the revision *and* write the running entries in one critical
        # section; claiming alone would let two concurrent calls both read the
        # same old revision and collide on ``b1``.
        async with ledger_lock:
            current = read_dispatch_ledger(_agent_state)
            revision = int(current.get("revision", 0)) + 1
            batch_key = f"b{revision}"
            if _agent_state is not None:
                batches = dict(current.get("batches", {}))
                batches[batch_key] = {
                    brief.task_id: {
                        "goal": brief.goal,
                        "status": "running",
                        "started_at": started_at[brief.task_id],
                    }
                    for brief in briefs
                }
                ordered = sorted(batches.items(), key=lambda item: _batch_order(item[0]))
                _agent_state.middle_context[DISPATCH_STATE_KEY] = {
                    "revision": revision,
                    "batches": dict(ordered[-_LEDGER_HISTORY:]),
                }

        deadline = time.monotonic() + batch_timeout_seconds
        started = time.perf_counter()

        async def run_one(brief: TaskBrief) -> DispatchResult:
            async with semaphore:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _failed(brief.task_id, "批次超时未完成")
                batch_limited = remaining <= task_timeout_seconds
                try:
                    run = await asyncio.wait_for(
                        runner.run(brief), timeout=min(task_timeout_seconds, remaining)
                    )
                except TimeoutError:
                    return _failed(
                        brief.task_id,
                        "批次超时未完成" if batch_limited else "子任务超时",
                    )
                except Exception as exc:  # degrade, never fail the whole turn
                    return _failed(brief.task_id, f"子任务失败：{exc}")
            refs, verified_ids = verify_claims(run.output, run.observed)
            return DispatchResult(
                task_id=brief.task_id,
                ok=True,
                summary=run.output.summary[:400],
                evidence_refs=refs,
                verified_product_ids=verified_ids,
            )

        results = await asyncio.gather(*(run_one(brief) for brief in briefs))
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        finished_at = _now()
        await _record(
            _agent_state,
            batch_key,
            {
                result.task_id: {
                    "goal": briefs[index].goal,
                    "status": _status_of(result),
                    "started_at": started_at[result.task_id],
                    "finished_at": finished_at,
                    "verified_product_ids": result.verified_product_ids,
                    "summary": result.summary,
                }
                for index, result in enumerate(results)
            },
        )

        return reply(
            {
                "dispatched": len(results),
                "concurrency": max_concurrency,
                "elapsed_ms": elapsed_ms,
                "results": [result.model_dump(mode="json") for result in results],
            }
        )

    return FunctionTool(
        task_dispatch,
        name="task_dispatch",
        description=(
            "把一批互不依赖的检索任务并发派给只读子 Agent；每个任务返回结论、"
            "证据与核验过的商品 id，中间过程不进入你的上下文。简单请求自己做，不要派。"
        ),
        input_schema=TaskDispatchArgs,
        is_state_injected=True,
    )


def _coerce_task(task: DispatchTaskArgs | Mapping[str, Any]) -> DispatchTaskArgs:
    if isinstance(task, DispatchTaskArgs):
        return task
    return DispatchTaskArgs.model_validate(task)


SUBAGENT_SEARCH_DESCRIPTION = """\
在商品目录里检索满足条件的商品。query 是自然语言描述。本任务的类目、预算、币种、
重量与排除材质已经固定在工具层，你传的值不会放宽它们；返回命中商品与被过滤的原因。"""

_OBSERVED_FIELDS = (
    "product_id",
    "title",
    "brand",
    "category",
    "price_major",
    "currency",
    "weight_kg",
    "material_tags",
    "selected_sku_id",
    "score",
)


class _RecordingSearchService:
    """Delegate to the real service and remember every product it returned.

    This is the ground truth ``verify_claims`` checks against: only what a tool
    actually returned can make a claim ``verified``.
    """

    def __init__(self, inner, observed: dict[str, dict[str, Any]]) -> None:
        self._inner = inner
        self._observed = observed

    def search(self, query, spec, limit=10):
        result = self._inner.search(query, spec, limit)
        for hit in _search_result_to_dict(result, spec.currency)["hits"]:
            self._observed[hit["product_id"]] = {
                key: hit[key] for key in _OBSERVED_FIELDS if key in hit
            }
        return result


def _memory_block(memory) -> str:
    """A read-only snapshot of the buyer's long-term preferences for the child.

    Long-term memory is external, keyed by buyer: sharing it costs no context
    isolation, unlike sharing the conversation.
    """
    if memory is None:
        return ""
    lines: list[str] = []
    for kind, label in (("preference", "偏好"), ("dislike", "不喜欢")):
        for item in memory.long_term_items(kind)[:24]:
            tags = f"（材质：{'、'.join(item.material_tags)}）" if item.material_tags else ""
            lines.append(f"- [长期·{label}] {item.text}{tags}")
    if not lines:
        return ""
    return "<system-info>买家的长期偏好（只读）：\n" + "\n".join(lines) + "\n</system-info>"


class AgentSubAgentRunner:
    """Run one brief in a fresh, read-only agent and record what tools saw."""

    def __init__(
        self,
        *,
        model,
        service,
        categories: Sequence[str],
        memory=None,
        agent_factory=None,
    ) -> None:
        self._model = model
        self._service = service
        self._categories = list(categories)
        self._memory = memory
        self._agent_factory = agent_factory or self._build_agent

    async def run(self, brief: TaskBrief) -> SubAgentRun:
        observed: dict[str, dict[str, Any]] = {}
        search_tool = make_product_search_tool(
            _RecordingSearchService(self._service, observed),
            fixed_constraints=brief.fixed_constraints(),
            description=SUBAGENT_SEARCH_DESCRIPTION,
        )
        prompt = subagent_system_prompt(
            brief_block=brief.render(),
            categories=self._categories,
            memory_block=_memory_block(self._memory),
        )
        agent = self._agent_factory(f"dispatch_{brief.task_id}", prompt, search_tool)
        allow_tool(agent, "product_search")
        reply = await agent.reply(
            UserMsg(name="user", content=brief.goal),
            structured_schema=SubAgentOutput,
        )
        output = getattr(reply, "structured_output", None)
        if output is None:
            raise RuntimeError("子 Agent 未返回结构化输出")
        if not isinstance(output, SubAgentOutput):
            output = SubAgentOutput.model_validate(output)
        return SubAgentRun(output=output, observed=observed)

    def _build_agent(self, name: str, prompt: str, search_tool: FunctionTool) -> Agent:
        """A fresh agent: no history, no buyer identity, one read-only tool."""
        return Agent(
            name=name,
            system_prompt=prompt,
            model=self._model,
            toolkit=Toolkit([search_tool]),
            state=AgentState(),
            react_config=ReActConfig(
                max_iters=6,
                interruption_raise_cancelled_error=True,
            ),
        )
