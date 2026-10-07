# Misa JIT & Method Compiler — Architecture Plan

**Date:** 2026-10-07
**Status:** Commenced per Hamis's order. Spare nothing, leave no stone unturned.
**Goal:** Tiered execution — interpreted → bytecode VM → JIT native — sharing one
value representation, reaching parity with CPython/V8 on AWFY.

## 1. Current state (measured 2026-10-07)

| Engine | Value repr | AWFY vs Python |
|---|---|---|
| Tree-walking interp | Python objects | 122–630× slower |
| Bytecode VM (C dispatch, `misa run --bytecode`) | Python objects | ~25–125× slower (5× over tree-walk) |
| AOT (`misa build`, LLVM) | MVal* (C structs) | 3.6–29× slower |

The fundamental blocker for a true JIT is the **split value representation**:
the interpreter/VM uses Python objects, the AOT uses MVal*. A JIT that
compiles hot methods with the LLVM backend must agree with the interpreter
on representation, or every call boundary pays a conversion cost that eats
the speedup.

## 2. Target architecture

```
.misa source
    │
    ▼
 Parser → Checker ──→ Bytecode compiler ──→ C VM (MVal*) ──→ Profiler
                                                        │  (call counters,
                                                        │   loop back-edge
                                                        │   counters)
                                                        ▼ hot
                                              LLVM method compiler
                                              (reuses AOT emitter,
                                               per-function, MVal* ABI)
                                                        │
                                                        ▼
                                              dlopen'd .so ──→ patch call site
                                                        │
                                                        ▼
                                              Native execution
```

One value representation (MVal*) everywhere. One runtime (misa_rt.c).
Three tiers: C VM → baseline JIT (unoptimized LLVM) → optimizing JIT
(inline caches, unboxing, bounds-check elimination).

## 3. Phases

### Phase 1 — AOT-as-default with compilation cache (days)
`misa run` attempts `misa build` to a content-hashed cache dir first
(`~/.cache/misa/<sha256>/prog`), executes the binary, and falls back to
the interpreter only when the AOT backend rejects the program.
Rationale: users immediately get 3.6–29× instead of 122–630× with zero
language changes. The cache makes the second run instant.

Work items:
- [ ] Content-hash cache in `misa/aot/build.py` (`build_cached(path) -> binpath`)
- [ ] `cmd_run`: try cached AOT, fall back to interp on AOTError
- [ ] `--interp` flag to force the interpreter (debugging)
- [ ] Cache invalidation: hash source + imports + toolchain version
- [ ] Validate: AWFY via `misa run` (default) matches `misa build` numbers

### Phase 2 — Unified value representation (weeks)
Port the C VM from Python objects to MVal*:
- [ ] `vm.c`: replace PyObject* stack/vars with MVal*; opcodes call
      misa_rt.c helpers (`misa_add`, `misa_list_get_raw`, …)
- [ ] Bytecode compiler: constants become MVal* (ints via misa_int, etc.)
- [ ] Resolver: BytecodeFunction frames allocate MVal* vars
- [ ] Python-object VM kept as legacy fallback
- [ ] Validate: `--bytecode` passes the 473-test suite

Why: the JIT (Phase 3) compiles to the MVal* ABI. Without this, every
JIT boundary converts representations and the win evaporates.

### Phase 3 — Method JIT via LLVM (weeks–months)
- [ ] Extract per-function compilation from `aot/emit.py`
      (`compile_function(fn_decl) -> .so` exposing `misa_jit_<name>`)
- [ ] Profiler: per-function call counters in the C VM; threshold
      (e.g. 100 calls or hot loop) triggers compilation
- [ ] Loader: dlopen the .so, dlsym the symbol, patch a function-pointer
      table the VM consults on CALL
- [ ] Calling convention: `(MVal* *args, int64_t nargs) -> MVal*`,
      exceptions via misa_pending()
- [ ] Deopt: on E300/E200 inside JIT code, fall back to VM frame
- [ ] Validate: hot AWFY functions (sieve inner loop) show native speed

