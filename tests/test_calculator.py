import copy
from datetime import date, timedelta
from decimal import Decimal, getcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from wheat_curve import calculate
from wheat_curve.calculator import TENORS, parameters
from wheat_curve.domain.curve import interpolate, log_linear_price
from wheat_curve.domain.dates import snapshot_times
from wheat_curve.domain.emmi import decompose_observation
from wheat_curve.domain.tenors import standard_nodes, tenor_to_date

ROOT = Path(__file__).resolve().parents[1]


class CalculatorTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/default_input.json").read_text(encoding="utf-8"))

    def calc(self):
        return calculate(**self.data)

    def event(self, kind, **values):
        base = {"event_id": "single", "event_type": kind, "time": "12:00:00"}
        self.data["events"] = [{**base, **values}]

    def trade(self, **changes):
        self.event("TRADE", trade_id="t1", price="21000", quantity="800",
                   maturity_date="2026-11-15", trade_type="NON_ADDRESS")
        self.data["events"][0].update(changes)

    def node(self, result, tenor):
        return next(p for p in result["curve"] if p["tenor"] == tenor)

    def test_default_example(self):
        result = self.calc()
        self.assertEqual(len(result["curve"]), 39)
        self.assertEqual(self.node(result, "4M")["price"], Decimal("20625"))
        self.assertEqual(self.node(result, "4M")["trade_weight"], Decimal(".25"))
        self.assertEqual(self.node(result, "4M")["valid_snapshots"], 21)
        self.assertEqual(self.node(result, "9M")["source"], "ORDERS_ONLY")
        self.assertEqual(self.node(result, "9M")["valid_snapshots"], 160)
        self.assertEqual(self.node(result, "7M")["source"], "TRADES_ONLY")

    def test_empty_events_carry_all(self):
        self.data["events"] = []
        for p, old in zip(self.calc()["curve"], self.data["previous_curve"]):
            self.assertEqual(p["price"], Decimal(old["price"]))
            self.assertEqual(p["source"], "FALLBACK_PREVIOUS_DAY")
            self.assertEqual(p["fallback_streak"], 1)

    def test_fallback_streak_warning(self):
        self.data["events"] = []
        self.data["previous_curve"][0]["fallback_streak"] = 1
        result = self.calc()
        self.assertEqual(self.node(result, "1D")["fallback_streak"], 2)
        self.assertEqual(result["issues"][0]["message"], "METHODOLOGY_REVIEW_REQUIRED")

    def test_result_can_be_fed_into_next_day(self):
        first = self.calc()
        second = calculate(first["curve"], [], calculation_date="2026-10-16", previous_date="2026-10-15")
        self.assertEqual(self.node(second, "4M")["price"], self.node(first, "4M")["price"])

    def test_exact_trade_threshold_and_reset_streak(self):
        self.trade()
        self.data["previous_curve"][3]["fallback_streak"] = 9
        node = self.node(self.calc(), "1M")
        self.assertEqual(node["price"], Decimal("21000"))
        self.assertEqual(node["fallback_streak"], 0)
        self.assertEqual(node["source"], "TRADES_ONLY")

    def test_trade_below_threshold(self):
        self.trade(quantity="799.999")
        self.assertEqual(self.node(self.calc(), "1M")["source"], "FALLBACK_PREVIOUS_DAY")

    def test_address_trade_excluded_and_reported(self):
        self.trade(trade_type="ADDRESS")
        result = self.calc()
        self.assertEqual(self.node(result, "1M")["source"], "FALLBACK_PREVIOUS_DAY")
        self.assertEqual(result["issues"][0]["message"], "ADDRESS_TRADE_EXCLUDED")

    def test_only_non_address_volume_used(self):
        self.trade(quantity="400")
        self.data["events"].append(dict(self.data["events"][0], event_id="address", trade_id="address",
                                        trade_type="ADDRESS", quantity="1000", price="30000"))
        node = self.node(self.calc(), "1M")
        self.assertEqual(node["trade_volume"], Decimal(400))
        self.assertEqual(node["source"], "FALLBACK_PREVIOUS_DAY")

    def test_160_snapshot_endpoints(self):
        times = snapshot_times()
        self.assertEqual(len(times), 160)
        self.assertEqual(times[0].isoformat(), "10:03:00")
        self.assertEqual(times[-1].isoformat(), "18:00:00")

    def test_short_lived_quotes_count_only_at_snapshot_time(self):
        originals = copy.deepcopy(self.data["events"][:2])
        self.data["events"] = []
        for i in range(20):
            minute = 3 * (i + 1)
            before = f"10:{minute - 1:02d}:59"
            after = f"{10 + minute // 60:02d}:{minute % 60:02d}:01"
            for j, order in enumerate(originals):
                oid = f"short-{i}-{j}"
                self.data["events"].append(dict(order, event_id=oid, order_id=oid, time=before))
                self.data["events"].append({"event_id": oid + "-cancel", "event_type": "ORDER_CANCEL",
                                             "order_id": oid, "time": after})
        node = self.node(self.calc(), "4M")
        self.assertEqual(node["valid_snapshots"], 20)
        self.assertEqual(node["source"], "ORDERS_ONLY")

    def test_no_opening_snapshot_and_close_event_included(self):
        originals = copy.deepcopy(self.data["events"][:2])
        self.data["events"] = [dict(o, time="10:00:00") for o in originals]
        self.data["events"] += [{"event_id": f"cancel-{i}", "event_type": "ORDER_CANCEL",
            "order_id": o["order_id"], "time": "10:01:00"} for i, o in enumerate(originals)]
        self.data["events"].append({"event_id": "close-trade", "event_type": "TRADE", "time": "18:00:00",
            "trade_id": "close-trade", "price": "20700", "quantity": "200",
            "maturity_date": "2027-02-15", "trade_type": "NON_ADDRESS"})
        node = self.node(self.calc(), "4M")
        self.assertEqual(node["valid_snapshots"], 0)
        self.assertEqual(node["source"], "TRADES_ONLY")

    def test_approved_full_weight_parameters(self):
        params = parameters()
        for tenor, volume in (("1D", 1600), ("3M", 1600), ("4M", 800), ("6M", 800),
                              ("7M", 400), ("12M", 400), ("13M", 400), ("24M", 400),
                              ("25M", 250), ("36M", 250)):
            self.assertEqual(params[tenor].threshold_trade_volume, Decimal(volume))

    def test_fallback_has_no_upper_limit(self):
        self.data["events"] = []
        for point in self.data["previous_curve"]:
            point["fallback_streak"] = 100
        result = self.calc()
        for node, old in zip(result["curve"], self.data["previous_curve"]):
            self.assertEqual(node["price"], Decimal(old["price"]))
            self.assertEqual(node["source"], "FALLBACK_PREVIOUS_DAY")
            self.assertEqual(node["fallback_streak"], 101)

    def test_parameter_override(self):
        self.trade(quantity="200")
        self.data["parameter_overrides"] = {"1M": {"min_trade_volume": "200"}}
        self.assertEqual(self.node(self.calc(), "1M")["source"], "TRADES_ONLY")

    def test_vwap(self):
        self.trade(quantity="400", price="20000")
        second = dict(self.data["events"][0], event_id="second", trade_id="second", price="22000")
        self.data["events"].append(second)
        self.assertEqual(self.node(self.calc(), "1M")["price"], Decimal("21000"))

    def test_valid_slice_count_19_20_21(self):
        originals = copy.deepcopy(self.data["events"][:2])
        for count, cancel_time in ((19, "11:00:00"), (20, "11:03:00"), (21, "11:06:00")):
            self.data["events"] = [dict(o, time="10:00:00") for o in originals]
            self.data["events"] += [{"event_id": f"cancel-{i}", "event_type": "ORDER_CANCEL",
                "order_id": o["order_id"], "time": cancel_time} for i, o in enumerate(originals)]
            result = self.calc()
            node = self.node(result, "4M")
            self.assertEqual(node["valid_snapshots"], count)
            self.assertEqual(node["source"], "ORDERS_ONLY" if count >= 20 else "FALLBACK_PREVIOUS_DAY")
            self.assertEqual(result["audit"]["4M"]["trimmed_low"], 2 if count == 21 else 0)

    def test_depth_threshold_and_spread_equality(self):
        self.data["events"] = self.data["events"][:2]
        for order, price in zip(self.data["events"], ("199", "201")):
            order.update(price=price, quantity="100", time="10:00:00")
        self.data["parameter_overrides"] = {"4M": {"spread_max": "0.01"}}
        self.assertEqual(self.node(self.calc(), "4M")["price"], Decimal(200))
        self.data["events"][0]["quantity"] = "99.999"
        self.assertEqual(self.node(self.calc(), "4M")["source"], "FALLBACK_PREVIOUS_DAY")

    def test_depth_is_marginal_not_average_price(self):
        self.data["events"] = self.data["events"][:2]
        for order, price in zip(self.data["events"], ("20600", "20700")):
            order.update(price=price, quantity="50", time="10:00:00")
        for order, price in zip(copy.deepcopy(self.data["events"]), ("20500", "20900")):
            order.update(event_id=order["event_id"] + "-2", order_id=order["order_id"] + "-2", price=price)
            self.data["events"].append(order)
        self.data["parameter_overrides"] = {"4M": {"spread_max": "0.02"}}
        result = self.calc()
        self.assertEqual(self.node(result, "4M")["price"], Decimal(20700))
        first = result["audit"]["4M"]["snapshot_details"][0]
        self.assertEqual(first["bid_depth"], Decimal(20500))
        self.assertEqual(first["ask_depth"], Decimal(20900))

    def test_modify_checks_resolved_maturity(self):
        self.data["events"] = self.data["events"][:1]
        self.data["previous_curve"] = [p for p in self.data["previous_curve"] if p["tenor"] != "5M"]
        self.data["events"].append({"event_id": "move", "event_type": "ORDER_MODIFY",
            "time": "12:00:00", "order_id": self.data["events"][0]["order_id"], "maturity_date": "2027-02-20"})
        with self.assertRaisesRegex(ValueError, "REFERENCE_NODE_MISSING"):
            self.calc()

    def test_trade_does_not_fill_book_implicitly(self):
        self.data["events"] = self.data["events"][:2]
        for o in self.data["events"]:
            o.update(time="10:00:00", quantity="100")
        self.data["events"].append({"event_id": "t", "event_type": "TRADE", "time": "12:00:00",
            "trade_id": "t", "price": "20700", "quantity": "100", "maturity_date": "2027-02-15",
            "trade_type": "NON_ADDRESS", "sell_order_id": self.data["events"][1]["order_id"]})
        self.assertEqual(self.node(self.calc(), "4M")["valid_snapshots"], 160)

    def test_inputs_not_mutated_and_determinism(self):
        original = copy.deepcopy(self.data)
        first = self.calc()
        self.assertEqual(first, self.calc())
        self.assertEqual(original, self.data)

    def test_order_sorting_by_time(self):
        expected = self.calc()
        self.data["events"] = sorted(self.data["events"], key=lambda e: e["time"])
        self.assertEqual(expected["curve"], self.calc()["curve"])

    def test_sparse_curve_is_not_invented(self):
        self.data["events"] = []
        self.data["previous_curve"] = self.data["previous_curve"][:1]
        result = self.calc()
        self.assertIsNone(self.node(result, "1M")["price"])
        self.assertEqual(self.node(result, "1M")["source"], "NONE")

    def test_missing_reference_rejected(self):
        self.trade(maturity_date="2026-11-20")
        self.data["previous_curve"] = self.data["previous_curve"][:1]
        with self.assertRaisesRegex(ValueError, "REFERENCE_NODE_MISSING"):
            self.calc()

    def test_invalid_numbers_rejected(self):
        for value in ("NaN", "Infinity", "0", "-1", 1.1, True):
            with self.subTest(value=value):
                self.trade(price=value)
                with self.assertRaises(ValueError):
                    self.calc()

    def test_duplicate_event_rejected(self):
        self.trade()
        self.data["events"] *= 2
        with self.assertRaises(ValueError):
            self.calc()

    def test_duplicate_trade_rejected(self):
        self.trade()
        self.data["events"].append(dict(self.data["events"][0], event_id="different"))
        with self.assertRaisesRegex(ValueError, "trade_id"):
            self.calc()

    def test_duplicate_order_rejected(self):
        first = self.data["events"][0]
        self.data["events"] = [first, dict(first, event_id="another")]
        with self.assertRaises(ValueError):
            self.calc()

    def test_crossed_book_rejected(self):
        self.data["events"] = self.data["events"][:2]
        self.data["events"][0]["price"] = "22000"
        with self.assertRaisesRegex(ValueError, "пересечён"):
            self.calc()

    def test_overfill_rejected(self):
        order = self.data["events"][0]
        self.data["events"] = [order, {"event_id": "fill", "event_type": "ORDER_FILL",
            "time": "12:00:00", "order_id": order["order_id"], "quantity": "301"}]
        with self.assertRaisesRegex(ValueError, "превышает"):
            self.calc()

    def test_unknown_order_rejected(self):
        self.event("ORDER_CANCEL", order_id="missing")
        with self.assertRaises(ValueError):
            self.calc()

    def test_missing_required_fields(self):
        self.trade()
        del self.data["events"][0]["quantity"]
        with self.assertRaises(ValueError):
            self.calc()

    def test_invalid_expiries_and_times(self):
        for changes in ({"maturity_date": "2026-10-15"}, {"maturity_date": "2029-10-16"},
                        {"time": "09:59:59"}, {"time": "18:00:01"}, {"time": "12:00:00+03:00"}):
            self.trade(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.calc()

    def test_strict_parameters(self):
        for changes in ({"1M": {"threshold_trade_volume": "0"}}, {"1M": {"unknown": "1"}},
                        {"unknown": {}}, []):
            self.data["parameter_overrides"] = changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.calc()

    def test_invalid_previous_curve(self):
        for points in ([], [self.data["previous_curve"][0]] * 2,
                       [{"tenor": "1D", "maturity_date": "2026-10-17", "price": "20000"}]):
            self.data["previous_curve"] = points
            with self.assertRaises(ValueError):
                self.calc()

    def test_local_decimal_context(self):
        expected = self.calc()["curve"]
        old = getcontext().prec
        try:
            getcontext().prec = 9
            self.assertEqual(expected, self.calc()["curve"])
        finally:
            getcontext().prec = old

    def test_log_linear_and_missing_segment(self):
        self.assertAlmostEqual(log_linear_price(Decimal(100), Decimal(121), Decimal(".5")), Decimal(110), places=23)
        base = date(2026, 10, 15)
        left, right = tenor_to_date(base, "1M"), tenor_to_date(base, "2M")
        target = left + (right - left) // 2
        self.assertIsNone(interpolate({"1M": Decimal(100)}, base, target, TENORS))
        self.assertIsNone(interpolate({"1M": Decimal(100)}, base, base, TENORS))

    def test_emmi_invariants(self):
        nodes = standard_nodes(date(2026, 10, 15), TENORS)
        reference = {t: Decimal(20000 + i * 100) for i, t in enumerate(TENORS)}
        obs = decompose_observation(source_id="test", maturity=date(2027, 5, 1), price=Decimal(21000),
            quantity=Decimal(200), side=None, reference_curve=reference, standard_nodes_list=nodes)
        a, b = obs
        self.assertEqual(a.weight + b.weight, 1)
        self.assertEqual(a.quantity + b.quantity, 200)
        self.assertEqual(a.price - b.price, reference[a.tenor] - reference[b.tenor])

    def test_rendered_line_is_log_linear_and_respects_gaps(self):
        from wheat_curve.plotting import _line
        points = [{"tenor": t, "maturity_date": tenor_to_date(date(2026, 10, 15), t).isoformat(),
                   "price": {"1M": "100", "2M": "121"}.get(t)} for t in TENORS]
        xs, ys = _line(points, "2026-10-15")
        self.assertAlmostEqual(ys[xs.index(date(2026, 11, 30))], 110)
        self.assertIsNone(ys[xs.index(date(2026, 12, 20))])

    def test_cli_default_produces_three_pngs(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("PNG dependency not installed; core remains usable")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--output", directory],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for filename in ("comparison.png", "yesterday.png", "today.png"):
                with Image.open(Path(directory) / filename) as picture:
                    self.assertEqual(picture.size, (1400, 800))
                    self.assertEqual(picture.format, "PNG")

    def test_cli_core_only_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--no-images", "--output", directory],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "curve.json").is_file())
            self.assertTrue((Path(directory) / "audit.json").is_file())
            bad = Path(directory) / "bad.json"
            bad.write_text('{"previous_curve": []}', encoding="utf-8")
            output = Path(directory) / "should-not-exist"
            result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--input", str(bad), "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
