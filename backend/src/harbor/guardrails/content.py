"""L3: filter tool output before it enters the model's context (spec §7.2).

Task 5 only needs the seam; Task 6 fills in the rules. The shape is fixed
now because ``GuardedTool`` depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilterResult:
    text: str
    hits: tuple[str, ...] = ()


class ContentFilter:
    def __init__(self, max_chars: int = 8000) -> None:
        self.max_chars = max_chars

    def filter(self, text: str) -> FilterResult:
        return FilterResult(text=text)