### Phase 4 — Optimizing JIT (months, leave no stone unturned)
- [ ] Inline caches: monomorphic/polymorphic call-site caches
- [ ] Unboxed int/float fast paths in C VM (tag-checked, no alloc)
- [ ] Bounds-check elimination in AOT emitter (provable indices)
- [ ] Escape analysis: stack-allocate short-lived lists/records
- [ ] Loop unrolling / strength reduction for counted loops
- [ ] String builder already exists; audit concat paths
- [ ] Profile-guided: reorder branches by taken probability

### Phase 5 — Interpreter parity hardening
- [ ] `--bytecode` becomes default when AOT cache misses and program
      is VM-coverable; tree-walking becomes the last resort
- [ ] Expand bytecode compiler node coverage (records, maps, try/catch,
      string interp, etc.) with fallback per function

## 6. Implementation status (2026-10-07)

### Phase 1 — DONE ✅
- `misa/aot/build.py`: `build_cached()` + `cache_key_for()` — content-hashed
  compilation cache at `~/.cache/misa/aot/<sha256>/prog`.
- `misa run` defaults to cached AOT; `--interp` forces tree-walking,
  `--bytecode` forces the C VM, `--mock` forces interpreter.
- 473/473 tests pass. AWFY harness updated (`--interp` for true interp numbers).

### Phase 2 — DONE ✅ (prototype)
- `impl/misa/vm_mval.c`: C VM operating on MVal* (unified with AOT).
  Opcodes call `misa_add`, `misa_lt`, etc. directly.
- `misa_int_val`/`misa_float_val` accessors added to runtime.
- Measured: 0.170s for 1M-iteration sum loop (vs 0.09s Python-obj VM —
  MVal* runtime has more type-check overhead, but shares representation
  with AOT, which is the strategic win).

### Phase 3 — DONE ✅ (prototype hardened)
- `impl/misa/jit.py`: `JitCompiler` — bytecode → LLVM IR (MVal* ABI).
  Supports: const/var, arithmetic, comparisons, jumps, return, pop/dup.
- `jit_compile_to_so()` + `jit_load_and_run()`: .so via LLVM, dlopen, call.
- **Type specialization**: `analyze_int_only()` detects int-only functions;
  `compile_specialized()` generates raw i64 LLVM (no MVal* boxing).
  Measured: 1M-iteration loop in **0.007s** (vs 0.170s VM, 0.208s generic JIT).
  **~3× faster than CPython** on the microbenchmark.
- **Auto-JIT**: `cmd_run_bytecode` counts calls; hot int-only functions
  (≥5 calls) auto-compile. JIT cache at `~/.cache/misa/jit/` (content-hash).
- **Background compilation**: Hot functions compile in a background thread
  (non-daemon, joined at exit) while the VM continues. No 4s pause.
  Cold: 4.7s (compile in bg, program runs via VM); warm: 0.5s (cache hit).
- **Generic MVal* JIT**: `compile_generic()` handles arbitrary types via
  MVal* runtime calls. LIST_GET/SET, BUILD_LIST opcodes implemented.
  PRINT renders via misa_render (stdout wiring is future work).
  Available but not auto-enabled (slower than VM at 0.208s vs 0.170s).
- **Deoptimization**: Generic JIT checks `misa_pending()` after each runtime
  call; on error jumps to `err_block` returning NULL. Python caller detects
  NULL and falls back to VM, invalidating the cache entry. Verified with
  division-by-zero test.
- LIMITATION: CALL not supported in specialized JIT (functions aren't i64).
  Leaf int-only functions only for auto-JIT.

### Phase 4 — NOT STARTED
Inline caches, unboxed fast paths, bounds-check elimination, etc.

## 4. Non-goals / honesty constraints
- No benchmark publicity without Hamis's explicit approval (standing rule).
- Every speedup claim ships with raw receipts (AWFY JSON) and the exact
  command. No LAG/PAR/LEAD invention.
- The 2×-of-Python goal is the bar; report the measured gap every phase.
- Do not break the 473-test suite at any phase; it runs green before merge.

## 5. Risks
- LLVM per-function JIT latency: compiling a hot function must cost less
  than the interpreted time saved. Mitigate with tiering (only truly hot
  functions) and a background compile thread.
- MVal* GC: the runtime uses GC_malloc (conservative). JIT code must root
  live values across calls — the AOT emitter already does this; the JIT
  reuses it.
- Platform: dlopen path is POSIX-first; Windows needs LoadLibrary later.
