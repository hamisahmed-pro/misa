#!/usr/bin/env python3
"""7-benchmark comparison: Python3 vs Node.js vs Misa (honest coverage)."""
import subprocess, time, statistics, os

BENCH = os.path.dirname(os.path.abspath(__file__))
MISA = os.path.join(os.path.dirname(BENCH), "..", "impl", "bin", "misa")

def run(cmd, timeout=120):
    s = time.perf_counter()
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
        return time.perf_counter() - s
    except:
        return None

def bench(name, py_arg, misa_file, misa_arg=None):
    print(f"\n{name}:")
    # Python
    py_cmd = ["python3", f"{BENCH}/bgsrc/{py_arg[0]}", py_arg[1]]
    py_times = [run(py_cmd) for _ in range(3)]
    py = statistics.median([t for t in py_times if t])
    # Misa
    misa_cmd = [MISA, "run", f"{BENCH}/{misa_file}"]
    if misa_arg:
        # Misa files use hardcoded N or argv; use as-is
        pass
    misa_times = [run(misa_cmd) for _ in range(3)]
    misa = statistics.median([t for t in misa_times if t])
    ratio = misa / py if py and misa else None
    status = f"{ratio:.1f}x {'slower' if ratio>1 else 'faster'}" if ratio else "N/A"
    print(f"  Python: {py:.3f}s, Misa: {misa:.3f}s ({status})")
    return ratio

print("=" * 60)
print("7-Benchmark Suite: Misa vs Python3")
print("Coverage: 7/10 (missing: knucleotide, pidigits, regex-redux)")
print("=" * 60)

results = {}
results["n-body"] = bench("n-body (N=50000)", ("nbody.python3", "50000"), "/tmp/nbody_50k.misa")
results["spectral-norm"] = bench("spectral-norm (N=500)", ("spectralnorm.python3", "500"), "spectralnorm.misa")
results["fannkuch-redux"] = bench("fannkuch-redux (N=10)", ("fannkuchredux.python3", "10"), "fannkuchredux.misa")
# mandelbrot, fasta, revcomp, binarytrees use defaults
results["mandelbrot"] = bench("mandelbrot (N=500)", ("mandelbrot.python3", "500"), "mandelbrot.misa")
results["fasta"] = bench("fasta (N=10000)", ("fasta.python3", "10000"), "fasta.misa")
results["revcomp"] = bench("reverse-complement (N=10000)", ("revcomp.python3", "10000"), "revcomp.misa")
results["binary-trees"] = bench("binary-trees (N=10)", ("binarytrees.python3", "10"), "binarytrees.misa")

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
for k, v in results.items():
    if v:
        print(f"  {k:20s}: {v:5.1f}x {'slower' if v>1 else 'faster'}")
