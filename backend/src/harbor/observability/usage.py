"""Token usage recording (S13-3).

The record is honest about what the provider actually reported: prompt and
completion tokens come from the provider's usage object, and a missing usage
object is recorded as ``unknown`` instead of being estimated from the prompt.

AgentScope's ``ChatUsage`` defaults the cache counters to zero, so a zero is
not evidence that the provider reported "no cache"; a positive value proves
the provider reported it. Anything at the default is therefore recorded as
``unknown`` rather than passed off as a measurement.
"""

from __future__ import annotations

from typing import Any

from harbor.observability import incr

__all__ = ["UNKNOWN", "record_usage", "usage_fields"]

UNKNOWN = "unknown"

_FIELDS = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")


def _usage_of(response: Any) -> Any | None:
    if isinstance(response, dict):
        return response.get("usage")
    return getattr(response, "usage", None)


def _reported(usage: Any, attribute: str) -> int | str | None:
    """A cache field is only real when the provider put a positive number there.

    Anything at AgentScope's default of 0 could be "not reported", so it is
    returned as ``None`` and becomes ``unknown`` upstream.
    """
    value = int(getattr(usage, attribute, 0) or 0)
    return value if value > 0 else None


def usage_fields(response: Any) -> dict[str, int | str]:
    """Usage as recorded: integers where reported, ``unknown`` where not."""
    usage = _usage_of(response)
    if usage is None:
        return dict.fromkeys(_FIELDS, UNKNOWN)
    return {
        "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": _reported(usage, "cache_input_tokens") or UNKNOWN,
        "cache_write_tokens": _reported(usage, "cache_creation_input_tokens") or UNKNOWN,
    }


def record_usage(response: Any) -> dict[str, int | str]:
    """Record one model call's usage on metrics; returns the span attributes.

    Only reported integers are counter increments. An unknown cache value is
    not counted as zero, and a missing usage object bumps one
    ``model.usage.missing`` counter so the gap is visible rather than silent.
    """
    fields = usage_fields(response)
    if fields["prompt_tokens"] == UNKNOWN:
        incr("model.usage.missing")
        return fields
    incr("model.usage.prompt_tokens", int(fields["prompt_tokens"]))
    incr("model.usage.completion_tokens", int(fields["completion_tokens"]))
    if fields["cache_read_tokens"] != UNKNOWN:
        incr("model.usage.cache_read_tokens", int(fields["cache_read_tokens"]))
    if fields["cache_write_tokens"] != UNKNOWN:
        incr("model.usage.cache_write_tokens", int(fields["cache_write_tokens"]))
    return fields
