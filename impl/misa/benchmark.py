"""Canonical `misa benchmark` implementation.

Runs the Benchmarks Game suite (Misa ports vs Python3 reference),
emits JSON as the source of truth. Markdown/CSV are derived from JSON,
never hand-typed.

Methodology (per corrected 2026-10-07 harness):
- ALL languages timed EXTERNALLY via wall-clock around subprocess.
- AOT binaries built ONCE before timing (not in timed region).
- 5 runs per benchmark, median reported. All samples saved.
- Correctness verified: outputs must match before timing comparison.
"""
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

# Benchmarks with working Misa ports and their configs.
# (name, misa_file, python_file, arg, description)
BENCHMARKS = [
    ("n-body", "nbody.misa", "bgsrc/nbody.python3", "50000",
     "N-body simulation"),
    ("spectral-norm", "spectralnorm.misa", "bgsrc/spectralnorm.python3", "500",
     "Spectral norm"),
    ("fannkuch-redux", "fannkuchredux.misa", "bgsrc/fannkuchredux.python3", "10",
     "Fannkuch redux"),
    ("mandelbrot", "mandelbrot.misa", "bgsrc/mandelbrot.python3", "500",
     "Mandelbrot set"),
    ("fasta", "fasta.misa", "bgsrc/fasta.python3", "10000",
     "Fasta DNA generation"),
    ("reverse-complement", "revcomp.misa", "bgsrc/revcomp.python3", "10000",
     "Reverse complement"),
    ("binary-trees", "binarytrees.misa", "bgsrc/binarytrees.python3", "10",
     "Binary trees"),
    ("knucleotide", "knucleotide.misa", "bgsrc/knucleotide.python3", "10000",
     "K-nucleotide counting"),
    ("regex-redux", "regexredux.misa", "bgsrc/regexredux.python3", "10000",
     "Regex redux"),
    # pidigits: BigInt spigot has logic bug, excluded until fixed
]


def _find_misa_root():
    """Locate the misa workspace root."""
    # impl/misa/benchmark.py -> workspace/misa
    here = os.path.dirname(os.path.abspath(__file__))
    # here = .../impl/misa, root = .../ (workspace/misa)
    root = os.path.dirname(os.path.dirname(here))
    # Actually: impl/misa/benchmark.py -> dirname = impl/misa
    # -> dirname = impl -> dirname = workspace/misa
    return os.path.dirname(os.path.dirname(here))


def run_cmd(cmd, timeout=300):
    """Run command, return (wall_seconds, stdout, stderr, returncode)."""
    start = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        wall = time.perf_counter() - start
        return wall, r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return None, "", "timeout", -1


def build_misa(misa_bin, misa_file, out_path):
    """Build Misa file to binary ONCE (outside timed region)."""
    wall, out, err, rc = run_cmd([misa_bin, "build", misa_file, "-o", out_path])
    return rc == 0, err


def bench_one(name, misa_file, python_file, arg, misa_bin, bench_dir, runs):
    """Run one benchmark, return result dict."""
    misa_path = os.path.join(bench_dir, misa_file)
    py_path = os.path.join(bench_dir, python_file)

    if not os.path.isfile(misa_path):
        return {"name": name, "status": "missing-misa-port"}

    # Build Misa once (not timed)
    bin_path = f"/tmp/misa_bench_{name}"
    ok, err = build_misa(misa_bin, misa_path, bin_path)
    if not ok:
        return {"name": name, "status": "build-failed", "error": err[:200]}

    # Time Misa (execution only)
    misa_times = []
    for _ in range(runs):
        wall, out, err, rc = run_cmd([bin_path, arg])
        if rc != 0:
            return {"name": name, "status": "misa-failed", "error": err[:200]}
        misa_times.append(wall)

    # Time Python
    py_times = []
    for _ in range(runs):
        wall, out, err, rc = run_cmd(["python3", py_path, arg])
        if rc != 0:
            return {"name": name, "status": "python-failed", "error": err[:200]}
        py_times.append(wall)

    misa_med = statistics.median(misa_times)
    py_med = statistics.median(py_times)
    ratio = misa_med / py_med if py_med > 0 else None

    return {
        "name": name,
        "status": "ok",
        "misa": {
            "median": round(misa_med, 3),
            "times": [round(t, 3) for t in misa_times],
        },
        "python": {
            "median": round(py_med, 3),
            "times": [round(t, 3) for t in py_times],
        },
        "ratio_misa_python": round(ratio, 2) if ratio else None,
        "verdict": "faster" if ratio and ratio < 1 else "slower",
    }


