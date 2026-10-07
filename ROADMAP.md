# Misa Implementation Roadmap — from Benchmark Review Plan (47 sections)

**Source:** Third-party "Benchmark Review Plan" (ChatGPT shared conversation),
studied 2026-10-07. This roadmap converts every actionable recommendation
into phased engineering work.

**Overall verdict from review:** ~7.2/10. "Beyond an experiment; the new fight
is coherence, speed, portability, reliability, and provability."

---

## Phase 1: Compiler Performance — Type-Directed Unboxing (Review §5, §6, §12)

**Status:** L1 partially done. L2/L3 not done.

### 1A. Primitive specialization (L1) — DONE
- [x] Int unboxing (raw i64 locals, direct LLVM add/sub/mul)
- [x] Float unboxing (raw double locals, direct fadd/fsub/fmul/fdiv)
- [x] Record field indexed access (misa_record_get_idx)

### 1B. Specialized function bodies (L2) — PARTIAL
- [x] `fib(Int) -> Int` emits `misa_fn_fib__int(i64) -> i64`
- [ ] Extend to Float functions: `f(Float) -> Float` emits raw double version
- [ ] Specialized generic functions (monomorphization)

### 1C. Typed arrays (L3) — NOT STARTED
- [ ] `[Int]` as `int64_t[]` instead of `MVal*[]`
- [ ] `[Float]` as `double[]` instead of `MVal*[]`
- [ ] `[Bool]` already done (unboxed bool list)
- [ ] Bounds-check elimination for provably-safe indices

### 1D. Native record layouts (Review §7)
- [ ] Typed records as LLVM structs (body.x → getelementptr/load)
- [ ] Currently: records are `MVal*` with name→index lookup
- [ ] Target: `type Body { x: Float, ... }` becomes `%Body = type { double, ... }`

---

## Phase 2: Cross-Platform Correctness (Review §18-22)

**Status:** iOS ABI fixed. WASM broken. Capability drift exists.

### 2A. Platform ABI audit — DONE
- [x] iOS `misa_platform_http_post` 7-param fix (was 6, memory corruption risk)

### 2B. ABI declaration single source of truth (Review §22)
- [ ] Generate `misa_platform.h` declarations from one source
- [ ] Cross-platform ABI test: mismatch → E600 error, not runtime crash
- [ ] Test: compile trivial program for each target, verify symbols match

### 2C. WASM build fix (Review §18)
- [ ] Emscripten target can't compile trivial program (time.parse in shared runtime)
- [ ] Investigate: isolate platform-specific code from shared runtime
- [ ] Goal: `misa build --target wasm32-unknown-emscripten hello.misa` works

### 2D. Capability matrix (Review §22)
- [ ] `spec/platform.json`: machine-readable per-target capability flags
- [ ] `process.run` frontend/backend drift → explicit `BuiltinCapability` matrix
- [ ] Tests derived from the matrix (not hand-written per target)

---

## Phase 3: Benchmarking Discipline (Review §8, §37, §38-41)

**Status:** Harness fixed. Canonical command built. Needs expansion.

### 3A. Corrected methodology — DONE
- [x] `bench/awfy/run_awfy_corrected.py` (external timing for all)
- [x] `misa benchmark` command (JSON source of truth)
- [x] 9/10 Benchmarks Game ports

### 3B. Expand `misa benchmark` (Review §9)
- [ ] Add PHP and Rust baselines (currently Python3 only)
- [ ] Add startup time measurement (launch → first output)
- [ ] Add compilation time measurement (source → executable)
- [ ] Add peak RSS, binary size reporting
- [ ] Report median, p95, min, max, stddev (currently median only)

### 3C. Benchmark rules enforcement (Review §38-41)
- [ ] Warmup: 3 runs before measurement
- [ ] 10-30 measurements per benchmark
- [ ] Short workloads repeated so measurement > 100ms
- [ ] Output canonical checksum verification

---

## Phase 4: Testing & Reliability (Review §29-31)

**Status:** Strong foundation. Needs differential + fuzzing.

### 4A. Existing — DONE
- [x] 473 unit tests, 36 AOT parity tests
- [x] 641 differential programs, 47 E-codes

