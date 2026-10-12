#!/usr/bin/env python3
"""Correctness-gated six-language sieve benchmark.

Compiles Go, Rust, and Misa before timing; runs Python, Node.js, PHP, Go,
Rust, and Misa as independent processes; validates exact output before and
during timing; randomizes paired run order; and writes raw samples plus
provenance. This is one matched workload, not a universal language ranking.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "bench" / "multilang"
DEFAULT_N = 500_000
SEED = 20261010


def run_checked(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                timeout=10, check=False)
        return ((result.stdout or result.stderr).splitlines() or ["unknown"])[0]
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def bootstrap_ratio_ci95(
    times: list[float], baseline: list[float], *, resamples: int = 2000,
    seed: int = SEED,
) -> list[float]:
    """Deterministic paired-bootstrap 95% interval for median runtime ratio."""
    if len(times) != len(baseline) or len(times) < 2 or resamples < 100:
        raise ValueError("paired bootstrap requires equal samples >= 2 and resamples >= 100")
    if any(not math.isfinite(x) or x <= 0 for x in times + baseline):
        raise ValueError("bootstrap samples must be finite positive seconds")
    pairs = list(zip(times, baseline))
    rng = random.Random(seed)
    ratios = []
    for _ in range(resamples):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        left = statistics.median(pair[0] for pair in draw)
        right = statistics.median(pair[1] for pair in draw)
        ratios.append(left / right)
    ratios.sort()
    return [
        ratios[max(0, math.ceil(0.025 * len(ratios)) - 1)],
        ratios[max(0, math.ceil(0.975 * len(ratios)) - 1)],
    ]


def compile_programs() -> dict[str, list[str]]:
    """Build ahead of timing so compiler work is not charged to execution."""
    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"misa-multilang-{os.getpid()}"
    out.mkdir(parents=True, exist_ok=True)
    sources = {
        "python": [sys.executable, str(HERE / "sieve.py")],
        "node": ["node", str(HERE / "sieve.js")],
        "php": ["php", str(HERE / "sieve.php")],
    }
    go_binary = out / "sieve-go"
    rust_binary = out / "sieve-rust"
    misa_binary = out / "sieve-misa"
    run_checked(["go", "build", "-trimpath", "-o", str(go_binary),
                 str(HERE / "sieve.go")])
    run_checked(["rustc", "-O", str(HERE / "sieve.rs"), "-o", str(rust_binary)])
    run_checked([sys.executable, str(ROOT / "impl" / "bin" / "misa"),
                 "build", str(HERE / "sieve.misa"), "-o", str(misa_binary)])
    sources.update({
        "go": [str(go_binary)],
        "rust": [str(rust_binary)],
        "misa": [str(misa_binary)],
    })
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--runs", type=int, default=9)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--json", type=Path, help="write the complete result JSON here")
    args = parser.parse_args()
    if args.n < 0 or args.runs < 3 or args.warmups < 0:
        parser.error("--n must be non-negative, --runs at least 3, and --warmups non-negative")

    required = ("python", "node", "php", "go", "rust", "misa")
    missing = [name for name, command in (
        ("node", ["node"]), ("php", ["php"]), ("go", ["go"]),
        ("rustc", ["rustc"]), ("gcc", ["gcc"])
    ) if shutil.which(command[0]) is None]
    if missing:
        raise RuntimeError("Missing required toolchains: " + ", ".join(missing))

    programs = compile_programs()
    expected = run_checked(programs["python"] + [str(args.n)]).stdout.strip()
    if not expected or len(expected.split()) != 2:
        raise RuntimeError(f"Python reference emitted malformed result: {expected!r}")
    if args.n == 500000 and expected != "41538 9914236195":
        raise AssertionError(
            "Known-answer check failed for N=500000: expected '41538 9914236195', "
            f"got {expected!r}"
        )

    # Check all implementations before collecting any timing samples.
    for name in required:
        actual = run_checked(programs[name] + [str(args.n)]).stdout.strip()
        if actual != expected:
            raise AssertionError(
                f"{name} output mismatch at N={args.n}: expected {expected!r}, got {actual!r}"
            )

    rng = random.Random(args.seed)
    samples = {name: [] for name in required}
    for _ in range(args.warmups):
        order = list(required)
        rng.shuffle(order)
        for name in order:
            actual = run_checked(programs[name] + [str(args.n)]).stdout.strip()
            if actual != expected:
                raise AssertionError(f"{name} output changed during warmup: {actual!r}")

    for trial in range(args.runs):
        order = list(required)
        rng.shuffle(order)
        for name in order:
            start = time.perf_counter()
            actual = run_checked(programs[name] + [str(args.n)]).stdout.strip()
            elapsed = time.perf_counter() - start
            if actual != expected:
                raise AssertionError(
                    f"{name} output changed in trial {trial + 1}: {actual!r}"
                )
            if not math.isfinite(elapsed) or elapsed <= 0:
                raise RuntimeError(f"{name} returned invalid elapsed time {elapsed!r}")
            samples[name].append(elapsed)
            print(f"trial={trial + 1} language={name} seconds={elapsed:.6f}", flush=True)

    medians = {name: statistics.median(values) for name, values in samples.items()}
    results = {}
    for name in required:
        values = samples[name]
        results[name] = {
            "median_seconds": medians[name],
            "min_seconds": min(values),
            "max_seconds": max(values),
            "stdev_seconds": statistics.stdev(values),
            "samples_seconds": values,
            "ratio_to_python": medians[name] / medians["python"],
            "ratio_ci95_to_python": (
                [1.0, 1.0] if name == "python"
                else bootstrap_ratio_ci95(values, samples["python"], seed=args.seed)
            ),
        }

    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=False).stdout.strip() or None
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=ROOT, capture_output=True, text=True, check=False).stdout
    tracked_diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=ROOT,
                                  capture_output=True, check=False).stdout
    sys.path.insert(0, str(ROOT / "impl"))
    llvmlite_version = None
    llvm_version = None
    memory_manager = "unknown"
    try:
        import llvmlite
        from llvmlite import binding as llvm
        llvmlite_version = llvmlite.__version__
        llvm_version = ".".join(map(str, llvm.llvm_version_info))
        from misa.aot.build import host_triple, runtime_flags
        defines, _cflags, _libs = runtime_flags(host_triple())
        memory_manager = ("Boehm GC" if "-DMISA_USE_GC" in defines
                          else "malloc fallback (no collector)")
    except Exception:
        # Toolchain setup is validated separately; retain explicit metadata
        # instead of hiding a benchmark result behind optional introspection.
        pass

    payload = {
        "schema": "misa-multilang-sieve-v1",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_revision": revision,
        "source_dirty": bool(status),
        "source_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
        "workload": {
            "name": "Sieve of Eratosthenes; prime count and sum",
            "n": args.n,
            "validated_stdout": expected,
            "warmups_per_language": args.warmups,
            "measured_samples_per_language": args.runs,
            "random_seed": args.seed,
            "timing": "independent process wall time; compilation excluded; randomized paired order",
            "correctness": "exact stdout equality against Python before and during measurement",
            "interpretation": "single matched workload; not a general language-performance ranking",
        },
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "processor": platform.processor()},
        "toolchains": {
            "python": sys.version.splitlines()[0],
            "node": version(["node", "--version"]),
            "php": version(["php", "--version"]),
            "go": version(["go", "version"]),
            "rustc": version(["rustc", "--version"]),
            "c_compiler": version(["gcc", "--version"]),
            "llvmlite": llvmlite_version,
            "llvm": llvm_version,
            "runtime_memory_manager": memory_manager,
            "misa": "repository revision; LLVM AOT",
        },
        "results": results,
    }
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered, encoding="utf-8")
        print(f"Wrote benchmark JSON to {args.json}")
    else:
        print(rendered)
    print("\nMedian execution times (lower is better; one workload only):")
    for name in required:
        print(f"  {name:>6}: {medians[name]:.6f}s "
              f"({results[name]['ratio_to_python']:.3f}x Python)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, AssertionError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
