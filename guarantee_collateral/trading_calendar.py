"""Explicit MOEX SPFI trading-date snapshot; never guess missing calendar coverage."""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path


@dataclass(frozen=True)
class TradingCalendar:
    dates: tuple[date, ...]
    coverage_start: date
    coverage_end: date
    source_url: str
    market: str = "MOEX SPFI"

    def __post_init__(self):
        object.__setattr__(self, "dates", tuple(self.dates))
        if (type(self.coverage_start) is not date or type(self.coverage_end) is not date
                or self.coverage_start > self.coverage_end or not self.dates
                or any(type(d) is not date for d in self.dates)
                or any(a >= b for a, b in zip(self.dates, self.dates[1:]))
                or self.dates[0] < self.coverage_start or self.dates[-1] > self.coverage_end
                or self.market != "MOEX SPFI" or not isinstance(self.source_url, str)
                or not self.source_url.startswith("https://")):
            raise ValueError("Некорректный снимок календаря MOEX SPFI")

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls(tuple(date.fromisoformat(d) for d in data["trading_dates"]),
                   date.fromisoformat(data["coverage_start"]), date.fromisoformat(data["coverage_end"]),
                   data["source_url"], data["market"])

    def risk_end_date(self, as_of: date, horizon: int) -> date:
        if type(horizon) is not int or horizon < 1:
            raise ValueError("Горизонт должен быть положительным целым числом")
        if as_of not in self.dates:
            raise ValueError("Дата оценки отсутствует в календаре MOEX SPFI")
        index = self.dates.index(as_of) + horizon
        if index >= len(self.dates):
            raise ValueError("Календарь не покрывает конец горизонта риска")
        return self.dates[index]

    def validate_history(self, dates):
        if not dates or dates[0] < self.coverage_start or dates[-1] > self.coverage_end:
            raise ValueError("Календарь не покрывает историю")
        expected = tuple(d for d in self.dates if dates[0] <= d <= dates[-1])
        if tuple(dates) != expected:
            raise ValueError("В истории пропущен торговый день либо есть неторговая дата")