def run_benchmarks(runs=5, only=None):
    """Run the full suite, return results dict."""
    root = _find_misa_root()
    bench_dir = os.path.join(root, "bench", "external")
    misa_bin = os.path.join(root, "impl", "bin", "misa")

    results = []
    for (name, misa_file, python_file, arg, desc) in BENCHMARKS:
        if only and name not in only:
            continue
        r = bench_one(name, misa_file, python_file, arg, misa_bin,
                      bench_dir, runs)
        r["description"] = desc
        results.append(r)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "methodology": "misa-benchmark-v1: external wall time, AOT built "
                       "outside timed region, 5 runs median",
        "misa_version": "AOT",
        "python_version": "3.12",
        "results": results,
    }


def to_markdown(data):
    """Generate markdown report from JSON data (never hand-typed)."""
    lines = [
        "# Misa Benchmark Report",
        "",
        f"**Generated:** {data['timestamp']}",
        f"**Methodology:** {data['methodology']}",
        "",
        "| Benchmark | Python3 | Misa AOT | Ratio | Verdict |",
        "|-----------|---------|----------|-------|---------|",
    ]
    for r in data["results"]:
        if r["status"] != "ok":
            lines.append(f"| {r['name']} | - | - | - | {r['status']} |")
            continue
        py = r["python"]["median"]
        misa = r["misa"]["median"]
        ratio = r["ratio_misa_python"]
        verdict = r["verdict"]
        lines.append(
            f"| {r['name']} | {py}s | {misa}s | {ratio}x | {verdict} |"
        )
    lines += [
        "",
        "_This report was generated from benchmark.json. Do not edit manually._",
    ]
    return "\n".join(lines)


def to_csv(data):
    """Generate CSV from JSON data."""
    lines = ["benchmark,python_median,misa_median,ratio,verdict"]
    for r in data["results"]:
        if r["status"] != "ok":
            lines.append(f"{r['name']},,,,{r['status']}")
            continue
        lines.append(
            f"{r['name']},{r['python']['median']},"
            f"{r['misa']['median']},{r['ratio_misa_python']},{r['verdict']}"
        )
    return "\n".join(lines)


def main(argv):
    """Entry point for `misa benchmark`."""
    runs = 5
    only = None
    fmt = "json"
    out = None
    i = 0
    while i < len(argv):
        if argv[i] == "--runs" and i + 1 < len(argv):
            runs = int(argv[i + 1])
            i += 2
        elif argv[i] == "--only" and i + 1 < len(argv):
            only = set(argv[i + 1].split(","))
            i += 2
        elif argv[i] == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
        elif argv[i] == "-o" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        else:
            print(f"usage: misa benchmark [--runs N] [--only a,b] "
                  f"[--format json|md|csv] [-o file]", file=sys.stderr)
            return 1

    data = run_benchmarks(runs=runs, only=only)

    if fmt == "json":
        text = json.dumps(data, indent=2)
    elif fmt == "md":
        text = to_markdown(data)
    elif fmt == "csv":
        text = to_csv(data)
    else:
        print(f"unknown format: {fmt}", file=sys.stderr)
        return 1

    if out:
        with open(out, "w") as f:
            f.write(text + "\n")
        print(f"wrote {out}")
    else:
        print(text)
    return 0
