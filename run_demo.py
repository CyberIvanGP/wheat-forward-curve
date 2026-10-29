"""Run the frozen full synthetic demo; never generate inputs at startup."""
from pathlib import Path
import sys

from run_project import main as run_project


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    return run_project(["--defaults-dir", str(Path(__file__).resolve().parent / "examples/full_demo"),
                        "--example", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
