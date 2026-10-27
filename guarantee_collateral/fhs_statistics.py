"""FHS/GARCH statistics adapted from supplied wheat_margin.py; frozen valuation date."""
from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil
from typing import Callable, Sequence
import json

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.signal import lfilter
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from financial_contracts import ForwardTrade as Trade, NodeCurves as Curves, price_forward
from wheat_curve.domain.tenors import tenor_to_date
from .market_history import MarketHistory as MarketData


@dataclass(frozen=True)
class RiskConfig:
    horizon_days: int
    risk_end_date: date  # Конец h торговых дней из вашего биржевого календаря.
    scenarios: int = 10_000
    confidence: float = 0.99
    fhs_window: int = 1_000  # Число дневных изменений, не число цен.
    min_observations: int = 200
    metric: str = 'var'     # 'var' либо 'es'.
    seed: int = 42
    volatility_mode: str = 'forecast'  # Либо 'recent_max'.
    recent_window: int = 20
    volatility_floor: object = 0.0    # Скаляр либо 2*K значений: сначала F, затем y.
    min_volatility_ratio: float | None = None
    previous_volatility: object = None  # Предыдущая применённая однодневная σ*.
    fhs_target: object = None  # Ставка FHS: скаляр/2*K; NaN = без ставки.


@dataclass(frozen=True)
class Shock:
    name: str
    forward_return: object = 0.0   # Относительный сдвиг: -0.15 = падение на 15%.
    zero_shift: object = 0.0       # Абсолютный сдвиг: +0.02 = +2 процентных пункта.


@dataclass(frozen=True)
class ConcentrationBucket:
    last_tenor: str
    limit1: Decimal
    limit2: Decimal
    addon1_per_tonne: Decimal
    addon2_per_tonne: Decimal


@dataclass(frozen=True)
class GarchParameters:
    omega: float
    alpha: float
    beta: float
    nu: float
    initial_variance: float


@dataclass
class FHSCalibration:
    parameters: tuple[GarchParameters, ...]
    residuals: np.ndarray
    start_date: date
    return_dates: tuple[date, ...]
    anchor_changes: np.ndarray
    tenors: np.ndarray


def year_fraction(start: date, end: date) -> float:
    return (end - start).days / 365.0


def _variances(x, p):
    forcing = np.empty(len(x))
    forcing[0] = p.initial_variance
    forcing[1:] = p.omega + p.alpha * x[:-1] ** 2
    return lfilter([1.0], [1.0, -p.beta], forcing)


def _fit_garch(x):
    scale = float(np.sqrt(np.mean(x * x)))
    if scale == 0:
        return GarchParameters(0.0, 0.0, 0.0, 10.0, 0.0)
    u = x / scale
    v0 = float(np.mean(u * u))

    def objective(theta):
        w, a, b, nu = theta
        if w <= 0 or a + b >= 1 or nu <= 2:
            return 1e100
        v = _variances(u, GarchParameters(w, a, b, nu, v0))
        c = gammaln(nu / 2) - gammaln((nu + 1) / 2) + 0.5 * np.log(np.pi * (nu - 2))
        return float(np.sum(c + 0.5 * np.log(v)
                           + 0.5 * (nu + 1) * np.log1p(u * u / (v * (nu - 2)))))

    best = None
    for a, b in ((0.05, 0.90), (0.10, 0.80), (0.03, 0.96)):
        result = minimize(objective, [max(1 - a - b, 1e-4), a, b, 8.0],
                          method='SLSQP', bounds=[(1e-10, 10), (0, 0.999), (0, 0.999), (2.05, 150)],
                          constraints=[{'type': 'ineq', 'fun': lambda z: 0.9995 - z[1] - z[2]}],
                          options={'maxiter': 400, 'ftol': 1e-8})
        if result.success and np.isfinite(result.fun) and result.x[1] + result.x[2] < 1:
            if best is None or result.fun < best.fun:
                best = result
    if best is None:
        raise RuntimeError('GARCH не сошлась; проверьте историю и настройки')
    w, a, b, nu = best.x
    return GarchParameters(float(w * scale ** 2), float(a), float(b), float(nu), float(v0 * scale ** 2))


def _changes(market, start=0):
    f, y = market.forward_history[start:], market.zero_history[start:]
    return np.column_stack((f[1:] / f[:-1] - 1, np.diff(y, axis=0)))


