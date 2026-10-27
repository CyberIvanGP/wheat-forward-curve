from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import http.client
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from calculation_run.files import InputFiles, MOSCOW, new_run_folder, read_json, write_json
from calculation_run.runner import ROOT, RunResult, merge_history, run_calculations
from interest_rate_curve import load_moex_g_curve_snapshot
from project_parameters import load_parameters
from test.local_server import make_server
from test.report import write_report


class ProjectRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user, self.defaults, self.out = [self.root / p for p in ("input", "default", "output")]
        self.user.mkdir()
        self.defaults.mkdir()
        self.day = date(2026, 10, 15)
        # Keep orchestration tests small and independent of the dense shipped demo.
        self.grain = {**read_json(ROOT / "examples/default_input.json"), "data_kind": "SYNTHETIC_TEST"}
        write_json(self.defaults / "wheat_curve_input.json", self.grain)
        write_json(self.defaults / "run_settings.json", {"calculation_date": str(self.day)})

    def discount(self, day):
        # Preserve the fixture's coefficients; explicit fake source only in tests.
        from dataclasses import replace
        return replace(load_moex_g_curve_snapshot(ROOT / "examples/moex_g_curve_snapshot_2026-10-26.json",
                                                  valuation_date=date(2026, 10, 26)), valuation_date=day)

    def run_project(self, **kwargs):
        kwargs.setdefault("calculation_date", str(self.day))
        return run_calculations(input_dir=self.user, defaults_dir=self.defaults, output_dir=self.out,
                                download=kwargs.pop("download", self.discount), **kwargs)

    def test_fixed_example_and_new_folder(self):
        first = self.run_project(example=True)
        second = self.run_project(example=True)
        self.assertNotEqual(first.folder, second.folder)
        self.assertEqual(first.info["modules"]["wheat_curve"]["status"], "ok")
        self.assertEqual(len(first.curve["curve"]), 39)
        self.assertTrue((first.folder / "excel/today_curve.xlsx").exists())
        self.assertTrue((first.folder / "results/interpolated_curve.json").exists())
        self.assertEqual(first.info["modules"]["margin"]["status"], "error")
        self.assertTrue(first.info["synthetic"])

    def test_empty_events_not_defaulted_and_missing_reference_is_defaulted(self):
        write_json(self.user / "wheat_curve_input.json", {"calculation_date": str(self.day),
                                                        "events": [], "data_kind": "REAL"})
        result = self.run_project()
        self.assertTrue(all(p["source"] == "FALLBACK_PREVIOUS_DAY" for p in result.curve["curve"]))
        sources = result.info["sources"]
        self.assertEqual(sources["wheat_curve_input.json:events"]["source"], "input")
        self.assertEqual(sources["wheat_curve_input.json:previous_curve"]["source"], "default")

    def test_null_is_invalid_not_defaulted(self):
        grain = {**self.grain, "events": None}
        write_json(self.user / "wheat_curve_input.json", grain)
        result = self.run_project()
        self.assertIsNone(result.curve)
        self.assertEqual(result.info["modules"]["g_curve"]["status"], "ok")

    def test_bad_json_is_not_hidden(self):
        path = self.user / "wheat_curve_input.json"
        path.write_text('{"events": [], "events": []}', encoding="utf-8")
        result = self.run_project()
        self.assertIn("Повторное поле", result.info["modules"]["wheat_curve"]["message"])
        self.assertIsNone(result.node_curves)

    def test_wrong_date_stops_only_dependent_modules(self):
        result = self.run_project(calculation_date="2026-10-16")
        self.assertIsNone(result.curve)
        self.assertIsNone(result.npv)
        self.assertEqual(result.info["modules"]["g_curve"]["status"], "ok")

    def test_moex_failure_still_saves_curve_excel_and_report(self):
        def fail(day):
            raise RuntimeError("MOEX is unavailable")
        result = self.run_project(download=fail)
        write_report(result)
        self.assertIsNotNone(result.curve)
        self.assertIsNone(result.discount_curve)
        self.assertIsNone(result.prepared_margin)
        self.assertIn("MOEX is unavailable", (result.folder / "report.html").read_text(encoding="utf-8"))

    def test_wrong_moex_date_rejected(self):
        result = self.run_project(download=lambda day: self.discount(day + timedelta(days=1)))
        self.assertIsNone(result.discount_curve)

    def test_default_clock_is_moscow_not_host_date(self):
        instant = datetime(2026, 10, 14, 22, 5, tzinfo=timezone.utc)
        result = self.run_project(calculation_date=None, now=instant)
        self.assertEqual(result.info["calculation_date"], "2026-10-15")
        self.assertEqual(result.info["date_source"], "moscow_clock")

    def test_example_ignores_bad_user_data(self):
        (self.user / "wheat_curve_input.json").write_text("bad JSON", encoding="utf-8")
        result = self.run_project(calculation_date=None, example=True)
        self.assertIsNotNone(result.curve)
        self.assertEqual(result.info["calculation_date"], "2026-10-15")

    def test_xlsx_typed_prices_dates_and_all_nodes(self):
        result = self.run_project()
        with ZipFile(result.folder / "excel/today_curve.xlsx") as book:
            self.assertIsNone(book.testzip())
            sheet = ElementTree.fromstring(book.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            cells = {c.attrib["r"]: c for c in sheet.findall(".//s:c", ns)}
            self.assertEqual(Decimal(cells["C6"].find("s:v", ns).text), result.curve["curve"][0]["price"])
            self.assertNotIn("t", cells["C6"].attrib)
            self.assertEqual(cells["B6"].attrib["s"], "3")
            self.assertIn("C44", cells)

    def test_output_input_copies_unchanged(self):
        path = self.user / "wheat_curve_input.json"
        write_json(path, self.grain)
        original = path.read_bytes()
        result = self.run_project()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((result.folder / "input/input/wheat_curve_input.json").read_bytes(), original)

    def test_folder_collision_is_safe(self):
        now = datetime(2026, 1, 1, tzinfo=MOSCOW)
        a, b = new_run_folder(self.out, now), new_run_folder(self.out, now)
        self.assertNotEqual(a, b)

    def test_history_append_replace_do_not_mutate(self):
        history = [{"date": "2026-10-14", "forward_curve": [], "discount_factors": {}}]
        original = deepcopy(history)
        merged, action = merge_history(history, self.day, [{"tenor": "1D"}], {})
        self.assertEqual(action, "APPENDED")
        self.assertEqual(history, original)
        replaced, action = merge_history(merged, self.day, [{"tenor": "1W"}], {})
        self.assertEqual(action, "REPLACED")
        self.assertEqual(len(replaced), 2)
        self.assertEqual(merged[-1]["forward_curve"][0]["tenor"], "1D")

    def test_future_history_rejected(self):
        with self.assertRaises(ValueError):
            merge_history([{"date": "2026-10-16"}], self.day, [], {})

    def test_bad_unused_default_does_not_override_user(self):
        write_json(self.user / "wheat_curve_input.json", self.grain)
        (self.defaults / "wheat_curve_input.json").write_text("bad", encoding="utf-8")
        result = self.run_project()
        self.assertIsNotNone(result.curve)

    def test_npv_without_history_works_but_requires_built_curve(self):
        trade = {"trade_id": "a", "quantity": "100", "strike": "20000", "side": 1,
                 "fixing_date": "2027-03-15", "payment_date": "2027-03-17"}
        write_json(self.user / "portfolio_margin_input.json", {"calculation_date": str(self.day), "trades": [trade]})
        result = self.run_project()
        self.assertIsNotNone(result.npv)
        before = {p.relative_to(result.folder): p.read_bytes() for p in result.folder.rglob("*") if p.is_file()}
        self.assertEqual(result.calculate_npv([trade]), result.npv)
        after = {p.relative_to(result.folder): p.read_bytes() for p in result.folder.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_full_margin_and_cached_scenarios(self):
        from tests.margin_test_data import synthetic_history
        from wheat_curve.domain.tenors import STANDARD_TENORS
        market, calendar, trade = synthetic_history()
        self.day = market.as_of
        history = []
        for day, prices, zeros in zip(market.dates, market.forward_prices, market.zero_rates):
            from wheat_curve.domain.tenors import standard_nodes
            history.append({"date": str(day), "forward_curve": [
                {"tenor": t, "price": p, "source": "SYNTHETIC_TEST"} for t, p in zip(STANDARD_TENORS, prices)],
                "discount_factors": {n.tenor: (-z * Decimal((n.maturity_date-day).days) / 365).exp()
                                     for n, z in zip(standard_nodes(day), zeros)}})
        grain = {"calculation_date": str(self.day), "previous_date": str(market.dates[-2]),
                 "previous_curve": history[-2]["forward_curve"], "events": [], "data_kind": "SYNTHETIC_TEST"}
        write_json(self.user / "wheat_curve_input.json", grain)
        write_json(self.user / "portfolio_margin_input.json", {
            "calculation_date": str(self.day), "history": history[:-1], "trades": [asdict(trade)],
            "data_kind": "SYNTHETIC_TEST", "history_source": "unit test"})
        shutil.copyfile(ROOT / "examples/moex_spfi_trading_calendar_2026.json", self.user / "moex_trading_calendar.json")
        parameters = load_parameters()
        parameters["margin"].update({"scenarios": 3, "min_observations": 10})
        parameters_path = self.root / "params.json"
        write_json(parameters_path, parameters)
        # The integration uses the real prepare/calculate path, but a fixed test calibration.
        from guarantee_collateral.fhs_statistics import calibrate_fhs
        with patch("guarantee_collateral.fhs_statistics.calibrate_fhs", wraps=calibrate_fhs) as fit:
            result = self.run_project(parameters_file=parameters_path, allow_synthetic=True)
            self.assertIsNotNone(result.prepared_margin, result.info["modules"])
            self.assertIsNotNone(result.margin, result.info["modules"])
            self.assertEqual(fit.call_count, 1)
            first = result.calculate_margin(result.portfolio)
            second = result.calculate_margin(result.portfolio)
            self.assertEqual(first, second)
            self.assertEqual(fit.call_count, 1)
        self.assertEqual(result.info["changes"][0]["history_today"], "APPENDED")
        self.assertTrue((result.folder / "results/prepared_scenarios.npz").exists())


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        (folder / "report.html").write_text("report", encoding="utf-8")
        (folder / "secret.json").write_text("private", encoding="utf-8")
        self.run = RunResult(folder, {"calculation_date": "2026-10-15"})
        self.server = make_server(self.run)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_port

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_report_and_no_private_file_access(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        for path in ("/secret.json", "/input/", "/../secret.json"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_idle_browser_connection_does_not_block_other_requests(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2):
            self.assertEqual(self.request("GET", "/")[0], 200)

    def test_dns_rebinding_rejected(self):
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": "evil.test"})[0], 403)

    def test_post_requires_origin_and_token(self):
        self.assertEqual(self.request("POST", "/api/npv", body='{"trades": []}')[0], 403)
        _, response = self.request("GET", "/api/session")
        headers = {"Origin": f"http://127.0.0.1:{self.port}", "X-Run-Token": json.loads(response)["token"],
                   "Content-Type": "application/json"}
        status, response = self.request("POST", "/api/npv", body='{"trades": []}', headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(response))

    def test_post_uses_strict_decimal_json(self):
        _, response = self.request("GET", "/api/session")
        headers = {"Origin": f"http://127.0.0.1:{self.port}", "X-Run-Token": json.loads(response)["token"],
                   "Content-Type": "application/json"}
        for path in ("/api/npv", "/api/margin"):
            for body in ('{"trades": [], "trades": []}', '{"trades": [{"quantity": NaN}]}'):
                with self.subTest(path=path, body=body):
                    status, response = self.request("POST", path, body=body, headers=headers)
                    self.assertEqual(status, 400)
                    self.assertNotIn("недоступны", json.loads(response)["error"])
                    self.assertNotIn("Нет успешно", json.loads(response)["error"])
        with patch.object(self.run, "calculate_npv", return_value={"ok": True}) as calculate:
            status, _ = self.request("POST", "/api/npv", body='{"trades": [{"quantity": 1.125}]}', headers=headers)
            self.assertEqual(status, 200)
            calculate.assert_called_once_with([{"quantity": Decimal("1.125")}])


if __name__ == "__main__":
    unittest.main()
