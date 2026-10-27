"""Восстановление активного стакана лимитных заявок по event log."""
from __future__ import annotations

from decimal import Decimal

from wheat_curve.domain.models import ActiveOrder, EventType, RawEvent, Side, ValidationIssue


class OrderBook:
    """Хранит активные NON_ADDRESS лимитные заявки, индексированные по order_id."""

    def __init__(self) -> None:
        self.active: dict[str, ActiveOrder] = {}
        self._seen_order_ids: set[str] = set()
        self._cancelled_order_ids: set[str] = set()

    def apply(self, event: RawEvent, issues: list[ValidationIssue]) -> None:
        if event.event_type == EventType.ORDER_NEW:
            self._apply_new(event, issues)
        elif event.event_type == EventType.ORDER_MODIFY:
            self._apply_modify(event, issues)
        elif event.event_type == EventType.ORDER_CANCEL:
            self._apply_cancel(event, issues)
        elif event.event_type == EventType.ORDER_FILL:
            self._apply_fill(event, issues)
        # TRADE обрабатывается отдельно (trades.py); если trade несёт buy_order_id/sell_order_id,
        # применение к стакану всё равно требует явных ORDER_FILL/CANCEL/MODIFY событий (§6 methodology).

    def _apply_new(self, event: RawEvent, issues: list[ValidationIssue]) -> None:
        oid = event.order_id
        if not oid or event.price is None or event.quantity is None or event.maturity_date is None or event.side is None:
            issues.append(ValidationIssue("error", f"ORDER_NEW: неполные данные заявки (order_id={oid})", event.row_index, event.day))
            return
        if oid in self._seen_order_ids:
            issues.append(ValidationIssue("error", f"Дублирующийся order_id при создании заявки: {oid}", event.row_index, event.day))
            return
        if event.price <= 0:
            issues.append(ValidationIssue("error", f"Заявка {oid}: цена должна быть положительной", event.row_index, event.day))
            return
        if event.quantity <= 0:
            issues.append(ValidationIssue("error", f"Заявка {oid}: объём должен быть положительным", event.row_index, event.day))
            return
        self._seen_order_ids.add(oid)
        self.active[oid] = ActiveOrder(
            order_id=oid,
            side=event.side,
            price=event.price,
            maturity_date=event.maturity_date,
            remaining_quantity=event.quantity,
            original_quantity=event.quantity,
            created_day=event.day,
        )

    def _apply_modify(self, event: RawEvent, issues: list[ValidationIssue]) -> None:
        oid = event.order_id
        if not oid or oid not in self.active:
            issues.append(ValidationIssue("error", f"ORDER_MODIFY: заявка не найдена в активном стакане: {oid}", event.row_index, event.day))
            return
        order = self.active[oid]
        if event.price is not None:
            if event.price <= 0:
                issues.append(ValidationIssue("error", f"Заявка {oid}: цена должна быть положительной", event.row_index, event.day))
                return
            order.price = event.price
        if event.quantity is not None:
            if event.quantity <= 0:
                issues.append(ValidationIssue("error", f"Заявка {oid}: объём должен быть положительным", event.row_index, event.day))
                return
            order.remaining_quantity = event.quantity
            order.original_quantity = event.quantity
        if event.maturity_date is not None:
            order.maturity_date = event.maturity_date

    def _apply_cancel(self, event: RawEvent, issues: list[ValidationIssue]) -> None:
        oid = event.order_id
        if not oid or oid not in self.active:
            issues.append(ValidationIssue("warning", f"Повторная или некорректная отмена заявки: {oid}", event.row_index, event.day))
            return
        del self.active[oid]
        self._cancelled_order_ids.add(oid)

    def _apply_fill(self, event: RawEvent, issues: list[ValidationIssue]) -> None:
        oid = event.order_id
        if not oid or oid not in self.active:
            issues.append(ValidationIssue("error", f"ORDER_FILL: заявка не найдена в активном стакане: {oid}", event.row_index, event.day))
            return
        if event.quantity is None or event.quantity <= 0:
            issues.append(ValidationIssue("error", f"ORDER_FILL: некорректный объём исполнения для {oid}", event.row_index, event.day))
            return
        order = self.active[oid]
        order.remaining_quantity -= event.quantity
        if order.remaining_quantity <= 0:
            del self.active[oid]

    def snapshot(self) -> list[ActiveOrder]:
        """Копия текущих активных заявок (для среза стакана)."""
        return [
            ActiveOrder(
                order_id=o.order_id,
                side=o.side,
                price=o.price,
                maturity_date=o.maturity_date,
                remaining_quantity=o.remaining_quantity,
                original_quantity=o.original_quantity,
                created_day=o.created_day,
            )
            for o in self.active.values()
        ]
