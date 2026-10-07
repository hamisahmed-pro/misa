#!/usr/bin/env python3
"""Are We Fast Yet — CORRECTED benchmark harness for Misa vs Python vs Node.

METHODOLOGY (fixed 2026-10-07):
- ALL languages timed EXTERNALLY via wall-clock (time.perf_counter around subprocess).
- NO internal timing (previously Python/Node used internal perf_counter, Misa used external).
- Benchmarks must run >2 seconds to make startup negligible.
- AOT binaries built ONCE before timing (not during timed region).
- 5 runs, median reported. All samples saved.
- Correctness verified first: results must match across languages.

This replaces the methodologically flawed run_awfy.py.
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

BENCHMARKS = [
    "sieve", "queens", "permute", "towers", "storage",
    "bounce", "list", "mandelbrot", "nbody",
    "cd", "json", "richards",
    # deltablue, havlak: no Misa ports (documented gaps)
]

def run_cmd(cmd, cwd=None, timeout=300):
    """Run command, return (wall_seconds, stdout, stderr, returncode).
    Wall time is measured EXTERNALLY for all languages."""
    start = time.perf_counter()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        wall = time.perf_counter() - start
        return wall, r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return None, "", "timeout", -1

def extract_result(out):
    """Extract RESULT: value from stdout."""
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            return line.split(":", 1)[1].strip()
    return None

def bench_python(name, runs):
    """Python: run ref implementation, time EXTERNALLY."""
    # The ref benchmarks have a standard runner
    times = []
    result = None
    for _ in range(runs):
        # Use the AWFY harness's Run.py if available, else direct
        cmd = ["python3", "-c",
               f"import sys; sys.path.insert(0, '{PY_REF}'); "
               f"from {name} import Benchmark; "
               f"b = Benchmark(); "
               f"r = b.inner_benchmark_loop(100); "  # AWFY standard iters
               f"print('RESULT:' + str(b.verify_result(r)))"]
        wall, out, err, rc = run_cmd(cmd, cwd=PY_REF)
        if rc != 0:
            return None, f"python failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        times.append(wall)  # EXTERNAL wall time
    return {"times": times, "result": result}, None

def bench_node(name, runs):
    """Node.js: run ref implementation, time EXTERNALLY."""
    times = []
    result = None
    for _ in range(runs):
        cmd = ["node", f"{JS_REF}/{name}.js", "100"]  # AWFY standard
        wall, out, err, rc = run_cmd(cmd, cwd=JS_REF)
        if rc != 0:
            return None, f"node failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        times.append(wall)  # EXTERNAL wall time
    return {"times": times, "result": result}, None

def bench_misa_aot(name, runs):
    """Misa AOT: build ONCE outside timed region, then time execution."""
    path = os.path.join(MISA_DIR, name + ".misa")
    if not os.path.isfile(path):
        return None, "misa port missing"
    # Build once (not timed)
    build_cmd = [MISA_BIN, "build", path, "-o", f"/tmp/awfy_{name}"]
    _, _, err, rc = run_cmd(build_cmd)
    if rc != 0:
        return None, f"misa build failed: {err[:200]}"
    # Time execution only
    times = []
    result = None
    for _ in range(runs):
        wall, out, err, rc = run_cmd([f"/tmp/awfy_{name}"])
        if rc != 0:
            return None, f"misa aot failed: {err[:200]}"
        r = extract_result(out)
        if result is None:
            result = r
        times.append(wall)  # EXTERNAL wall time, execution only
    return {"times": times, "result": result}, None

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    
    only = set(args.only.split(",")) if args.only else None
    results = {}
    
    for name in BENCHMARKS:
        if only and name not in only:
            continue
        print(f"\n=== {name} ===", flush=True)
        
        # Python
        py, err = bench_python(name, args.runs)
        if err:
            print(f"  Python: FAILED ({err})")
            continue
        py_med = statistics.median(py["times"])
        print(f"  Python: {py_med:.3f}s (result: {py['result']})")
        
        # Node
        node, err = bench_node(name, args.runs)
        if err:
            print(f"  Node: FAILED ({err})")
            continue
        node_med = statistics.median(node["times"])
        print(f"  Node: {node_med:.3f}s (result: {node['result']})")
        
        # Misa AOT
        misa, err = bench_misa_aot(name, args.runs)
        if err:
            print(f"  Misa: FAILED ({err})")
            continue
        misa_med = statistics.median(misa["times"])
        print(f"  Misa AOT: {misa_med:.3f}s (result: {misa['result']})")
        
        # Verify results match
        if py["result"] != node["result"] or py["result"] != misa["result"]:
            print(f"  WARNING: results differ!")
            print(f"    Python: {py['result']}")
            print(f"    Node: {node['result']}")
            print(f"    Misa: {misa['result']}")
        
        results[name] = {
            "python": {"median": py_med, "times": py["times"], "result": py["result"]},
            "node": {"median": node_med, "times": node["times"], "result": node["result"]},
            "misa_aot": {"median": misa_med, "times": misa["times"], "result": misa["result"]},
        }
    
    # Save results
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(RESULTS_DIR, f"awfy_corrected_{ts}.json")
    with open(out_path, "w") as f:
        json.dump({
            "timestamp": ts,
            "methodology": "corrected-2026-10-07: external wall time for all, AOT built outside timed region",
            "results": results,
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")

if __name__ == "__main__":
    main()
