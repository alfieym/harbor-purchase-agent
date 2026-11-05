"""S9: dispatch contracts and evidence verification.

A sub-agent never reports ``verified`` itself. It reports *claims*; this module
turns each claim into an ``EvidenceRef`` by comparing it against what the
sub-agent's own ``product_search`` calls actually returned. That asymmetry is
the whole point: the parent may trust the evidence, not the narrator.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from harbor.agents.tools import FixedSearchConstraints


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
                f"- excluded_material_tags: "
                f"{'、'.join(self.excluded_material_tags) or '无'}",
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
            verified = claim.field in fields and _values_match(
                claim.value, fields[claim.field]
            )
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
