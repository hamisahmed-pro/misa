# Misa compiler, runtime, and AI-native improvement report

**Updated:** 2026-10-10
**Repository:** `hamisahmed-pro/misa`, branch `main`
**Scope:** implement the prior review recommendations, validate interpreter/AOT behavior, and run fresh Misa-versus-Python benchmarks.

## Executive summary

This implementation advances performance and agent usability without changing Misa's source-level philosophy: typed, explicit operations; reliable diagnostics; and a native AOT path. It delivers:

1. **A verified scalar MIR-to-AOT bridge** for a deliberately narrow, pure straight-line Int/Float subset. LLVM emission is now driven by verified, optimized MIR for eligible functions. Unsupported constructs safely retain the established emitter.
2. **Safer typed-list fast reads** for Int/Float arithmetic contexts, while ordinary indexed expressions remain boxed. Broader benchmark testing uncovered and helped eliminate an invalid raw-value-to-pointer LLVM cast.
3. **`list.reserve(xs, capacity)`** for explicit AOT storage preallocation, with length/content preservation and interpreter-compatible validation.
4. **Agent-facing diagnostics and profiling improvements:** versioned `misa check --json` diagnostics retain legacy fields, and `misa profile --memory` reports opt-in peak interpreter heap via `tracemalloc`, explicitly not process RSS. Existing profile output continues to expose CPU hotspots.
5. **A more reproducible benchmark harness:** selectable small/default/large inputs, seeded paired random ordering, semantic preflight and repeated-output checks, percentile/variance reporting, RSS measurement without `/usr/bin/time`, and explicit skip states for source pairs that cannot be compared fairly.

The validated small-scale suite still shows Python ahead on all three comparable, correctness-passing workloads. The geometric mean is **2.0074× Misa/Python runtime** (greater than 1 means Misa is slower): n-body 2.51×, spectral norm 2.62×, fannkuch-redux 1.23×. This is evidence about three small workloads on one sandbox, not a general language ranking. There is no claim that the current changes make Misa faster than CPython.

## Implemented changes

### Compiler and runtime

- AOT keeps LLVM O2 with strict floating-point behavior and the versioned AOT cache.
- Eligible pure scalar functions are lowered through the existing AST→MIR stage, verified, optimized, and emitted as LLVM scalar instructions. Int division/modulo is intentionally excluded from this path to preserve checked runtime error behavior. Float scalar signatures receive raw-double implementations and boxed wrappers; calls use raw paths only when expression types are proven.
- The MIR integration is conservative: it does not pretend that all language constructs are in MIR. Control flow, effects, aggregates, unsupported operators, and other unsupported declarations continue using the established emitter.
- Typed `[Int]`/`[Float]` indexing can load raw scalar values when the value is immediately consumed in a proven scalar conversion/arithmetic context. Generic value expressions continue through the boxed path. A real fannkuch compilation caught an unsafe early version; the current path narrows raw loads to `_to_i64`/`_to_f64` and the normal suites were rerun afterward.
- `list.reserve([T], Int)` allocates at least the requested capacity for boxed and specialized bool/int/float AOT lists. It does not change list length or contents; the interpreter validates and treats it as a no-op because Python already over-allocates.
- Regression fixtures cover raw typed reads, scalar MIR Int/Float functions, and reserve semantics. `list.reserve` handles negative/invalid capacity with E300.

### Agent-facing development loop

- `misa check --json` exposes a versioned `diagnostics` projection while retaining the existing `errors` / `warnings` schema for compatibility with current tools.
- `misa profile --memory --json` can report peak interpreter heap and its measurement scope; it does not claim native RSS or allocation-site attribution.
- The benchmark command now accepts `--scale small|default|large`; raw JSON identifies scale, arguments, host, sample counts, timing distribution and peak RSS. `/usr/bin/time` is no longer a required system package.
- Misa source now documents the new standard-library and compiler behaviors.

## Fresh benchmark run

Headline raw data: [`results-20261010-small.json`](external/results-20261010-small.json). The run used three measured samples per implementation after warmups, randomized paired order, process-level wall-clock timing, output preflight, and repeated output validation. AOT build time is outside execution timings. `n-body` and `spectral-norm` compare their canonical final numeric output to the Python reference within an absolute tolerance of 1e-8; other supported cases require exact stdout. The wider diagnostic pass (including every invalid/mismatched outcome) is in [`results-20261010-sweep-small.json`](external/results-20261010-sweep-small.json); it predates the definitive three-case sample and is not used for the headline score.

