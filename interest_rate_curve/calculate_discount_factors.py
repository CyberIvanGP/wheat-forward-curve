"""Source-independent interface for discount factors, not quoted interest rates."""

from datetime import date
from decimal import Decimal
from typing import Iterable, Protocol


class DiscountCurve(Protocol):
    valuation_date: date

    def discount_factor(self, payment_date: date) -> Decimal: ...


def calculate_discount_factors(
    curve: DiscountCurve, payment_dates: Iterable[date],
) -> dict[date, Decimal]:
    """Return date -> Decimal D; no network, files or source-specific assumptions."""
    if type(curve.valuation_date) is not date:
        raise ValueError("valuation_date должна иметь тип datetime.date")
    dates = list(payment_dates)
    for payment_date in dates:
        if type(payment_date) is not date:
            raise ValueError("Даты платежей должны иметь тип datetime.date")
        if payment_date < curve.valuation_date:
            raise ValueError("Дата платежа не может быть раньше даты оценки")
    result = {}
    for payment_date in dates:
        if payment_date in result:
            continue
        factor = curve.discount_factor(payment_date)
        if not isinstance(factor, Decimal) or not factor.is_finite() or factor <= 0:
            raise ValueError("Источник должен вернуть положительный конечный Decimal D")
        if payment_date == curve.valuation_date and factor != 1:
            raise ValueError("Источник должен вернуть D(d,d) = 1")
        result[payment_date] = factor
    return result
