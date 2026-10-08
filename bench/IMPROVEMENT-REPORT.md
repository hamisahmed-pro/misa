# Misa performance and AI-native improvement report

**Date:** 2026-10-09
**Scope:** implementation in this repository, local validation, and benchmark results. The push operation is evaluated as a backend/runtime optimization; it does not by itself make Misa faster than Python.

## Executive summary

This change set makes two concrete compiler/runtime improvements while keeping Misa's explicit, typed, agent-readable approach:

1. Native AOT object generation now runs LLVM's standard **O2 speed pipeline**, with strict floating-point semantics and an invalidated AOT cache namespace.
2. Misa now has typed, explicit **`list.push(xs, value)`**, an amortized O(1), alias-visible append supported by both the interpreter and AOT runtime, including specialized bool/int/float list storage.

The benchmark suite's three matched workloads all pass output parity checks. On the fresh three-trial run, CPython 3.12 is still faster in all three. Geometric-mean Python/Misa runtime ratio is **0.242×** (lower than 1 means Misa is slower). The best relative result is fannkuch-redux at **0.376×**. This is a material improvement to that workload: its five-trial ratio moved from **0.190×** with LLVM O2 but the old repeated-concatenation Misa port to **0.374×** after switching array construction to `list.push`.

## What changed

- **AOT optimization:** use LLVM speed-level O2 in target-machine creation and an O2 module pass pipeline; verify the transformed module; keep fast-math disabled. AOT cache key moved to `v2-o2` so stale binaries are not reused.
- **List API:** `list.push` has a polymorphic `[T], T -> Void` signature and checks list/value tags at runtime in AOT. Boxed and bool/int/float-specialized native lists append using geometric capacity growth. The interpreter uses native Python `list.append` semantics.
- **Benchmark repairs:** corrected benchmark argument forwarding and spectral-norm final-vector behavior, and replaced repeatedly copied `xs = xs + [value]` loop construction with `list.push` in n-body, spectral-norm, and fannkuch-redux.
- **Regression coverage and docs:** differential and AOT tests check repeated growth and alias visibility; standard-library guide, AOT spec, README, and roadmap document what shipped and what remains planned. During rebase, the repository's newer upstream typed-AST-to-MIR foundation and its tests were preserved.
- **Test determinism:** AOT throws test now exercises a refused localhost connection rather than relying on public `example.com` availability.

## Fresh measured results

The checked-in raw data is `results-20261009.json`. It records the exact samples, compiler/runtime description, CPython version, platform, CPU, input sizes, warmup count, trial counts, and output fingerprints. The runner compiles Misa before timing, excludes compilation, warms both implementations, randomizes order within each trial, and verifies outputs on every measured run.

| Workload | Input | CPython median | Misa AOT median | Python/Misa ratio |
|---|---:|---:|---:|---:|
| n-body | 50,000 | 0.1389 s | 0.8335 s | 0.167× |
| spectral norm | 500 | 1.0863 s | 4.8273 s | 0.225× |
| fannkuch-redux | 10 | 2.2649 s | 6.0257 s | 0.376× |
| **Geometric mean** | — | — | — | **0.242×** |

These are wall-clock subprocess timings on one sandbox host, not a broad language ranking. Three trials are enough to establish an initial reproducible signal, not characterize all hardware or variance. Earlier five-trial O2 data is in the job's temporary files rather than this repository; the five-trial post-`list.push` sample gave a geometric mean of 0.249× and fannkuch-redux 0.374×. The reliable within-run comparison is the fannkuch case, whose Misa time fell from 11.67 s to 5.95 s across two five-trial runs; other workload cross-run timings are more affected by host variability.

## Validation

- Unit suite: **473 passed, 0 failed**.
- Differential interpreter/AOT: **11/11 passed**.
- AOT suite: **34/37 passed** including the new `a36_list_push` case. The remaining three DB cases could not build because the optional SQLite amalgamation is absent at `~/workspace/deps/sqlite/`; the AOT throws case now passes offline. These are environment dependency gaps, not ignored test failures.
- Benchmark runner: three workloads, output parity verified on warmups and each timed sample.
- `git diff --check`: clean before final commit.

## Review and recommendations

### Highest leverage next steps

1. **Lower collection element access and loop induction to raw values.** The benchmark still spends heavily in dynamic list indexing, boxing/unboxing, runtime checks, and generic object operations. Extend the proven Int fast path to typed list loops and typed Float function boundaries, guarded by type-checker facts and differential tests.
2. **Complete MIR-to-AOT integration and systematic optimization.** Typed AST-to-MIR construction and initial MIR passes now exist upstream. Next, connect verified MIR optimization/lowering to production AOT builds, expand type-directed unboxing and specialization there, and prove parity against the interpreter. This reduces the need for ever more local emitter heuristics while keeping compilation inspectable for agents.
3. **Use workload profiles and counters.** Add an opt-in `misa profile` output with time/allocation hotspots and stable JSON. Agents can turn evidence into targeted changes without guessing, while avoiding performance claims based on one benchmark family.
4. **Add allocation-aware collection APIs.** Consider `list.with_capacity` or a builder once a measured workload requires it. Keep explicit mutation visible in source (`list.push`) rather than silently rewriting arbitrary aliasing-sensitive expressions.
5. **Expand controlled benchmarks.** Add larger and smaller sizes, multiple workloads for allocation-heavy, numeric, text, and I/O paths, and retain repeated samples/variance. Compare like-for-like semantics and report startup separately from steady execution.
6. **AI-native product work should remain a first-class axis.** Improve compiler/checker diagnostics with structured machine-readable codes, edit locations and suggested fixes; provide schema-constrained model calls and an agent-oriented project/test/benchmark protocol. These can make agents more effective without implying native speed parity.

### Review cautions

- LLVM O2 is a general-purpose optimizer, not a guarantee of improvement on every program; keep correctness parity tests and benchmark representative programs whenever LLVM or llvmlite changes.
- `list.push` mutates by design; aliases observe the mutation. This is documented and consistent with the existing `set.add` mutation model, but is a semantic choice worth keeping explicit.
- The current C runtime is allocation-heavy and lacks a tracing collector in this configuration; O2 cannot eliminate the dominant tagged-value/runtime overhead. The measured data supports prioritizing representation and allocation work over more optimizer flags.
- The checked-in benchmark ports are equivalent in outputs at the prescribed inputs, but remain separate language implementations; algorithmic similarity and source review matter in addition to output fingerprints.

## Reproduction

From the repository root, with `llvmlite` and the implementation dependencies installed:

```sh
PYTHONPATH=impl python3 bench/external/run_comparison.py \
  --runs 5 --warmups 1 --json /tmp/misa-python-results.json
```

For compiler and interpreter validation:

```sh
PYTHONPATH=impl python3 tests/run_tests.py
PYTHONPATH=impl python3 tests/test_differential.py
PYTHONPATH=impl python3 tests/aot/run_aot_tests.py
```
