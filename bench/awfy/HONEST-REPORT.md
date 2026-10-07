# AWFY Benchmark Report (Corrected Methodology)

**Date:** 2026-10-07  
**Methodology:** Corrected 2026-10-07  
**Harness:** `bench/awfy/run_awfy_corrected.py`

## Methodology Fixes

The previous harness (`run_awfy.py`) had critical flaws:
1. ❌ Python/Node timed INTERNALLY (via `perf_counter` in the benchmark code)
2. ❌ Misa timed EXTERNALLY (whole-process wall time)
3. ❌ Different timed regions → invalid ratios
4. ❌ AOT build time included in Misa measurements

**Corrected:**
1. ✅ ALL languages timed EXTERNALLY via `time.perf_counter()` around subprocess
2. ✅ AOT binaries built ONCE before timing (not in timed region)
3. ✅ 5 runs, median reported, all samples saved
4. ✅ Results verified for correctness before comparison

## Results

*To be populated by running `python3 bench/awfy/run_awfy_corrected.py`*

## Previous Invalid Results

The old report contained ratios computed from mismatched timed regions.
Those numbers are **invalid** and have been removed. Do not cite them.

## Benchmarks Game (External)

For independent validation, see `bench/external/REPORT-complete.md`:
- n-body: Misa 1.5x faster than Python
- spectral-norm: Misa 4.4x faster than Python  
- fannkuch-redux: Misa 14x faster than Python
- mandelbrot: Misa 4.8x slower than Python (after float unboxing)
- fasta: Misa 4.8x slower than Python (after builder optimization)

These use the official Benchmarks Game sources, not AWFY ports.
