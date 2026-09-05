#!/usr/bin/env python3
"""Astra profile of the paired, hardened Solplan engine (no model fallback)."""
from pathlib import Path
import importlib.util
import sys


def main():
    engine = Path(__file__).resolve().parents[2] / "solplan/scripts/run_solplan.py"
    if not engine.is_file() or engine.is_symlink():
        print("astraplan: paired solplan engine missing; reinstall the omaxxing skill stack", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("omaxx_planner", engine)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(planner="astra")


if __name__ == "__main__":
    raise SystemExit(main())
