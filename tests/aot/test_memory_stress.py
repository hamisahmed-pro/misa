#!/usr/bin/env python3
"""AOT allocation-churn smoke test with a generous GC-mode RSS ceiling."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "impl"))
from misa import benchmark

MISA = os.path.join(ROOT, "impl", "bin", "misa")
SOURCE = os.path.join(ROOT, "tests", "aot", "prog", "a40_allocation_stress.misa")
EXPECTED = "200210000"
GC_RSS_CEILING_KIB = 256 * 1024


def main():
    with tempfile.TemporaryDirectory(prefix="misa-memory-stress-") as tmp:
        binary = os.path.join(tmp, "allocation-stress")
        wall, out, err, rc, _rss = benchmark.run_cmd(
            [MISA, "build", SOURCE, "-o", binary], timeout=300
        )
        if rc != 0:
            raise AssertionError(f"AOT build failed ({rc}): {err}")
        wall, out, err, rc, rss = benchmark.run_cmd([binary], timeout=120)
        if rc != 0:
            raise AssertionError(f"stress binary failed ({rc}): {err}")
        if out.strip() != EXPECTED:
            raise AssertionError(f"wrong stress result: {out.strip()!r}, expected {EXPECTED}")

        mode = benchmark._toolchain_metadata()["runtime_memory_manager"]
        if mode == "Boehm GC" and rss is not None and rss > GC_RSS_CEILING_KIB:
            raise AssertionError(
                f"Boehm-GC stress run peaked at {rss} KiB; "
                f"ceiling is {GC_RSS_CEILING_KIB} KiB"
            )
        print(f"AOT allocation stress: PASS; manager={mode}; peak_rss_kib={rss}")
        if mode != "Boehm GC":
            print("RSS ceiling skipped: this run does not use Boehm GC.")


if __name__ == "__main__":
    main()
