"""L4 + structural assertions on the final reply (spec §7.3, §8).

``L3`` guards what other people's text does to the model; ``L4`` guards what
the model's text does to the buyer. A reply that fails a rule is *repaired*,
never rejected: a turn with no text is a 502, and the buyer gets nothing at
all. Every repair uses a fixed Chinese string so the behaviour is assertable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

FALLBACK_TEXT = "抱歉，我暂时没有整理出可用的答复，请再说一次需求。"
PRICE_PLACEHOLDER = "〔价格以确认页为准〕"
COMPLETION_UNCONFIRMED = "订单尚未创建，需要你在订单确认页点击确认"
CANCEL_UNCONFIRMED = "订单尚未取消，请先在订单页发起取消"
JSON_BLOCK_INVALID = "〔内含无法展示的数据〕"
TRUNCATION_MARK = "…"

_MARKER = re.compile(
    r"<\|im_(?:start|end)\|>"
    r"|<tool_call>.*?</tool_call>"
    r"|<tool_result>.*?</tool_result>"
    r"|</?(?:tool_call|tool_result|function|system)>",
    re.IGNORECASE | re.DOTALL,
)
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n?```", re.IGNORECASE | re.DOTALL)
_INTERNAL = (
    "Traceback",
    ".py:",
    "harbor.",
    "sqlite3.",
    "site-packages",
    "draft_id=",
)
_PRICE = re.compile(
    r"[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*元|CNY\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_CONFIRMED = re.compile(r"已下单|订单已创建|已为你下单|下单成功|已支付")
_CANCELLED = re.compile(r"已取消|已为你取消|取消成功")
_OVERCLAIM = re.compile(
    r"(?:保证|一定|绝对)[^。！？!?\n]{0,15}"
    r"(?:价格|有货|现货|明天|当天|到货|正品|便宜)"
)


@dataclass(frozen=True)
class GroundingFacts:
    """What actually happened this turn, as the reply is allowed to claim it."""

    prices: frozenset[tuple[str, Decimal]]
    has_confirmed_order: bool = False
    has_cancelled_order: bool = False


@dataclass(frozen=True)
class OutputResult:
    text: str
    hits: tuple[str, ...] = ()


class OutputGuard:
    def __init__(self, max_chars: int = 4000) -> None:
        self.max_chars = max_chars

    def check(self, text: str, facts: GroundingFacts) -> OutputResult:
        hits: set[str] = set()

        if not text or not text.strip():
            return OutputResult(FALLBACK_TEXT, ("empty",))

        text = self._strip_markers(text, hits)
        text = self._check_json_blocks(text, hits)
        if not text.strip():
            hits.add("empty")
            text = FALLBACK_TEXT

        text = self._truncate(text, hits)
        text = self._remove_internal(text, hits)
        text = self._ground_prices(text, facts, hits)
        text = self._ground_completions(text, facts, hits)
        text = self._soften_overclaims(text, hits)

        if not text.strip():
            text = FALLBACK_TEXT
        return OutputResult(text, tuple(sorted(hits)))

    # ---------------------------------------------------------------- rules

    def _strip_markers(self, text: str, hits: set[str]) -> str:
        if not _MARKER.search(text):
            return text
        hits.add("unrendered_marker")
        return _MARKER.sub("", text)

    def _check_json_blocks(self, text: str, hits: set[str]) -> str:
        def replace(match: re.Match[str]) -> str:
            try:
                json.loads(match.group(1))
            except ValueError:
                hits.add("json_block_invalid")
                return JSON_BLOCK_INVALID
            return match.group(0)

        return _JSON_BLOCK.sub(replace, text)

    def _truncate(self, text: str, hits: set[str]) -> str:
        if len(text) <= self.max_chars:
            return text
        hits.add("too_long")
        return text[: self.max_chars] + TRUNCATION_MARK

    def _remove_internal(self, text: str, hits: set[str]) -> str:
        if not any(marker in text for marker in _INTERNAL):
            return text
        hits.add("internal_id")
        parts = re.split(r"([。！？!?\n])", text)
        kept: list[str] = []
        for index in range(0, len(parts), 2):
            sentence = parts[index]
            if any(marker in sentence for marker in _INTERNAL):
                continue
            kept.append(sentence)
            if index + 1 < len(parts):
                kept.append(parts[index + 1])
        return "".join(kept).strip()

    def _ground_prices(
        self, text: str, facts: GroundingFacts, hits: set[str]
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            amount = _amount(match.group(0))
            if amount is None:
                return match.group(0)
            if _is_grounded(amount, facts.prices):
                return match.group(0)
            hits.add("ungrounded_price")
            return PRICE_PLACEHOLDER

        return _PRICE.sub(replace, text)

    def _ground_completions(
        self, text: str, facts: GroundingFacts, hits: set[str]
    ) -> str:
        if not facts.has_confirmed_order and _CONFIRMED.search(text):
            hits.add("completion_claim")
            text = _CONFIRMED.sub(COMPLETION_UNCONFIRMED, text)
        if not facts.has_cancelled_order and _CANCELLED.search(text):
            hits.add("completion_claim")
            text = _CANCELLED.sub(CANCEL_UNCONFIRMED, text)
        return text

    def _soften_overclaims(self, text: str, hits: set[str]) -> str:
        if not _OVERCLAIM.search(text):
            return text
        hits.add("overclaim")
        return _OVERCLAIM.sub("以商品页和确认页为准", text)


def _amount(rendered: str) -> Decimal | None:
    digits = re.search(r"\d+(?:\.\d+)?", rendered)
    if digits is None:
        return None
    try:
        return Decimal(digits.group(0))
    except InvalidOperation:  # pragma: no cover - regex guarantees a number
        return None


def _is_grounded(amount: Decimal, prices: frozenset[tuple[str, Decimal]]) -> bool:
    return any(
        currency == "CNY" and abs(price - amount) <= Decimal("0.01")
        for currency, price in prices
    )
