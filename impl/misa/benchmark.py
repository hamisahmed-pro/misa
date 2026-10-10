"""Canonical `misa benchmark` implementation.

Runs the Benchmarks Game suite (Misa ports vs Python3 reference),
emits JSON as the source of truth. Markdown/CSV are derived from JSON,
never hand-typed.

Methodology (per corrected 2026-10-07 harness):
- ALL languages timed EXTERNALLY via wall-clock around subprocess.
- AOT binaries built ONCE before timing (not in timed region).
- 10 runs per benchmark by default, median reported. All samples saved.
- Correctness verified: outputs must match before timing comparison.
"""
import json
import os
import statistics
import hashlib
import math
import platform
import random
import shutil
import subprocess
import sys
import time
import tempfile
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
# These reference programs consume stdin while their current Misa ports
# synthesize simplified data internally. Skipping is both safer and more
# honest than comparing unmatched workloads or blocking on an unprovided stream.
STDIN_MISMATCHED = {"reverse-complement", "knucleotide", "regex-redux"}


_TEMP_DIRS = []


def _temporary_path(prefix):
    """Return a collision-free path in a private temp directory.

    Keeping the TemporaryDirectory objects alive makes cleanup automatic at
    process exit while isolating concurrent benchmark invocations.
    """
    directory = tempfile.TemporaryDirectory(prefix="misa-benchmark-")
    _TEMP_DIRS.append(directory)
    return os.path.join(directory.name, prefix)


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
    # /usr/bin/time is not installed on every supported host. Use the
    # platform Python to collect child peak RSS via resource, with a private
    # stderr sentinel removed before returning program diagnostics.
    wrapper = '''import subprocess, sys
try:
    import resource
except ImportError:
    resource = None
p = subprocess.run(sys.argv[1:], capture_output=True)
sys.stdout.buffer.write(p.stdout)
sys.stderr.buffer.write(p.stderr)
if resource is not None:
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # macOS reports bytes; Linux and the BSDs report KiB.
    if sys.platform == "darwin":
        rss = (rss + 1023) // 1024
    print("\\n__MISA_CHILD_RSS_KB__=" + str(rss), file=sys.stderr)
sys.exit(p.returncode)
'''
    timed = [sys.executable, "-c", wrapper, *cmd]

    start = time.perf_counter()
    try:
        r = subprocess.run(timed, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout)
        wall = time.perf_counter() - start
        rss = None
        marker = "__MISA_CHILD_RSS_KB__="
        lines = r.stderr.splitlines()
        keep = []
        for line in lines:
            if line.startswith(marker):
                try:
                    rss = int(line[len(marker):])
                except ValueError:
                    pass
            else:
                keep.append(line)
        r.stderr = "\n".join(keep)
        return wall, r.stdout, r.stderr, r.returncode, rss
    except subprocess.TimeoutExpired:
        return None, "", "timeout", -1, None


def build_misa(misa_bin, misa_file, out_path):
    """Build Misa file to binary ONCE (outside timed region)."""
    wall, out, err, rc, _ = run_cmd([misa_bin, "build", misa_file, "-o", out_path])
    return rc == 0, err


