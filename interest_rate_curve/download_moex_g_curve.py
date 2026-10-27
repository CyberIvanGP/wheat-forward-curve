"""MOEX Nelson–Siegel + nine Gaussian corrections. No wheat-core imports."""

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


ISS_URL = "https://iss.moex.com/iss/engines/stock/zcyc.json"
PARAMETER_NAMES = ("B1", "B2", "B3", "T1") + tuple(f"G{i}" for i in range(1, 10))
SHORT_END_YEARS = Decimal("0.25")
MAX_MODEL_YEARS = Decimal("30")


class CurveDataError(ValueError):
    """Invalid or inconsistent source data."""


class CurveUnavailableError(CurveDataError):
    """No exact-date snapshot; never silently substitute another date."""


class CurveDownloadError(RuntimeError):
    """HTTPS request failed; no cached or stale fallback is applied."""


def _decimal(value, name):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise CurveDataError(f"{name}: требуется десятичная строка, int или Decimal")
    try:
        number = Decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise CurveDataError(f"{name}: некорректное число") from exc
    if not number.is_finite():
        raise CurveDataError(f"{name}: число должно быть конечным")
    return number


def _rows(payload, block, required):
    try:
        table = payload[block]
        columns, data = table["columns"], table["data"]
        if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
                or len(set(columns)) != len(columns) or not isinstance(data, list)
                or not set(required) <= set(columns)):
            raise CurveDataError(f"{block}: неверные столбцы или данные")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in data):
            raise CurveDataError(f"{block}: длина строки не совпадает со столбцами")
        return [dict(zip(columns, row)) for row in data]
    except (KeyError, TypeError) as exc:
        raise CurveDataError(f"Отсутствует корректный блок {block}") from exc


