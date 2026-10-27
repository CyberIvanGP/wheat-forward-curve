"""Explicit synthetic fixtures for tests, not the proposed market-data generator."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import numpy as np

from financial_contracts import ForwardTrade
from guarantee_collateral import MarketHistory, TradingCalendar

ROOT = Path(__file__).resolve().parents[1]


def synthetic_history(count=201):
    calendar = TradingCalendar.load(ROOT / "examples/moex_spfi_trading_calendar_2026.json")
    dates = calendar.dates[:count]
    rng = np.random.default_rng(123)
    common = rng.normal(0, .003, (count - 1, 1))
    returns = common + rng.normal(0, .0005, (count - 1, 39))
    prices = np.vstack([np.linspace(20000, 23000, 39), np.zeros((count - 1, 39))])
    prices[1:] = prices[0] * np.cumprod(1 + returns, axis=0)
    zero = np.vstack([np.linspace(.12, .15, 39), np.zeros((count - 1, 39))])
    zero[1:] = zero[0] + np.cumsum(rng.normal(0, .0001, (count - 1, 39)), axis=0)
    market = MarketHistory(dates, tuple(tuple(Decimal(str(x)) for x in row) for row in prices),
                           tuple(tuple(Decimal(str(x)) for x in row) for row in zero),
                           tuple(("SYNTHETIC_TEST",) * 39 for _ in dates),
                           "SYNTHETIC_TEST", "Deterministic unit-test fixture, seed 123; NOT REAL DATA")
    trade = ForwardTrade("BUY_1000", Decimal(1000), Decimal(21000), 1,
                         dates[-1] + timedelta(days=180), dates[-1] + timedelta(days=182))
    return market, calendar, trade
