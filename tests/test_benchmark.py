#!/usr/bin/env python3
"""Regression tests for the benchmark harness contract."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa import benchmark


def main():
    # The harness now exposes a five-element run_cmd result including RSS.
    wall, out, err, rc, rss = benchmark.run_cmd(
        [sys.executable, "-c", "print('benchmark-contract')"]
    )
    assert rc == 0, err
    assert out.strip() == "benchmark-contract"
    assert wall is not None and wall >= 0
    assert rss is None or rss >= 0

    original = benchmark.run_cmd

    def fake_run(cmd, timeout=300):
        if "build" in cmd:
            return 0.01, "", "", 0, 1234
        return 0.01, "hi\n", "", 0, 2345

    benchmark.run_cmd = fake_run
    try:
        result = benchmark.bench_startup("misa", runs=3)
        assert result["status"] == "ok", result
        assert len(result["times"]) == 3
    finally:
        benchmark.run_cmd = original

    print("Benchmark harness: PASS")


if __name__ == "__main__":
    main()