### 4B. Compiler differential testing (Review §31) — NOT STARTED
- [ ] Automated interp vs AOT vs JIT comparison
- [ ] Compare: stdout, stderr, exit codes, error codes, float rendering
- [ ] Compare: serialized output, side effects
- [ ] Run on every PR

### 4C. Fuzzing in CI (Review §31) — NOT STARTED
- [ ] `misa fuzz --seed ...` command
- [ ] Nightly 100k programs with interp == AOT invariant
- [ ] Per-PR fuzzing gate

---

## Phase 5: Documentation Coherence (Review §23)

**Status:** NOT STARTED

### 5A. README generation
- [ ] Single roadmap source (e.g., `spec/roadmap.json`)
- [ ] README phase status generated from source, not hand-edited
- [ ] CI check: README matches source (fail PR if drift)

### 5B. Claim language (Review §14)
- [ ] Tone down "first in the world" / "nobody else has it"
- [ ] Use defensible language for checked throws, AI-native features
- [ ] Audit all docs for unsubstantiated superlatives

---

## Phase 6: Stdlib Gaps (Review §25-26)

**Status:** Beyond toy stage. Specific gaps remain.

### 6A. Iterator fusion (Review §26) — NOT STARTED
- [ ] Build `iter()` properly
- [ ] AOT iterator fusion: `map(filter(map(...)))` → single loop
- [ ] This is a major optimization; requires MIR (Phase 7)

### 6B. String operations — PARTIAL
- [ ] Unicode normalization (missing)
- [ ] Additional string search/split operations
- [x] Basic string ops exist

### 6C. Other gaps
- [ ] Lazy iterators
- [ ] Date/time comprehensive
- [ ] Hashing utilities
- [ ] FS traversal/watchers
- [ ] HTTP client depth

---

## Phase 7: Misa IR — Typed Optimizing IR (Review §32-33)

**Status:** NOT STARTED. Major milestone.

### 7A. Design
- [ ] `spec/mir.md`: MIR specification (typed AST → MIR → optimized MIR → LLVM)
- [ ] SSA form, type-annotated
- [ ] Design: how MVal* boxing is represented and eliminated

### 7B. Implementation phases
- [ ] MIR construction from typed AST
- [ ] Optimization passes: constant folding, DCE, inlining
- [ ] Unboxing pass (systematic, not ad-hoc like current)
- [ ] Escape analysis (for string-builder lowering, stack allocation)
- [ ] Monomorphization (specialized generics)
- [ ] Lower to LLVM

### 7C. Optimizations enabled by MIR
- [ ] Bounds-check elimination
- [ ] String-builder lowering (systematic vs current pattern-match)
- [ ] Iterator fusion
- [ ] Record layout optimization

---

## Explicitly NOT Doing (Review §34)

Per the review's "WHAT NOT TO DO NEXT":
- [ ] ❌ Registry website / package search
- [ ] ❌ More syntax
- [ ] ❌ More AI providers
- [ ] ❌ More stdlib APIs (beyond Phase 6 gaps)
- [ ] ❌ UI widgets
- [ ] ❌ Marketing claims
- [ ] ❌ Big new features
- [ ] ❌ More roadmap docs (this file is the last one)

---

## Priority Order (Review §46)

1. Make interpreter and AOT essentially indistinguishable
2. Build the typed optimizing IR (Phase 7)
3. Finish platform correctness — WASM + iOS + Android (Phase 2)
4. Build the canonical six-language benchmark (Phase 3B)
5. Prove the AI-agent advantage experimentally

---

## 90-Day Breakdown (Review §35-37)

**Days 1-30: Stabilize**
- Phase 2B (ABI single source), 2C (WASM fix), 2D (capability matrix)
- Phase 4B (differential testing), 5A (README generation)

**Days 31-60: Compiler Performance**
- Phase 1B (Float specialization), 1C (typed arrays), 1D (native records)
- Phase 7A (MIR design), 7B (MIR construction)

**Days 61-90: Benchmarking + Proof**
- Phase 3B (six-language benchmark), 3C (rules enforcement)
- Phase 4C (fuzzing), 6A (iterator fusion via MIR)

---

*This roadmap is derived from the third-party Benchmark Review Plan studied
2026-10-07. Items marked DONE were completed before or during this session.
The user ordered full implementation; phases are ordered by review priority.*
