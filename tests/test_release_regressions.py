from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
import unittest

from calculation_run.files import read_json, parse_json
from calculation_run.runner import ROOT, RunResult
from financial_contracts import NodeCurves
from wheat_curve import calculate
from wheat_curve.domain.tenors import standard_nodes


class ReleaseRegressionTests(unittest.TestCase):
    def test_json_rejects_duplicate_fields_at_any_depth(self):
        for document in ('{"a": 1, "a": 2}', '{"trades": [{"quantity": 1, "quantity": 2}]}'):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, "Повторное поле"):
                parse_json(document)

    def test_json_numbers_are_decimal_and_nonfinite_rejected(self):
        self.assertEqual(parse_json('{"quantity": 0.123456789012345678901}')['quantity'],
                         Decimal("0.123456789012345678901"))
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_json('{"quantity": ' + value + '}')

    def test_grain_independent_of_external_rounding_and_precision(self):
        data = read_json(ROOT / "examples/default_input.json")
        expected = calculate(**data)
        for rounding in (ROUND_UP, ROUND_DOWN):
            with localcontext() as context:
                context.prec, context.rounding = 6, rounding
                self.assertEqual(calculate(**data), expected)
                self.assertEqual((context.prec, context.rounding), (6, rounding))

    def test_portfolio_sum_independent_of_external_decimal_context(self):
        day = date(2026, 10, 15)
        curves = NodeCurves(day, (Decimal("20643.123456789"),) * 39, (Decimal(".137"),) * 39)
        run = RunResult(ROOT, {"calculation_date": str(day)}, node_curves=curves, discount_curve=curves)
        records = [{"trade_id": str(i), "quantity": "123.456789", "strike": "20123.456789",
                    "side": side, "fixing_date": "2027-03-15", "payment_date": "2027-03-17"}
                   for i, side in enumerate((1, 1, -1))]
        expected = run.calculate_npv(records)
        for rounding in (ROUND_UP, ROUND_DOWN):
            with localcontext() as context:
                context.prec, context.rounding = 6, rounding
                self.assertEqual(run.calculate_npv(records), expected)
                self.assertEqual((context.prec, context.rounding), (6, rounding))

    def test_calendar_cache_does_not_share_mutable_lists(self):
        day = date(2026, 1, 31)
        expected = standard_nodes(day)
        changed = standard_nodes(day)
        changed.clear()
        self.assertEqual(standard_nodes(day), expected)
        self.assertEqual(standard_nodes(day, ["2M", "1M"]), [expected[4], expected[3]])
        self.assertNotEqual(standard_nodes(date(2026, 2, 1)), expected)


if __name__ == "__main__":
    unittest.main()
