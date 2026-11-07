"""Read-only guard status (S11).

An operator needs to tell "the budget ran out" from "the provider is down"
without reading logs; the counters are the same ones the events carry.
"""

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/guardrails", tags=["guardrails"])


@router.get("/status")
def guard_status(request: Request) -> dict[str, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    guard = getattr(runtime, "guard", None)
    if guard is None:
        return {"enabled": False, "budget": {}, "circuits": {}, "metrics": {}}
    return {
        "enabled": True,
        # S11-8: an operator must be able to tell "the guardrail fired" from
        # "someone turned it off", so the applied switches are part of status.
        "switches": guard.switches.as_dict(),
        "budget": {
            "used": guard.budget.used_today(),
            "cap": guard.budget.daily_cap,
            "tier": guard.budget.tier().value,
        },
        "circuits": guard.circuits.snapshot(),
        "tool_circuits": guard.tool_circuits.snapshot(),
        "metrics": guard.metrics.snapshot(),
    }
