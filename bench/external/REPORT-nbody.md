# External Benchmark Report: Benchmarks Game n-body

**Date:** 2026-10-07  
**Source:** The Computer Language Benchmarks Game (benchmarksgame-team)  
**URL:** https://benchmarksgame-team.pages.debian.net/benchmarksgame/  
**Method:** Official Python3 and Node.js implementations vs Misa port

## Methodology

- **Benchmark:** n-body (N-body simulation)
- **Input:** N=50,000 iterations
- **Runs:** 3 runs each, median reported
- **Timing:** External wall-clock via Python `time.perf_counter()`
- **Misa:** AOT-compiled via `misa run` (cached binary)
- **Python:** Official `nbody.python3` from Benchmarks Game
- **Node:** Official `nbody.node-8.node` from Benchmarks Game

## Results

| Implementation | Median Time | vs Python3 | vs Node.js |
|----------------|-------------|------------|------------|
| Python3 (official) | 0.338s | 1.00x | 2.75x slower |
| Node.js (official) | 0.123s | 0.36x | 1.00x |
| Misa AOT | 2.097s | 6.21x slower | 17.11x slower |

## Analysis

Misa is **6.2x slower than Python3** and **17.1x slower than Node.js** on the n-body benchmark.

### Why?

The n-body benchmark is:
1. **Floating-point heavy** — Misa's Float operations go through generic MVal* dispatch
2. **Object/record heavy** — Body records with field access
3. **No integer hot loops** — The JIT's int specialization doesn't apply

The Misa port is derived from the AWFY nbody (not a direct port of the Benchmarks Game Python), but does equivalent work: N iterations of the O(n²) force calculation.

### Honest Assessment

This is a **legitimate performance gap**. The Misa AOT compiler generates generic code that:
- Boxes/unboxes floats for every operation
- Does dynamic dispatch for record field access
- Cannot optimize the tight inner loop

The JIT's int specialization (which achieves 0.007s on integer loops) does not help here because n-body is fundamentally floating-point.

## What This Means

- Misa's **integer performance** is excellent (JIT: ~3x faster than CPython on int loops)
- Misa's **floating-point performance** needs work (6x slower than Python on n-body)
- The gap is in the AOT codegen, not the language design

## Next Steps

To close this gap:
1. Float specialization in the JIT (like int specialization)
2. Unboxed float representation in AOT
3. Record field access optimization
4. SIMD for the inner loop (long-term)

## Reproducibility

```bash
# Python3 (official)
python3 bench/external/bgsrc/nbody.python3 50000

# Node.js (official)  
node bench/external/bgsrc/nbody.node.js 50000

# Misa (AWFY-derived port, N=50000 hardcoded)
impl/bin/misa run /tmp/nbody_50k.misa
```

All source files from the Benchmarks Game are in `bench/external/bgsrc/`.
