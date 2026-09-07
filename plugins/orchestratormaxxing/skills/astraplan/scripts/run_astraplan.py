#!/usr/bin/env python3
"""Run the shared bounded planner lifecycle with GPT-6 Astra."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SOLPLAN_RUNNER = (
    Path(__file__).resolve().parents[2]
    / "solplan"
    / "scripts"
    / "run_solplan.py"
)


def _load_solplan_runner():
    spec = importlib.util.spec_from_file_location("orchestratormaxxing_run_solplan", SOLPLAN_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared planner runner: {SOLPLAN_RUNNER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    runner = _load_solplan_runner()
    return runner.main(
        argv,
        model=runner.ASTRA_MODEL,
        allowed_models=(runner.ASTRA_MODEL,),
    )


if __name__ == "__main__":
    raise SystemExit(main())
