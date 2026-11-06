"""S10: order service — the only writer of prices, and the model-facing boundary.

The model proposes *what* to buy (product, SKU, quantity). It never proposes a
price: every line is priced from retrieval evidence, converted to the order
currency, and quantized to that currency's minor unit. A hit that the model
invented, or one whose product id disagrees with the line, is rejected rather
than priced.

Evidence resolution order is fixed — durable table, this turn's search context,
then the S7 compression appendix — because each source is strictly weaker than
the one before it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from harbor.features.catalog.models.money import quantize_major
from harbor.features.catalog.search import fx
from harbor.features.orders.evidence import EvidenceRecord, SqliteEvidenceStore
from harbor.features.orders.models import (
    CancellationRequest,
    NewDraftItem,
    Order,
    OrderDraft,
    SearchEvidence,
)
from harbor.features.orders.store import (
    CancellationNotPending,
    DraftNotPending,
    NoCancellationRequest,
    OrderAlreadyCancelled,
    OrderStoreError,
    SqliteOrderStore,
)

MAX_ITEMS = 20
MAX_QUANTITY = 99

T = TypeVar("T")


@dataclass(frozen=True)
class DraftRequestItem:
    """What the model asked for: an identity and a quantity, never a price."""

    product_id: str
    sku_id: str
    quantity: int = 1


@dataclass(frozen=True)
class DraftCreation:
    draft: OrderDraft
    replayed: bool


class OrderValidationError(Exception):
    """A draft request the service refused, with a machine-readable code."""

    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        self.code = code
        self.message = message
        self.line = line
        super().__init__(message)


class OrderServiceError(Exception):
    """Base class for application-action failures that map to HTTP errors."""


class OrderNotFound(OrderServiceError):
    """The subject does not exist, or does not belong to this buyer."""


class OrderConflict(OrderServiceError):
    """The subject is in a state that forbids this action."""

    def __init__(self, current_status: str | None, message: str | None = None) -> None:
        self.current_status = current_status
        super().__init__(message or f"status is {current_status}")


class OrderStorageError(OrderServiceError):
    """The backing store failed for a reason other than a state conflict."""


@dataclass(frozen=True)
class _ResolvedEvidence:
    title: str
    unit_price: Decimal
    currency: str


class OrderService:
    """Validates, prices, and stores order drafts."""

    def __init__(self, store: SqliteOrderStore, evidence: SqliteEvidenceStore) -> None:
        self._store = store
        self._evidence = evidence

    async def create_draft(
        self,
        *,
        buyer_id: str,
        shopping_session_id: str,
        items: Sequence[DraftRequestItem],
        currency: str = "CNY",
        context_evidence: Sequence[SearchEvidence] = (),
        appendix_evidence: Sequence[SearchEvidence] = (),
    ) -> DraftCreation:
        self._validate_identity(buyer_id, shopping_session_id)
        if currency not in fx.CNY_PER_UNIT:
            raise OrderValidationError("invalid_currency", f"unsupported currency {currency!r}")
        if not items:
            raise OrderValidationError("empty_items", "草稿至少需要一行商品")
        if len(items) > MAX_ITEMS:
            raise OrderValidationError("too_many_items", f"草稿最多 {MAX_ITEMS} 行")

        seen: set[str] = set()
        for line, item in enumerate(items, start=1):
            self._validate_item(item, line)
            if item.sku_id in seen:
                raise OrderValidationError("duplicate_sku", f"SKU {item.sku_id} 重复", line=line)
            seen.add(item.sku_id)

        durable = await self._evidence.find_many(
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
            sku_ids=[item.sku_id for item in items],
        )
        context_by_sku = {hit.sku_id: hit for hit in context_evidence}
        appendix_by_sku = {hit.sku_id: hit for hit in appendix_evidence}

        priced: list[NewDraftItem] = []
        for line, item in enumerate(items, start=1):
            evidence = self._resolve_evidence(
                item,
                line=line,
                durable=durable,
                context_by_sku=context_by_sku,
                appendix_by_sku=appendix_by_sku,
            )
            converted = fx.convert(evidence.unit_price, evidence.currency, currency)
            priced.append(
                NewDraftItem(
                    product_id=item.product_id,
                    sku_id=item.sku_id,
                    title=evidence.title,
                    quantity=item.quantity,
                    unit_price=quantize_major(converted, currency),
                    currency=currency,
                    evidence_unit_price=evidence.unit_price,
                    evidence_currency=evidence.currency,
                )
            )

        draft, replayed = await self._store.create_draft(
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
            currency=currency,
            items=priced,
        )
        return DraftCreation(draft=draft, replayed=replayed)

    async def get_pending_draft(
        self, *, buyer_id: str, shopping_session_id: str
    ) -> OrderDraft | None:
        return await self._store.get_pending_draft(
            buyer_id=buyer_id, shopping_session_id=shopping_session_id
        )

    async def record_search_hits(
        self,
        *,
        buyer_id: str,
        shopping_session_id: str,
        evidence: Sequence[SearchEvidence],
    ) -> None:
        """Persist observations best-effort; callers must not let failures escape."""
        if not evidence:
            return
        observed_at = datetime.now(UTC).isoformat()
        records = [
            EvidenceRecord(
                buyer_id=buyer_id,
                shopping_session_id=shopping_session_id,
                sku_id=hit.sku_id,
                product_id=hit.product_id,
                title=hit.title,
                unit_price=hit.unit_price,
                currency=hit.currency,
                observed_at=observed_at,
            )
            for hit in evidence
        ]
        await self._evidence.record(
            buyer_id=buyer_id,
            shopping_session_id=shopping_session_id,
            records=records,
        )

    # -- application actions ----------------------------------------------
    #
    # These are thin translations. The state machines live in the store, so a
    # buyer-facing endpoint and an internal caller cannot disagree about what
    # "confirm" does; the service only decides which exception the caller sees.

    async def confirm_draft(self, *, draft_id: str, buyer_id: str) -> tuple[Order, bool]:
        result = await self._translate(
            self._store.confirm_draft(draft_id=draft_id, buyer_id=buyer_id)
        )
        if result is None:
            raise OrderNotFound(f"draft {draft_id} not found")
        return result

    async def cancel_draft(self, *, draft_id: str, buyer_id: str) -> tuple[OrderDraft, bool]:
        result = await self._translate(
            self._store.cancel_draft(draft_id=draft_id, buyer_id=buyer_id)
        )
        if result is None:
            raise OrderNotFound(f"draft {draft_id} not found")
        return result

    async def get_order(
        self, *, order_id: str, buyer_id: str
    ) -> tuple[Order, CancellationRequest | None]:
        order = await self._translate(self._store.get_order(order_id=order_id, buyer_id=buyer_id))
        if order is None:
            raise OrderNotFound(f"order {order_id} not found")
        request = await self._translate(self._store.latest_cancellation(order_id=order_id))
        return order, request

    async def request_cancellation(
        self,
        *,
        order_id: str,
        buyer_id: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> tuple[CancellationRequest, bool]:
        result = await self._translate(
            self._store.request_cancellation(
                order_id=order_id,
                buyer_id=buyer_id,
                idempotency_key=idempotency_key,
                reason=reason,
            )
        )
        if result is None:
            raise OrderNotFound(f"order {order_id} not found")
        return result

    async def confirm_cancellation(
        self, *, order_id: str, buyer_id: str
    ) -> tuple[Order, CancellationRequest, bool]:
        result = await self._translate(
            self._store.confirm_cancellation(order_id=order_id, buyer_id=buyer_id)
        )
        if result is None:
            raise OrderNotFound(f"order {order_id} not found")
        return result

    async def withdraw_cancellation(
        self, *, order_id: str, buyer_id: str
    ) -> tuple[CancellationRequest, bool]:
        result = await self._translate(
            self._store.withdraw_cancellation(order_id=order_id, buyer_id=buyer_id)
        )
        if result is None:
            raise OrderNotFound(f"order {order_id} has no cancellation request")
        return result

    @staticmethod
    async def _translate(awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except (DraftNotPending, CancellationNotPending) as exc:
            raise OrderConflict(exc.current_status, str(exc)) from exc
        except OrderAlreadyCancelled as exc:
            raise OrderConflict("cancelled", str(exc)) from exc
        except NoCancellationRequest as exc:
            raise OrderConflict(None, str(exc)) from exc
        except OrderStoreError as exc:
            raise OrderStorageError(str(exc)) from exc

    # -- validation -------------------------------------------------------

    @staticmethod
    def _validate_identity(buyer_id: str, shopping_session_id: str) -> None:
        if not buyer_id or not buyer_id.strip():
            raise OrderValidationError("missing_identity", "缺少 buyer_id")
        if not shopping_session_id or not shopping_session_id.strip():
            raise OrderValidationError("missing_identity", "缺少 shopping_session_id")

    @staticmethod
    def _validate_item(item: DraftRequestItem, line: int) -> None:
        if not item.product_id or not item.product_id.strip():
            raise OrderValidationError("invalid_item", "缺少 product_id", line=line)
        if not item.sku_id or not item.sku_id.strip():
            raise OrderValidationError("invalid_item", "缺少 sku_id", line=line)
        valid_quantity = (
            isinstance(item.quantity, int)
            and not isinstance(item.quantity, bool)
            and 1 <= item.quantity <= MAX_QUANTITY
        )
        if not valid_quantity:
            raise OrderValidationError(
                "invalid_quantity",
                f"数量必须在 1–{MAX_QUANTITY} 之间",
                line=line,
            )

    def _resolve_evidence(
        self,
        item: DraftRequestItem,
        *,
        line: int,
        durable: dict[str, EvidenceRecord],
        context_by_sku: dict[str, SearchEvidence],
        appendix_by_sku: dict[str, SearchEvidence],
    ) -> _ResolvedEvidence:
        stored = durable.get(item.sku_id)
        if stored is not None:
            self._check_product_id(stored.product_id, item, line)
            return _ResolvedEvidence(
                title=stored.title,
                unit_price=stored.unit_price,
                currency=stored.currency,
            )

        hit = context_by_sku.get(item.sku_id) or appendix_by_sku.get(item.sku_id)
        if hit is None:
            raise OrderValidationError(
                "evidence_missing", f"未找到 SKU {item.sku_id} 的检索证据", line=line
            )
        self._check_product_id(hit.product_id, item, line)
        return _ResolvedEvidence(
            title=hit.title,
            unit_price=hit.unit_price,
            currency=hit.currency,
        )

    @staticmethod
    def _check_product_id(product_id: str | None, item: DraftRequestItem, line: int) -> None:
        # ``None`` means the S7 compression appendix, which does not keep the
        # product id; there is nothing to cross-check against.
        if product_id is not None and product_id != item.product_id:
            raise OrderValidationError(
                "evidence_mismatch",
                f"证据商品 {product_id} 与行项目 {item.product_id} 不一致",
                line=line,
            )
