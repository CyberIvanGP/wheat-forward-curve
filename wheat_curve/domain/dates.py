"""Календарные операции: добавление недель/месяцев/лет с учётом длины месяца."""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def add_months(d: date, months: int) -> date:
    """d + N календарных месяцев. Если такого дня в месяце нет — последний день месяца.

    Например: 31 Jan + 1M -> последний день февраля. 31 Mar + 1M -> 30 Apr.
    """
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def add_years(d: date, years: int) -> date:
    """d + N лет. 29 Feb + 1Y -> 28 Feb, если целевой год не високосный."""
    return add_months(d, years * 12)


def days_between(d1: date, d2: date) -> int:
    """Фактическое число календарных дней от d1 до d2 (может быть отрицательным)."""
    return (d2 - d1).days


TRADING_WINDOW_START = time(10, 0, 0)
TRADING_WINDOW_END = time(18, 0, 0)


def is_within_trading_window(t: time) -> bool:
    return TRADING_WINDOW_START <= t <= TRADING_WINDOW_END


def snapshot_times() -> list[time]:
    """Срезы на концах трёхминутных интервалов: 10:03, ..., 18:00 (160)."""
    out = []
    cur = datetime(2000, 1, 1, 10, 3, 0)
    end = datetime(2000, 1, 1, 18, 0, 0)
    while cur <= end:
        out.append(cur.time())
        cur += timedelta(minutes=3)
    return out
