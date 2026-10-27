from datetime import date
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
import tempfile
import unittest
from xml.etree import ElementTree

from calculation_run.runner import RunResult, parse_trades
from test.report import chart, write_report


class ReportRenderingTests(unittest.TestCase):
    def test_price_axis_has_exact_100_steps_and_linear_coordinates(self):
        points = [{"date": f"2026-10-{d}", "price": Decimal(p)}
                  for d, p in ((16, "20000"), (17, "20100"), (18, "20200"))]
        svg = ElementTree.fromstring(chart(points))
        labels = [int(t.text.replace(" ", "")) for t in svg.findall("text") if t.attrib.get("x") == "73"]
        self.assertEqual(labels, list(range(19900, 20301, 100)))
        coordinates = [tuple(map(float, xy.split(','))) for xy in svg.find("polyline").attrib["points"].split()]
        self.assertAlmostEqual(coordinates[0][1] - coordinates[1][1], coordinates[1][1] - coordinates[2][1])
        self.assertIn("линейная шкала", svg.attrib["aria-label"])

    def test_missing_points_are_not_connected(self):
        svg = ElementTree.fromstring(chart([
            {"date": "2026-10-16", "price": 20000},
            {"date": "2026-10-17", "price": None},
            {"date": "2026-10-18", "price": 20200}]))
        self.assertEqual(len(svg.findall("polyline")), 2)

    def test_constant_price_and_no_data(self):
        self.assertIn("polyline", chart([{"date": "2026-10-16", "price": 20000}]))
        self.assertIn("недоступна", chart([]))

    def test_portfolio_example_is_valid_and_appears_above_editor(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            info = {"calculation_date": "2026-10-15", "modules": {}, "sources": {},
                    "warnings": [], "changes": [], "synthetic": False}
            write_report(RunResult(folder, info))
            html = (folder / "report.html").read_text(encoding="utf-8")
            self.assertIn("время Московское", html)
            self.assertIn("Ось цен линейная, шаг 100", html)
            encoded = re.search(r'<pre id="portfolio-example"><code>(.*?)</code></pre>', html, re.S).group(1)
            trades = parse_trades(json.loads(unescape(encoded)))
            self.assertEqual(len(trades), 2)
            self.assertEqual({t.side for t in trades}, {-1, 1})
            self.assertTrue(all(t.fixing_date > date(2026, 10, 15) and t.payment_date >= t.fixing_date for t in trades))
            self.assertLess(html.index('id="portfolio-example"'), html.index('<textarea id="portfolio"'))
            self.assertIn('const initialPortfolio = []', html)
