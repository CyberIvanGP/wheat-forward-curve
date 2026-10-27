"""Обработка event log по дням: сортировка событий, восстановление стакана, срезы, сделки."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from wheat_curve.domain.dates import is_within_trading_window, snapshot_times
from wheat_curve.domain.models import ActiveOrder, EventType, RawEvent, Trade, TradeType, ValidationIssue
from wheat_curve.engine.order_book import OrderBook


@dataclass
class Snapshot:
    time: time
    orders: list[ActiveOrder]


@dataclass
class DayData:
    day: int
    trades: list[Trade] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    events_applied: int = 0
    order_new_count: int = 0
    order_modify_count: int = 0
    order_cancel_count: int = 0
    order_fill_count: int = 0


def sort_events(events: list[RawEvent]) -> list[RawEvent]:
    """Сортировка: day, time, original row order (§11 methodology)."""
    return sorted(events, key=lambda e: (e.day, e.time, e.row_index))


def process_day(day: int, day_events: list[RawEvent], issues: list[ValidationIssue]) -> DayData:
    """Восстанавливает стакан и строит все 3-минутные срезы + сделки для одного дня.

    Событие в момент среза применяется до построения этого среза (§6 methodology).
    """
    book = OrderBook()
    result = DayData(day=day)
    ordered_events = sort_events(day_events)

    filtered_events: list[RawEvent] = []
    for e in ordered_events:
        if not is_within_trading_window(e.time):
            issues.append(ValidationIssue("warning", f"Событие вне торгового окна 10:00-18:00 (day={day}, time={e.time}) — исключено из расчёта", e.row_index, day))
            continue
        filtered_events.append(e)

    times = snapshot_times()
    event_idx = 0
    n_events = len(filtered_events)

    for snap_time in times:
        while event_idx < n_events and filtered_events[event_idx].time <= snap_time:
            e = filtered_events[event_idx]
            if e.event_type == EventType.TRADE:
                _apply_trade(e, result, issues)
            else:
                book.apply(e, issues)
                _count_event(e, result)
            result.events_applied += 1
            event_idx += 1
        result.snapshots.append(Snapshot(time=snap_time, orders=book.snapshot()))

    # события после 18:00 в окне не попадают (были бы отфильтрованы is_within_trading_window),
    # но на случай событий ровно после последнего среза (не должно случаться) — применим остаток.
    while event_idx < n_events:
        e = filtered_events[event_idx]
        if e.event_type == EventType.TRADE:
            _apply_trade(e, result, issues)
        else:
            book.apply(e, issues)
            _count_event(e, result)
        result.events_applied += 1
        event_idx += 1

    return result


def _count_event(e: RawEvent, result: DayData) -> None:
    if e.event_type == EventType.ORDER_NEW:
        result.order_new_count += 1
    elif e.event_type == EventType.ORDER_MODIFY:
        result.order_modify_count += 1
    elif e.event_type == EventType.ORDER_CANCEL:
        result.order_cancel_count += 1
    elif e.event_type == EventType.ORDER_FILL:
        result.order_fill_count += 1


def _apply_trade(e: RawEvent, result: DayData, issues: list[ValidationIssue]) -> None:
    if e.price is None or e.quantity is None or e.maturity_date is None or e.trade_type is None:
        issues.append(ValidationIssue("error", f"TRADE: неполные обязательные поля (trade_id={e.trade_id})", e.row_index, e.day))
        return
    if e.price <= 0:
        issues.append(ValidationIssue("error", f"TRADE {e.trade_id}: цена должна быть положительной", e.row_index, e.day))
        return
    if e.quantity <= 0:
        issues.append(ValidationIssue("error", f"TRADE {e.trade_id}: объём должен быть положительным", e.row_index, e.day))
        return
    result.trades.append(
        Trade(
            trade_id=e.trade_id or e.event_id,
            day=e.day,
            time=e.time,
            price=e.price,
            quantity=e.quantity,
            maturity_date=e.maturity_date,
            trade_type=e.trade_type,
            buy_order_id=e.buy_order_id,
            sell_order_id=e.sell_order_id,
        )
    )
