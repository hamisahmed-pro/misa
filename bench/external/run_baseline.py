#!/usr/bin/env python3
"""Full Benchmarks Game suite: Python3 vs Node.js baseline (10 benchmarks)."""
import subprocess, time, statistics, os

BENCH = os.path.dirname(os.path.abspath(__file__))

def run(cmd, timeout=300):
    s = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return time.perf_counter() - s, True
    except subprocess.TimeoutExpired:
        return timeout, False

# Benchmark configs: (name, python_file, node_file, arg)
# Smaller args for quick completion
configs = [
    ("n-body", "bgsrc/nbody.python3", "bgsrc/nbody.node.js", "20000"),
    ("spectral-norm", "bgsrc/spectralnorm.python3", "bgsrc/spectralnorm.node.js", "200"),
    ("fannkuch-redux", "bgsrc/fannkuchredux.python3", None, "9"),
    ("mandelbrot", "bgsrc/mandelbrot.python3", "bgsrc/mandelbrot.node.js", "500"),
    ("fasta", "bgsrc/fasta.python3", "bgsrc/fasta.node.js", "10000"),
    ("knucleotide", "bgsrc/knucleotide.python3", "bgsrc/knucleotide.node.js", "10000"),
    ("pidigits", "bgsrc/pidigits.python3", "bgsrc/pidigits.node.js", "500"),
    ("regex-redux", "bgsrc/regexredux.python3", "bgsrc/regexredux.node.js", "10000"),
    ("reverse-complement", "bgsrc/revcomp.python3", "bgsrc/revcomp.node.js", "10000"),
    ("binary-trees", "bgsrc/binarytrees.python3", "bgsrc/binarytrees.node.js", "10"),
]

print(f"{'Benchmark':<20} | {'Python3':>10} | {'Node.js':>10}")
print("-" * 50)
for name, py, node, arg in configs:
    py_cmd = ["python3", f"{BENCH}/{py}", arg]
    t_py, ok_py = run(py_cmd)
    py_str = f"{t_py:.2f}s" if ok_py else "timeout"
    
    if node:
        node_cmd = ["node", f"{BENCH}/{node}", arg]
        t_node, ok_node = run(node_cmd)
        node_str = f"{t_node:.2f}s" if ok_node else "timeout"
    else:
        node_str = "skip"
    
    print(f"{name:<20} | {py_str:>10} | {node_str:>10}")
