#!/usr/bin/env python3
"""Extensive Benchmarks Game suite: n-body, spectral-norm, fannkuch-redux, mandelbrot."""
import subprocess, time, statistics, os

BENCH = os.path.dirname(os.path.abspath(__file__))
MISA = os.path.join(os.path.dirname(BENCH), "..", "impl", "bin", "misa")

def run(cmd, timeout=300):
    s = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return time.perf_counter() - s

def bench(name, py_cmd, node_cmd, misa_cmd, n, runs=3):
    print(f"\n{name} (N={n})")
    print("-" * 50)
    py = statistics.median([run(py_cmd + [str(n)]) for _ in range(runs)])
    node = statistics.median([run(node_cmd + [str(n)]) for _ in range(runs)])
    misa = statistics.median([run(misa_cmd) for _ in range(runs)])
    print(f"  Python3: {py:.3f}s")
    print(f"  Node.js: {node:.3f}s")
    print(f"  Misa:    {misa:.3f}s")
    print(f"  Misa/Python: {misa/py:.1f}x {'slower' if misa>py else 'faster'}")
    return {"python": py, "node": node, "misa": misa}

results = {}
# n-body: N=50000
results["nbody"] = bench(
    "n-body",
    ["python3", f"{BENCH}/bgsrc/nbody.python3"],
    ["node", f"{BENCH}/bgsrc/nbody.node.js"],
    [MISA, "run", "/tmp/nbody_50k.misa"],
    50000
)
# spectral-norm: N=500
results["spectralnorm"] = bench(
    "spectral-norm",
    ["python3", f"{BENCH}/bgsrc/spectralnorm.python3"],
    ["node", f"{BENCH}/bgsrc/spectralnorm.node.js"],
    [MISA, "run", f"{BENCH}/spectralnorm.misa"],
    500
)
# fannkuch-redux: N=10
results["fannkuch"] = bench(
    "fannkuch-redux",
    ["python3", f"{BENCH}/bgsrc/fannkuchredux.python3"],
    ["node", f"{BENCH}/bgsrc/fannkuchredux.node.js"],
    [MISA, "run", f"{BENCH}/fannkuchredux.misa"],
    10
)

print("\n" + "="*50)
print("SUMMARY (Misa vs Python3)")
print("="*50)
for k, v in results.items():
    r = v["misa"] / v["python"]
    print(f"  {k:15s}: {r:5.1f}x {'slower' if r>1 else 'faster'}")
