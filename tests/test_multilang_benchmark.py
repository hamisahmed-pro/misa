#!/usr/bin/env python3
"""Unit tests for cross-language benchmark statistics (no compilers required)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "multilang" / "run.py"
spec = importlib.util.spec_from_file_location("misa_multilang_benchmark", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def main():
    exact = module.bootstrap_ratio_ci95(
        [2.0, 4.0, 6.0, 8.0, 10.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        resamples=500,
        seed=42,
    )
    assert exact == [2.0, 2.0], exact
    repeat = module.bootstrap_ratio_ci95(
        [2.0, 4.0, 6.0, 8.0, 10.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        resamples=500,
        seed=42,
    )
    assert repeat == exact, "bootstrap must be deterministic"
    try:
        module.bootstrap_ratio_ci95([1.0, float("nan")], [1.0, 1.0])
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite samples must be rejected")
    print("Multilanguage benchmark statistics: PASS")


if __name__ == "__main__":
    main()
