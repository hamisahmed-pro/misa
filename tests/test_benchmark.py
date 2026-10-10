#!/usr/bin/env python3
"""Regression tests for the benchmark harness contract."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa import benchmark


def main():
    assert benchmark._same_benchmark_output(
        "n-body", "-0.16902000037197656\n",
        "-0.169075164\n-0.169020000\n")
    assert not benchmark._same_benchmark_output(
        "n-body", "-0.1690\n", "-0.1689\n")
    assert benchmark._same_benchmark_output("fannkuch-redux", "7\n", "7\n")
    assert not benchmark._same_benchmark_output("fannkuch-redux", "7\n", "8\n")
    passing = {"results": [{
        "name": "n-body", "status": "ok", "ratio_misa_python": 1.2,
        "misa": {"times": [0.1, 0.14]},
        "python": {"times": [0.1, 0.1]},
    }]}
    assert benchmark.validate_required_cases(passing, ["n-body"], 2) == []
    assert any("expected exactly one" in e for e in
               benchmark.validate_required_cases(passing, ["fasta"], 2))
    assert any("expected 3" in e for e in
               benchmark.validate_required_cases(passing, ["n-body"], 3))
    # Fail closed on non-finite, non-positive, or internally inconsistent timings.
    bad_nan = {"results": [{**passing["results"][0],
                            "misa": {"times": [0.1, float("nan")]}}]}
    assert any("finite positive seconds" in e for e in
               benchmark.validate_required_cases(bad_nan, ["n-body"], 2))
    bad_zero = {"results": [{**passing["results"][0],
                             "python": {"times": [0.0, 0.1]}}]}
    assert any("finite positive seconds" in e for e in
               benchmark.validate_required_cases(bad_zero, ["n-body"], 2))
    bad_ratio = {"results": [{**passing["results"][0],
                              "ratio_misa_python": float("inf")}]}
    assert any("finite positive number" in e for e in
               benchmark.validate_required_cases(bad_ratio, ["n-body"], 2))
    inconsistent = {"results": [{**passing["results"][0],
                                 "ratio_misa_python": 8.0}]}
    assert any("disagrees with sample medians" in e for e in
               benchmark.validate_required_cases(inconsistent, ["n-body"], 2))

    failed = {"results": [{**passing["results"][0], "status": "checksum-mismatch"}]}
    assert any("checksum-mismatch" in e for e in
               benchmark.validate_required_cases(failed, ["n-body"], 2))
    duplicated = {"results": passing["results"] * 2}
    assert any("exactly one" in e for e in
               benchmark.validate_required_cases(duplicated, ["n-body"], 2))
    metadata = benchmark._toolchain_metadata()
    assert metadata["python"]
    assert metadata["runtime_memory_manager"] in {
        "Boehm GC", "malloc fallback (no collector)", "unknown"
    }

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
