"""Итоговая кривая: log-linear интерполяция по календарным дням."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from wheat_curve.domain.models import Curve
from wheat_curve.domain.tenors import standard_nodes


def log_linear_price(left: Decimal, right: Decimal, weight: Decimal) -> Decimal:
    """Линейный по времени ln(P); без преобразований в float."""
    if not left.is_finite() or not right.is_finite() or left <= 0 or right <= 0:
        raise ValueError('Log-linear интерполяция требует положительных конечных цен')
    if not weight.is_finite() or not Decimal('0') <= weight <= Decimal('1'):
        raise ValueError('Вес интерполяции должен быть в диапазоне [0, 1]')
    if weight == 0 or left == right:
        return left
    if weight == 1:
        return right
    return ((Decimal('1') - weight) * left.ln() + weight * right.ln()).exp()


def interpolate(
    curve: Curve, calc_date: date, target_date: date, tenors: list[str] | None = None,
) -> Decimal | None:
    """Log-linear интерполяция на произвольную дату 1D<=T<=3Y (§9 methodology).

    Возвращает None, если target_date вне диапазона [1D, 3Y] от calc_date.
    """
    nodes = standard_nodes(calc_date, tenors)
    if target_date < nodes[0].maturity_date or target_date > nodes[-1].maturity_date:
        return None
    for n in nodes:
        if n.maturity_date == target_date:
            return curve.get(n.tenor)
    left = None
    right = None
    for n in nodes:
        if n.maturity_date < target_date:
            left = n
        if n.maturity_date > target_date and right is None:
            right = n
            break
    if left is None or right is None:
        return None
    f_left = curve.get(left.tenor)
    f_right = curve.get(right.tenor)
    if f_left is None or f_right is None:
        return None
    total_days = (right.maturity_date - left.maturity_date).days
    part_days = (target_date - left.maturity_date).days
    frac = Decimal(part_days) / Decimal(total_days)
    return log_linear_price(f_left, f_right, frac)
