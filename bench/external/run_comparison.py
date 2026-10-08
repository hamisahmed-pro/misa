#!/usr/bin/env python3
"""Matched CPython vs Misa AOT benchmark runner.

Builds the Misa ports before timing, checks every program result, warms each
implementation, randomizes the order within each trial, and reports raw wall
clock samples. Run from any directory with the repo's Python dependencies
installed: python3 bench/external/run_comparison.py [--runs 5] [--json out.json]
"""
from __future__ import annotations
import argparse
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = {"nbody": 50_000, "spectralnorm": 500, "fannkuchredux": 10}
PY_SOURCES = ROOT / "bench" / "external" / "bgsrc"
MISA_SOURCES = ROOT / "bench" / "external"
SEED = 20261008
TOLERANCE = 1e-8

def checked(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"Command failed ({p.returncode}): {cmd!r}\n{p.stderr[-2000:]}")
    return p

def fingerprint(case: str, stdout: str):
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if case == "nbody":
        # Reference emits initial and final energies; Misa emits final only.
        return float(lines[-1])
    if case == "spectralnorm":
        return float(lines[-1])
    if len(lines) < 2 or not lines[-1].startswith("Pfannkuchen("):
        raise ValueError(f"Unexpected fannkuch-redux output: {stdout!r}")
    return int(lines[-2]), int(lines[-1].rsplit("=", 1)[1].strip())

def same_result(case: str, lhs: str, rhs: str) -> bool:
    a, b = fingerprint(case, lhs), fingerprint(case, rhs)
    if case == "fannkuchredux":
        return a == b
    return math.isclose(a, b, rel_tol=0.0, abs_tol=TOLERANCE)

def execute(cmd: list[str], *, cwd: Path, env: dict[str, str]):
    start = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=300)
    elapsed = time.perf_counter() - start
    if p.returncode:
        raise RuntimeError(f"Run failed ({p.returncode}): {cmd!r}\n{p.stderr[-2000:]}")
    return elapsed, p.stdout.strip()

def build_misa(case: str, binary: Path, *, env: dict[str, str]):
    source = MISA_SOURCES / f"{case}.misa"
    checked([str(ROOT / "impl" / "bin" / "misa"), "build", str(source), "-o", str(binary)],
            cwd=ROOT, env=env)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="measured samples per implementation and case (default: 5)")
    parser.add_argument("--warmups", type=int, default=1, help="unmeasured warm-ups per implementation and case (default: 1)")
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    parser.add_argument("--json", type=Path, help="write raw measurements to this JSON file")
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups non-negative")
    cases = CASES if args.case == "all" else {args.case: CASES[args.case]}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "impl") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    rng = random.Random(SEED)
    results = {}
    with tempfile.TemporaryDirectory(prefix="misa-bench-") as tmp:
        tmpdir = Path(tmp)
        binaries = {}
        for case in cases:
            binary = tmpdir / case
            print(f"Building {case} (build time excluded from samples)...", flush=True)
            build_misa(case, binary, env=env)
            binaries[case] = binary
        for case, size in cases.items():
            cmds = {
                "CPython 3.12": [sys.executable, str(PY_SOURCES / f"{case}.python3"), str(size)],
                "Misa AOT": [str(binaries[case]), str(size)],
            }
            warm_outputs = {}
            for impl, cmd in cmds.items():
                out = ""
                for _ in range(args.warmups):
                    _, out = execute(cmd, cwd=ROOT, env=env)
                if args.warmups == 0:
                    _, out = execute(cmd, cwd=ROOT, env=env)
                warm_outputs[impl] = out
            if not same_result(case, warm_outputs["CPython 3.12"], warm_outputs["Misa AOT"]):
                raise AssertionError(f"{case}: output mismatch at N={size}: {warm_outputs!r}")
            samples = {impl: [] for impl in cmds}
            for trial in range(args.runs):
                order = list(cmds)
                rng.shuffle(order)
                for impl in order:
                    elapsed, out = execute(cmds[impl], cwd=ROOT, env=env)
                    if not same_result(case, warm_outputs["CPython 3.12"], out):
                        raise AssertionError(f"{case}/{impl}: inconsistent output in trial {trial+1}: {out!r}")
                    samples[impl].append(elapsed)
                    print(f"{case} N={size} trial={trial+1} {impl}: {elapsed:.6f}s", flush=True)
            medians = {impl: statistics.median(times) for impl, times in samples.items()}
            ratio = medians["CPython 3.12"] / medians["Misa AOT"]
            results[case] = {
                "input_n": size,
                "sample_count": args.runs,
                "warmups": args.warmups,
                "samples_seconds": samples,
                "median_seconds": medians,
                "python_over_misa_speed_ratio": ratio,
                "validated_result": fingerprint(case, warm_outputs["CPython 3.12"]),
            }
            print(f"{case}: Python/Misa median speed ratio {ratio:.3f}x", flush=True)
    geomean = math.exp(sum(math.log(v["python_over_misa_speed_ratio"]) for v in results.values()) / len(results))
    try:
        revision = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except subprocess.CalledProcessError:
        revision = "unknown"
    payload = {
        "schema": "misa-python-aot-benchmark-v1",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "revision": revision,
        "misa_version": "see repository revision",
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), "unknown"),
        "logical_cpu_count": os.cpu_count(),
        "compiler_pipeline": "misa build; LLVM module O2 pass pipeline; compilation excluded from timed samples",
        "method": "wall-clock subprocess timing including startup; warmups excluded; randomized within-trial order; median of raw samples; output checked on every run",
        "cases": results,
        "geometric_mean_python_over_misa_speed_ratio": geomean,
    }
    print(f"Geometric-mean Python/Misa ratio: {geomean:.3f}x")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Raw results: {args.json}")

if __name__ == "__main__":
    main()
