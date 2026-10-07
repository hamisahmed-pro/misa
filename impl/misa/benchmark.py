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
import hashlib
import platform
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
    """Run command, return (wall_seconds, stdout, stderr, returncode, peak_rss_kb)."""
    timed = ["/usr/bin/time", "-f", "%e %M"] + cmd

    start = time.perf_counter()
    try:
        r = subprocess.run(timed, capture_output=True, text=True,
                           timeout=timeout)
        wall = time.perf_counter() - start
        meta = r.stderr.rstrip().splitlines()
        rss = None
        if meta:
            parts = meta[-1].split()
            if len(parts) == 2:
                try: rss = int(parts[1])
                except ValueError: pass
                r.stderr = "\n".join(meta[:-1])
        return wall, r.stdout, r.stderr, r.returncode, rss
    except subprocess.TimeoutExpired:
        return None, "", "timeout", -1, None


def build_misa(misa_bin, misa_file, out_path):
    """Build Misa file to binary ONCE (outside timed region)."""
    wall, out, err, rc, _ = run_cmd([misa_bin, "build", misa_file, "-o", out_path])
    return rc == 0, err


def bench_one(name, misa_file, python_file, arg, misa_bin, bench_dir, runs, warmups=3):
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

    # Warmup outside measurements, then independent process measurements.
    for _ in range(warmups):
        wall, out, err, rc, _rss = run_cmd([bin_path, arg])
        if rc != 0: return {"name": name, "status": "misa-failed", "error": err[:200]}
    misa_times = []
    misa_rss = []
    misa_out = None
    for _ in range(runs):
        wall, out, err, rc, rss = run_cmd([bin_path, arg])
        if rc != 0:
            return {"name": name, "status": "misa-failed", "error": err[:200]}
        misa_times.append(wall); misa_rss.append(rss or 0)
        if misa_out is None: misa_out = out
        elif out != misa_out: return {"name": name, "status": "nondeterministic-misa"}

    # Time Python
    for _ in range(warmups):
        wall, out, err, rc, _rss = run_cmd(["python3", py_path, arg])
        if rc != 0: return {"name": name, "status": "python-failed", "error": err[:200]}
    py_times = []
    py_rss = []
    py_out = None
    for _ in range(runs):
        wall, out, err, rc, rss = run_cmd(["python3", py_path, arg])
        if rc != 0:
            return {"name": name, "status": "python-failed", "error": err[:200]}
        py_times.append(wall); py_rss.append(rss or 0)
        if py_out is None: py_out = out
        elif out != py_out: return {"name": name, "status": "nondeterministic-python"}
    if misa_out != py_out:
        return {"name": name, "status": "checksum-mismatch", "misa_sha256": hashlib.sha256(misa_out.encode()).hexdigest(), "python_sha256": hashlib.sha256(py_out.encode()).hexdigest()}

    misa_med = statistics.median(misa_times)
    py_med = statistics.median(py_times)
    ratio = misa_med / py_med if py_med > 0 else None

    return {
        "name": name,
        "status": "ok",
        "misa": {
            "median": round(misa_med, 3),
            "times": [round(t, 6) for t in misa_times],
            "p95": round(sorted(misa_times)[max(0, int(len(misa_times)*0.95)-1)], 6),
            "min": round(min(misa_times), 6), "max": round(max(misa_times), 6),
            "stddev": round(statistics.stdev(misa_times), 6) if len(misa_times)>1 else 0,
            "peak_rss_kb": max(misa_rss),
        },
        "python": {
            "median": round(py_med, 3),
            "times": [round(t, 6) for t in py_times],
            "p95": round(sorted(py_times)[max(0, int(len(py_times)*0.95)-1)], 6),
            "min": round(min(py_times), 6), "max": round(max(py_times), 6),
            "stddev": round(statistics.stdev(py_times), 6) if len(py_times)>1 else 0,
            "peak_rss_kb": max(py_rss),
        },
        "ratio_misa_python": round(ratio, 2) if ratio else None,
        "verdict": "faster" if ratio and ratio < 1 else "slower",
    }


def bench_startup(misa_bin, runs=5):
    """Measure hello-world startup time (launch to first output)."""
    prog = 'print("hi")\n'
    path = "/tmp/bench_hello.misa"
    with open(path, "w") as f:
        f.write(prog)
    # Build once
    bin_path = "/tmp/bench_hello_bin"
    wall, out, err, rc = run_cmd([misa_bin, "build", path, "-o", bin_path])
    if rc != 0:
        return {"status": "build-failed", "error": err[:200]}
    times = []
    for _ in range(runs):
        wall, out, err, rc, _rss = run_cmd([bin_path])
        if rc != 0 or out.strip() != "hi":
            return {"status": "run-failed", "error": err[:200]}
        times.append(wall)
    return {
        "status": "ok",
        "median": round(statistics.median(times), 4),
        "times": [round(t, 4) for t in times],
    }


def bench_compile(misa_bin, runs=3):
    """Measure compilation time (source to executable)."""
    # Use nbody.misa as a medium-size program
    root = _find_misa_root()
    src = os.path.join(root, "bench", "external", "nbody.misa")
    if not os.path.isfile(src):
        return {"status": "missing-source"}
    times = []
    for _ in range(runs):
        out_path = "/tmp/bench_compile_out"
        if os.path.exists(out_path):
            os.remove(out_path)
        wall, out, err, rc, _rss = run_cmd([misa_bin, "build", src, "-o", out_path])
        if rc != 0:
            return {"status": "build-failed", "error": err[:200]}
        times.append(wall)
    # Binary size
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    return {
        "status": "ok",
        "median": round(statistics.median(times), 3),
        "times": [round(t, 3) for t in times],
        "binary_bytes": size,
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
                      bench_dir, runs, warmups=3)
        r["description"] = desc
        results.append(r)

    # Add startup and compile benchmarks (unless --only filters them out)
    if not only or "startup" in only:
        results.append({
            "name": "startup",
            "description": "Hello-world cold start (launch to output)",
            **bench_startup(misa_bin, runs),
        })
    if not only or "compile" in only:
        results.append({
            "name": "compile",
            "description": "AOT compilation time (nbody.misa)",
            **bench_compile(misa_bin),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "methodology": "misa-benchmark-v1: external wall time, AOT built "
                       "outside timed region, 5 runs median",
        "misa_version": "AOT",
        "host": platform.platform(),
        "cpu": platform.processor(),
        "methodology": "external wall time; 3 warmups; measured independent processes; median/p95/min/max/stddev; peak RSS; exact stdout equality",
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
        "## Compute Benchmarks (vs Python3)",
        "",
        "| Benchmark | Python3 | Misa AOT | Ratio | Verdict |",
        "|-----------|---------|----------|-------|---------|",
    ]
    for r in data["results"]:
        if r["name"] in ("startup", "compile"):
            continue
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
    lines += ["", "## Startup & Compilation", ""]
    for r in data["results"]:
        if r["name"] == "startup" and r["status"] == "ok":
            lines.append(f"- **Startup** (hello-world): {r['median']}s median")
        if r["name"] == "compile" and r["status"] == "ok":
            kb = r["binary_bytes"] // 1024
            lines.append(f"- **Compile** (nbody.misa): {r['median']}s median, "
                         f"{kb}KB binary")
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
    runs = 10
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