| Workload | Input | Misa median | Python median | Misa/Python | Peak RSS Misa / Python |
|---|---:|---:|---:|---:|---:|
| n-body | 5,000 | 0.101 s | 0.040 s | 2.51× slower | 80,204 / 11,496 KiB |
| spectral norm | 100 | 0.196 s | 0.075 s | 2.62× slower | 207,812 / 11,528 KiB |
| fannkuch-redux | 8 | 0.054 s | 0.044 s | 1.23× slower | 11,720 / 11,524 KiB |
| **Geometric mean (3 valid cases)** | — | — | — | **2.0074× slower** | — |

Warmups and runs are small-scale smoke/representative workloads. These sizes are intentionally not directly merged with the prior default-size results in `results-20261009.json`. The sample is too small and host-specific for cross-machine conclusions. The memory figures are peak child RSS from `resource.getrusage` on Linux; RSS is not directly comparable to `tracemalloc` heap peaks from `misa profile --memory`.

### Cases deliberately not scored

The runner records, rather than conceals, non-comparable or invalid cases:

- **Mandelbrot and FASTA:** the current Misa ports do not produce the same full output/algorithmic result as their selected Python reference programs; the harness reports checksum mismatch and does not time-score them.
- **Binary trees:** the checked-in Misa program currently fails native compilation (`i64`/`i8**` store mismatch) and is not scored.
- **Reverse-complement, knucleotide, regex-redux:** the Python references require stdin fixtures, while the current Misa ports do not consume equivalent inputs. They are skipped to avoid hanging or comparing different work.
- **pidigits:** remains excluded in the existing suite pending repair of the Misa port.

These failures are meaningful follow-up work; they are not benchmark wins or silently filtered data. The benchmark sweep also exposed the typed-read codegen regression, which was fixed before producing the final passing three-workload results.

## Validation

- `tests/test_benchmark.py`: **PASS**, including semantic N-body comparison and exact-output rejection checks.
- `tests/run_tests.py`: **477 passed, 0 failed**.
- `tests/aot/run_aot_tests.py`: **37/40 passed**. The three failures are database AOT cases that cannot build because the optional SQLite amalgamation is absent at `~/workspace/deps/sqlite/`. Typed list reads, scalar MIR, and reserve fixtures pass.
- Fresh benchmark suite: three comparable workload cases pass correctness and all timed repeated-output checks; mismatched stdin cases are explicitly skipped and known broken/unequal source pairs are not scored.
- `git diff --check` and Python syntax checks are required before commit/push.

## Review: what is and is not solved

This is an incremental production bridge, **not a complete MIR compiler**. The first MIR slice is limited to scalar, straight-line numeric functions. It provides a real backend path without destabilizing the much larger existing language surface, and establishes a safe expansion gate: unsupported functions must fall back explicitly. Float specializations and typed indexing also need broader generated/property-based parity coverage before extending their eligibility further.

Misa remains slower on the validated numeric cases, and spectral norm has a particularly high memory peak in AOT. LLVM O2 alone cannot erase tagged-value traffic, indirect calls, allocation costs, or runtime checks. The next performance boundary is a coherent typed IR plus workload-informed allocation/representation work, not more optimizer flags.

## Prioritized next recommendations

1. **Complete MIR coverage in explicit increments:** add CFG/basic blocks, branches/loops, checked arithmetic and effect annotations; use a verifier and differential tests at every stage. Expand the production bridge only when semantics and error propagation can be preserved.
2. **Investigate memory and allocation behavior:** profile peak RSS and allocation counts by runtime subsystem; reduce temporary boxed values and validate spectral-norm allocation hotspots. Keep interpreter heap measurements distinct from process RSS.
3. **Fix benchmark-suite validity:** repair and parity-test Mandelbrot, FASTA, and binary trees; add deterministic shared stdin fixture plumbing for the three stdin workloads; repair pidigits. Make only validated cases contribute to aggregate scores.
4. **Expand compiler correctness tests:** generate typed-list aliasing, out-of-bounds, mixed generic use and scalar-call combinations; add float NaN/Inf/comparison tests and optimizer differential fuzzing.
5. **Improve agent diagnostics and repairs:** add source spans/fix-its where trustworthy, stable schema versioning, and protocol tests across checker/CLI changes. Suggestions must remain optional and machine-readable.
6. **Scale benchmarks only after correctness:** retain paired repetitions, exact raw data, variance, memory, startup and compile costs as separate measures. Use at least 10–20 trials for claims and compare on multiple hosts before asserting general speedups.

## Reproduction

```sh
PYTHONPATH=impl python3 tests/test_benchmark.py
PYTHONPATH=impl python3 tests/run_tests.py
PYTHONPATH=impl python3 tests/aot/run_aot_tests.py
PYTHONPATH=impl impl/bin/misa benchmark --runs 3 --scale small \
  -o /tmp/misa-benchmark-small.json
```
