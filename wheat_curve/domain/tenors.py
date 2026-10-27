"""Стандартные узлы форвардной кривой зерна."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from wheat_curve.domain.dates import add_days, add_months, add_years

STANDARD_TENORS: list[str] = ["1D", "1W", "2W"] + [f"{m}M" for m in range(1, 37)]


def tenor_to_date(base: date, tenor: str) -> date:
    """Календарная дата экспирации срока `tenor`, отсчитанного от `base`."""
    if tenor == "1D":
        return add_days(base, 1)
    if tenor == "1W":
        return add_days(base, 7)
    if tenor == "2W":
        return add_days(base, 14)
    if tenor == "1M":
        return add_months(base, 1)
    if tenor == "2M":
        return add_months(base, 2)
    if tenor == "3M":
        return add_months(base, 3)
    if tenor == "6M":
        return add_months(base, 6)
    if tenor == "1Y":
        return add_years(base, 1)
    if tenor == "2Y":
        return add_years(base, 2)
    if tenor == "3Y":
        return add_years(base, 3)
    if tenor.endswith("M") and tenor[:-1].isdigit() and 1 <= int(tenor[:-1]) <= 36:
        return add_months(base, int(tenor[:-1]))
    raise ValueError(f"Unknown tenor: {tenor}")


@dataclass(frozen=True)
class StandardNode:
    tenor: str
    maturity_date: date


def standard_nodes(base: date, tenors: list[str] | None = None) -> list[StandardNode]:
    """Стандартные узлы кривой на дату расчёта `base`, отсортированные по сроку."""
    return list(_cached_nodes(base, tuple(STANDARD_TENORS if tenors is None else tenors)))


@lru_cache(maxsize=512)
def _cached_nodes(base: date, tenors: tuple[str, ...]) -> tuple[StandardNode, ...]:
    return tuple(StandardNode(t, tenor_to_date(base, t)) for t in tenors)


def is_maturity_beyond_3y(base: date, maturity: date) -> bool:
    return maturity > tenor_to_date(base, "3Y")