@dataclass(frozen=True)
class MoexGCurve:
    valuation_date: date
    snapshot_time: time
    parameters: tuple[Decimal, ...]
    published_yields: tuple[tuple[Decimal, Decimal], ...]

    def __post_init__(self):
        if type(self.valuation_date) is not date:
            raise CurveDataError("Дата оценки должна иметь тип datetime.date")
        if type(self.snapshot_time) is not time or self.snapshot_time.tzinfo is not None:
            raise CurveDataError("Время снимка должно быть московским временем без tzinfo")
        if len(self.parameters) != 13:
            raise CurveDataError("Ожидаются B1, B2, B3, T1 и G1…G9")
        params = tuple(_decimal(v, name) for name, v in zip(PARAMETER_NAMES, self.parameters))
        if params[3] <= 0:
            raise CurveDataError("T1 должен быть положительным")
        points = tuple((_decimal(t, "period"), _decimal(y, "value"))
                       for t, y in self.published_yields)
        if (not points or any(t <= 0 or y <= -100 for t, y in points)
                or len({t for t, _ in points}) != len(points)):
            raise CurveDataError("Некорректные опубликованные доходности")
        if min(t for t, _ in points) != SHORT_END_YEARS:
            raise CurveDataError("Ожидалась первая опубликованная ставка на срок 0.25 года")
        object.__setattr__(self, "parameters", params)
        object.__setattr__(self, "published_yields", tuple(sorted(points)))

    @classmethod
    def from_iss_response(cls, payload: dict, *, valuation_date: date) -> "MoexGCurve":
        if type(valuation_date) is not date:
            raise CurveDataError("valuation_date должна иметь тип datetime.date")
        if not isinstance(payload, dict):
            raise CurveDataError("Ожидался JSON-объект MOEX")
        params = _rows(payload, "params", ("tradedate", "tradetime", *PARAMETER_NAMES))
        if not params:
            raise CurveUnavailableError(f"Нет G-кривой MOEX за {valuation_date}")
        if len(params) != 1:
            raise CurveDataError("Ожидалась одна строка параметров снимка")
        row = params[0]
        if row["tradedate"] != valuation_date.isoformat():
            raise CurveUnavailableError(
                f"Запрошена дата {valuation_date}, MOEX вернула {row['tradedate']}")
        try:
            snapshot_time = time.fromisoformat(row["tradetime"])
        except (TypeError, ValueError) as exc:
            raise CurveDataError("Некорректное время снимка MOEX") from exc
        yields = _rows(payload, "yearyields", ("tradedate", "tradetime", "period", "value"))
        if not yields:
            raise CurveDataError("Нет таблицы опубликованных доходностей")
        if any(p["tradedate"] != row["tradedate"] or p["tradetime"] != row["tradetime"]
               for p in yields):
            raise CurveDataError("Параметры и доходности относятся к разным снимкам")
        return cls(valuation_date, snapshot_time,
                   tuple(row[name] for name in PARAMETER_NAMES),
                   tuple((p["period"], p["value"]) for p in yields))

    def continuous_rate(self, years: Decimal | str | int) -> Decimal:
        """Continuous zero rate as a fraction; flat rate below 0.25 years."""
        maturity = _decimal(years, "years")
        if maturity < 0 or maturity > MAX_MODEL_YEARS:
            raise CurveDataError("Срок модели должен находиться в диапазоне 0…30 лет")
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            maturity = max(maturity, SHORT_END_YEARS)
            b1, b2, b3, tau, *corrections = self.parameters
            x = maturity / tau
            decay = (-x).exp()
            basis_points = b1 + (b2 + b3) * (1 - decay) / x - b3 * decay
            center, width = Decimal(0), Decimal("0.6")
            for correction in corrections:
                basis_points += correction * (-((maturity - center) / width) ** 2).exp()
                center += width
                width *= Decimal("1.6")
            return basis_points / Decimal(10000)

    def effective_annual_yield_percent(self, years: Decimal | str | int) -> Decimal:
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            return (self.continuous_rate(years).exp() - 1) * 100

    def discount_factor(self, payment_date: date) -> Decimal:
        if type(payment_date) is not date or payment_date < self.valuation_date:
            raise CurveDataError("Нужна дата платежа не раньше даты оценки")
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
            years = Decimal((payment_date - self.valuation_date).days) / Decimal(365)
            return (-self.continuous_rate(years) * years).exp()

    def to_iss_response(self) -> dict:
        day, clock = self.valuation_date.isoformat(), self.snapshot_time.isoformat()
        return {
            "params": {"columns": ["tradedate", "tradetime", *PARAMETER_NAMES],
                       "data": [[day, clock, *map(str, self.parameters)]]},
            "yearyields": {"columns": ["tradedate", "tradetime", "period", "value"],
                           "data": [[day, clock, str(t), str(y)] for t, y in self.published_yields]},
            "snapshot_metadata": {"source": "MOEX GCURVE", "url": ISS_URL,
                                  "timezone": "Europe/Moscow", "snapshot_format_version": 1},
        }


def download_moex_g_curve(
    valuation_date: date, *, timeout: int = 20, use_system_proxy: bool = False,
) -> MoexGCurve:
    """Fetch the source's available snapshot on exactly this date, not a time-of-day quote."""
    if type(valuation_date) is not date:
        raise CurveDataError("valuation_date должна иметь тип datetime.date")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("timeout должен быть положительным целым числом секунд")
    if type(use_system_proxy) is not bool:
        raise ValueError("use_system_proxy должен иметь тип bool")
    query = urlencode({"date": valuation_date.isoformat(), "iss.meta": "off",
                       "iss.only": "params,yearyields"})
    request = Request(f"{ISS_URL}?{query}", headers={"User-Agent": "wheat-curve-calculator/1.1"})
    opener = build_opener() if use_system_proxy else build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8-sig")
    except (HTTPError, URLError, OSError) as exc:
        raise CurveDownloadError(f"Не удалось загрузить G-кривую MOEX: {exc}") from exc
    try:
        payload = json.loads(text, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise CurveDataError("MOEX вернула некорректный JSON") from exc
    return MoexGCurve.from_iss_response(payload, valuation_date=valuation_date)


def save_moex_g_curve_snapshot(curve: MoexGCurve, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(curve.to_iss_response(), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


def load_moex_g_curve_snapshot(path: str | Path, *, valuation_date: date) -> MoexGCurve:
    with Path(path).open(encoding="utf-8-sig") as stream:
        payload = json.load(stream, parse_float=Decimal)
    return MoexGCurve.from_iss_response(payload, valuation_date=valuation_date)
