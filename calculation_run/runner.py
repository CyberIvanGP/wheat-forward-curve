"""One run: isolated module failures, exact-date inputs, no data generation."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
import shutil

from interest_rate_curve import download_moex_g_curve
from project_parameters import DEFAULT_PARAMETERS_PATH, load_parameters
from wheat_curve import calculate
from wheat_curve.domain.curve import interpolate
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes
from .excel_export import export_today_curve
from .files import InputFiles, MOSCOW, new_run_folder, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
GRAIN_FIELDS = {"calculation_date", "previous_date", "previous_curve", "events",
                "parameter_overrides", "data_kind", "source_description"}
MARGIN_FIELDS = {"calculation_date", "data_kind", "history_source", "history", "trades"}
CALENDAR_FIELDS = {"market", "coverage_start", "coverage_end", "trading_dates", "source_url",
                   "verified_on", "secondary_source_url", "secondary_source", "notes"}


def parse_trades(records):
    from financial_contracts import ForwardTrade
    fields = {"trade_id", "quantity", "strike", "side", "fixing_date", "payment_date"}
    if not isinstance(records, list):
        raise ValueError("Портфель должен быть массивом сделок")
    trades = []
    for item in records:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("Нужны trade_id, quantity, strike, side, fixing_date, payment_date")
        trades.append(ForwardTrade(**{**item, "fixing_date": date.fromisoformat(item["fixing_date"]),
                                      "payment_date": date.fromisoformat(item["payment_date"])}))
    if len({t.trade_id for t in trades}) != len(trades):
        raise ValueError("Повторные trade_id")
    return trades


def merge_history(records, day, points, discount_factors):
    if not isinstance(records, list) or not records:
        raise ValueError("Нужен непустой архив кривых")
    dates = [date.fromisoformat(item["date"]) for item in records]
    if any(a >= b for a, b in zip(dates, dates[1:])) or dates[-1] > day:
        raise ValueError("Архив должен быть упорядочен и не содержать будущие даты")
    record = {"date": day.isoformat(), "forward_curve": points, "discount_factors": discount_factors}
    replaced = dates[-1] == day
    return records[:(-1 if replaced else len(records))] + [record], ("REPLACED" if replaced else "APPENDED")


@dataclass
class RunResult:
    folder: Path
    info: dict
    curve: dict | None = None
    discount_curve: object = None
    node_curves: object = None
    prepared_margin: object = None
    portfolio: list = field(default_factory=list)
    npv: dict | None = None
    margin: dict | None = None

    def calculate_npv(self, records):
        from financial_contracts import price_forward
        if self.node_curves is None or self.discount_curve is None:
            raise ValueError("Нет успешно рассчитанных зерновой и процентной кривых")
        trades = parse_trades(records)
        values = {t.trade_id: price_forward(t, self.node_curves, discount_curve=self.discount_curve) for t in trades}
        with localcontext() as context:
            context.prec, context.rounding = 28, ROUND_HALF_EVEN
            portfolio = sum(values.values(), Decimal(0))
        return {"currency": "RUB", "valuation_date": self.info["calculation_date"],
                "trades": values, "portfolio": portfolio}

    def calculate_margin(self, records):
        if self.prepared_margin is None:
            raise ValueError("Сценарии ГО недоступны: смотри ошибку подготовки ГО")
        return self.prepared_margin.calculate(parse_trades(records))


def run_calculations(*, input_dir=None, defaults_dir=None, output_dir=None, example=False,
                     calculation_date=None, parameters_file=None, allow_synthetic=False,
                     now=None, download=None, progress=None):
    now = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    folder = new_run_folder(output_dir or ROOT / "output", now)
    result = RunResult(folder, {"run_id": folder.name, "started_at_moscow": now.isoformat(),
                              "mode": "example" if example else "normal", "modules": {},
                              "sources": {}, "changes": [], "warnings": [], "synthetic": False})
    files = InputFiles(input_dir or ROOT / "input", defaults_dir or ROOT / "default_input", folder, example=example)
    result.info["sources"] = files.sources

    def stage(name, operation):
        if progress:
            progress(name)
        try:
            value = operation()
            result.info["modules"][name] = {"status": "ok"}
            return value
        except Exception as exc:
            result.info["modules"][name] = {"status": "error", "type": type(exc).__name__, "message": str(exc)}
            return None

    def settings():
        settings_path = (files.defaults if example else files.user) / "run_settings.json"
        config = read_json(settings_path) if settings_path.exists() else {}
        if not isinstance(config, dict) or set(config) - {"calculation_date"}:
            raise ValueError("run_settings.json: допустимо только calculation_date")
        if settings_path.exists():
            shutil.copyfile(settings_path, folder / "input" / "run_settings.json")
        chosen = calculation_date or config.get("calculation_date")
        if example and chosen is None:
            raise ValueError("В комплектном примере нужна фиксированная calculation_date")
        day = date.fromisoformat(chosen) if chosen is not None else now.date()
        result.info["calculation_date"] = day.isoformat()
        result.info["date_source"] = "argument" if calculation_date else ("run_settings" if chosen else "moscow_clock")
        return day

    day = stage("settings", settings)
    def parameters():
        source = Path(parameters_file or DEFAULT_PARAMETERS_PATH)
        target = folder / "parameters.json"
        shutil.copyfile(source, target)
        params = load_parameters(target)
        result.info["parameters_source"] = str(source.resolve())
        return params
    params = stage("parameters", parameters)
    if day is not None and params is not None:
        def grain():
            previous, reference = files.date_checked_block("wheat_curve_input.json", "previous_curve", GRAIN_FIELDS, day)
            events, observations = files.date_checked_block("wheat_curve_input.json", "events", GRAIN_FIELDS, day)
            kinds = (reference.get("data_kind", "UNSPECIFIED"), observations.get("data_kind", "UNSPECIFIED"))
            if any(k not in ("REAL", "SYNTHETIC_TEST", "UNSPECIFIED") for k in kinds):
                raise ValueError("data_kind: REAL либо SYNTHETIC_TEST")
            result.info["grain_data_kinds"] = list(kinds)
            result.info["synthetic"] |= "SYNTHETIC_TEST" in kinds
            override_document = files.document("default" if example else "input", "wheat_curve_input.json", GRAIN_FIELDS)
            overrides = override_document[0].get("parameter_overrides") if override_document else None
            value = calculate(previous, events, calculation_date=day.isoformat(),
                              previous_date=reference["previous_date"], parameter_overrides=overrides,
                              parameters_file=folder / "parameters.json")
            write_json(folder / "input" / "effective_wheat_curve_input.json", {
                "calculation_date": day.isoformat(), "previous_date": reference["previous_date"],
                "previous_curve": previous, "events": events, "parameter_overrides": overrides,
                "data_kind": "SYNTHETIC_TEST" if result.info["synthetic"] else kinds[0]})
            write_json(folder / "results" / "audit.json", value.pop("audit"))
            write_json(folder / "results" / "curve.json", value)
            write_json(folder / "results" / "effective_forward_parameters.json", value["parameters"])
            return value
        result.curve = stage("wheat_curve", grain)

        if result.curve is not None:
            stage("excel", lambda: export_today_curve(result.curve, folder / "excel" / "today_curve.xlsx"))
            def images():
                from wheat_curve.plotting import save_images
                save_images(result.curve, folder / "images")
            stage("images", images)
            def daily_curve():
                nodes = standard_nodes(day)
                prices = {p["tenor"]: p["price"] for p in result.curve["curve"]}
                points = []
                target = nodes[0].maturity_date
                while target <= nodes[-1].maturity_date:
                    points.append({"date": target.isoformat(), "price": interpolate(prices, day, target)})
                    target += timedelta(days=1)
                write_json(folder / "results" / "interpolated_curve.json", points)
            stage("interpolation", daily_curve)

        def interest():
            curve = (download or download_moex_g_curve)(day)
            if curve.valuation_date != day:
                raise ValueError("MOEX вернула кривую за другую дату")
            write_json(folder / "input" / "downloaded_moex_g_curve.json", curve.to_iss_response())
            result.info["sources"]["g_curve"] = {"source": "MOEX_HTTPS", "date": day.isoformat(),
                                                       "snapshot_time_moscow": curve.snapshot_time.isoformat()}
            factors = {n.tenor: curve.discount_factor(n.maturity_date) for n in standard_nodes(day)}
            write_json(folder / "results" / "discount_factors.json", {
                "valuation_date": day.isoformat(), "day_count": "ACT/365F", "discount_factors": factors})
            return curve
        result.discount_curve = stage("g_curve", interest)

        def trades():
            records, _ = files.date_checked_block("portfolio_margin_input.json", "trades", MARGIN_FIELDS, day)
            parse_trades(records)
            write_json(folder / "input" / "effective_portfolio.json", {"calculation_date": day, "trades": records})
            return records
        portfolio = stage("portfolio_input", trades)
        if portfolio is not None:
            result.portfolio = portfolio

        def valuation_curves():
            from financial_contracts import NodeCurves
            if result.curve is None or result.discount_curve is None:
                raise ValueError("Нужны успешно построенная сегодняшняя кривая и G-curve")
            prices = tuple(p["price"] for p in result.curve["curve"])
            rates = tuple(result.discount_curve.continuous_rate(Decimal((n.maturity_date - day).days) / 365)
                          for n in standard_nodes(day))
            return NodeCurves(day, prices, rates)
        result.node_curves = stage("valuation_curves", valuation_curves)

        def npv():
            if portfolio is None:
                raise ValueError("Нет корректного входного портфеля")
            value = result.calculate_npv(portfolio)
            write_json(folder / "results" / "npv.json", value)
            return value
        result.npv = stage("npv", npv)

        def prepare():
            from guarantee_collateral import MarketHistory, TradingCalendar
            from guarantee_collateral.calculate_portfolio_margin import prepare_portfolio_margin
            if result.node_curves is None:
                raise ValueError("Нет успешно построенных кривых на дату расчёта")
            history, context = files.date_checked_block("portfolio_margin_input.json", "history", MARGIN_FIELDS, day)
            kind = context["data_kind"]
            grain_kinds = result.info["grain_data_kinds"]
            if "UNSPECIFIED" in grain_kinds:
                raise ValueError("Для ГО укажи data_kind зерновых данных: REAL или SYNTHETIC_TEST")
            if kind == "SYNTHETIC_TEST" or "SYNTHETIC_TEST" in grain_kinds:
                kind = "SYNTHETIC_TEST"
                result.info["synthetic"] = True
            if kind != "REAL" and not (example or allow_synthetic):
                raise ValueError("Смешанные синтетические данные требуют --allow-synthetic-test-data")
            # Validate the supplied archive before replacing its last record.
            original = MarketHistory.from_records(history, data_kind=context["data_kind"],
                                                  source_description=context["history_source"])
            calendar_data, _ = files.block("moex_trading_calendar.json", "trading_dates", CALENDAR_FIELDS)
            source = files.sources["moex_trading_calendar.json:trading_dates"]["file"]
            calendar = TradingCalendar.load(folder / source)
            calendar.validate_history(original.dates)
            factors = {n.tenor: result.discount_curve.discount_factor(n.maturity_date) for n in standard_nodes(day)}
            merged, action = merge_history(history, day, result.curve["curve"], factors)
            market = MarketHistory.from_records(merged, data_kind=kind,
                source_description=context["history_source"] + "; current record rebuilt in run " + folder.name)
            calendar.validate_history(market.dates)
            result.info["changes"].append({"date": day.isoformat(), "history_today": action,
                                          "reason": "Использована новая рассчитанная кривая и загруженная G-curve"})
            write_json(folder / "input" / "effective_market_history.json", {
                "calculation_date": day, "data_kind": kind, "history_source": market.source_description,
                "history": merged})
            prepared = prepare_portfolio_margin(market, calendar, parameters_file=folder / "parameters.json",
                                                allow_synthetic_test_data=example or allow_synthetic)
            import numpy as np
            np.savez_compressed(folder / "results" / "prepared_scenarios.npz",
                fhs_forward=prepared.engine.fhs[0], fhs_zero=prepared.engine.fhs[1],
                historical_forward=prepared.engine.historical[0], historical_zero=prepared.engine.historical[1])
            return prepared
        result.prepared_margin = stage("margin_preparation", prepare)

        def margin():
            if portfolio is None:
                raise ValueError("Нет корректного входного портфеля")
            value = result.calculate_margin(portfolio)
            write_json(folder / "results" / "portfolio_margin.json", value)
            return value
        result.margin = stage("margin", margin)

    result.info["finished_at_moscow"] = datetime.now(MOSCOW).isoformat()
    defaults = [k for k, v in files.sources.items() if v.get("source") == "default"]
    if defaults:
        result.info["warnings"].append({"default_blocks_used": defaults})
    if result.info["synthetic"]:
        result.info["warnings"].append("Есть синтетические данные. Результат не является оценкой реального рынка.")
    write_json(folder / "run_info.json", result.info)
    return result
