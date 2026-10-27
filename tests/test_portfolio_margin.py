from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

import numpy as np

from financial_contracts import ForwardTrade, NodeCurves, price_forward
from guarantee_collateral import calculate_portfolio_margin, MarketHistory, TradingCalendar
from guarantee_collateral.calculate_portfolio_margin import _buckets
from guarantee_collateral.fhs_statistics import (
    FHSCalibration, GarchParameters, GOEngine, RiskConfig, Shock,
    _tail, _variances, calibrate_fhs,
)
from project_parameters import load_parameters
from wheat_curve.calculator import parameters
from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes
from tests.margin_test_data import ROOT, synthetic_history


def test_calibration(market):
    x = np.column_stack((market.forward_history[1:] / market.forward_history[:-1] - 1,
                         np.diff(market.zero_history, axis=0)))
    ps = []
    for j in range(78):
        v = float(np.mean(x[:, j] ** 2))
        ps.append(GarchParameters(v * .05, .05, .90, 8., v))
    sigma = np.column_stack([np.sqrt(_variances(x[:, j], p)) for j, p in enumerate(ps)])
    residuals = np.divide(x, sigma, out=np.zeros_like(x), where=sigma > 0)
    return FHSCalibration(tuple(ps), residuals, market.dates[0], market.dates[1:], x, market.tenors.copy())


class ForwardValuationTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 27)
        self.curves = NodeCurves(self.day, (Decimal(20000),) * 39, (Decimal(".15"),) * 39)
        self.trade = ForwardTrade("a", "100", "19000", 1, date(2027, 1, 27), date(2027, 2, 1))

    def test_two_dates_and_sign_and_decimal(self):
        expected = Decimal(100000) * self.curves.discount_factor(self.trade.payment_date)
        self.assertEqual(price_forward(self.trade, self.curves), expected)
        self.assertEqual(price_forward(replace(self.trade, side=-1), self.curves), -expected)

    def test_log_linear_uses_actual_dates(self):
        prices = list(self.curves.forward)
        prices[3], prices[4] = Decimal(100), Decimal(121)
        curve = replace(self.curves, forward=tuple(prices))
        left, right = standard_nodes(self.day)[3:5]
        target = left.maturity_date + timedelta(days=15)
        self.assertEqual((right.maturity_date - left.maturity_date).days, 30)
        self.assertAlmostEqual(curve.forward_price(target), Decimal(110), places=23)

    def test_linear_zero_rates_preserved(self):
        rates = list(self.curves.zero)
        rates[3], rates[4] = Decimal(".10"), Decimal(".20")
        curve = replace(self.curves, zero=tuple(rates))
        target = standard_nodes(self.day)[3].maturity_date + timedelta(days=15)
        with localcontext() as ctx:
            ctx.prec = 28
            expected = (-Decimal(".15") * Decimal((target-self.day).days) / 365).exp()
        self.assertAlmostEqual(curve.discount_factor(target), expected, places=26)

    def test_alternative_discount_provider(self):
        class Other:
            valuation_date = self.day

            def discount_factor(self, day):
                return Decimal(".9")
        self.assertEqual(price_forward(self.trade, self.curves, discount_curve=Other()), Decimal(90000))

    def test_invalid_trade_and_outside_curve(self):
        for changes in ({"side": True}, {"quantity": 1.2}, {"quantity": "0"},
                        {"payment_date": self.day}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.trade, **changes)
        for t in (replace(self.trade, fixing_date=self.day),
                  replace(self.trade, fixing_date=date(2030, 1, 1), payment_date=date(2030, 1, 1))):
            with self.assertRaises(ValueError):
                price_forward(t, self.curves)


class PortfolioMarginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market, cls.calendar, cls.trade = synthetic_history()
        cls.calibration = test_calibration(cls.market)

    def run_margin(self, trades=None, path=None):
        return calculate_portfolio_margin(self.market, trades or [self.trade], self.calendar,
                                          calibration=self.calibration, parameters_file=path,
                                          allow_synthetic_test_data=True)

    def config_file(self, directory, **updates):
        params = load_parameters()
        params["margin"].update(updates)
        path = Path(directory) / "parameters.json"
        path.write_text(json.dumps(params, default=str), encoding="utf-8")
        return path

    def test_optimized_variances_match_original_recursion(self):
        x = np.array([.01, -.02, .03, .005])
        p = GarchParameters(.00001, .05, .9, 8, .0001)
        expected = [p.initial_variance]
        for i in range(1, len(x)):
            expected.append(p.omega + p.alpha*x[i-1]**2 + p.beta*expected[-1])
        np.testing.assert_allclose(_variances(x, p), expected, rtol=1e-14)

    def test_default_parameters_match_agreement(self):
        risk = load_parameters()["margin"]
        self.assertEqual((risk["horizon_days"], risk["scenarios"], risk["fhs_window"], risk["min_observations"]), (5,10000,1000,200))
        self.assertEqual(risk["confidence"], "0.99")
        self.assertEqual(parameters()["1D"].threshold_trade_volume, 1600)
        self.assertEqual(parameters()["25M"].threshold_trade_volume, 250)
        self.assertEqual(_buckets(risk["concentration"]), [])

    def test_single_and_portfolio_exact_hedge(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config_file(temp, scenarios=100)
            short = replace(self.trade, trade_id="short", side=-1)
            result = self.run_margin([self.trade, short], path)
            for name in ("fhs", "historical", "hypothetical", "event", "concentration", "go", "current_value"):
                self.assertEqual(result["portfolio"][name], 0, name)
            self.assertEqual(result["valuation_date"], self.market.as_of.isoformat())
            self.assertEqual(result["scenario_valuation"], "FROZEN_AS_OF")
            self.assertEqual(result["calibration_observations"], 200)
            self.assertEqual(result["fhs_scenario_count"], 100)
            self.assertEqual(result["historical_scenario_count"], 196)
            self.assertIsInstance(result["trades"][self.trade.trade_id]["current_value"], Decimal)

    def test_seed_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config_file(temp, scenarios=40)
            first = self.run_margin(path=path)
            second = self.run_margin(path=path)
            self.assertEqual(first["portfolio"], second["portfolio"])

    def test_net_volume_is_not_sum_of_individual_margin(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config_file(temp, scenarios=50)
            short = replace(self.trade, trade_id="short", side=-1, quantity=Decimal(400))
            reduced = replace(self.trade, quantity=Decimal(600))
            portfolio = self.run_margin([self.trade, short], path)["portfolio"]
            equivalent = self.run_margin([reduced], path)["portfolio"]
            for component in ("fhs", "historical", "go", "current_value"):
                self.assertLess(abs(portfolio[component]-equivalent[component]), Decimal("1e-8"))

    def test_reject_whole_portfolio_if_fixing_at_or_before_horizon(self):
        end = self.calendar.risk_end_date(self.market.as_of, 5)
        for fixing in (self.market.as_of-timedelta(days=1), self.market.as_of, end-timedelta(days=1), end):
            bad = replace(self.trade, trade_id="bad", fixing_date=fixing)
            with self.subTest(fixing=fixing), patch("guarantee_collateral.calculate_portfolio_margin.GOEngine") as engine:
                with self.assertRaisesRegex(ValueError, "портфель отклонён"):
                    self.run_margin([self.trade, bad])
                engine.assert_not_called()

    def test_fixing_after_horizon_allowed(self):
        end = self.calendar.risk_end_date(self.market.as_of, 5)
        trade = replace(self.trade, fixing_date=end+timedelta(days=1), payment_date=end+timedelta(days=2))
        with tempfile.TemporaryDirectory() as temp:
            self.run_margin([trade], self.config_file(temp, scenarios=5))

    def test_archive_does_not_silently_fill_missing_days(self):
        market = replace(self.market, dates=self.market.dates[:5]+self.market.dates[6:],
                         forward_prices=self.market.forward_prices[:5]+self.market.forward_prices[6:],
                         zero_rates=self.market.zero_rates[:5]+self.market.zero_rates[6:],
                         forward_sources=self.market.forward_sources[:5]+self.market.forward_sources[6:])
        with self.assertRaisesRegex(ValueError,"пропущен"):
            self.calendar.validate_history(market.dates)

    def test_calendar_holidays_and_coverage(self):
        self.assertEqual(self.calendar.risk_end_date(date(2026,10,30),5), date(2026,11,9))
        self.assertIn(date(2026,3,9),self.calendar.dates)
        self.assertNotIn(date(2026,11,4),self.calendar.dates)
        with self.assertRaises(ValueError):
            self.calendar.risk_end_date(date(2026,12,30),5)

    def test_synthetic_requires_explicit_permission(self):
        with self.assertRaisesRegex(ValueError,"REAL"):
            calculate_portfolio_margin(self.market,[self.trade],self.calendar)

    def test_short_history_rejected(self):
        market, calendar, trade = synthetic_history(30)
        with self.assertRaisesRegex(ValueError,"Недостаточно истории"):
            calculate_portfolio_margin(market,[trade],calendar,allow_synthetic_test_data=True)

    def test_var_and_fractional_es(self):
        losses=np.array([1.,2.,3.,4.])
        self.assertEqual(_tail(losses,.75,"var"),3.)
        self.assertEqual(_tail(losses,.5,"es"),3.5)
        self.assertEqual(_tail(losses,.625,"es"), 11/3)

    def test_bad_concentration_activation_is_not_silent(self):
        settings=load_parameters()["margin"]["concentration"]
        settings["addon1_per_tonne"]="100"
        with self.assertRaises(ValueError):
            _buckets(settings)
        settings.update(limit1="500",limit2="1000",addon2_per_tonne="200",buckets=[{"last_tenor":"36M"}])
        bucket=_buckets(settings)[0]
        engine=object.__new__(GOEngine)
        engine.market=self.market
        self.assertEqual(engine._concentration([replace(self.trade, quantity=Decimal(1200))],[bucket]), Decimal(90000))

    def test_hypothetical_scenarios_and_event_can_be_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            shock={"name":"test price down","forward_return":"-0.1","zero_shift":"0"}
            path=self.config_file(temp, scenarios=5, hypothetical_scenarios=[shock],event_scenarios=[shock])
            result=self.run_margin(path=path)["portfolio"]
            self.assertGreater(result["hypothetical"],0)
            self.assertEqual(result["hypothetical"],result["event"])
            self.assertEqual(result["go"], max(result["fhs"],result["historical"],result["hypothetical"])+result["event"])

    def test_records_round_trip_rates_and_carried_flags(self):
        records=[]
        for j,day in enumerate(self.market.dates[:3]):
            with localcontext() as ctx:
                ctx.prec=28
                dfs={n.tenor:str((-self.market.zero_rates[j][i]*Decimal((n.maturity_date-day).days)/365).exp())
                     for i,n in enumerate(standard_nodes(day))}
            records.append({"date":day.isoformat(),"forward_curve":[
                {"tenor":t,"price":str(self.market.forward_prices[j][i]),"source":"FALLBACK_PREVIOUS_DAY"}
                for i,t in enumerate(STANDARD_TENORS)],"discount_factors":dfs})
        restored=MarketHistory.from_records(records,data_kind="REAL",source_description="Parser test, not a claim of market authenticity")
        self.assertEqual(restored.forward_prices,self.market.forward_prices[:3])
        self.assertEqual(restored.forward_sources[0][0],"FALLBACK_PREVIOUS_DAY")
        self.assertLess(abs(restored.zero_rates[0][0]-self.market.zero_rates[0][0]),Decimal("1e-24"))
        records[0]["forward_curve"][0]["price"]=None
        with self.assertRaises(ValueError):
            MarketHistory.from_records(records,data_kind="REAL",source_description="test")

    def test_real_optimizer_converges_and_parameters_are_stationary(self):
        cfg=RiskConfig(5,self.calendar.risk_end_date(self.market.as_of,5),scenarios=5)
        result=calibrate_fhs(self.market,cfg)
        self.assertEqual(len(result.parameters),78)
        for p in result.parameters:
            self.assertGreater(p.omega,0)
            self.assertGreater(p.nu,2)
            self.assertGreaterEqual(p.alpha,0)
            self.assertGreaterEqual(p.beta,0)
            self.assertLess(p.alpha+p.beta,1)

    def test_previous_volatility_can_be_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.config_file(temp,scenarios=5,min_volatility_ratio="0.9",previous_volatility="0.001")
            result=self.run_margin(path=path)
            self.assertTrue(all(v>=.0009-1e-15 for v in result["one_day_volatility"]))

    def test_common_parameter_file_and_override_precedence(self):
        data=load_parameters()
        data["forward_curve"]["4M"]["min_trade_volume"]="321"
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"settings.json"
            path.write_text(json.dumps(data),encoding="utf-8")
            self.assertEqual(parameters(parameters_file=path)["4M"].min_trade_volume,321)
            self.assertEqual(parameters({"4M":{"min_trade_volume":"123"}},parameters_file=path)["4M"].min_trade_volume,123)

    def test_cli_rejects_synthetic_and_bad_trade_without_replacing_result(self):
        from tests.create_margin_test_input import build_input
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/"input.json"
            output=Path(temp)/"result.json"
            data=build_input()
            source.write_text(json.dumps(data),encoding="utf-8")
            output.write_text("previous result",encoding="utf-8")
            command=[sys.executable,str(ROOT/"run_portfolio_margin_calculation.py"),"--input",str(source),
                     "--calendar",str(ROOT/"examples/moex_spfi_trading_calendar_2026.json"),"--output",str(output)]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual(output.read_text(encoding="utf-8"),"previous result")
            data["trades"][0]["fixing_date"]=data["calculation_date"]
            source.write_text(json.dumps(data),encoding="utf-8")
            result=subprocess.run(command+["--allow-synthetic-test-data"],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual(output.read_text(encoding="utf-8"),"previous result")


if __name__ == "__main__":
    unittest.main()
