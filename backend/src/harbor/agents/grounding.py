"""Ground the final cards in evidence from *this* turn's tool results.

The agent's context keeps every earlier ``GenerateStructuredOutput`` call, so a
model can answer a new message by copying the cards it produced before. No
prompt can reliably prevent that: the context itself contains a plausible
answer. The check therefore lives after the reply, where the tool results of
the current turn are known.

A card counts as grounded when a tool returned it during this turn:

* ``product_search`` — the card's ``sku_id`` is a hit's ``selected_sku_id``,
  belongs to a hit's ``product_id``, or its title matches a hit exactly
  (whitespace- and case-insensitive);
* ``task_dispatch`` — S9's child agents report ``verified_product_ids``, which
  were themselves checked against the child's real search results.
"""

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from agentscope.message import Msg, ToolResultState

from harbor.agents.cards import ProductCard
from harbor.features.orders.models import SearchEvidence

_SEARCH_TOOL = "product_search"
_DISPATCH_TOOL = "task_dispatch"


def tool_results_since(context: Sequence[Msg], known_ids: set[str]) -> list[Any]:
    """The ``tool_result`` blocks added after ``known_ids`` was snapshotted.

    True tool results always have an id; a block without one (a stub, or a
    provider that omits it) is treated as new rather than silently ignored.
    """
    return [
        block
        for message in context
        for block in message.get_content_blocks("tool_result")
        if getattr(block, "id", None) not in known_ids
    ]


def ground_cards(cards: Iterable[ProductCard], blocks: Iterable[Any]) -> list[ProductCard]:
    """Keep only the cards this turn's tools actually returned."""
    evidence = _Evidence()
    for block in blocks:
        evidence.add(block)
    return [card for card in cards if evidence.supports(card)]


def search_evidence_from_context(context: Sequence[Msg]) -> list[SearchEvidence]:
    """Price observations from this session's successful ``product_search`` calls.

    Later observations win: a SKU re-searched after a price change must price a
    draft at the newest value, not the first one the model saw. S7 compression
    may have removed these messages from context — the durable evidence table is
    tried first — but whatever is still here beats making the model recite a
    price.
    """
    latest: dict[str, SearchEvidence] = {}
    for message in context:
        for block in message.get_content_blocks("tool_result"):
            if getattr(block, "name", None) != _SEARCH_TOOL:
                continue
            if getattr(block, "state", None) != ToolResultState.SUCCESS:
                continue
            for text in _text_blocks(block):
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                for hit in payload.get("hits") or []:
                    evidence = _search_hit_evidence(hit)
                    if evidence is not None:
                        latest[evidence.sku_id] = evidence
    return list(latest.values())


def dispatch_prices_from_context(context: Sequence[Msg]) -> frozenset[tuple[str, Decimal]]:
    """Prices S9's child agents verified against their own search results.

    The parent quotes these after a dispatch, and its context no longer holds
    the raw ``product_search`` hits the children saw, so the verified refs are
    the only durable evidence for those numbers.
    """
    prices: set[tuple[str, Decimal]] = set()
    for message in context:
        for block in message.get_content_blocks("tool_result"):
            if getattr(block, "name", None) != _DISPATCH_TOOL:
                continue
            if getattr(block, "state", None) != ToolResultState.SUCCESS:
                continue
            for text in _text_blocks(block):
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                for result in payload.get("results") or []:
                    if not isinstance(result, dict) or not result.get("ok"):
                        continue
                    for ref in result.get("evidence_refs") or []:
                        if (
                            not isinstance(ref, dict)
                            or not ref.get("verified")
                            or ref.get("field") != "price_major"
                        ):
                            continue
                        try:
                            amount = Decimal(str(ref.get("value")))
                        except ArithmeticError:
                            continue
                        prices.add((str(ref.get("currency") or "CNY"), amount))
    return frozenset(prices)


def _search_hit_evidence(hit: Any) -> SearchEvidence | None:
    if not isinstance(hit, dict):
        return None
    sku_id = hit.get("selected_sku_id")
    price = hit.get("price_major")
    currency = hit.get("currency")
    if not sku_id or price is None or not currency:
        return None
    try:
        unit_price = Decimal(str(price))
    except ArithmeticError:
        return None
    return SearchEvidence(
        product_id=hit.get("product_id") or None,
        sku_id=str(sku_id),
        title=str(hit.get("title") or ""),
        unit_price=unit_price,
        currency=str(currency),
    )


def _text_blocks(block: Any) -> list[str]:
    output = getattr(block, "output", None)
    if isinstance(output, str):
        return [output]
    texts: list[str] = []
    for item in output or []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return texts


def _normalized_title(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


class _Evidence:
    """The product ids, skus, and titles this turn's tools returned."""

    def __init__(self) -> None:
        self.product_ids: set[str] = set()
        self.skus: set[str] = set()
        self.titles: set[str] = set()

    def add(self, block: Any) -> None:
        name = getattr(block, "name", None)
        if name not in (_SEARCH_TOOL, _DISPATCH_TOOL):
            return
        if getattr(block, "state", None) != ToolResultState.SUCCESS:
            return
        for text in _text_blocks(block):
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                self._add_payload(name, payload)

    def _add_payload(self, name: str, payload: dict) -> None:
        if name == _SEARCH_TOOL:
            for hit in payload.get("hits") or []:
                if not isinstance(hit, dict):
                    continue
                if hit.get("product_id"):
                    self.product_ids.add(str(hit["product_id"]))
                if hit.get("selected_sku_id"):
                    self.skus.add(str(hit["selected_sku_id"]))
                if hit.get("title"):
                    self.titles.add(_normalized_title(hit["title"]))
            return
        for result in payload.get("results") or []:
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            for product_id in result.get("verified_product_ids") or []:
                self.product_ids.add(str(product_id))
            for ref in result.get("evidence_refs") or []:
                if (
                    isinstance(ref, dict)
                    and ref.get("verified")
                    and ref.get("field") == "title"
                    and ref.get("value")
                ):
                    self.titles.add(_normalized_title(ref["value"]))

    def supports(self, card: ProductCard) -> bool:
        sku = card.sku_id or ""
        if sku and sku in self.skus:
            return True
        if sku and any(
            sku == product_id or sku.startswith(f"{product_id}-") for product_id in self.product_ids
        ):
            return True
        return _normalized_title(card.title) in self.titles
