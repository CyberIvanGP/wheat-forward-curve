"""Generate a dense synthetic event log with two broad price humps, explicitly once."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path
import random
import shutil

from calculation_run.files import MOSCOW, read_json, write_json
from financial_contracts.forward_valuation import decimal_number
from project_parameters import load_parameters
from wheat_curve import calculate
from wheat_curve.domain.curve import log_linear_price
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes, tenor_to_date

ROOT = Path(__file__).resolve().parents[1]
PARAMETERS = Path(__file__).with_name("parameters.json")


def target_prices(day, anchors):
    if not isinstance(anchors, list) or len(anchors) < 2:
        raise ValueError("Нужны минимум две опорные точки формы")
    points = [(tenor_to_date(day, a["tenor"]), decimal_number(a["price"], "price", positive=True)) for a in anchors]
    if any(a[0] >= b[0] for a, b in zip(points, points[1:])):
        raise ValueError("Опорные точки должны возрастать по срокам")
    nodes = standard_nodes(day)
    if points[0][0] != nodes[0].maturity_date or points[-1][0] != nodes[-1].maturity_date:
        raise ValueError("Опорные точки должны покрывать 1D–36M")
    result = {}
    for node in nodes:
        for (left, lp), (right, rp) in zip(points, points[1:]):
            if left <= node.maturity_date <= right:
                weight = Decimal((node.maturity_date-left).days) / Decimal((right-left).days)
                result[node.tenor] = log_linear_price(lp, rp, weight)
                break
    return result


def build_input(settings=None, *, calculation_parameters=None):
    settings = read_json(PARAMETERS) if settings is None else settings
    params = load_parameters(calculation_parameters)["forward_curve"]
    day, previous = (date.fromisoformat(settings[k]) for k in ("calculation_date", "previous_date"))
    if previous >= day:
        raise ValueError("previous_date должна быть раньше calculation_date")
    sessions, levels, trades_count = (settings[k] for k in ("sessions", "levels_per_side", "trades_per_session_per_node"))
    if any(type(v) is not int or v < 1 for v in (sessions, levels, trades_count)):
        raise ValueError("Количество сессий, уровней и сделок должно быть положительным целым")
    if sessions > 24 or levels > 20 or trades_count > 100 or trades_count % 2:
        raise ValueError("Допустимо до 24 сессий, 20 уровней, чётное число сделок до 100 на сессию")
    noise, half, step, fraction, previous_factor = (
        decimal_number(settings[k], k) for k in ("session_noise_fraction", "half_spread_fraction",
                                               "level_step_fraction", "daily_trade_full_weight_fraction",
                                               "previous_price_factor"))
    if not (0 <= noise < half < Decimal(".01") and step > 0 and 0 < fraction < 1 and previous_factor > 0):
        raise ValueError("Некорректные амплитуды, доля полного веса или коэффициент вчерашней цены")
    rng = random.Random(settings["seed"])
    # One common intraday displacement keeps small node noise from creating extra humps.
    shifts = [Decimal(rng.randint(-1000, 1000)) / 1000 * noise for _ in range(sessions)]
    events, targets = [], target_prices(day, settings["shape_anchors"])
    opening = datetime.combine(day, datetime.min.time()) + timedelta(hours=10)
    session_seconds = 28800 // sessions
    serial = 0

    def emit(kind, when, **fields):
        nonlocal serial
        serial += 1
        events.append({"event_id": f"event-{serial:06d}", "event_type": kind,
                       "time": when.time().isoformat(), **fields})

    for node in standard_nodes(day):
        tenor, maturity = node.tenor, node.maturity_date.isoformat()
        config = params[tenor]
        depth = Decimal(config["min_order_volume"])
        daily_volume = Decimal(config["threshold_trade_volume"]) * fraction
        if daily_volume < Decimal(config["min_trade_volume"]):
            raise ValueError(f"{tenor}: выбранный объём ниже минимума сделок")
        quantity = daily_volume / (sessions * trades_count)
        # Each execution leaves an active quote, then replenishment restores its original size.
        order_volume = max(depth, quantity * 2)
        half_here = min(half, Decimal(config["spread_max"]) / 4)
        if half_here <= 0:
            raise ValueError("Для демонстрации нужен положительный допустимый спред")
        for session in range(sessions):
            start = opening + timedelta(seconds=session * session_seconds)
            if session:
                for side in ("BUY", "SELL"):
                    for level in range(levels):
                        emit("ORDER_CANCEL", start, order_id=f"{tenor}-{session-1}-{side}-{level}")
            center = targets[tenor] * (1 + shifts[session])
            best = {}
            for side, sign in (("BUY", -1), ("SELL", 1)):
                for level in range(levels):
                    price = (center * (1 + sign * (half_here + level * step))).quantize(Decimal(".000001"))
                    identifier = f"{tenor}-{session}-{side}-{level}"
                    emit("ORDER_NEW", start, order_id=identifier, side=side, price=price,
                         quantity=order_volume, maturity_date=maturity, trade_type="NON_ADDRESS")
                    if level == 0:
                        best[side] = (identifier, price)
            for i in range(trades_count):
                when = start + timedelta(seconds=(i+1) * session_seconds // (trades_count+1))
                resting_side = "SELL" if i % 2 == 0 else "BUY"
                identifier, price = best[resting_side]
                trade_id = f"{tenor}-{session}-trade-{i}"
                emit("TRADE", when, trade_id=trade_id, price=price, quantity=quantity,
                     maturity_date=maturity, trade_type="NON_ADDRESS",
                     **{"sell_order_id" if resting_side == "SELL" else "buy_order_id": identifier})
                emit("ORDER_FILL", when, order_id=identifier, quantity=quantity)
                emit("ORDER_MODIFY", when, order_id=identifier, quantity=order_volume)
    # Stable sorting retains TRADE -> FILL -> MODIFY and cancellation-before-replacement order.
    events.sort(key=lambda e: e["time"])
    return {"calculation_date": day.isoformat(), "previous_date": previous.isoformat(),
            "data_kind": "SYNTHETIC_TEST",
            "source_description": "Fixed dense synthetic demo, two broad humps; not observed market liquidity",
            "previous_curve": [{"tenor": t, "maturity_date": tenor_to_date(previous, t).isoformat(),
                                "price": (targets[t] * previous_factor).quantize(Decimal(".000001")),
                                "fallback_streak": 0} for t in STANDARD_TENORS],
            "events": events}


def verify_input(data, calculation_parameters=None):
    args = {k: data[k] for k in ("calculation_date", "previous_date", "previous_curve", "events")}
    result = calculate(**args, parameters_file=calculation_parameters)
    if any(p["source"] != "MIXED" or p["valid_snapshots"] != 160 for p in result["curve"]):
        raise ValueError("Проверка генератора: все 39 узлов должны быть MIXED, 160 допустимых срезов")
    curve = result["curve"]
    peaks = [b["tenor"] for a, b, c in zip(curve, curve[1:], curve[2:]) if a["price"] < b["price"] > c["price"]]
    if peaks != ["18M", "30M"]:
        raise ValueError(f"Проверка формы: ожидались максимумы 18M, 30M, получены {peaks}")
    counts = dict(Counter(e["event_type"] for e in data["events"]))
    return {"counts": counts, "node_count": 39, "observed_nodes": 39, "carried_nodes": 0,
            "peaks": [{"tenor": p["tenor"], "price": p["price"]} for p in curve if p["tenor"] in peaks],
            "all_nodes_mixed": True, "valid_snapshots_per_node": 160}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Явно создать фиксированный плотный демонабор с двумя горбами")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--generator-parameters", type=Path, default=PARAMETERS)
    parser.add_argument("--calculation-parameters", type=Path)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args(argv)
    files = ("wheat_curve_input.json", "run_settings.json", "generation_info.json")
    existing = [args.output_dir / name for name in files if (args.output_dir / name).exists()]
    if existing and not args.replace_existing:
        parser.error("Файлы уже существуют. Обычный запуск их не заменяет; нужен явный --replace-existing")
    settings = read_json(args.generator_parameters)
    with localcontext() as ctx:
        ctx.prec = 28
        data = build_input(settings, calculation_parameters=args.calculation_parameters)
        verification = verify_input(data, args.calculation_parameters)
    stamp = datetime.now(MOSCOW).strftime("%Y-%m-%d_%H-%M-%S_%f")
    if existing:
        backup = ROOT / "output" / ("default_input_backup_" + stamp)
        backup.mkdir(parents=True)
        for path in existing:
            shutil.copyfile(path, backup / path.name)
        print(f"Previous data backup: {backup}", flush=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "wheat_curve_input.json", data)
    write_json(args.output_dir / "run_settings.json", {"calculation_date": data["calculation_date"]})
    info = {"data_kind": "SYNTHETIC_TEST", "generated_at_moscow": stamp,
            "settings": settings, "verification": verification,
            "wheat_input_sha256": hashlib.sha256((args.output_dir / "wheat_curve_input.json").read_bytes()).hexdigest(),
            "automatic_regeneration": False}
    write_json(args.output_dir / "generation_info.json", info)
    print(verification, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
