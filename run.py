"""python run.py [--input examples/default_input.json] [--output output] [--no-images]."""
import argparse
from datetime import date, time
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
import sys

from wheat_curve import calculate

ROOT = Path(__file__).resolve().parent


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Расчёт EOD-кривой пшеницы без веб-интерфейса")
    parser.add_argument("--input", type=Path, default=ROOT / "examples" / "default_input.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--no-images", action="store_true", help="Только расчёт и JSON, без внешних зависимостей")
    parser.add_argument("--parameters", type=Path, help="Общий файл параметров вместо стандартного")
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8-sig") as stream:
            data = json.load(stream, parse_float=Decimal)
        allowed = {"calculation_date", "previous_date", "previous_curve", "events", "parameter_overrides"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Входной JSON: неизвестные поля или неверная структура")
        required = allowed - {"parameter_overrides"}
        if required - data.keys():
            raise ValueError(f"Входной JSON: не хватает {', '.join(sorted(required - data.keys()))}")
        result = calculate(**data, parameters_file=args.parameters)
        # Validate and calculate fully before writing any output.
        args.output.mkdir(parents=True, exist_ok=True)
        audit = result.pop("audit")
        (args.output / "curve.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=encode) + "\n", encoding="utf-8")
        (args.output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=encode) + "\n", encoding="utf-8")
        if not args.no_images:
            from wheat_curve.plotting import save_images
            save_images(result, args.output)
        measured = sum(p["source"] not in ("NONE", "FALLBACK_PREVIOUS_DAY") for p in result["curve"])
        carried = sum(p["source"] == "FALLBACK_PREVIOUS_DAY" for p in result["curve"])
        print(f"Date: {result['calculation_date']}; measured: {measured}; carried: {carried}; missing: {39 - measured - carried}")
        print(f"Result: {args.output.resolve()}")
        print("curve.json; audit.json" + ("" if args.no_images else "; comparison.png; yesterday.png; today.png"))
        return 0
    except (ValueError, TypeError, KeyError, ArithmeticError, OSError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, ImportError):
            print("PNG dependency: python -m pip install -r requirements.txt; core only: --no-images", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
