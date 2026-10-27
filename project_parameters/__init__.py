"""One editable parameter file; no NumPy/SciPy dependency in wheat calculations."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

DEFAULT_PARAMETERS_PATH = Path(__file__).with_name("default_calculation_parameters.json")


def load_parameters(path=None):
    with Path(path or DEFAULT_PARAMETERS_PATH).open(encoding="utf-8-sig") as stream:
        result = json.load(stream, parse_float=Decimal)
    if (not isinstance(result, dict) or set(result) != {"schema_version", "parameter_sources", "forward_curve", "margin"}
            or result["schema_version"] != 1):
        raise ValueError("Неверная структура общего файла параметров")
    if not all(isinstance(result[k], dict) for k in ("forward_curve", "margin", "parameter_sources")):
        raise ValueError("Неверные разделы параметров")
    return deepcopy(result)
