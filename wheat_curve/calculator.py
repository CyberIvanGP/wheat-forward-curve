"""Public Python API. Input values are strings, integers or Decimal, never float."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, time
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from project_parameters import load_parameters

from .domain.emmi import decompose_observation
from .domain.models import EventType, RawEvent, Side, TenorParameters, TradeType
from .domain.tenors import STANDARD_TENORS, standard_nodes, tenor_to_date
from .engine.daily_calculator import compute_daily
from .engine.event_processor import process_day, sort_events
from .engine.multi_day_calculator import combine_tenor
from .engine.order_book import OrderBook

TENORS = list(STANDARD_TENORS)
VERSION = "wheat-eod-standalone-1.1"


def number(value, field: str, *, zero: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError(f"{field}: используйте строку, int или Decimal, не float/bool")
    try:
        result = Decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field}: некорректное десятичное число") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError(f"{field}: требуется конечное {'неотрицательное' if zero else 'положительное'} число")
    return result


def parameters(overrides: dict | None = None, *, parameters_file=None) -> dict[str, TenorParameters]:
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("parameter_overrides: требуется объект по узлам")
    result = {}
    defaults = load_parameters(parameters_file)["forward_curve"]
    fields = {"min_order_volume", "min_trade_volume", "threshold_trade_volume", "spread_max"}
    if set(defaults) != set(TENORS):
        raise ValueError("Параметры должны содержать все 39 узлов")
    for tenor in TENORS:
        values = defaults[tenor]
        if not isinstance(values, dict) or set(values) != fields:
            raise ValueError(f"Неверные параметры узла {tenor}")
        result[tenor] = TenorParameters(tenor, **{
            field: number(values[field], field, zero=(field == "spread_max")) for field in fields})
    fields = {"min_order_volume", "min_trade_volume", "threshold_trade_volume", "spread_max"}
    for tenor, changes in (overrides or {}).items():
        if tenor not in result or not isinstance(changes, dict):
            raise ValueError(f"parameters: неизвестный узел или неверные параметры: {tenor}")
        for field, value in changes.items():
            if field not in fields:
                raise ValueError(f"parameters.{tenor}: неизвестное поле {field}")
            setattr(result[tenor], field, number(value, field, zero=(field == "spread_max")))
    return result


def _previous(points: list[dict], previous_date: date):
    if not isinstance(points, list) or not points:
        raise ValueError("previous_curve: нужен непустой массив точек")
    curve, streaks, seen = {}, {}, set()
    by_date = {n.maturity_date.isoformat(): n.tenor for n in standard_nodes(previous_date, TENORS)}
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("previous_curve: каждая точка должна быть объектом")
        if set(point) - {"tenor", "maturity_date", "price", "fallback_streak", "source",
                         "trade_price", "trade_volume", "order_price", "trade_weight", "valid_snapshots"}:
            raise ValueError("previous_curve: неизвестное поле точки")
        tenor = point.get("tenor") or by_date.get(point.get("maturity_date"))
        if tenor not in TENORS or tenor in seen:
            raise ValueError(f"previous_curve: неизвестный или повторный tenor {tenor}")
        seen.add(tenor)
        if "maturity_date" in point and point["maturity_date"] != tenor_to_date(previous_date, tenor).isoformat():
            raise ValueError(f"previous_curve.{tenor}: дата не соответствует previous_date и tenor")
        streak = point.get("fallback_streak", 0)
        if type(streak) is not int or streak < 0:
            raise ValueError(f"previous_curve.{tenor}: fallback_streak должен быть целым >= 0")
        if "price" not in point:
            raise ValueError(f"previous_curve.{tenor}: отсутствует price")
        if point["price"] is not None:
            curve[tenor] = number(point["price"], f"previous_curve.{tenor}.price")
        streaks[tenor] = streak
    return curve, streaks


def _events(items: list[dict], calculation_date: date) -> list[RawEvent]:
    if not isinstance(items, list):
        raise ValueError("events: нужен массив событий")
    result, ids, trades = [], set(), set()
    fields = {"event_id", "event_type", "time", "order_id", "trade_id", "side", "price",
              "quantity", "maturity_date", "trade_type", "buy_order_id", "sell_order_id", "comment"}
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) - fields:
            raise ValueError(f"events[{i}]: неверный объект или неизвестные поля")
        try:
            eid = item["event_id"]
            if not isinstance(eid, str) or not eid.strip() or eid in ids:
                raise ValueError("event_id должен быть непустым и уникальным")
            ids.add(eid)
            kind = EventType(item["event_type"])
            clock = time.fromisoformat(item["time"])
            if clock.tzinfo is not None or not time(10) <= clock <= time(18):
                raise ValueError("time: требуется местное время Москвы в окне 10:00–18:00")
            values = {k: item[k] for k in fields - {"event_id", "event_type", "time"} if k in item}
            for field in ("order_id", "trade_id", "buy_order_id", "sell_order_id"):
                if field in values and (not isinstance(values[field], str) or not values[field].strip()):
                    raise ValueError(f"{field}: требуется непустая строка")
            for field in ("price", "quantity"):
                if field in values:
                    values[field] = number(values[field], field)
            if "side" in values:
                values["side"] = Side(values["side"])
            if "maturity_date" in values:
                maturity = date.fromisoformat(values["maturity_date"])
                if not calculation_date < maturity <= tenor_to_date(calculation_date, "36M"):
                    raise ValueError("maturity_date: срок должен быть от 1D до 36M")
                values["maturity_date"] = maturity
            if "trade_type" in values:
                values["trade_type"] = TradeType(values["trade_type"])
            if kind == EventType.TRADE:
                required = {"price", "quantity", "maturity_date", "trade_id", "trade_type"}
                tid = values.get("trade_id")
                if tid in trades:
                    raise ValueError(f"повторный trade_id: {tid}")
                trades.add(tid)
            elif kind == EventType.ORDER_NEW:
                required = {"order_id", "side", "price", "quantity", "maturity_date"}
                if values.get("trade_type", TradeType.NON_ADDRESS) != TradeType.NON_ADDRESS:
                    raise ValueError("для стакана допустимы только NON_ADDRESS лимитные заявки")
            else:
                required = {"order_id", "quantity"} if kind == EventType.ORDER_FILL else {"order_id"}
                if "side" in values or "trade_type" in values:
                    raise ValueError("изменение стороны/типа требует новой заявки")
                if kind == EventType.ORDER_MODIFY and not {"price", "quantity", "maturity_date"} & values.keys():
                    raise ValueError("ORDER_MODIFY: нет изменяемых полей")
            if required - values.keys():
                raise ValueError(f"не хватает полей: {', '.join(sorted(required - values.keys()))}")
            allowed_values = {
                EventType.TRADE: {"trade_id", "price", "quantity", "maturity_date", "trade_type", "buy_order_id", "sell_order_id", "side"},
                EventType.ORDER_NEW: {"order_id", "side", "price", "quantity", "maturity_date", "trade_type"},
                EventType.ORDER_MODIFY: {"order_id", "price", "quantity", "maturity_date"},
                EventType.ORDER_CANCEL: {"order_id"},
                EventType.ORDER_FILL: {"order_id", "quantity"},
            }[kind] | {"comment"}
            if values.keys() - allowed_values:
                raise ValueError(f"лишние поля для {kind.value}: {', '.join(sorted(values.keys() - allowed_values))}")
            result.append(RawEvent(i, 1, clock, eid, kind, **values))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"events[{i}]: {exc}") from exc
    return result


def _validate_observation(identifier, maturity, price, quantity, side, reference, nodes):
    observations = decompose_observation(source_id=identifier, maturity=maturity,
        price=price, quantity=quantity, side=side,
        reference_curve=reference, standard_nodes_list=nodes)
    if not observations:
        raise ValueError(f"{identifier}: REFERENCE_NODE_MISSING для EMMI")
    if any(o.price <= 0 for o in observations):
        raise ValueError(f"{identifier}: EMMI дал неположительную цену")


def _validate_book(events: list[RawEvent], reference, nodes) -> None:
    book, issues = OrderBook(), []
    for event in sort_events(events):
        if event.event_type == EventType.TRADE:
            continue
        if event.event_type == EventType.ORDER_FILL:
            order = book.active.get(event.order_id)
            if order is not None and event.quantity > order.remaining_quantity:
                raise ValueError(f"{event.event_id}: ORDER_FILL превышает остаток заявки")
        book.apply(event, issues)
        if issues:
            raise ValueError(f"{event.event_id}: {issues[-1].message}")
        if event.event_type in (EventType.ORDER_NEW, EventType.ORDER_MODIFY):
            order = book.active[event.order_id]
            _validate_observation(event.event_id, order.maturity_date, order.price,
                                  order.remaining_quantity, order.side, reference, nodes)
        books = {}
        for order in book.active.values():
            bid, ask = books.get(order.maturity_date, (None, None))
            if order.side == Side.BUY:
                bid = order.price if bid is None else max(bid, order.price)
            else:
                ask = order.price if ask is None else min(ask, order.price)
            books[order.maturity_date] = bid, ask
            if bid is not None and ask is not None and bid > ask:
                raise ValueError(f"{event.event_id}: пересечён исходный стакан, BestBid > BestAsk")


def calculate(previous_curve: list[dict], events: list[dict], *, calculation_date: str,
              previous_date: str, parameter_overrides: dict | None = None, parameters_file=None) -> dict:
    """Return Decimal-valued result. Invalid input raises ValueError; no partial publication.

    Tenors roll with calculation_date. A fallback carries the price of the SAME TENOR,
    not a fixed calendar expiry. Previous maturities are not re-interpolated to today's dates.
    """
    with localcontext() as context:
        context.prec, context.rounding = 28, ROUND_HALF_EVEN
        today, yesterday = date.fromisoformat(calculation_date), date.fromisoformat(previous_date)
        if yesterday >= today:
            raise ValueError("previous_date должна быть раньше calculation_date")
        reference, streaks = _previous(previous_curve, yesterday)
        params = parameters(parameter_overrides, parameters_file=parameters_file)
        parsed = _events(events, today)
        excluded = [e for e in parsed if e.event_type == EventType.TRADE and e.trade_type == TradeType.ADDRESS]
        parsed = [e for e in parsed if not (e.event_type == EventType.TRADE and e.trade_type == TradeType.ADDRESS)]
        nodes = standard_nodes(today, TENORS)
        _validate_book(parsed, reference, nodes)
        # Check each price-bearing observation before the core can silently drop it.
        for event in parsed:
            if event.event_type != EventType.TRADE:
                continue
            _validate_observation(event.event_id, event.maturity_date, event.price,
                                  event.quantity, event.side, reference, nodes)
        issues = [{"level": "warning", "message": "ADDRESS_TRADE_EXCLUDED",
                   "event_id": e.event_id, "trade_id": e.trade_id, "row_index": e.row_index}
                  for e in excluded]
        day_data = process_day(1, parsed, issues)
        diagnostics = compute_daily(1, day_data, today, reference, params, TENORS)
        curve = []
        for node in nodes:
            final = combine_tenor(node.tenor, diagnostics.tenor_diagnostics[node.tenor], params[node.tenor],
                                  reference.get(node.tenor), streaks.get(node.tenor, 0))
            if final.fallback_streak >= 2:
                issues.append({"level": "warning", "message": "METHODOLOGY_REVIEW_REQUIRED",
                               "tenor": node.tenor, "fallback_streak": final.fallback_streak})
            curve.append({"tenor": node.tenor, "maturity_date": node.maturity_date.isoformat(),
                          "price": final.final_price, "source": final.source.value,
                          "fallback_streak": final.fallback_streak,
                          "trade_price": final.trade_price, "trade_volume": final.trade_volume,
                          "order_price": final.order_price, "trade_weight": final.weight,
                          "valid_snapshots": final.valid_snapshots})
        return {"methodology_version": VERSION, "calculation_date": calculation_date,
                "previous_date": previous_date, "price_unit": "RUB/t", "quantity_unit": "t",
                "parameters": {t: asdict(p) for t, p in params.items()}, "curve": curve,
                "previous_curve": [{"tenor": t, "maturity_date": tenor_to_date(yesterday, t).isoformat(),
                                    "price": reference.get(t), "fallback_streak": streaks.get(t, 0)} for t in TENORS],
                "issues": [asdict(i) if not isinstance(i, dict) else i for i in issues],
                "audit": {t: asdict(d) for t, d in diagnostics.tenor_diagnostics.items()}}
