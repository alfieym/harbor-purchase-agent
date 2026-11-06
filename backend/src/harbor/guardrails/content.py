"""L3: filter tool output before it enters the model's context (spec §7.2).

This is the only channel that needs injection filtering: tool output is other
people's text arriving in the model's head. Structured output must stay
parseable — S10's ``grounding.py`` does ``json.loads`` on ``product_search``
results — so JSON is cleaned leaf by leaf and never wrapped or truncated at
the envelope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤]"
)
_ROLE = re.compile(
    r"<\|im_(?:start|end)\|>|\[/?INST\]|###\s*(?:system|instruction)\b|^system\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_INSTRUCTION = re.compile(
    r"ignore\s+(?:all\s+)?previous\s+instructions?"
    r"|disregard\s+\w*\s*instructions?"
    r"|you\s+are\s+now"
    r"|忽略(?:以上|之前)的?指令"
    r"|你现在是"
    r"|系统提示",
    re.IGNORECASE,
)
_ROLE_TRANSLATION = str.maketrans(
    {"<": "＜", ">": "＞", "[": "［", "]": "］", "#": "＃", ":": "："}
)

OPEN_BOUNDARY = "<untrusted-tool-output>"
CLOSE_BOUNDARY = "</untrusted-tool-output>"
TRUNCATION_MARK = "…[truncated]"


@dataclass(frozen=True)
class FilterResult:
    text: str
    hits: tuple[str, ...] = ()


class ContentFilter:
    def __init__(self, max_chars: int = 8000) -> None:
        self.max_chars = max_chars

    def filter(self, text: str) -> FilterResult:
        hits: set[str] = set()
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            cleaned = self._clean_text(text, hits)
            hits.add("wrapped")
            return FilterResult(
                f"{OPEN_BOUNDARY}{cleaned}{CLOSE_BOUNDARY}", tuple(sorted(hits))
            )

        cleaned_data = self._clean_json(data, hits)
        if not hits:
            # Nothing to fix: hand back the original bytes, not a re-dump that
            # would change formatting for no reason.
            return FilterResult(text)
        return FilterResult(
            json.dumps(cleaned_data, ensure_ascii=False), tuple(sorted(hits))
        )

    def _clean_json(self, value: Any, hits: set[str]) -> Any:
        if isinstance(value, str):
            return self._clean_text(value, hits)
        if isinstance(value, list):
            return [self._clean_json(item, hits) for item in value]
        if isinstance(value, dict):
            return {key: self._clean_json(item, hits) for key, item in value.items()}
        return value

    def _clean_text(self, value: str, hits: set[str]) -> str:
        if _CONTROL.search(value):
            hits.add("control_chars")
            value = _CONTROL.sub("", value)

        if _ROLE.search(value):
            hits.add("role_markers")
            value = _ROLE.sub(lambda match: match.group(0).translate(_ROLE_TRANSLATION), value)

        if _INSTRUCTION.search(value):
            hits.add("instruction_phrases")
            value = _INSTRUCTION.sub("[filtered]", value)

        if len(value) > self.max_chars:
            hits.add("truncated")
            value = value[: self.max_chars] + TRUNCATION_MARK
        return value
