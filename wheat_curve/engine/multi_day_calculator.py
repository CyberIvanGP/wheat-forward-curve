"""Оркестрация расчёта по всем дням: reference curve carry-over, weighting, fallback."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from wheat_curve.domain.models import Curve, PriceSource, RawEvent, TenorParameters, ValidationIssue
from wheat_curve.domain.tenors import STANDARD_TENORS
from wheat_curve.engine.daily_calculator import DailyCalculationResult, TenorDayDiagnostics, compute_daily
from wheat_curve.engine.event_processor import DayData, process_day


@dataclass
class TenorFinalResult:
    tenor: str
    trade_price: Decimal | None
    trade_volume: Decimal
    order_price: Decimal | None
    valid_snapshots: int
    threshold_volume: Decimal
    weight: Decimal
    final_price: Decimal | None
    source: PriceSource
    fallback_streak: int


@dataclass
class DayResult:
    day: int
    calc_date: date
    curve: Curve
    tenor_results: dict[str, TenorFinalResult]
    diagnostics: DailyCalculationResult
    day_data: DayData


def combine_tenor(
    tenor: str,
    d: TenorDayDiagnostics,
    params: TenorParameters,
    prev_final: Decimal | None,
    prev_fallback_streak: int,
) -> TenorFinalResult:
    ptrade = d.trade_price
    porder = d.order_price
    weight = min(d.trade_volume / params.threshold_trade_volume, Decimal("1")) if params.threshold_trade_volume > 0 else Decimal("0")

    if ptrade is not None and porder is not None:
        final = weight * ptrade + (Decimal("1") - weight) * porder
        source = PriceSource.MIXED
        streak = 0
    elif ptrade is not None:
        final = ptrade
        source = PriceSource.TRADES_ONLY
        weight = Decimal("1")
        streak = 0
    elif porder is not None:
        final = porder
        source = PriceSource.ORDERS_ONLY
        weight = Decimal("0")
        streak = 0
    else:
        final = prev_final
        source = PriceSource.FALLBACK_PREVIOUS_DAY if final is not None else PriceSource.NONE
        weight = Decimal("0")
        streak = prev_fallback_streak + 1 if final is not None else prev_fallback_streak

    return TenorFinalResult(
        tenor=tenor,
        trade_price=ptrade,
        trade_volume=d.trade_volume,
        order_price=porder,
        valid_snapshots=d.valid_snapshots,
        threshold_volume=params.threshold_trade_volume,
        weight=weight,
        final_price=final,
        source=source,
        fallback_streak=streak,
    )


def run_multi_day(
    events: list[RawEvent],
    seed_curve: Curve,
    base_date: date,
    num_days: int,
    parameters: dict[str, TenorParameters],
    tenors: list[str] | None = None,
) -> tuple[list[DayResult], list[ValidationIssue]]:
    """Полный расчёт кривой на все дни 1..num_days. Day 0 = seed_curve."""
    selected = STANDARD_TENORS if tenors is None else tenors
    issues: list[ValidationIssue] = []
    events_by_day: dict[int, list[RawEvent]] = {}
    for e in events:
        events_by_day.setdefault(e.day, []).append(e)

    results: list[DayResult] = []
    reference_curve: Curve = dict(seed_curve)
    fallback_streaks: dict[str, int] = {t: 0 for t in selected}
    review_flags: dict[str, bool] = {t: False for t in selected}

    for day in range(1, num_days + 1):
        calc_date = base_date + timedelta(days=day - 1)
        day_events = events_by_day.get(day, [])
        day_data = process_day(day, day_events, issues)
        diagnostics = compute_daily(day, day_data, calc_date, reference_curve, parameters, selected)

        tenor_results: dict[str, TenorFinalResult] = {}
        new_curve: Curve = {}
        for tenor in selected:
            r = combine_tenor(
                tenor,
                diagnostics.tenor_diagnostics[tenor],
                parameters[tenor],
                reference_curve.get(tenor),
                fallback_streaks[tenor],
            )
            tenor_results[tenor] = r
            if r.final_price is not None:
                new_curve[tenor] = r.final_price
            fallback_streaks[tenor] = r.fallback_streak
            if r.fallback_streak >= 2 and not review_flags[tenor]:
                review_flags[tenor] = True
                issues.append(
                    ValidationIssue(
                        "warning",
                        f"METHODOLOGY_REVIEW_REQUIRED: узел {tenor} использует fallback второй день подряд (day={day})",
                        day=day,
                    )
                )
            elif r.fallback_streak < 2:
                review_flags[tenor] = False

        results.append(
            DayResult(
                day=day,
                calc_date=calc_date,
                curve=new_curve,
                tenor_results=tenor_results,
                diagnostics=diagnostics,
                day_data=day_data,
            )
        )
        reference_curve = new_curve if len(new_curve) == len(selected) else {**reference_curve, **new_curve}

    return results, issues
