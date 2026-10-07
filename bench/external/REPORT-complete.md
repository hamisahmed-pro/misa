# External Benchmark Report: Complete Benchmarks Game Suite

**Date:** 2026-10-07  
**Source:** The Computer Language Benchmarks Game (benchmarksgame-team)  
**URL:** https://benchmarksgame-team.pages.debian.net/benchmarksgame/

## Methodology

- **Benchmarks:** n-body (N=50000), spectral-norm (N=500), fannkuch-redux (N=10)
- **Runs:** 3 runs each, median reported
- **Timing:** External wall-clock
- **Misa:** AOT-compiled via `misa run` (cached binary)
- **Python:** Official Benchmarks Game implementations
- **Node:** Official Benchmarks Game implementations

## Results

| Benchmark | Python3 | Node.js | Misa AOT | Misa vs Python |
|-----------|---------|---------|----------|----------------|
| n-body | 0.337s | 0.138s | **0.185s** | **1.8x faster** |
| spectral-norm | 2.714s | 0.116s | **0.475s** | **5.7x faster** |
| fannkuch-redux | 5.570s | 0.084s | **0.373s** | **14.9x faster** |

## Critical Codegen Evaluation

### Initial Finding (Before Optimization)

The first benchmark run showed:
- n-body: Misa **6.1x slower** than Python
- spectral-norm: Misa 5.4x faster
- fannkuch-redux: Misa 14x faster

**The anomaly:** n-body was slow while the others were fast. All three are floating-point/array workloads. The difference: n-body uses **records** (`Body` type with `.x`, `.y` field access), while the others use plain arrays.

### Root Cause

Every record field access (`bs[i].x`) compiled to:
```c
misa_record_get(obj, "x", line, col)
```

Which does a **linear search with strcmp**:
```c
for (i = 0; i < obj->u.record.len; i++) {
    if (strcmp(obj->u.record.names[i], field) == 0)
        return obj->u.record.vals[i];
}
```

For n-body with 5 bodies × 7 fields × 50,000 iterations × O(n²) pairs:
- ~1,000,000 field accesses
- Each does up to 7 `strcmp` calls
- **~7,000,000 string comparisons**

### The Fix

**1. Runtime:** Added `misa_record_get_idx(MVal *obj, int64_t idx, ...)` 
- Direct array index access, no string comparison
- O(1) instead of O(n) with strcmp

**2. AOT Emitter:** Static field index resolution
- Track variable types in `FnCtx.var_types`
- For `var[i].field` where `var: [RecordType]`, resolve field index from `TypeDecl`
- For `var.field` where `var: RecordType`, same
- Emit `misa_record_get_idx` instead of `misa_record_get`

**3. Type propagation:**
- Function params: `bodies: [Body]` → tracked
- Let/var: `var bs = bodies` → propagate type from RHS
- `let bi = bodies[i]` → infer element type `Body` from `[Body]`

### Result

n-body: **6.1x slower → 1.8x faster** (11x improvement!)

## What This Proves

1. **Misa's AOT codegen is competitive** — faster than CPython on all three benchmarks
2. **The bottleneck was algorithmic** (string search), not fundamental
3. **Static type information enables powerful optimizations** — the emitter already had the `TypeDecl`, it just wasn't using it for field access

## Remaining Work

The deeper fix (storing records as native LLVM structs) would eliminate the function call overhead entirely. But the current 11x improvement shows the value of:
- Using static type info that's already available
- Avoiding O(n) algorithms in hot paths
- Measuring with real benchmarks, not microbenchmarks

## Reproducibility

```bash
# Official sources in bench/external/bgsrc/
python3 bench/external/bgsrc/nbody.python3 50000
node bench/external/bgsrc/nbody.node.js 50000
impl/bin/misa run /tmp/nbody_50k.misa

# Full suite
python3 bench/external/run_extensive.py
```
