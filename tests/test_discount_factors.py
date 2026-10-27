import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_UP, localcontext
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from interest_rate_curve import (
    CurveDataError, CurveDownloadError, CurveUnavailableError, MoexGCurve,
    calculate_discount_factors, download_moex_g_curve,
    load_moex_g_curve_snapshot, save_moex_g_curve_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/moex_g_curve_snapshot_2026-10-26.json"
DAY = date(2026, 10, 26)


class DiscountFactorTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)
        self.curve = MoexGCurve.from_iss_response(self.payload, valuation_date=DAY)

    def test_reproduces_all_published_effective_yields(self):
        for years, expected in self.curve.published_yields:
            with self.subTest(years=years):
                self.assertLess(abs(self.curve.effective_annual_yield_percent(years) - expected), Decimal("0.00005"))

    def test_one_year_discount_factor(self):
        self.assertLess(abs(self.curve.discount_factor(DAY + timedelta(days=365)) - Decimal("0.8767820433311845")), Decimal("1e-15"))

    def test_effective_and_continuous_discounting_agree(self):
        with localcontext() as ctx:
            ctx.prec = 28
            years = Decimal(400) / 365
            effective = self.curve.effective_annual_yield_percent(years) / 100
            expected = (-(1 + effective).ln() * years).exp()
            self.assertLess(abs(self.curve.discount_factor(DAY + timedelta(days=400)) - expected), Decimal("1e-26"))

    def test_same_day_is_one(self):
        self.assertEqual(self.curve.discount_factor(DAY), Decimal(1))

    def test_short_end_keeps_rate_not_factor(self):
        with localcontext() as ctx:
            ctx.prec = 28
            rate = self.curve.continuous_rate("0.25")
            for days in (1, 7, 14, 30, 90):
                self.assertEqual(self.curve.discount_factor(DAY + timedelta(days=days)),
                                 (-rate * (Decimal(days) / 365)).exp())
        self.assertGreater(self.curve.discount_factor(DAY + timedelta(days=1)),
                           self.curve.discount_factor(DAY + timedelta(days=90)))

    def test_flat_rate_floor_boundary(self):
        self.assertEqual(self.curve.continuous_rate("0"), self.curve.continuous_rate("0.25"))
        self.assertEqual(self.curve.continuous_rate("0.249999"), self.curve.continuous_rate("0.25"))
        self.assertNotEqual(self.curve.continuous_rate("0.250001"), self.curve.continuous_rate("0.25"))

    def test_leap_year_uses_fixed_365(self):
        curve = MoexGCurve(date(2028, 1, 1), self.curve.snapshot_time,
                          self.curve.parameters, self.curve.published_yields)
        with localcontext() as ctx:
            ctx.prec = 28
            years = Decimal(366) / 365
            expected = (-curve.continuous_rate(years) * years).exp()
        self.assertEqual(curve.discount_factor(date(2029, 1, 1)), expected)

    def test_past_payment_rejected(self):
        with self.assertRaises(CurveDataError):
            self.curve.discount_factor(DAY - timedelta(days=1))

    def test_datetime_not_accepted_as_date(self):
        with self.assertRaises(CurveDataError):
            self.curve.discount_factor(datetime(2026, 10, 26))

    def test_numeric_invalid_inputs(self):
        for value in (True, 0.25, "NaN", "Infinity", "-1", "30.0001"):
            with self.subTest(value=value), self.assertRaises(CurveDataError):
                self.curve.continuous_rate(value)

    def test_negative_rates_supported(self):
        params = (Decimal(-100), Decimal(0), Decimal(0), Decimal(1)) + (Decimal(0),) * 9
        curve = MoexGCurve(DAY, self.curve.snapshot_time, params, ((Decimal(".25"), Decimal("-.995")),))
        self.assertGreater(curve.discount_factor(DAY + timedelta(days=365)), 1)

    def test_context_does_not_change_results(self):
        expected = self.curve.discount_factor(DAY + timedelta(days=400))
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 6, ROUND_UP
            self.assertEqual(self.curve.discount_factor(DAY + timedelta(days=400)), expected)
            self.assertEqual(ctx.prec, 6)
            self.assertEqual(ctx.rounding, ROUND_UP)

    def test_snapshot_round_trip_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "snapshot.json"
            save_moex_g_curve_snapshot(self.curve, target)
            with patch("interest_rate_curve.download_moex_g_curve.build_opener", side_effect=AssertionError("network")):
                restored = load_moex_g_curve_snapshot(target, valuation_date=DAY)
                self.assertEqual(restored, self.curve)

    def test_wrong_snapshot_date_rejected(self):
        with self.assertRaises(CurveUnavailableError):
            MoexGCurve.from_iss_response(self.payload, valuation_date=DAY - timedelta(days=1))

    def test_no_data_rejected(self):
        self.payload["params"]["data"] = []
        with self.assertRaises(CurveUnavailableError):
            MoexGCurve.from_iss_response(self.payload, valuation_date=DAY)

    def test_inconsistent_yield_date_or_time_rejected(self):
        for field, value in ((0, "2026-10-23"), (1, "17:00:00")):
            payload = copy.deepcopy(self.payload)
            payload["yearyields"]["data"][0][field] = value
            with self.assertRaises(CurveDataError):
                MoexGCurve.from_iss_response(payload, valuation_date=DAY)

    def test_bad_tables_and_parameters_rejected(self):
        mutations = (
            lambda p: p.pop("params"),
            lambda p: p["params"]["data"].append(p["params"]["data"][0]),
            lambda p: p["params"]["data"][0].pop(),
            lambda p: p["params"]["data"][0].__setitem__(5, "0"),
            lambda p: p["params"]["data"][0].__setitem__(2, "NaN"),
            lambda p: p["params"]["data"][0].__setitem__(2, 1549.8),
            lambda p: p["yearyields"].__setitem__("data", []),
            lambda p: p["yearyields"]["data"].pop(0),
        )
        for mutation in mutations:
            payload = copy.deepcopy(self.payload)
            mutation(payload)
            with self.subTest(mutation=mutation), self.assertRaises(CurveDataError):
                MoexGCurve.from_iss_response(payload, valuation_date=DAY)

    def test_reordered_columns_supported(self):
        for table in (self.payload["params"], self.payload["yearyields"]):
            table["columns"].reverse()
            for row in table["data"]:
                row.reverse()
        self.assertEqual(MoexGCurve.from_iss_response(self.payload, valuation_date=DAY), self.curve)

    def test_download_uses_explicit_date_https_timeout_and_decimal(self):
        raw = json.dumps(self.payload).replace('"1549.833630"', '1549.833630').encode()
        with patch("interest_rate_curve.download_moex_g_curve.build_opener") as build:
            fetch = build.return_value.open
            fetch.return_value = BytesIO(raw)
            curve = download_moex_g_curve(DAY, timeout=5)
            self.assertEqual(build.call_args.args[0].proxies, {})
        request = fetch.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://iss.moex.com/"))
        self.assertIn("date=2026-10-26", request.full_url)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 5)
        self.assertEqual(curve, self.curve)

    def test_download_errors_do_not_fallback(self):
        with patch("interest_rate_curve.download_moex_g_curve.build_opener") as build:
            build.return_value.open.side_effect = URLError("offline")
            with self.assertRaises(CurveDownloadError):
                download_moex_g_curve(DAY)
        with patch("interest_rate_curve.download_moex_g_curve.build_opener") as build:
            build.return_value.open.return_value = BytesIO(b"<html>error</html>")
            with self.assertRaises(CurveDataError):
                download_moex_g_curve(DAY)

    def test_system_proxy_is_opt_in(self):
        with patch("interest_rate_curve.download_moex_g_curve.build_opener") as build:
            build.return_value.open.return_value = BytesIO(json.dumps(self.payload).encode())
            download_moex_g_curve(DAY, use_system_proxy=True)
            build.assert_called_once_with()

    def test_generic_interface_accepts_another_curve(self):
        class OtherCurve:
            valuation_date = DAY

            def discount_factor(self, payment_date):
                return Decimal(1) if payment_date == DAY else Decimal(".9")

        later = DAY + timedelta(days=365)
        self.assertEqual(calculate_discount_factors(OtherCurve(), [DAY, later, later]),
                         {DAY: Decimal(1), later: Decimal(".9")})

    def test_generic_interface_rejects_invalid_factors(self):
        for invalid in (0.9, Decimal(0), Decimal(-1), Decimal("NaN")):
            class OtherCurve:
                valuation_date = DAY

                def discount_factor(self, payment_date):
                    return invalid

            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                calculate_discount_factors(OtherCurve(), [DAY + timedelta(days=1)])

    def test_batch_preserves_order_and_validates_dates(self):
        days = [DAY + timedelta(days=365), DAY, DAY + timedelta(days=1)]
        self.assertEqual(list(calculate_discount_factors(self.curve, days)), days)
        self.assertEqual(calculate_discount_factors(self.curve, []), {})
        with self.assertRaises(ValueError):
            calculate_discount_factors(self.curve, ["2026-10-26"])
        with self.assertRaises(ValueError):
            calculate_discount_factors(self.curve, [DAY - timedelta(days=1)])

    def test_cli_offline_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "factors.json"
            base = [sys.executable, str(ROOT / "run_discount_factor_calculation.py"),
                    "--valuation-date", "2026-10-26", "--payment-dates", "2026-10-27", "2027-10-26",
                    "--snapshot", str(FIXTURE), "--output", str(output)]
            run = subprocess.run(base, cwd=temp, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["day_count"], "ACT/365F")
            self.assertEqual(Decimal(result["discount_factors"]["2027-10-26"]),
                             self.curve.discount_factor(date(2027, 10, 26)))
            previous = output.read_bytes()
            bad = base.copy()
            bad[bad.index("--valuation-date") + 1] = "2026-10-25"
            run = subprocess.run(bad, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
