"""Shared Decimal valuation for NPV and scenario margin; separate fixing/payment dates."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from interest_rate_curve import DiscountCurve
from wheat_curve.domain.curve import interpolate
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes


def decimal_number(value, name, *, positive=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name}: нужна строка, int или Decimal, не float/bool")
    try:
        result = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError(f"{name}: неверное число") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name}: некорректное значение")
    return result


@dataclass(frozen=True)
class ForwardTrade:
    trade_id: str
    quantity: Decimal
    strike: Decimal
    side: int
    fixing_date: date
    payment_date: date

    def __post_init__(self):
        if not isinstance(self.trade_id, str) or not self.trade_id.strip():
            raise ValueError("Нужен непустой trade_id")
        object.__setattr__(self, "quantity", decimal_number(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "strike", decimal_number(self.strike, "strike", positive=True))
        if type(self.side) is not int or self.side not in (-1, 1):
            raise ValueError("side должен быть +1 или -1")
        if type(self.fixing_date) is not date or type(self.payment_date) is not date:
            raise ValueError("Даты сделки должны иметь тип date")
        if self.payment_date < self.fixing_date:
            raise ValueError("Выплата не может быть раньше фиксации")


@dataclass(frozen=True)
class NodeCurves:
    valuation_date: date
    forward: tuple[Decimal, ...]
    zero: tuple[Decimal, ...]

    def __post_init__(self):
        if type(self.valuation_date) is not date:
            raise ValueError("Нужна дата оценки")
        if len(self.forward) != 39 or len(self.zero) != 39:
            raise ValueError("Нужны все 39 узлов зерновой и процентной кривых")
        object.__setattr__(self, "forward", tuple(decimal_number(x, "forward", positive=True) for x in self.forward))
        object.__setattr__(self, "zero", tuple(decimal_number(x, "zero") for x in self.zero))

    @classmethod
    def from_scenario(cls, valuation_date, forward, zero):
        # Explicit float64 -> Decimal boundary; statistical precision is not restored.
        return cls(valuation_date, tuple(Decimal(str(x)) for x in forward),
                   tuple(Decimal(str(x)) for x in zero))

    def forward_price(self, fixing_date: date) -> Decimal:
        if type(fixing_date) is not date:
            raise ValueError("Нужна дата фиксации")
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            result = interpolate(dict(zip(STANDARD_TENORS, self.forward)),
                                 self.valuation_date, fixing_date)
            if result is None:
                raise ValueError("Дата фиксации вне доступной зерновой кривой")
            return result

    def discount_factor(self, payment_date: date) -> Decimal:
        if type(payment_date) is not date or payment_date < self.valuation_date:
            raise ValueError("Некорректная дата выплаты")
        if payment_date == self.valuation_date:
            return Decimal(1)
        nodes = standard_nodes(self.valuation_date)
        if not nodes[0].maturity_date <= payment_date <= nodes[-1].maturity_date:
            raise ValueError("Дата выплаты вне доступной процентной кривой")
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            for i, node in enumerate(nodes):
                if payment_date == node.maturity_date:
                    rate = self.zero[i]
                    break
                if payment_date < node.maturity_date:
                    left = nodes[i - 1].maturity_date
                    weight = Decimal((payment_date - left).days) / Decimal((node.maturity_date - left).days)
                    rate = (1 - weight) * self.zero[i - 1] + weight * self.zero[i]
                    break
            years = Decimal((payment_date - self.valuation_date).days) / Decimal(365)
            return (-rate * years).exp()


def price_forward(
    trade: ForwardTrade, curves: NodeCurves, as_of: date | None = None,
    *, discount_curve: DiscountCurve | None = None,
) -> Decimal:
    day = curves.valuation_date if as_of is None else as_of
    if day != curves.valuation_date or trade.fixing_date <= day:
        raise ValueError("Поддерживается только будущая фиксация на дату переданной кривой")
    discount = curves if discount_curve is None else discount_curve
    if discount.valuation_date != day:
        raise ValueError("Даты зерновой и дисконтной кривых не совпадают")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        factor = discount.discount_factor(trade.payment_date)
        if not isinstance(factor, Decimal) or not factor.is_finite() or factor <= 0:
            raise ValueError("Некорректный дисконт-фактор")
        return trade.side * trade.quantity * factor * (curves.forward_price(trade.fixing_date) - trade.strike)