def calibrate_fhs(market: MarketData, config: RiskConfig) -> FHSCalibration:
    """Ежедневная калибровка. Для заморозки сохраните и передайте результат в GOEngine."""
    start = max(0, len(market.dates) - config.fhs_window - 1)
    x = _changes(market, start)
    if len(x) < config.min_observations:
        raise ValueError('Недостаточно истории для GARCH')
    parameters = tuple(_fit_garch(x[:, j]) for j in range(x.shape[1]))
    sigma = np.column_stack([np.sqrt(_variances(x[:, j], p)) for j, p in enumerate(parameters)])
    residuals = np.divide(x, sigma, out=np.zeros_like(x), where=sigma > 0)
    return FHSCalibration(parameters, residuals, market.dates[start],
                          tuple(market.dates[start + 1:]), x.copy(), market.tenors.copy())


def _vector(value, size, name, allow_nan=False):
    arr = np.broadcast_to(np.asarray(value, dtype=float), (size,)).copy()
    valid = ~np.isinf(arr) if allow_nan else np.isfinite(arr)
    if not np.all(valid):
        raise ValueError(f'Некорректный параметр {name}')
    return arr


def _tail(losses, confidence, metric, positive_only=True):
    a = np.sort(np.asarray(losses, dtype=float))
    if a.size == 0 or not np.all(np.isfinite(a)):
        raise ValueError('Пустые или нечисловые сценарные убытки')
    if metric == 'var':
        value = a[ceil(confidence * len(a)) - 1]
    else:
        position = confidence * len(a)
        j = int(np.floor(position))
        value = (a[j:].sum() - (position - j) * a[j]) / (len(a) - position)
    return max(0.0, float(value)) if positive_only else float(value)


def _validate_config(c, market):
    if (not isinstance(c.horizon_days, int) or c.horizon_days < 1
            or not isinstance(c.scenarios, int) or c.scenarios < 1):
        raise ValueError('Горизонт и число сценариев должны быть положительными целыми')
    if not (0 < c.confidence < 1) or c.metric not in ('var', 'es'):
        raise ValueError('Некорректная метрика или вероятность')
    if c.volatility_mode not in ('forecast', 'recent_max') or c.recent_window < 1:
        raise ValueError('Некорректный способ оценки волатильности')
    if c.min_observations < 10 or c.fhs_window < c.min_observations:
        raise ValueError('Некорректное историческое окно')
    if c.risk_end_date <= market.as_of or (c.risk_end_date - market.as_of).days < c.horizon_days:
        raise ValueError('Некорректная дата конца горизонта')
    if c.min_volatility_ratio is not None:
        if not 0 <= c.min_volatility_ratio <= 1 or c.previous_volatility is None:
            raise ValueError('Для ограничения снижения нужна предыдущая σ* и коэффициент от 0 до 1')


