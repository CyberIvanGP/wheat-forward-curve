"""Доменные модели: события, заявки, сделки, кривая, параметры."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from enum import Enum

from wheat_curve.domain.tenors import STANDARD_TENORS


class EventType(str, Enum):
    ORDER_NEW = "ORDER_NEW"
    ORDER_MODIFY = "ORDER_MODIFY"
    ORDER_CANCEL = "ORDER_CANCEL"
    ORDER_FILL = "ORDER_FILL"
    TRADE = "TRADE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeType(str, Enum):
    ADDRESS = "ADDRESS"
    NON_ADDRESS = "NON_ADDRESS"


class PriceSource(str, Enum):
    MIXED = "MIXED"
    TRADES_ONLY = "TRADES_ONLY"
    ORDERS_ONLY = "ORDERS_ONLY"
    FALLBACK_PREVIOUS_DAY = "FALLBACK_PREVIOUS_DAY"
    NONE = "NONE"


@dataclass
class RawEvent:
    """Одна строка входного event log."""

    row_index: int
    day: int
    time: time
    event_id: str
    event_type: EventType
    order_id: str | None = None
    trade_id: str | None = None
    side: Side | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    maturity_date: date | None = None
    trade_type: TradeType | None = None
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    comment: str | None = None


@dataclass
class ActiveOrder:
    order_id: str
    side: Side
    price: Decimal
    maturity_date: date
    remaining_quantity: Decimal
    original_quantity: Decimal
    created_day: int


@dataclass
class Trade:
    trade_id: str
    day: int
    time: time
    price: Decimal
    quantity: Decimal
    maturity_date: date
    trade_type: TradeType
    buy_order_id: str | None = None
    sell_order_id: str | None = None


@dataclass
class TenorParameters:
    tenor: str
    min_order_volume: Decimal = Decimal("100")
    min_trade_volume: Decimal = Decimal("100")
    threshold_trade_volume: Decimal = Decimal("500")
    spread_max: Decimal = Decimal("0.02")


def default_parameters() -> dict[str, TenorParameters]:
    return {t: TenorParameters(tenor=t) for t in STANDARD_TENORS}


Curve = dict[str, Decimal]
"""Кривая: словарь {tenor_code: price}; отсутствующие цены не включаются."""


@dataclass
class ValidationIssue:
    level: str  # "error" | "warning"
    message: str
    row_index: int | None = None
    day: int | None = None


@dataclass
class DecomposedObservation:
    tenor: str
    price: Decimal
    quantity: Decimal
    side: Side | None
    source_id: str
    original_maturity: date
    original_price: Decimal
    original_quantity: Decimal
    weight: Decimal