def _p95(values):
    """Nearest-rank 95th percentile (the maximum for fewer than 20 samples)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _bootstrap_ratio_ci95(misa_times, python_times, resamples=2000, seed=20261009):
    """Deterministic paired-bootstrap 95% interval for the ratio of medians.

    Pairing follows each randomized benchmark trial index. The interval is
    descriptive, not proof of universal performance or independence across
    separate benchmark sessions.
    """
    if (len(misa_times) != len(python_times) or len(misa_times) < 2
            or resamples < 100):
        raise ValueError("paired bootstrap requires equal samples >= 2 and resamples >= 100")
    pairs = list(zip(misa_times, python_times))
    rng = random.Random(seed)
    ratios = []
    count = len(pairs)
    for _ in range(resamples):
        draw = [pairs[rng.randrange(count)] for _ in range(count)]
        left = statistics.median(pair[0] for pair in draw)
        right = statistics.median(pair[1] for pair in draw)
        if right > 0 and math.isfinite(left) and math.isfinite(right):
            ratios.append(left / right)
    if not ratios:
        raise ValueError("bootstrap produced no valid ratios")
    ratios.sort()
    lower = ratios[max(0, math.ceil(0.025 * len(ratios)) - 1)]
    upper = ratios[max(0, math.ceil(0.975 * len(ratios)) - 1)]
    return [round(lower, 6), round(upper, 6)]


def _same_benchmark_output(name, left, right):
    """Compare official results semantically where reference formatting differs."""
    if name in {"n-body", "spectral-norm"}:
        try:
            a = float([x.strip() for x in left.splitlines() if x.strip()][-1])
            b = float([x.strip() for x in right.splitlines() if x.strip()][-1])
            return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8)
        except (ValueError, IndexError):
            return False
    return left == right


def validate_required_cases(data, required, runs):
    """Fail closed unless each required case has valid, positive measurements.

    This gate intentionally validates the shape and numeric quality of the
    measurements in addition to status/sample counts. It does not trust a
    non-null ratio alone: corrupted JSON, NaN/Infinity, zero/negative timing,
    or an inconsistent reported ratio must never enter a performance score.
    """
    errors = []
    by_name = {}
    for result in data.get("results", []):
        by_name.setdefault(result.get("name"), []).append(result)

    for name in required:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            errors.append(f"{name}: expected exactly one result, found {len(matches)}")
            continue

        result = matches[0]
        if result.get("status") != "ok":
            errors.append(f"{name}: status is {result.get('status', 'missing')}")
            continue

        ratio = result.get("ratio_misa_python")
        if (not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
                or not math.isfinite(ratio) or ratio <= 0):
            errors.append(f"{name}: runtime ratio must be a finite positive number")

        medians = {}
        for impl in ("misa", "python"):
            samples = result.get(impl, {}).get("times", [])
            if not isinstance(samples, list) or len(samples) != runs:
                count = len(samples) if isinstance(samples, list) else "invalid"
                errors.append(f"{name}: {impl} has {count} samples, expected {runs}")
                continue
            valid_samples = all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0
                for value in samples
            )
            if not valid_samples:
                errors.append(f"{name}: {impl} samples must all be finite positive seconds")
                continue
            medians[impl] = statistics.median(samples)

        for impl in ("misa", "python"):
            median = result.get(impl, {}).get("median")
            if (not isinstance(median, (int, float)) or isinstance(median, bool)
                    or not math.isfinite(median) or median <= 0):
                errors.append(f"{name}: {impl} median must be a finite positive number")
            elif impl in medians and not math.isclose(
                median, medians[impl], rel_tol=0.0, abs_tol=0.00000000051
            ):
                errors.append(
                    f"{name}: reported {impl} median {median} disagrees with "
                    f"sample median {medians[impl]:.9g}"
                )

        if "misa" in medians and "python" in medians:
            expected_ratio = medians["misa"] / medians["python"]
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio):
                # The canonical report rounds ratios to two decimal places.
                if not math.isclose(ratio, expected_ratio, rel_tol=0.00005, abs_tol=0.000005):
                    errors.append(
                        f"{name}: reported ratio {ratio} disagrees with sample medians "
                        f"({expected_ratio:.6g})"
                    )
    return errors


def _source_provenance(root):
    """Capture source identity before benchmark execution begins."""
    revision = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "-C", root, "status", "--porcelain",
                             "--untracked-files=all"], capture_output=True,
                            text=True, check=False)
    diff = subprocess.run(["git", "-C", root, "diff", "--binary", "HEAD"],
                          capture_output=True, check=False)
    status_text = status.stdout or ""
    return {
        "source_revision": (revision.stdout.strip() or None),
        "source_dirty": bool(status_text),
        "source_status_sha256": hashlib.sha256(status_text.encode()).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(diff.stdout or b"").hexdigest(),
    }


def _toolchain_metadata():
    """Best-effort exact compiler/runtime version metadata."""
    try:
        import llvmlite
        from llvmlite import binding as llvm
        llvmlite_version = llvmlite.__version__
        llvm_version = ".".join(map(str, llvm.llvm_version_info))
    except Exception:
        llvmlite_version = None
        llvm_version = None
    cc = shutil.which("gcc")
    cc_version = None
    if cc:
        try:
            result = subprocess.run([cc, "--version"], capture_output=True,
                                    text=True, timeout=5, check=False)
            cc_version = (result.stdout or result.stderr).splitlines()[0]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    gc_mode = None
    gc_package_version = None
    try:
        from .aot.build import host_triple, runtime_flags
        defines, _cflags, _libs = runtime_flags(host_triple())
        gc_mode = ("Boehm GC" if "-DMISA_USE_GC" in defines
                   else "malloc fallback (no collector)")
    except Exception:
        gc_mode = "unknown"
    try:
        pkg = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "libgc-dev"],
            capture_output=True, text=True, timeout=3, check=False)
        if pkg.returncode == 0:
            gc_package_version = pkg.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "python": platform.python_version(),
        "llvmlite": llvmlite_version,
        "llvm": llvm_version,
        "c_compiler": cc_version,
        "runtime_memory_manager": gc_mode,
        "boehm_gc_package_version": gc_package_version,
        "aot_optimization": "LLVM O2; fast-math disabled",
    }


def bench_one(name, misa_file, python_file, arg, misa_bin, bench_dir, runs,
              warmups=3, rng=None, case_id=None):
    """Run one benchmark, return result dict."""
    if name in STDIN_MISMATCHED:
        return {"name": name, "status": "skipped-unmatched-stdin",
                "reason": "Python reference requires stdin; Misa port does not use the same input fixture"}
    misa_path = os.path.join(bench_dir, misa_file)
    py_path = os.path.join(bench_dir, python_file)

    if not os.path.isfile(misa_path):
        return {"name": name, "status": "missing-misa-port"}

    # Build Misa once (not timed)
    bin_path = _temporary_path(f"misa_bench_{name}")
    ok, err = build_misa(misa_bin, misa_path, bin_path)
    if not ok:
        return {"name": name, "status": "build-failed", "error": err[:200]}

    commands = {
        "misa": [bin_path, arg],
        "python": [sys.executable, py_path, arg],
    }
    rng = rng or random.Random(20261009)

    # Untimed preflight makes correctness independent of warmup count.
    outputs = {}
    for impl in ("misa", "python"):
        _wall, output, err, rc, _rss = run_cmd(commands[impl])
        if rc != 0:
            return {"name": name, "status": f"{impl}-failed",
                    "error": err[:200]}
        outputs[impl] = output
    if not _same_benchmark_output(name, outputs["misa"], outputs["python"]):
        return {"name": name, "status": "checksum-mismatch",
                "misa_sha256": hashlib.sha256(outputs["misa"].encode()).hexdigest(),
                "python_sha256": hashlib.sha256(outputs["python"].encode()).hexdigest()}

    # Paired, randomized order limits systematic thermal/frequency/order bias.
    for _ in range(warmups):
        order = ["misa", "python"]
        rng.shuffle(order)
        for impl in order:
            _wall, output, err, rc, _rss = run_cmd(commands[impl])
            if rc != 0:
                return {"name": name, "status": f"{impl}-failed",
                        "error": err[:200]}
            if not _same_benchmark_output(name, output, outputs[impl]):
                return {"name": name,
                        "status": f"nondeterministic-{impl}"}

    samples = {"misa": [], "python": []}
    rss_samples = {"misa": [], "python": []}
    for _ in range(runs):
        order = ["misa", "python"]
        rng.shuffle(order)
        for impl in order:
            wall, output, err, rc, rss = run_cmd(commands[impl])
            if rc != 0:
                return {"name": name, "status": f"{impl}-failed",
                        "error": err[:200]}
            if not _same_benchmark_output(name, output, outputs[impl]):
                return {"name": name,
                        "status": f"nondeterministic-{impl}"}
            samples[impl].append(wall)
            rss_samples[impl].append(rss or 0)
    misa_times, py_times = samples["misa"], samples["python"]
    misa_rss, py_rss = rss_samples["misa"], rss_samples["python"]

    misa_med = statistics.median(misa_times)
    py_med = statistics.median(py_times)
    ratio = misa_med / py_med if py_med > 0 else None

    return {
        "name": name,
        "case_id": case_id or name,
        "input_argument": arg,
        "status": "ok",
        "misa": {
            "median": round(misa_med, 9),
            "times": [round(t, 9) for t in misa_times],
            "p95": round(_p95(misa_times), 9),
            "min": round(min(misa_times), 9), "max": round(max(misa_times), 9),
            "stddev": round(statistics.stdev(misa_times), 9) if len(misa_times)>1 else 0,
            "peak_rss_kb": max(misa_rss),
        },
        "python": {
            "median": round(py_med, 9),
            "times": [round(t, 9) for t in py_times],
            "p95": round(_p95(py_times), 9),
            "min": round(min(py_times), 9), "max": round(max(py_times), 9),
            "stddev": round(statistics.stdev(py_times), 9) if len(py_times)>1 else 0,
            "peak_rss_kb": max(py_rss),
        },
        "ratio_misa_python": round(ratio, 6) if ratio else None,
        "ratio_ci95": _bootstrap_ratio_ci95(misa_times, py_times),
        "verdict": "faster" if ratio and ratio < 1 else "slower",
    }


def bench_startup(misa_bin, runs=5):
    """Measure hello-world startup time (launch to first output)."""
    prog = 'print("hi")\n'
    path = _temporary_path("bench_hello.misa")
    with open(path, "w") as f:
        f.write(prog)
    # Build once
    bin_path = _temporary_path("bench_hello_bin")
    wall, out, err, rc, _rss = run_cmd([misa_bin, "build", path, "-o", bin_path])
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
        out_path = _temporary_path("bench_compile_out")
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


def run_benchmarks(runs=10, only=None, scale="default"):
    """Run the full suite, return results dict."""
    root = _find_misa_root()
    provenance = _source_provenance(root)
    toolchain = _toolchain_metadata()
    bench_dir = os.path.join(root, "bench", "external")
    misa_bin = os.path.join(root, "impl", "bin", "misa")

    results = []
    rng = random.Random(20261009)
    scale_sizes = {
        "small": {"n-body": "5000", "spectral-norm": "100",
                  "fannkuch-redux": "8", "mandelbrot": "100",
                  "fasta": "1000", "reverse-complement": "1000",
                  "binary-trees": "8", "knucleotide": "1000",
                  "regex-redux": "1000"},
        "large": {"n-body": "100000", "spectral-norm": "1000",
                  "fannkuch-redux": "11", "mandelbrot": "1000",
                  "fasta": "100000", "reverse-complement": "100000",
                  "binary-trees": "12", "knucleotide": "100000",
                  "regex-redux": "100000"},
    }
    if scale not in {"small", "default", "large"}:
        raise ValueError("scale must be small, default, or large")
    for (name, misa_file, python_file, default_arg, desc) in BENCHMARKS:
        if only and name not in only:
            continue
        arg = (scale_sizes[scale].get(name, default_arg)
               if scale != "default" else default_arg)
        r = bench_one(name, misa_file, python_file, arg, misa_bin,
                      bench_dir, runs, warmups=3, rng=rng,
                      case_id=f"{name}:{scale}")
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

    valid_ratios = [r["ratio_misa_python"] for r in results
                    if r.get("status") == "ok"
                    and r.get("ratio_misa_python") is not None]
    aggregate = {
        "scored_cases": len(valid_ratios),
        "score_definition": "geometric mean of Misa median / Python median; >1 means Misa slower",
        "misa_python_geomean": (round(math.exp(
            sum(math.log(x) for x in valid_ratios) / len(valid_ratios)), 4)
            if valid_ratios else None),
    }
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "misa_version": "AOT",
        "host": platform.platform(),
        "cpu": platform.processor(),
        "methodology": {
            "timing": "external wall-clock per independent process; paired randomized implementation order",
            "warmups": 3,
            "measured_runs": runs,
            "aot_build": "outside runtime measurements",
            "statistics": ["median", "p95", "min", "max", "stddev"],
            "memory": "peak child-process RSS from resource.getrusage in a Python wrapper",
            "correctness": "exact stdout equality except n-body/spectral-norm compare canonical numeric result within 1e-8",
            "checksum": "SHA-256 reported on output mismatch"
        },
        "scale": scale,
        "random_seed": 20261009,
        **provenance,
        "toolchain": toolchain,
        "aggregate": aggregate,
        "results": results,
    }


def to_markdown(data):
    """Generate markdown report from JSON data (never hand-typed)."""
    lines = [
        "# Misa Benchmark Report",
        "",
        f"**Generated:** {data['timestamp']}",
        f"**Methodology:** {json.dumps(data['methodology'], sort_keys=True)}",
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
    scale = "default"
    required = []
    i = 0
    while i < len(argv):
        if argv[i] == "--runs" and i + 1 < len(argv):
            runs = int(argv[i + 1])
            i += 2
        elif argv[i] == "--only" and i + 1 < len(argv):
            only = set(argv[i + 1].split(","))
            i += 2
        elif argv[i] == "--require" and i + 1 < len(argv):
            required = [name for name in argv[i + 1].split(",") if name]
            i += 2
        elif argv[i] == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
        elif argv[i] == "--scale" and i + 1 < len(argv):
            scale = argv[i + 1]
            if scale not in {"small", "default", "large"}:
                print("--scale must be small, default, or large", file=sys.stderr)
                return 1
            i += 2
        elif argv[i] == "-o" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        else:
            print(f"usage: misa benchmark [--runs N] [--only a,b] "
                  "[--require a,b] [--scale small|default|large] "
                  f"[--format json|md|csv] [-o file]", file=sys.stderr)
            return 1

    if runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 1

    data = run_benchmarks(runs=runs, only=only, scale=scale)
    gate_errors = validate_required_cases(data, required, runs) if required else []
    if required:
        data["acceptance"] = {
            "required_cases": required,
            "passed": not gate_errors,
            "errors": gate_errors,
        }

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
    if gate_errors:
        print("benchmark acceptance failed: " + "; ".join(gate_errors),
              file=sys.stderr)
        return 1
    return 0
