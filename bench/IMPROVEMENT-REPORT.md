# Misa compiler, runtime, and AI-native improvement report

**Updated:** 2026-10-10
**Repository:** [`hamisahmed-pro/misa`](https://github.com/hamisahmed-pro/misa), branch `main`
**Implementation commit:** `51cc5f0` (follow-up build-artifact hygiene: `c20fcb4`)
**Scope:** implement the supplied technical review, validate interpreter/AOT behavior, and compare Misa AOT against the checked-in CPython references.

## Executive summary

The work improves Misa without changing its AI-native philosophy: keep operations and failures explicit, expose stable machine-readable feedback, and make optimized execution safe rather than silently guessing. It adds a deliberately narrow, verifier-gated MIR-to-LLVM path, useful amortized list APIs and typed-list reads, an agent-oriented check/profile surface, and a correctness-first benchmark harness.

The final clean-checkout small benchmark used **10 measured samples per implementation** after warmups, with randomized paired execution order and semantic output checks. All three scored programs passed. Misa AOT's geometric-mean median runtime was **1.5287× CPython's** (about **52.9% more elapsed time**); it was slower in every measured case. This is a measured baseline, not a claim that the work makes Misa faster than Python.

A separate same-source allocator A/B on spectral norm (`N=100`) resolved the apparent extreme RSS anomaly: with Boehm GC, Misa peak RSS fell from **207,812 KiB to 11,540 KiB** (**94.45% lower, 18.01× smaller**) and median time improved from **0.173 s to 0.113 s** (**34.7% faster**). The paired A/B has three samples per mode and is diagnostic; its benchmark metadata truthfully records that generated build artifacts made the source tree appear dirty. The later definitive three-workload result is from a clean pushed revision and reports the collector/package explicitly.

## Scorecard

Subjective engineering score for **this implementation against the stated goals**, not a ranking of the whole language or ecosystem. Weights emphasize measured performance and correctness rather than feature count.

| Dimension | Score | Weight | Review |
|---|---:|---:|---|
| Measured performance vs CPython | 6.0/10 | 25% | Misa remains slower on all three validated small numeric cases; allocator mode materially changes both RSS and time. |
| Compiler/runtime correctness | 7.5/10 | 20% | Fail-closed verifier and parity gates are strong; MIR coverage remains intentionally narrow and AOT has known unsupported benchmark ports. |
| AI-native diagnostics/tooling | 8.5/10 | 20% | Versioned JSON diagnostics, optional memory profiling, structured benchmark provenance, and a more reliable feedback loop. |
| Benchmark reproducibility | 9.0/10 | 20% | Ten paired randomized samples, correctness gates, source/toolchain/GC provenance, variance and RSS; still one host and small workloads. |
| Maturity and portability | 6.0/10 | 15% | Cross-platform work continues; Boehm GC is an optional host dependency and non-GC fallback has an important memory limitation. |
| **Weighted implementation score** | **7.4/10** | **100%** | Strong, carefully bounded engineering progress; not yet evidence of Python-beating performance or production-wide maturity. |

## Implemented

### Compiler and runtime

- Added a **verified scalar MIR-to-AOT bridge** for pure, straight-line Int/Float functions. Eligible functions lower through AST→MIR, verification, optimization, and LLVM emission; unsupported functions fail closed to the established emitter. This is a real but deliberately limited MIR backend, not full MIR coverage.
- Kept LLVM **O2** with strict floating-point semantics (fast-math disabled). Checked/error-sensitive integer operations remain outside the specialized slice.
- Added **typed-list fast reads** in proven Int/Float arithmetic contexts; generic indexed expressions retain the boxed path.
- Added **`list.push(xs, value)`** with amortized O(1) append and **`list.reserve(xs, capacity)`** for capacity preallocation in interpreter/AOT-compatible APIs. The AOT runtime supports specialized bool/int/float list storage as well as boxed lists.
- Extended MIR verifier/optimizer contracts and tests, including conservative handling of unsupported effects/operations.

### Agent-facing development loop

- `misa check --json` now includes versioned `schema_version: "misa.check.v1"` normalized diagnostics while retaining the legacy `errors`/`warnings` fields.
- `misa profile --memory` reports opt-in peak **interpreter Python heap** via `tracemalloc`; it explicitly does not represent process RSS or allocation-site attribution.
- Benchmark JSON records source revision/dirty state and hashes, exact toolchain details, allocator mode and package version, workload/sample provenance, output validation, timing distributions, and child-process peak RSS.
- Acceptance gates reject missing, duplicate, failed, under-sampled, or semantically mismatched required cases instead of quietly including them in a score.

## Definitive Misa-vs-Python benchmark

Raw artifact: [`external/results-20261010-small.json`](external/results-20261010-small.json). The run used source revision `c20fcb4165e76f822f8e358d4ff709b1c85d40c2`, `source_dirty: false`, Python 3.12.3, llvmlite 0.50.0 / LLVM 22.1.0, GCC 13.3.0, Boehm GC package `1:8.2.6-1build1`, and LLVM O2. It used three warmups and ten measured runs per implementation. Build time is excluded from execution timing. Numeric outputs are compared canonically within absolute tolerance `1e-8`; the other selected case requires exact output.

| Workload (small input) | Misa AOT median | CPython median | Misa/Python time | Peak RSS Misa / Python |
|---|---:|---:|---:|---:|
| n-body (`5,000`) | 0.073 s | 0.038 s | 1.90× slower | 11,704 / 11,704 KiB |
| spectral norm (`100`) | 0.102 s | 0.064 s | 1.58× slower | 11,572 / 11,704 KiB |
| fannkuch-redux (`8`) | 0.050 s | 0.042 s | 1.19× slower | 11,568 / 11,540 KiB |
| **Geometric mean, 3 validated cases** | — | — | **1.5287× slower** | — |

The small-input sample is useful for an implementation smoke/representative comparison, not a universal language ranking. It is one sandbox/host, covers only three numeric workloads, and is not a substitute for larger inputs or multiple machines. The raw file retains all 10 timings per case and all available metadata.

### Collector A/B memory investigation

Raw paired runs: [`external/results-20261010-gc-ab.json`](external/results-20261010-gc-ab.json). Both used the same `007968c` source revision and the same tracked-diff SHA. The status hashes differ because each allocator mode left its own untracked native object cache; this is why the later `.gitignore` change ignores generated Python/AOT artifacts and the definitive run was repeated cleanly. The experiment is three runs per mode, so treat the precise timings as diagnostic, not as a broad speed claim.

| Mode | Misa median | Misa peak RSS | Python peak RSS | Misa/Python |
|---|---:|---:|---:|---:|
| malloc fallback (no collector) | 0.173 s | 207,812 KiB | 11,540 KiB | 2.66× |
| Boehm GC | 0.113 s | 11,540 KiB | 11,704 KiB | 1.63× |
| **Change with GC** | **34.7% lower time** | **94.45% lower; 18.01× smaller** | — | — |

This isolates allocator configuration as the cause of the dramatic Misa-specific memory peak in this workload. The selected Python reference already stayed near 11.5 MiB. The A/B also suggests allocator overhead affected runtime, but does not imply that GC makes Misa faster than CPython: even with the collector it remained slower on spectral norm.

## Validation

- Unit suite: **477 passed, 0 failed**.
- AOT parity: **40/40 passed**, including typed reads, MIR scalar functions, `list.push` and `list.reserve`.
- Differential interpreter-versus-AOT suite: **11/11 passed**.
- Benchmark acceptance: all **3/3 required workloads passed** semantic/output checks; ten measured samples per implementation.
- Generated README: `python3 tools/gen_readme.py --check` passed; `README.md` and `spec/roadmap.json` agree on phases and test counts.
- Python syntax checks and `git diff --check` passed.
- The current raw benchmark artifact identifies Boehm GC and exact package version. CI installs `libgc-dev`, checks benchmark provenance, and uploads the JSON artifact. Generated Python/AOT build products are now ignored to preserve clean-source measurements.

## Known gaps and limitations

- The current numeric suite still shows Python ahead. A scalar MIR bridge and LLVM O2 are foundations, not evidence of overall performance parity.
- The MIR subset excludes control flow/CFG, effects, aggregates, and unsupported operations; this boundary should remain verifier-gated.
- The suite scores only n-body, spectral norm, and fannkuch-redux. Mandelbrot/FASTA references are currently mismatched, binary trees fail native compilation, stdin-based pairs lack equivalent fixture plumbing, and pidigits remains excluded. These are correctness/comparability gaps, not benchmark wins.
- `libgc-dev` is not bundled or auto-installed for local builds. Without it, Misa falls back to plain `malloc` with no collection; long-running allocation-heavy programs can retain temporary values until exit and grow substantially. CI and benchmarks now explicitly install/report Boehm GC.
- The memory profiler is interpreter-only and provides a peak heap, not native allocation-site detail. The benchmark RSS is process-level and should not be compared directly with `tracemalloc` values.
- Results are host- and toolchain-specific; startup/compile costs are separate from the reported runtime score.

## Prioritized recommendations

1. **Expand MIR in safe slices:** add CFG/basic blocks, branch/loop lowering, explicit effects and checked arithmetic, then differentially test before allowing new nodes through the AOT gate.
2. **Optimize the demonstrated hot paths:** investigate dynamic dispatch, boxing, collection representation, and loop overhead with native profiling; target the verified three-case workloads and larger scales rather than adding optimization flags alone.
3. **Strengthen benchmark coverage:** repair and parity-test Mandelbrot, FASTA, binary trees, pidigits and shared stdin fixtures; add larger sizes and repeat across independent hosts. Keep invalid pairs excluded until repaired.
4. **Deepen correctness testing:** generated/property-based tests for list aliasing, bounds errors, mixed generic/typed use, scalar calls, Float NaN/Inf, and optimizer differential fuzzing.
5. **Make agent diagnostics more actionable:** trustworthy source spans, stable fix-it structures, and protocol/schema compatibility tests; keep speculative repairs optional and machine-readable.
6. **Improve native memory observability:** add AOT allocation counts/subsystem profiles (separate from RSS and interpreter heap), and test collection under long-running/retained-reference workloads.
7. **Clarify target support:** document runtime-library requirements per platform and validate collector or fallback behavior in each supported native build.

## Reproduction

```sh
sudo apt-get install -y libgc-dev       # recommended for host AOT builds
PYTHONPATH=impl python3 tests/test_benchmark.py
PYTHONPATH=impl python3 tests/run_tests.py
PYTHONPATH=impl python3 tests/aot/run_aot_tests.py
PYTHONPATH=impl impl/bin/misa benchmark \
  --runs 10 --scale small \
  --only n-body,spectral-norm,fannkuch-redux \
  --require n-body,spectral-norm,fannkuch-redux \
  -o bench/external/results-20261010-small.json
```
