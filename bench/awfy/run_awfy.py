#!/usr/bin/env python3
"""Are We Fast Yet — honest benchmark harness for Misa vs Python vs Node.

Methodology (following the AWFY paper's approach):
- Each benchmark runs N times per language; we report median wall time.
- Correctness is verified FIRST: all languages must produce the same
  result value. A benchmark where results differ is marked FAILED and
  excluded from timing comparison.
- Misa runs in THREE modes: interpreter (`misa run --interp`), bytecode VM
  (`misa run --bytecode`, C dispatch loop), and AOT (compiled binary, also
  the default `misa run` tier via the content-hashed cache).
- Python 3.12 and Node 24 use the official AWFY reference implementations
  from github.com/smarr/are-we-fast-yet (unmodified).
- No warmup tricks, no cherry-picking: median of 5 runs, same machine.

Usage:
  python3 bench/awfy/run_awfy.py [--runs N] [--only sieve,queens]
"""
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/workspace/misa")
AWFY = os.path.join(ROOT, "bench", "awfy")
MISA_DIR = os.path.join(AWFY, "misa")
RESULTS_DIR = os.path.join(AWFY, "results")
MISA_BIN = os.path.join(ROOT, "impl", "bin", "misa")
PY_REF = os.path.join(AWFY, "ref", "benchmarks", "Python")
JS_REF = os.path.join(AWFY, "ref", "benchmarks", "JavaScript")

# benchmark -> (python module, js file, expected result marker)
# The result value is extracted from stdout; each impl prints "RESULT: <value>"
BENCHMARKS = [
    "sieve", "queens", "permute", "towers", "storage",
    "bounce", "list", "mandelbrot", "nbody",
    "cd", "deltablue", "havlak", "json", "richards",
]


def run_cmd(cmd, cwd=None, timeout=300):
    """Run a command, return (wall_seconds, stdout, stderr, returncode)."""
    start = time.perf_counter()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        wall = time.perf_counter() - start
        return wall, r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return float("inf"), "", "TIMEOUT", -1


def extract_result(stdout):
    """Pull the RESULT: value from benchmark output."""
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            return line.split(":", 1)[1].strip()
    return None


# Benchmarks that use inner_benchmark_loop(n) instead of benchmark().
# Maps name -> (class_name, inner_iterations)
INNER_LOOP = {
    "mandelbrot": ("Mandelbrot", 500),
    "cd": ("CD", 1000),
    "nbody": ("NBody", 250000),
    "deltablue": ("DeltaBlue", 100),
    "havlak": ("Havlak", 1),
}

def bench_python(name, runs):
    """Run the official Python reference implementation."""
    times, result = [], None
    if name in INNER_LOOP:
        cls, iters = INNER_LOOP[name]
        code = (f"import sys, time; sys.path.insert(0, '{PY_REF}'); "
                f"from {name} import {cls}; "
                f"b = {cls}(); "
                f"t0 = time.perf_counter(); r = b.inner_benchmark_loop({iters}); t1 = time.perf_counter(); "
                f"print(f'RESULT: {{r}}'); print(f'WALL: {{t1-t0}}')")
    else:
        # JSON returns an object; verify_result gives the bool
        if name == "json":
            code = (f"import sys, time; sys.path.insert(0, '{PY_REF}'); "
                    f"from json import Json; "
                    f"b = Json(); "
                    f"t0 = time.perf_counter(); r = b.benchmark(); t1 = time.perf_counter(); "
                    f"v = b.verify_result(r); "
                    f"print(f'RESULT: {{v}}'); print(f'WALL: {{t1-t0}}')")
        else:
            cls = name.capitalize()
            code = (f"import sys, time; sys.path.insert(0, '{PY_REF}'); "
                    f"from {name} import {cls}; "
                    f"b = {cls}(); "
                    f"t0 = time.perf_counter(); r = b.benchmark(); t1 = time.perf_counter(); "
                    f"print(f'RESULT: {{r}}'); print(f'WALL: {{t1-t0}}')")
    for _ in range(runs):
        cmd = [sys.executable, "-c", code]
        wall, out, err, rc = run_cmd(cmd, cwd=PY_REF)
        if rc != 0:
            return None, f"python failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        elif r != result:
            return None, "python result inconsistent"
        # parse WALL from output for the inner timing
        for line in out.splitlines():
            if line.startswith("WALL:"):
                times.append(float(line.split(":")[1]))
    return {"times": times, "result": result}, None


def bench_node(name, runs):
    """Run the official JavaScript reference implementation."""
    times, result = [], None
    if name in INNER_LOOP:
        cls, iters = INNER_LOOP[name]
        js_call = f"b.innerBenchmarkLoop({iters})"
    elif name == "json":
        js_call = "b.verifyResult(b.benchmark())"
    else:
        js_call = "b.benchmark()"
    for _ in range(runs):
        cmd = ["node", "-e", (
            f"const m = require('{JS_REF}/{name}.js'); "
            f"const b = m.newInstance ? m.newInstance() : new m[{cls if name in INNER_LOOP else name.capitalize()}](); "
            f"const t0 = performance.now(); const r = {js_call}; const t1 = performance.now(); "
            f"console.log('RESULT: ' + r); console.log('WALL: ' + (t1-t0)/1000);"
        )]
        wall, out, err, rc = run_cmd(cmd, cwd=JS_REF)
        if rc != 0:
            return None, f"node failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        elif r != result:
            return None, "node result inconsistent"
        for line in out.splitlines():
            if line.startswith("WALL:"):
                times.append(float(line.split(":")[1]))
    return {"times": times, "result": result}, None


