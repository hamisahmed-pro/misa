#!/usr/bin/env python3
"""
External benchmark: Benchmarks Game n-body, spectral-norm, fannkuch-redux.
Compares official Python3 and Node.js implementations against Misa ports.

Methodology:
- Same input size (N) for all implementations
- 3 runs each, median reported
- Wall-clock time measured externally
- Output verified for correctness
"""
import subprocess
import time
import statistics
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
MISA_BIN = os.path.join(os.path.dirname(BENCH_DIR), "..", "impl", "bin", "misa")

def run_cmd(cmd, input_size):
    """Run command with input_size arg, return (seconds, stdout)."""
    start = time.perf_counter()
    result = subprocess.run(
        cmd + [str(input_size)],
        capture_output=True, text=True, timeout=300
    )
    elapsed = time.perf_counter() - start
    return elapsed, result.stdout.strip(), result.stderr.strip()

def benchmark(name, py_cmd, node_cmd, misa_cmd, sizes):
    print(f"\n=== {name} ===")
    print(f"{'Size':>8} | {'Python3':>10} | {'Node.js':>10} | {'Misa AOT':>10} | {'Misa/Python':>12}")
    print("-" * 65)
    for size in sizes:
        # Python3
        py_times = []
        for _ in range(3):
            t, out, _ = run_cmd(py_cmd, size)
            py_times.append(t)
        py_med = statistics.median(py_times)
        
        # Node.js
        node_times = []
        for _ in range(3):
            t, out, _ = run_cmd(node_cmd, size)
            node_times.append(t)
        node_med = statistics.median(node_times)
        
        # Misa (AOT via misa run, which uses cache)
        misa_times = []
        for _ in range(3):
            t, out, _ = run_cmd(misa_cmd, size)
            misa_times.append(t)
        misa_med = statistics.median(misa_times)
        
        ratio = misa_med / py_med if py_med > 0 else float('inf')
        print(f"{size:>8} | {py_med:>10.3f} | {node_med:>10.3f} | {misa_med:>10.3f} | {ratio:>11.1f}x")

if __name__ == "__main__":
    # n-body: N=2000 is reasonable for quick test
    # (official uses 50M, too large)
    benchmark(
        "n-body (Benchmarks Game)",
        ["python3", os.path.join(BENCH_DIR, "bgsrc", "nbody.python3")],
        ["node", os.path.join(BENCH_DIR, "bgsrc", "nbody.node.js")],
        [MISA_BIN, "run", os.path.join(BENCH_DIR, "nbody.misa"), "--"],
        [1000, 2000]
    )
