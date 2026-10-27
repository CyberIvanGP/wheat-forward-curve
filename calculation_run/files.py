"""Strict JSON inputs, block-level defaults and reproducible run folders."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import shutil

MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Нельзя сохранить {type(value).__name__}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторное поле JSON: {key}")
        result[key] = value
    return result


def parse_json(document):
    return json.loads(document, parse_float=Decimal, object_pairs_hook=_unique_object,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"Недопустимо: {x}")))


def read_json(path):
    return parse_json(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=encode,
                               allow_nan=False) + "\n", encoding="utf-8")


def new_run_folder(root, now=None):
    stamp = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).strftime("%Y-%m-%d_%H-%M-%S_%f")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        path = root / (stamp if not index else f"{stamp}_{index}")
        try:
            path.mkdir()
            for child in ("input", "results", "excel", "images"):
                (path / child).mkdir()
            return path
        except FileExistsError:
            continue
    raise OSError("Не удалось выделить уникальную папку запуска")


class InputFiles:
    def __init__(self, user, defaults, run_folder, *, example=False):
        self.user, self.defaults = Path(user), Path(defaults)
        self.folder = Path(run_folder)
        self.example = example
        self.sources = {}
        self._cache = {}

    def document(self, kind, name, allowed):
        cache_key = (kind, name)
        if cache_key in self._cache:
            return self._cache[cache_key]
        path = (self.user if kind == "input" else self.defaults) / name
        if not path.exists():
            return None
        target = self.folder / "input" / kind / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        data = read_json(target)
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError(f"{name}: неизвестные поля или нужен JSON-объект")
        self._cache[cache_key] = (data, target)
        return data, target

    def block(self, name, key, allowed):
        # A present null/empty block is not replaced by a default.
        for kind in (("default",) if self.example else ("input", "default")):
            document = self.document(kind, name, allowed)
            if document is None:
                continue
            data, path = document
            if key in data:
                self.sources[f"{name}:{key}"] = {
                    "source": kind, "file": path.relative_to(self.folder).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "calculation_date": data.get("calculation_date"),
                    "data_kind": data.get("data_kind", "UNSPECIFIED"),
                }
                return data[key], data
        raise FileNotFoundError(f"Нет блока {key} в input/{name} и в комплектных данных")

    def date_checked_block(self, name, key, allowed, day):
        value, context = self.block(name, key, allowed)
        if context.get("calculation_date") != day.isoformat():
            raise ValueError(f"{name}:{key}: дата данных {context.get('calculation_date')} != дата расчёта {day}")
        return value, context
