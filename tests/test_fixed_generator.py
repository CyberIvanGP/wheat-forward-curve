from collections import Counter
from contextlib import redirect_stderr
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from io import StringIO
import tempfile
import unittest

from calculation_run.files import read_json
from generator.create_fixed_demo_inputs import PARAMETERS, ROOT, build_input, main, verify_input
from wheat_curve.domain.tenors import STANDARD_TENORS


class FixedGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = read_json(PARAMETERS)
        cls.data = build_input(cls.settings)
        cls.verification = verify_input(cls.data)

    def test_shape_is_calculated_and_all_nodes_observed(self):
        self.assertEqual([p["tenor"] for p in self.verification["peaks"]], ["18M", "30M"])
        self.assertTrue(self.verification["all_nodes_mixed"])
        self.assertEqual(self.verification["carried_nodes"], 0)
        self.assertEqual(self.verification["valid_snapshots_per_node"], 160)

    def test_dense_event_counts(self):
        self.assertEqual(Counter(e["event_type"] for e in self.data["events"]), {
            "ORDER_NEW": 1872, "TRADE": 936, "ORDER_CANCEL": 1560,
            "ORDER_FILL": 936, "ORDER_MODIFY": 936})

    def test_every_node_has_same_number_of_trades_and_orders(self):
        by_maturity = Counter(e["maturity_date"] for e in self.data["events"] if e["event_type"] == "TRADE")
        self.assertEqual(len(by_maturity), 39)
        self.assertEqual(set(by_maturity.values()), {24})
        self.assertTrue(all(e.get("trade_type", "NON_ADDRESS") == "NON_ADDRESS" for e in self.data["events"]))

    def test_deterministic_and_settings_not_mutated(self):
        original = deepcopy(self.settings)
        self.assertEqual(build_input(self.settings), self.data)
        self.assertEqual(original, self.settings)

    def test_volume_exceeds_minimum_but_preserves_order_weight(self):
        params = read_json(ROOT / "project_parameters/default_calculation_parameters.json")["forward_curve"]
        volumes = Counter()
        for event in self.data["events"]:
            if event["event_type"] == "TRADE":
                volumes[event["maturity_date"]] += event["quantity"]
        from datetime import date
        from wheat_curve.domain.tenors import standard_nodes
        for node in standard_nodes(date.fromisoformat(self.data["calculation_date"])):
            volume = volumes[node.maturity_date.isoformat()]
            self.assertGreaterEqual(volume, Decimal(params[node.tenor]["min_trade_volume"]))
            self.assertLess(volume, Decimal(params[node.tenor]["threshold_trade_volume"]))

    def test_refuses_to_replace_existing_files_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wheat_curve_input.json"
            path.write_text("original", encoding="utf-8")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(["--output-dir", temp])
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_invalid_settings_are_rejected(self):
        for changes in ({"trades_per_session_per_node": 3}, {"sessions": 0},
                        {"daily_trade_full_weight_fraction": "1"}, {"session_noise_fraction": "0.1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_input({**self.settings, **changes})

    def test_unique_ids_sorted_events(self):
        events = self.data["events"]
        self.assertEqual(len(events), len({e["event_id"] for e in events}))
        self.assertEqual([e["time"] for e in events], sorted(e["time"] for e in events))
        self.assertEqual([p["tenor"] for p in self.data["previous_curve"]], list(STANDARD_TENORS))


if __name__ == "__main__":
    unittest.main()