class GOEngine:
    def __init__(self, market: MarketData, config: RiskConfig,
                 calibration: FHSCalibration | None = None):
        _validate_config(config, market)
        self.market, self.config = market, config
        self.current = Curves(market.as_of, market.forward_prices[-1], market.zero_rates[-1])
        self.calibration = calibration if calibration is not None else calibrate_fhs(market, config)
        cal = self.calibration
        if not np.array_equal(cal.tenors, market.tenors):
            raise ValueError('Сетка замороженной калибровки изменилась')
        try:
            start = market.dates.index(cal.start_date)
        except ValueError as exc:
            raise ValueError('Для заморозки нужен архив с исходной датой калибровки') from exc
        x = _changes(market, start)
        old_n = len(cal.anchor_changes)
        if (tuple(market.dates[start + 1:start + 1 + old_n]) != cal.return_dates
                or len(x) < old_n or not np.allclose(x[:old_n], cal.anchor_changes, rtol=1e-10, atol=1e-14)):
            raise ValueError('Исходная история замороженной калибровки изменилась')
        self.volatility = self._forecast_volatility(x)
        self.fhs = self._fhs_scenarios()
        self.historical = self._historical_scenarios()

    def _forecast_volatility(self, x):
        c, cal = self.config, self.calibration
        d = x.shape[1]
        sigma = np.empty((c.horizon_days, d))
        for j, p in enumerate(cal.parameters):
            v = _variances(x[:, j], p)
            next_v = p.omega + p.alpha * x[-1, j] ** 2 + p.beta * v[-1]
            for m in range(c.horizon_days):
                if c.volatility_mode == 'recent_max':
                    sigma[m, j] = np.sqrt(np.max(v[-c.recent_window:]))
                else:
                    sigma[m, j] = np.sqrt(next_v)
                next_v = p.omega + (p.alpha + p.beta) * next_v
        floor = _vector(c.volatility_floor, d, 'volatility_floor')
        if np.any(floor < 0):
            raise ValueError('Нижняя граница волатильности должна быть неотрицательной')
        sigma = np.maximum(sigma, floor)
        if c.min_volatility_ratio is not None:
            prev = _vector(c.previous_volatility, d, 'previous_volatility')
            if np.any(prev < 0):
                raise ValueError('Предыдущая волатильность должна быть неотрицательной')
            sigma = np.maximum(sigma, c.min_volatility_ratio * prev)
        if c.fhs_target is not None:
            targets = _vector(c.fhs_target, d, 'fhs_target', allow_nan=True)
            for j, target in enumerate(targets):
                if np.isnan(target):
                    continue
                if target <= 0 or sigma[0, j] <= 0:
                    raise ValueError('Для ставки FHS нужны положительные ставка и волатильность')
                e = cal.residuals[:, j]
                average_tail = (abs(_tail(e, c.confidence, c.metric, False))
                                + abs(_tail(-e, c.confidence, c.metric, False))) / 2
                if average_tail <= 0:
                    raise ValueError('Нельзя масштабировать нулевые хвосты')
                sigma[:, j] *= target / (sigma[0, j] * average_tail)
                minimum = floor[j]
                if c.min_volatility_ratio is not None:
                    minimum = max(minimum, c.min_volatility_ratio * prev[j])
                if np.any(sigma[:, j] < minimum - 1e-14):
                    raise ValueError('Ставка FHS противоречит нижней границе волатильности')
        return sigma

    def _fhs_scenarios(self):
        c, cal = self.config, self.calibration
        k = len(self.market.tenors)
        rng = np.random.default_rng(c.seed)
        f = np.broadcast_to(self.market.forward_history[-1], (c.scenarios, k)).copy()
        y = np.broadcast_to(self.market.zero_history[-1], (c.scenarios, k)).copy()
        for m in range(c.horizon_days):
            # Совместная выборка сохраняет связь между ценами, сроками и ставками.
            idx = rng.integers(0, len(cal.residuals), size=c.scenarios)
            z = cal.residuals[idx] * self.volatility[m]
            if np.any(z[:, :k] <= -1):
                raise ValueError('FHS даёт неположительную цену; смените модель факторов, не обрезайте хвост')
            f *= 1 + z[:, :k]
            y += z[:, k:]
        if not np.all(np.isfinite(f)) or not np.all(np.isfinite(y)):
            raise ValueError('Нечисловые FHS-сценарии')
        return f, y

    def _historical_scenarios(self):
        h = self.config.horizon_days
        f, y = self.market.forward_history, self.market.zero_history
        if len(f) <= h:
            raise ValueError('Истории недостаточно для горизонта риска')
        return (self.market.forward_history[-1] * (f[h:] / f[:-h]),
                self.market.zero_history[-1] + y[h:] - y[:-h])

    def _stress_scenarios(self, shocks):
        k = len(self.market.tenors)
        f, y = [], []
        for shock in shocks:
            change = _vector(shock.forward_return, k, shock.name)
            shift = _vector(shock.zero_shift, k, shock.name)
            if np.any(change <= -1):
                raise ValueError('Стресс даёт неположительную цену')
            f.append(self.market.forward_history[-1] * (1 + change))
            y.append(self.market.zero_history[-1] + shift)
        return np.asarray(f).reshape(-1, k), np.asarray(y).reshape(-1, k)

    def _values(self, trades, scenario_set, price_fn):
        f, y = scenario_set
        result = np.empty((len(f), len(trades)), dtype=object)
        for n in range(len(f)):
            curves = Curves.from_scenario(self.market.as_of, f[n], y[n])
            for j, trade in enumerate(trades):
                result[n, j] = price_fn(trade, curves, self.market.as_of)
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in result.flat):
            raise ValueError('Функция оценки должна возвращать конечный Decimal')
        return result

    def _concentration(self, trades, buckets):
        if not buckets:
            return Decimal(0)
        ends = [tenor_to_date(self.market.as_of, b.last_tenor) for b in buckets]
        if any(a >= b for a, b in zip(ends, ends[1:])):
            raise ValueError('Границы концентрационных корзин должны возрастать')
        volumes = [Decimal(0) for _ in buckets]
        for trade in trades:
            j = next((i for i, end in enumerate(ends) if trade.fixing_date <= end), None)
            if j is None:
                raise ValueError('Срок сделки не покрыт концентрационными корзинами')
            volumes[j] += trade.side * trade.quantity
        total = Decimal(0)
        for b, volume in zip(buckets, volumes):
            values = (b.limit1, b.limit2, b.addon1_per_tonne, b.addon2_per_tonne)
            if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values) or b.limit2 < b.limit1:
                raise ValueError('Некорректные пороги/ставки концентрации')
            excess2 = max(abs(volume) - b.limit2, Decimal(0))
            excess1 = max(abs(volume) - b.limit1, Decimal(0)) - excess2
            total += excess1 * b.addon1_per_tonne + excess2 * b.addon2_per_tonne
        return total

    def calculate(self, trades: Sequence[Trade], *,
                  hypothetical_scenarios: Sequence[Shock],
                  event_scenarios: Sequence[Shock],
                  concentration_buckets: Sequence[ConcentrationBucket],
                  price_fn: Callable = price_forward):
        """ГО отдельных сделок и портфеля. Пустые списки отключают компоненты явно."""
        trades = tuple(trades)
        if not trades or len({t.trade_id for t in trades}) != len(trades):
            raise ValueError('Нужны сделки с уникальными идентификаторами')
        for t in trades:
            if (not t.quantity.is_finite() or t.quantity <= 0 or not t.strike.is_finite()
                    or t.strike <= 0 or t.side not in (-1, 1)):
                raise ValueError('Некорректные параметры сделки')
            if t.fixing_date <= self.config.risk_end_date or t.payment_date < t.fixing_date:
                raise ValueError('Фиксация должна быть после горизонта, выплата не раньше фиксации')
        current = np.array([price_fn(t, self.current, self.market.as_of) for t in trades], dtype=object)
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in current):
            raise ValueError('Некорректная текущая оценка')
        groups = {'fhs': self.fhs, 'historical': self.historical,
                  'hypothetical': self._stress_scenarios(hypothetical_scenarios),
                  'event': self._stress_scenarios(event_scenarios)}
        losses = {name: current - self._values(trades, scenarios, price_fn)
                  for name, scenarios in groups.items()}

        def summarize(selected):
            position = [trades[j] for j in selected]
            risks = {}
            for name, matrix in losses.items():
                a = matrix[:, selected].sum(axis=1)
                if len(a) == 0:
                    risks[name] = Decimal(0)
                elif name in ('hypothetical', 'event'):
                    risks[name] = max(Decimal(0), np.max(a))
                else:
                    risks[name] = Decimal(str(_tail(a, self.config.confidence, self.config.metric)))
            risks['concentration'] = self._concentration(position, concentration_buckets)
            risks['go'] = (max(risks['fhs'], risks['historical'], risks['hypothetical'])
                           + risks['event'] + risks['concentration'])
            risks['current_value'] = current[selected].sum()
            return risks

        omitted = [name for name, values in [('hypothetical', hypothetical_scenarios),
                   ('event', event_scenarios), ('concentration', concentration_buckets)] if not values]
        return {'currency': 'RUB', 'scope': 'model_risk_margin',
                'confidence': self.config.confidence, 'metric': self.config.metric,
                'excluded_components': omitted,
                'valuation_date': self.market.as_of.isoformat(),
                'risk_end_date': self.config.risk_end_date.isoformat(),
                'horizon_trading_days': self.config.horizon_days,
                'scenario_valuation': 'FROZEN_AS_OF',
                'portfolio': summarize(list(range(len(trades)))),
                'trades': {t.trade_id: summarize([j]) for j, t in enumerate(trades)},
                'fhs_scenario_count': len(self.fhs[0]),
                'historical_scenario_count': len(self.historical[0]),
                'one_day_volatility': self.volatility[0].tolist()}


