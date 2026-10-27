"""Offline, file-based portfolio margin. No website, network or implicit test data."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

from guarantee_collateral import TradingCalendar, calculate_from_input


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Неизвестный тип результата: {type(value).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Модельное ГО портфеля зерновых форвардов")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output/portfolio_margin.json")
    parser.add_argument("--allow-synthetic-test-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {p.resolve() for p in (args.input, args.calendar, args.parameters) if p is not None}:
            raise ValueError("Результат не должен заменять вход, календарь или параметры")
        with args.input.open(encoding="utf-8-sig") as stream:
            data = json.load(stream, parse_float=Decimal)
        result = calculate_from_input(data, TradingCalendar.load(args.calendar),
                                      parameters_file=args.parameters,
                                      allow_synthetic_test_data=args.allow_synthetic_test_data)
        text = json.dumps(result, ensure_ascii=False, indent=2, default=encode)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"GO RUB: {result['portfolio']['go']}; NPV RUB: {result['portfolio']['current_value']}")
        print(f"Result: {args.output.resolve()}")
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, ArithmeticError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