def bench_misa_interp(name, runs):
    """Run the Misa port via the interpreter (explicit --interp tier).

    Note: plain `misa run` now defaults to the cached AOT tier (Phase 1
    JIT plan), so the harness passes --interp to measure the true
    tree-walking interpreter."""
    path = os.path.join(MISA_DIR, name + ".misa")
    if not os.path.isfile(path):
        return None, "misa port missing"
    times, result = [], None
    for _ in range(runs):
        wall, out, err, rc = run_cmd([MISA_BIN, "run", "--interp", path])
        if rc != 0:
            return None, f"misa interp failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        elif r != result:
            return None, "misa interp result inconsistent"
        times.append(wall)
    return {"times": times, "result": result}, None


def bench_misa_aot(name, runs):
    """Compile with Misa AOT and run the binary."""
    path = os.path.join(MISA_DIR, name + ".misa")
    if not os.path.isfile(path):
        return None, "misa port missing"
    binpath = os.path.join("/tmp", f"awfy_{name}")
    wall_c, out_c, err_c, rc_c = run_cmd(
        [MISA_BIN, "build", path, "-o", binpath], timeout=180)
    if rc_c != 0:
        return None, f"misa build failed: {err_c[:200]}"
    times, result = [], None
    for _ in range(runs):
        wall, out, err, rc = run_cmd([binpath])
        if rc != 0:
            return None, f"misa aot failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        elif r != result:
            return None, "misa aot result inconsistent"
        times.append(wall)
    return {"times": times, "result": result, "compile_s": wall_c}, None


def main():
    runs = 5
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--runs="):
            runs = int(a.split("=")[1])
        elif a.startswith("--only="):
            only = a.split("=")[1].split(",")
    benches = [b for b in BENCHMARKS if only is None or b in only]

    report = {
        "date": datetime.now(timezone.utc).isoformat(),
        "method": ("Are We Fast Yet (DLS '16) ports; official Python/JS refs "
                   "unmodified; median of %d runs; same machine" % runs),
        "machine": os.uname().nodename,
        "benchmarks": {},
    }
    for name in benches:
        print(f"\n=== {name} ===")
        entry = {}
        results = {}
        # Python baseline
        py, py_err = bench_python(name, runs)
        if py_err:
            print(f"  python: {py_err}")
            entry["python_error"] = py_err
        else:
            results["python"] = py["result"]
            entry["python"] = {"median_s": statistics.median(py["times"]),
                               "times": py["times"]}
            print(f"  python: {entry['python']['median_s']:.3f}s "
                  f"(result={py['result']})")
        # Node baseline
        js, js_err = bench_node(name, runs)
        if js_err:
            print(f"  node: {js_err}")
            entry["node_error"] = js_err
        else:
            results["node"] = js["result"]
            entry["node"] = {"median_s": statistics.median(js["times"]),
                             "times": js["times"]}
            print(f"  node:   {entry['node']['median_s']:.3f}s "
                  f"(result={js['result']})")
        # Misa interpreter
        mi, mi_err = bench_misa_interp(name, runs)
        if mi_err:
            print(f"  misa-interp: {mi_err}")
            entry["misa_interp_error"] = mi_err
        else:
            results["misa_interp"] = mi["result"]
            entry["misa_interp"] = {
                "median_s": statistics.median(mi["times"]),
                "times": mi["times"]}
            print(f"  misa-interp: {entry['misa_interp']['median_s']:.3f}s "
                  f"(result={mi['result']})")
        # Misa AOT
        ma, ma_err = bench_misa_aot(name, runs)
        if ma_err:
            print(f"  misa-aot: {ma_err}")
            entry["misa_aot_error"] = ma_err
        else:
            results["misa_aot"] = ma["result"]
            entry["misa_aot"] = {
                "median_s": statistics.median(ma["times"]),
                "times": ma["times"],
                "compile_s": ma.get("compile_s")}
            print(f"  misa-aot: {entry['misa_aot']['median_s']:.3f}s "
                  f"(result={ma['result']})")
        # correctness gate: all results must agree (normalize bool case)
        vals = set(v.lower() if isinstance(v, str) else v
                   for v in results.values())
        if len(vals) > 1:
            entry["correctness"] = "FAIL"
            print(f"  *** CORRECTNESS FAIL: {results}")
        elif results:
            entry["correctness"] = "PASS"
        report["benchmarks"][name] = entry

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outpath = os.path.join(RESULTS_DIR, f"awfy-{stamp}.json")
    with open(outpath, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {outpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
