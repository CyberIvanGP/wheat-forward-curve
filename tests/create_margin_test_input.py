"""Write a deterministic SYNTHETIC_TEST fixture. Not the production market generator."""

import argparse
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from pathlib import Path

from wheat_curve.domain.tenors import STANDARD_TENORS, standard_nodes
from .margin_test_data import synthetic_history


def build_input():
    market, calendar, trade = synthetic_history()
    rows = []
    for j, day in enumerate(market.dates):
        with localcontext() as ctx:
            ctx.prec = 28
            factors = {node.tenor: str((-market.zero_rates[j][i] * Decimal((node.maturity_date - day).days) / 365).exp())
                       for i, node in enumerate(standard_nodes(day))}
        rows.append({"date": day.isoformat(), "forward_curve": [
            {"tenor": t, "price": str(market.forward_prices[j][i]), "source": "SYNTHETIC_TEST"}
            for i, t in enumerate(STANDARD_TENORS)], "discount_factors": factors})
    trades = [trade, replace(trade, trade_id="SELL_400", side=-1, quantity=Decimal(400))]
    return {"calculation_date": market.as_of.isoformat(), "data_kind": "SYNTHETIC_TEST",
            "history_source": market.source_description, "history": rows,
            "trades": [{"trade_id": t.trade_id, "quantity": str(t.quantity), "strike": str(t.strike),
                        "side": t.side, "fixing_date": t.fixing_date.isoformat(),
                        "payment_date": t.payment_date.isoformat()} for t in trades]}


def main():
    parser = argparse.ArgumentParser(description="Только синтетический вход для проверки модуля ГО")
    parser.add_argument("--output", type=Path, default=Path("output/synthetic_margin_test_input.json"))
    args = parser.parse_args()
    data = build_input()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SYNTHETIC_TEST ONLY: {args.output.resolve()}")


if __name__ == "__main__":
    main()
