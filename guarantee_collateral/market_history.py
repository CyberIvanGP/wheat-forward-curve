"""Real curve archive: fixed tenor labels, actual calendar maturities, no filling."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import numpy as np

from financial_contracts.forward_valuation import decimal_number
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes


@dataclass(frozen=True)
class MarketHistory:
    dates: tuple[date, ...]
    forward_prices: tuple[tuple[Decimal, ...], ...]
    zero_rates: tuple[tuple[Decimal, ...], ...]
    forward_sources: tuple[tuple[str, ...], ...]
    data_kind: str
    source_description: str
    forward_history: np.ndarray = field(init=False, repr=False, compare=False)
    zero_history: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        dates = tuple(self.dates)
        if (len(dates) < 3 or any(type(d) is not date for d in dates)
                or any(a >= b for a, b in zip(dates, dates[1:]))):
            raise ValueError("История должна содержать возрастающие даты")
        if (self.data_kind not in ("REAL", "SYNTHETIC_TEST") or not isinstance(self.source_description, str)
                or not self.source_description.strip()):
            raise ValueError("Нужно явно указать REAL либо SYNTHETIC_TEST и источник истории")
        forward = tuple(tuple(decimal_number(x, "forward", positive=True) for x in row) for row in self.forward_prices)
        zero = tuple(tuple(decimal_number(x, "zero") for x in row) for row in self.zero_rates)
        flags = tuple(tuple(row) for row in self.forward_sources)
        for matrix in (forward, zero, flags):
            if len(matrix) != len(dates) or any(len(row) != 39 for row in matrix):
                raise ValueError("История требует 39 узлов на каждую дату без пропусков")
        if any(not isinstance(x, str) or not x for row in flags for x in row):
            raise ValueError("Нужны признаки происхождения всех зерновых цен")
        for name, matrix in (("forward_history", forward), ("zero_history", zero)):
            arr = np.array(matrix, dtype=np.float64)
            if not np.all(np.isfinite(arr)) or (name == "forward_history" and np.any(arr <= 0)):
                raise ValueError("Преобразование истории в float64 дало недопустимые значения")
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "forward_prices", forward)
        object.__setattr__(self, "zero_rates", zero)
        object.__setattr__(self, "forward_sources", flags)

    @property
    def as_of(self):
        return self.dates[-1]

    @property
    def tenors(self):
        return np.array(STANDARD_TENORS)

    @classmethod
    def from_records(cls, records, *, data_kind, source_description):
        if not isinstance(records, list):
            raise ValueError("history должен быть массивом ежедневных снимков")
        dates, prices, rates, sources = [], [], [], []
        for record in records:
            day = date.fromisoformat(record["date"])
            points = record["forward_curve"]
            if not isinstance(points, list) or len(points) != 39:
                raise ValueError("forward_curve требует 39 точек")
            by_tenor = {p["tenor"]: p for p in points}
            if set(by_tenor) != set(STANDARD_TENORS):
                raise ValueError("Неверные или повторные сроки forward_curve")
            dfs = record["discount_factors"]
            if not isinstance(dfs, dict) or set(dfs) != set(STANDARD_TENORS):
                raise ValueError("discount_factors требует все 39 сроков")
            daily_prices, daily_rates, daily_sources = [], [], []
            with localcontext() as ctx:
                ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
                for node in standard_nodes(day):
                    point = by_tenor[node.tenor]
                    if "maturity_date" in point and point["maturity_date"] != node.maturity_date.isoformat():
                        raise ValueError("Экспирация исторического узла не соответствует его дате")
                    price = decimal_number(point["price"], "price", positive=True)
                    source = point["source"]
                    if source not in ("MIXED", "TRADES_ONLY", "ORDERS_ONLY", "FALLBACK_PREVIOUS_DAY", "EXPERT", "SYNTHETIC_TEST"):
                        raise ValueError("Неверный источник цены узла")
                    if data_kind == "REAL" and source == "SYNTHETIC_TEST":
                        raise ValueError("Синтетическая цена не может быть помечена REAL")
                    factor = decimal_number(dfs[node.tenor], "discount_factor", positive=True)
                    tau = Decimal((node.maturity_date - day).days) / 365
                    daily_prices.append(price)
                    daily_rates.append(-factor.ln() / tau)
                    daily_sources.append(source)
            dates.append(day)
            prices.append(tuple(daily_prices))
            rates.append(tuple(daily_rates))
            sources.append(tuple(daily_sources))
        return cls(tuple(dates), tuple(prices), tuple(rates), tuple(sources), data_kind, source_description)
