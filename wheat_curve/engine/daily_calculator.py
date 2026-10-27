"""Дневной расчёт для одного дня: VWAP по сделкам, срезы стакана, дневная цена по заявкам.

См. docs/METHODOLOGY_IMPLEMENTATION.md §5-7.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from wheat_curve.domain.emmi import decompose_observation
from wheat_curve.domain.models import Curve, DecomposedObservation, Side, TenorParameters
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes
from wheat_curve.engine.event_processor import DayData, Snapshot


@dataclass
class SnapshotTenorDiagnostic:
    time: time
    bid_depth: Decimal | None
    ask_depth: Decimal | None
    vbid: Decimal
    vask: Decimal
    spread: Decimal | None
    valid: bool
    reason: str
    slice_price: Decimal | None
    buy_observations: list[DecomposedObservation] = field(default_factory=list)
    sell_observations: list[DecomposedObservation] = field(default_factory=list)


@dataclass
class TenorDayDiagnostics:
    tenor: str
    trade_price: Decimal | None = None
    trade_volume: Decimal = Decimal("0")
    trade_observations: list[DecomposedObservation] = field(default_factory=list)
    order_price: Decimal | None = None
    total_snapshots: int = 0
    valid_snapshots: int = 0
    rejected_by_volume: int = 0
    rejected_by_spread: int = 0
    trimmed_low: int = 0
    trimmed_high: int = 0
    snapshots_used_in_mean: int = 0
    snapshot_details: list[SnapshotTenorDiagnostic] = field(default_factory=list)


@dataclass
class DailyCalculationResult:
    day: int
    tenor_diagnostics: dict[str, TenorDayDiagnostics] = field(default_factory=dict)


def compute_trade_prices(
    day_data: DayData,
    calc_date: date,
    reference_curve: Curve,
    parameters: dict[str, TenorParameters],
    tenors: list[str] | None = None,
) -> dict[str, TenorDayDiagnostics]:
    selected = STANDARD_TENORS if tenors is None else tenors
    nodes = standard_nodes(calc_date, selected)
    diag = {t: TenorDayDiagnostics(tenor=t) for t in selected}

    for trade in day_data.trades:
        obs_list = decompose_observation(
            source_id=trade.trade_id,
            maturity=trade.maturity_date,
            price=trade.price,
            quantity=trade.quantity,
            side=None,
            reference_curve=reference_curve,
            standard_nodes_list=nodes,
        )
        for obs in obs_list:
            diag[obs.tenor].trade_observations.append(obs)
            diag[obs.tenor].trade_volume += obs.quantity

    for tenor in selected:
        d = diag[tenor]
        min_trade_volume = parameters[tenor].min_trade_volume
        if d.trade_volume >= min_trade_volume and d.trade_volume > 0:
            numerator = sum((o.price * o.quantity for o in d.trade_observations), Decimal("0"))
            d.trade_price = numerator / d.trade_volume
    return diag


def _decompose_active_orders(
    snapshot: Snapshot, calc_date: date, reference_curve: Curve, tenors: list[str] | None = None
) -> dict[str, tuple[list[DecomposedObservation], list[DecomposedObservation]]]:
    """Раскладывает активные заявки среза по EMMI, группируя по (tenor) -> (buy_obs, sell_obs)."""
    selected = STANDARD_TENORS if tenors is None else tenors
    nodes = standard_nodes(calc_date, selected)
    grouped: dict[str, tuple[list[DecomposedObservation], list[DecomposedObservation]]] = {
        t: ([], []) for t in selected
    }
    for order in snapshot.orders:
        obs_list = decompose_observation(
            source_id=order.order_id,
            maturity=order.maturity_date,
            price=order.price,
            quantity=order.remaining_quantity,
            side=order.side,
            reference_curve=reference_curve,
            standard_nodes_list=nodes,
        )
        for obs in obs_list:
            buy_list, sell_list = grouped[obs.tenor]
            if order.side == Side.BUY:
                buy_list.append(obs)
            else:
                sell_list.append(obs)
    return grouped


def _depth_price(observations: list[DecomposedObservation], min_volume: Decimal, descending: bool) -> tuple[Decimal | None, Decimal]:
    """Накопление объёма до min_volume; возвращает (depth_price|None, total_volume)."""
    sorted_obs = sorted(observations, key=lambda o: o.price, reverse=descending)
    cum = Decimal("0")
    total = sum((o.quantity for o in observations), Decimal("0"))
    for o in sorted_obs:
        cum += o.quantity
        if cum >= min_volume:
            return o.price, total
    return None, total


def compute_order_prices(
    day_data: DayData,
    calc_date: date,
    reference_curve: Curve,
    parameters: dict[str, TenorParameters],
    diag: dict[str, TenorDayDiagnostics],
    tenors: list[str] | None = None,
) -> None:
    """Заполняет order-related поля diag (in-place) по всем срезам дня."""
    selected = STANDARD_TENORS if tenors is None else tenors
    for snapshot in day_data.snapshots:
        grouped = _decompose_active_orders(snapshot, calc_date, reference_curve, selected)
        for tenor in selected:
            d = diag[tenor]
            params = parameters[tenor]
            buy_obs, sell_obs = grouped[tenor]
            vbid = sum((o.quantity for o in buy_obs), Decimal("0"))
            vask = sum((o.quantity for o in sell_obs), Decimal("0"))
            d.total_snapshots += 1

            bid_depth, _ = _depth_price(buy_obs, params.min_order_volume, descending=True)
            ask_depth, _ = _depth_price(sell_obs, params.min_order_volume, descending=False)

            valid = False
            reason = ""
            spread = None
            slice_price = None

            if vbid < params.min_order_volume or vask < params.min_order_volume or bid_depth is None or ask_depth is None:
                reason = "insufficient_depth"
                d.rejected_by_volume += 1
            else:
                mid = (ask_depth + bid_depth) / Decimal("2")
                spread = (ask_depth - bid_depth) / mid if mid != 0 else None
                if spread is None or spread > params.spread_max:
                    reason = "spread_too_wide"
                    d.rejected_by_spread += 1
                else:
                    valid = True
                    reason = "valid"
                    slice_price = mid
                    d.valid_snapshots += 1

            d.snapshot_details.append(
                SnapshotTenorDiagnostic(
                    time=snapshot.time,
                    bid_depth=bid_depth,
                    ask_depth=ask_depth,
                    vbid=vbid,
                    vask=vask,
                    spread=spread,
                    valid=valid,
                    reason=reason,
                    slice_price=slice_price,
                    buy_observations=buy_obs,
                    sell_observations=sell_obs,
                )
            )

    for tenor in selected:
        d = diag[tenor]
        valid_prices = [s.slice_price for s in d.snapshot_details if s.valid and s.slice_price is not None]
        n = len(valid_prices)
        if n < 20:
            d.order_price = None
            continue
        sorted_prices = sorted(valid_prices)
        if n == 20:
            d.order_price = sum(sorted_prices, Decimal("0")) / Decimal(n)
            d.snapshots_used_in_mean = n
        else:
            m = n // 10
            trimmed = sorted_prices[m: n - m] if m > 0 else sorted_prices
            d.trimmed_low = m
            d.trimmed_high = m
            d.snapshots_used_in_mean = len(trimmed)
            d.order_price = sum(trimmed, Decimal("0")) / Decimal(len(trimmed))


def compute_daily(
    day: int,
    day_data: DayData,
    calc_date: date,
    reference_curve: Curve,
    parameters: dict[str, TenorParameters],
    tenors: list[str] | None = None,
) -> DailyCalculationResult:
    diag = compute_trade_prices(day_data, calc_date, reference_curve, parameters, tenors)
    compute_order_prices(day_data, calc_date, reference_curve, parameters, diag, tenors)
    return DailyCalculationResult(day=day, tenor_diagnostics=diag)
