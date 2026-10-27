"""Public portfolio-margin API. No network and no hidden synthetic archive."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math

from financial_contracts import ForwardTrade, NodeCurves, price_forward
from financial_contracts.forward_valuation import decimal_number
from project_parameters import load_parameters
from wheat_curve.domain.tenors import STANDARD_TENORS, tenor_to_date
from .fhs_statistics import ConcentrationBucket, GOEngine, RiskConfig, Shock
from .market_history import MarketHistory
from .trading_calendar import TradingCalendar


def _config(parameters, market, calendar):
    settings = parameters["margin"]
    allowed = {"horizon_days", "scenarios", "confidence", "fhs_window", "min_observations",
               "metric", "seed", "volatility_mode", "recent_window", "volatility_floor",
               "min_volatility_ratio", "previous_volatility", "fhs_target", "hypothetical_scenarios", "event_scenarios", "concentration"}
    if not isinstance(settings, dict) or set(settings) != allowed:
        raise ValueError("Неверный набор параметров margin")
    for name in ("horizon_days", "scenarios", "fhs_window", "min_observations", "recent_window", "seed"):
        if type(settings[name]) is not int or settings[name] < (0 if name == "seed" else 1):
            raise ValueError(f"{name}: нужно целое число допустимого диапазона")
    numeric = {}
    for name in ("confidence", "volatility_floor", "min_volatility_ratio", "previous_volatility", "fhs_target"):
        value = settings[name]
        if value is None:
            numeric[name] = None
        elif isinstance(value, list) and name in ("volatility_floor", "previous_volatility", "fhs_target"):
            if len(value) != 78:
                raise ValueError(f"{name}: нужно 78 значений, сначала F, затем y")
            numeric[name] = [float(decimal_number(v, name)) for v in value]
        else:
            numeric[name] = float(decimal_number(value, name))
        vals = numeric[name] if isinstance(numeric[name], list) else [numeric[name]]
        if any(v is not None and not math.isfinite(v) for v in vals):
            raise ValueError(f"{name}: значение не представимо в float64")
    if numeric["confidence"] is None or numeric["volatility_floor"] is None:
        raise ValueError("confidence и volatility_floor не могут быть null")
    config = RiskConfig(
        risk_end_date=calendar.risk_end_date(market.as_of, settings["horizon_days"]),
        **{k: settings[k] for k in ("horizon_days", "scenarios", "fhs_window", "min_observations",
                                   "metric", "seed", "volatility_mode", "recent_window")}, **numeric)
    return config


def _shocks(items):
    if not isinstance(items, list):
        raise ValueError("Сценарии должны быть массивом")
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "forward_return", "zero_shift"}:
            raise ValueError("Сценарий требует name, forward_return и zero_shift")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Нужно имя сценария")
        values = {}
        for field in ("forward_return", "zero_shift"):
            value = item[field]
            if isinstance(value, list):
                if len(value) != 39:
                    raise ValueError("Шок должен содержать 39 значений")
                values[field] = [float(decimal_number(v, field)) for v in value]
            else:
                values[field] = float(decimal_number(value, field))
        result.append(Shock(name, **values))
    return result


def _buckets(settings):
    if not isinstance(settings, dict) or set(settings) != {"limit1", "limit2", "addon1_per_tonne", "addon2_per_tonne", "buckets"}:
        raise ValueError("Неверная структура concentration")
    rate1 = decimal_number(settings["addon1_per_tonne"], "addon1_per_tonne")
    rate2 = decimal_number(settings["addon2_per_tonne"], "addon2_per_tonne")
    if rate1 < 0 or rate2 < 0 or not isinstance(settings["buckets"], list):
        raise ValueError("Неверные ставки или корзины концентрации")
    if rate1 == rate2 == 0:
        return []
    if not settings["buckets"]:
        raise ValueError("Для ненулевой надбавки нужно явно задать корзины и лимиты")
    result = []
    for bucket in settings["buckets"]:
        if not isinstance(bucket, dict) or set(bucket) - {"last_tenor", "limit1", "limit2"} or "last_tenor" not in bucket:
            raise ValueError("Неверная концентрационная корзина")
        if bucket["last_tenor"] not in STANDARD_TENORS:
            raise ValueError("Граница корзины должна быть стандартным узлом")
        limits = [decimal_number(bucket.get(k, settings[k]), k) for k in ("limit1", "limit2")]
        if limits[0] < 0 or limits[1] < limits[0]:
            raise ValueError("Требуется 0 <= limit1 <= limit2")
        result.append(ConcentrationBucket(bucket["last_tenor"], *limits, rate1, rate2))
    return result


def calculate_portfolio_margin(
    market: MarketHistory, trades: list[ForwardTrade], calendar: TradingCalendar,
    *, parameters_file=None, allow_synthetic_test_data=False, calibration=None,
):
    trades = tuple(trades)
    prepared = prepare_portfolio_margin(market, calendar, parameters_file=parameters_file,
                                       allow_synthetic_test_data=allow_synthetic_test_data,
                                       calibration=calibration, trades=trades)
    return prepared.calculate(trades)


def prepare_portfolio_margin(market, calendar, *, parameters_file=None,
                             allow_synthetic_test_data=False, calibration=None, trades=None):
    """Prepare scenarios once; repeated portfolio valuation reuses the same engine."""
    if market.data_kind != "REAL" and not allow_synthetic_test_data:
        raise ValueError("Рабочий расчёт ГО требует REAL архив; тестовый режим включается явно")
    params = load_parameters(parameters_file)
    calendar.validate_history(market.dates)
    config = _config(params, market, calendar)
    if trades is not None:
        _validate_trades(trades, config, NodeCurves(market.as_of, market.forward_prices[-1], market.zero_rates[-1]))
    settings = params["margin"]
    hypothetical = _shocks(settings["hypothetical_scenarios"])
    event = _shocks(settings["event_scenarios"])
    buckets = _buckets(settings["concentration"])
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        engine = GOEngine(market, config, calibration)
    metadata = {
        "methodology_version": "wheat-margin-1.0",
        "data_kind": market.data_kind, "history_source": market.source_description,
        "history_start": market.dates[0].isoformat(), "history_end": market.as_of.isoformat(),
        "history_observations": len(market.dates) - 1,
        "calibration_observations": len(engine.calibration.residuals),
        "tenors": list(STANDARD_TENORS), "calendar_source": calendar.source_url,
        "calendar_market": calendar.market, "parameters": params,
        "carried_price_observations": sum(v == "FALLBACK_PREVIOUS_DAY" for row in market.forward_sources for v in row),
        "fhs_calibration": {"parameters": [asdict(p) for p in engine.calibration.parameters],
                            "start_date": engine.calibration.start_date.isoformat()},
        "warning": "MODEL ГО, NOT OFFICIAL NCC LIMIT OR COLLATERAL TOP-UP",
    }
    return PreparedPortfolioMargin(engine, hypothetical, event, buckets, metadata)


def _validate_trades(trades, config, current):
    if not trades or any(not isinstance(t, ForwardTrade) for t in trades) or len({t.trade_id for t in trades}) != len(trades):
        raise ValueError("Нужны сделки ForwardTrade с уникальными trade_id")
    for trade in trades:
        if trade.fixing_date <= config.risk_end_date:
            raise ValueError(f"{trade.trade_id}: фиксация должна быть строго после {config.risk_end_date}; портфель отклонён")
        price_forward(trade, current)


class PreparedPortfolioMargin:
    def __init__(self, engine, hypothetical, event, buckets, metadata):
        self.engine, self.hypothetical, self.event = engine, hypothetical, event
        self.buckets, self.metadata = buckets, metadata

    def calculate(self, trades):
        trades = tuple(trades)
        _validate_trades(trades, self.engine.config, self.engine.current)
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            result = self.engine.calculate(trades, hypothetical_scenarios=self.hypothetical,
                                           event_scenarios=self.event,
                                           concentration_buckets=self.buckets)
        result.update(self.metadata)
        return result


def calculate_from_input(data, calendar, *, parameters_file=None, allow_synthetic_test_data=False):
    if not isinstance(data, dict) or set(data) != {"calculation_date", "data_kind", "history_source", "history", "trades"}:
        raise ValueError("Неверная структура входа ГО")
    if not isinstance(data["trades"], list):
        raise ValueError("trades должен быть массивом")
    market = MarketHistory.from_records(data["history"], data_kind=data["data_kind"], source_description=data["history_source"])
    if data["calculation_date"] != market.as_of.isoformat():
        raise ValueError("Последняя дата архива должна совпадать с calculation_date")
    trades = []
    fields = {"trade_id", "quantity", "strike", "side", "fixing_date", "payment_date"}
    for item in data["trades"]:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("Неверные поля сделки")
        trades.append(ForwardTrade(**{**item, "fixing_date": date.fromisoformat(item["fixing_date"]),
                                      "payment_date": date.fromisoformat(item["payment_date"])}))
    return calculate_portfolio_margin(market, trades, calendar, parameters_file=parameters_file,
                                      allow_synthetic_test_data=allow_synthetic_test_data)
