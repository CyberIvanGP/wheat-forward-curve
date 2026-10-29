from dataclasses import replace
from datetime import date
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calculation_run.files import read_json
from calculation_run.runner import ROOT, run_calculations
from guarantee_collateral import MarketHistory, TradingCalendar
from interest_rate_curve import load_moex_g_curve_snapshot
import run_demo

DATA = ROOT / "examples/full_demo"


class FullDemoTests(unittest.TestCase):
    def test_launcher_uses_fixed_dataset_and_example_mode(self):
        with patch.object(run_demo, "run_project", return_value=0) as command:
            self.assertEqual(run_demo.main(["--serve"]), 0)
            command.assert_called_once_with(["--defaults-dir", str(DATA), "--example", "--serve"])

    def test_archive_is_complete_and_explicitly_synthetic(self):
        data = read_json(DATA / "portfolio_margin_input.json")
        self.assertEqual(data["data_kind"], "SYNTHETIC_TEST")
        history = MarketHistory.from_records(data["history"], data_kind=data["data_kind"],
                                             source_description=data["history_source"])
        calendar = TradingCalendar.load(DATA / "moex_trading_calendar.json")
        calendar.validate_history(history.dates)
        self.assertEqual(len(history.dates), 201)
        self.assertEqual(history.as_of, date(2026, 10, 16))
        self.assertEqual(len(data["trades"]), 2)
        self.assertEqual(calendar.risk_end_date(history.as_of, 5), date(2026, 10, 23))

    def test_full_pipeline_preserves_inputs_and_prepares_margin(self):
        files = list(DATA.glob("*.json"))
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        snapshot = load_moex_g_curve_snapshot(ROOT / "examples/moex_g_curve_snapshot_2026-10-26.json",
                                             valuation_date=date(2026, 10, 26))
        # Offline test transport only; the real demo still downloads exact-date MOEX data.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            user = folder / "user"
            user.mkdir()
            (user / "run_settings.json").write_text("invalid user file", encoding="utf-8")
            result = run_calculations(input_dir=user, defaults_dir=DATA, output_dir=folder / "out",
                                      example=True, download=lambda day: replace(snapshot, valuation_date=day))
            self.assertTrue(all(s["status"] == "ok" for s in result.info["modules"].values()), result.info)
            self.assertEqual(result.info["calculation_date"], "2026-10-16")
            self.assertTrue(result.info["synthetic"])
            self.assertIsNotNone(result.prepared_margin)
            self.assertEqual(result.margin["fhs_scenario_count"], 10000)
            self.assertEqual(result.margin["historical_scenario_count"], 201 - 5)
            self.assertEqual(result.info["changes"][0]["history_today"], "REPLACED")
            for name in ("results/portfolio_margin.json", "results/prepared_scenarios.npz",
                         "results/npv.json", "excel/today_curve.xlsx"):
                self.assertTrue((result.folder / name).is_file(), name)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


if __name__ == "__main__":
    unittest.main()
