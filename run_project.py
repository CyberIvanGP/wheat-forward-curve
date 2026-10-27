"""Portable entry point. Existing standalone commands remain available."""

import argparse
from pathlib import Path

from calculation_run.runner import ROOT, run_calculations


def main(argv=None):
    parser = argparse.ArgumentParser(description="Кривая пшеницы, NPV и ГО: единый запуск")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "input")
    parser.add_argument("--defaults-dir", type=Path, default=ROOT / "default_input")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--date", help="Дата расчёта YYYY-MM-DD; иначе настройки или текущая дата Москвы")
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--example", action="store_true", help="Только фиксированные комплектные данные и их дата")
    parser.add_argument("--allow-synthetic-test-data", action="store_true")
    parser.add_argument("--serve", action="store_true", help="Оставить локальный сервер для изменения сделок")
    parser.add_argument("--port", type=int, default=0, help="0: свободный локальный порт")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535")
    run = run_calculations(input_dir=args.input_dir, defaults_dir=args.defaults_dir,
                           output_dir=args.output_dir, example=args.example,
                           calculation_date=args.date, parameters_file=args.parameters,
                           allow_synthetic=args.allow_synthetic_test_data,
                           progress=lambda name: print(f"Расчёт: {name}", flush=True))
    from test.report import write_report
    write_report(run)
    errors = [name for name, state in run.info["modules"].items() if state["status"] == "error"]
    print(f"Папка запуска: {run.folder.resolve()}")
    print(f"Отчёт: {(run.folder / 'report.html').resolve()}")
    for name in errors:
        print(f"{name}: {run.info['modules'][name]['message']}")
    if args.serve:
        from test.local_server import serve
        serve(run, args.port)
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
