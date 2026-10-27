"""Explicit MOEX download or offline snapshot -> dated Decimal discount factors."""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from interest_rate_curve import (
    CurveDownloadError, calculate_discount_factors, download_moex_g_curve,
    load_moex_g_curve_snapshot, save_moex_g_curve_snapshot,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Дисконт-факторы MOEX G-curve, ACT/365F")
    parser.add_argument("--valuation-date", required=True, type=date.fromisoformat)
    parser.add_argument("--payment-dates", required=True, nargs="+", type=date.fromisoformat)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--download", action="store_true", help="Явно загрузить MOEX через HTTPS")
    source.add_argument("--snapshot", type=Path, help="Считать локальный снимок, без сети")
    parser.add_argument("--use-system-proxy", action="store_true", help="Использовать прокси Python/ОС при загрузке")
    parser.add_argument("--save-snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output/discount_factors.json")
    args = parser.parse_args(argv)
    try:
        if any(d < args.valuation_date for d in args.payment_dates):
            raise ValueError("Дата платежа не может быть раньше даты оценки")
        if args.output.resolve() == (args.snapshot.resolve() if args.snapshot else None):
            raise ValueError("Выходной файл не должен заменять входной снимок")
        if args.save_snapshot and args.output.resolve() == args.save_snapshot.resolve():
            raise ValueError("Снимок и дисконт-факторы должны сохраняться в разные файлы")
        curve = (download_moex_g_curve(args.valuation_date, use_system_proxy=args.use_system_proxy) if args.download else
                 load_moex_g_curve_snapshot(args.snapshot, valuation_date=args.valuation_date))
        factors = calculate_discount_factors(curve, args.payment_dates)
        result = {
            "valuation_date": curve.valuation_date.isoformat(),
            "source": "MOEX GCURVE", "source_url": "https://iss.moex.com/iss/engines/stock/zcyc.json",
            "snapshot_time_moscow": curve.snapshot_time.isoformat(),
            "day_count": "ACT/365F", "short_end": "Flat model rate below 0.25 years",
            "discount_factors": {day.isoformat(): str(value) for day, value in factors.items()},
        }
        if args.save_snapshot:
            save_moex_g_curve_snapshot(curve, args.save_snapshot)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"MOEX snapshot: {curve.valuation_date} {curve.snapshot_time} MSK")
        print(f"Discount factors: {len(factors)}; output: {args.output.resolve()}")
        return 0
    except (ValueError, TypeError, OSError, ArithmeticError, CurveDownloadError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
