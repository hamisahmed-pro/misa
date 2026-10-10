#!/usr/bin/env python3
"""Compatibility shim for the retired legacy benchmark runner.

The previous script ignored process exit codes and did not verify output
correctness, so its timing ratios were not suitable for performance claims.
Use the canonical correctness-gated runner instead:

    python3 impl/bin/misa benchmark --runs 10 --scale default --format json -o bench/results/benchmark.json

All scored workloads, failures, timing samples, and provenance are now owned by
impl/misa/benchmark.py. Extra CLI arguments are forwarded unchanged.
"""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
MISA = ROOT / "impl" / "bin" / "misa"

if not MISA.is_file():
    print(f"error: Misa CLI not found at {MISA}", file=sys.stderr)
    raise SystemExit(2)

print(
    "NOTICE: this legacy runner is retired because it did not validate "
    "exit codes and benchmark outputs. Forwarding to 'misa benchmark'.",
    file=sys.stderr,
)
os.execv(sys.executable, [sys.executable, str(MISA), "benchmark", *sys.argv[1:]])
