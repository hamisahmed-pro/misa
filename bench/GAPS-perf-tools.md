# Misa Gap-Hunt: Runtime Performance + Tooling vs Python, Rust, Go

Date: 2026-10-06. All numbers measured (best of 3, wall time) — no estimates.
Misa = AOT binary (`misa build`, host). Python = 3.12.3. Go = 1.24.7
(tarball under ~/workspace/go1.24). Rust skipped (optional per brief).

## 2026-10-06 (evening) — performance sprint results

All three gaps closed (best of 3, same methodology):

| Benchmark | Before | After | Python 3.12 | Misa vs Py |
|---|---|---|---|---|
| fib(35) recursive | 13.59 s | **0.21 s** | 1.60 s | **7.7× FASTER** |
| sum 10M ints (loop) | 3.61 s | **0.27 s** | 0.98 s | **3.7× FASTER** |
| concat 100k (naive `+`) | 13.39 s | 7.18 s | 0.18 s | 40× slower |
| concat 100k (`Text.Builder`) | — | **0.04 s** | 0.18 s | **4× FASTER** |
| SQLite 10k inserts | 0.42 s | **0.039 s** | 0.019 s | 2.1× slower |

What changed:
- **P-1**: `Text.Builder` (`builder()`, `b.append(s)`, `b.build()`) with
  exponential overallocation; plus a fast path in `misa_text_concat`.
- **P-2**: type-directed unboxing in the AOT emitter. Level 1: raw i64
  arithmetic for checker-proven `Int` operands. Level 2: raw i64
  function specialization (`misa_fn_f__int`) for `Int -> Int` functions
  — zero allocations in the hot path. Wrapping semantics preserved.
- **P-3**: prepared-statement cache (per connection, keyed by SQL),
  `db.exec_many(db, sql, rows)`, `db.begin`/`db.commit`/`db.rollback`.

## Part 1 — Performance benchmarks (original, pre-sprint)

| Benchmark | Misa AOT | Python 3.12 | Go 1.24 | Misa vs Py | Misa vs Go |
|---|---|---|---|---|---|
| fib(35) recursive | 13.59 s | 1.60 s | 0.075 s | **8.5× slower** | **181× slower** |
| sum 10M ints (loop) | 3.61 s | 0.98 s | 0.007 s | **3.7× slower** | **553× slower** |
| concat 100k strings | 13.39 s | 0.18 s | 0.002 s (Builder) / 0.41 s (naive `+=`) | **74.9× slower** | **8757× / 32.6× slower** |
| JSON parse + traverse (~1.5 MB) | 0.073 s | 0.061 s | 0.042 s | 1.2× slower | 1.7× slower |
| SQLite: 10k inserts + aggregate | 0.42 s | 0.036 s (executemany) / 0.016 s (per-row) | n/a | **11.8× / 26× slower** | — |

Methodology notes:
- All outputs verified byte-identical across languages (fib=9227465,
  loop sum=49999995000000, strlen=100000, json sum=312487500, rows=10000).
- Misa programs: `/tmp/bench/*.misa` (not committed; rebuild with
  `misa build`). Go sources in `/tmp/bench/go/src/`.
- String idiom differs by language (see analysis): Python `s += "x"`
  (CPython overallocation optimization), Misa `s = s + "x"`, Go shown
  both ways — idiomatic `strings.Builder` and naive `+=`.
- SQLite: Misa has no `executemany`; Python measured both ways. Go
  skipped (would need CGO driver download).

### The 3 biggest performance gaps

**P-1. String building: 75× Python, 33× Go-naive.** Root cause: every
`s + "x"` allocates a brand-new string and copies both sides — O(n²)
with no overallocation, plus tagged-value overhead per op. CPython
optimizes `+=` with exponential overallocation; Go's Builder does the
same explicitly. Misa has neither path.
→ Overtake: exponential-overallocation fast path in the runtime's
string-append (`misa_str_concat`), plus a `Text.Builder` stdlib type
with a compiler intrinsic for the `s = s + x` loop pattern. Realistic
target: within 2–3× of Python. Medium effort, large visible win.
**CLOSED 2026-10-06**: `Text.Builder` does 100k appends in 0.04 s
(4× faster than Python's 0.18 s); naive `+` improved 2× via fast path.

**P-2. fib recursion: 8.5× Python, 181× Go.** Root cause: no unboxing
(AOT.md: "There is no unboxing in v0.3… Correctness first"). Every `Int`
is a tagged value; every call and every `+`/`-`/`<` dispatches through
a runtime helper. Python's ints are also boxed, but CPython's
bytecode/dispatch loop is far more optimized than Misa's helper calls.
→ Overtake: **type-directed unboxing in the AOT emitter** — when the
checker proves both operands are `Int`, emit raw i64 arithmetic
(preserving the specified wrapping semantics) instead of helper calls;
same for monomorphic call sites. This is the single highest-leverage
perf project: it collapses the fib AND loop gaps toward Go in one
change. Large effort, fundamental win. (Interpreter is second priority;
Python-level fast paths help less while the GIL-era dispatch remains.)
**CLOSED 2026-10-06**: fib(35) 13.59 s → 0.21 s (7.7× faster than
Python); 10M loop 3.61 s → 0.27 s (3.7× faster than Python). Level 1:
unboxed binops; Level 2: raw i64 function specialization.

**P-3. SQLite inserts: 12–26× Python.** Root cause (verified in
`misa_db.c`): `sqlite3_prepare_v2` runs on EVERY `db.exec` (no
prepared-statement cache), no bulk-insert API, one implicit transaction
per statement. Python implicitly batches and reuses prepares.
→ Overtake: prepared-statement cache keyed by SQL text in `misa_db.c`,
`db.exec_many(sql, [[any]])` bulk API, and explicit `db.begin` /
`db.commit` transaction control. Near-parity with Python is achievable;
this is mostly engineering, not research. Medium effort.
**CLOSED 2026-10-06**: 10k inserts 0.42 s → 0.039 s (2.1× Python's
0.019 s, within the 3× target). Prepare cache + `db.exec_many` +
`db.begin`/`db.commit`/`db.rollback`.

### Bright spot (keep)

**JSON parse + traverse is at 1.2× Python / 1.7× Go.** The C JSON parser
(`misa_json.c`) is genuinely competitive — byte-parity behavior at near
native-parser speed. This is the standard new code should be held to.

## Part 2 — Tooling gap matrix

HAVE / PARTIAL / MISSING (Misa column is the one that matters):

| Tool | Python | Rust | Go | Misa |
|---|---|---|---|---|
| Debugger (breakpoints, stepping) | pdb (stdlib) | rust-gdb / lldb | delve | **MISSING** |
| Profiler (CPU / memory) | cProfile | perf + flamegraph | pprof (stdlib!) | **MISSING** |
| REPL | yes (stdlib) | evcxr (3rd-party) | yaegi (3rd-party) | **MISSING** |
| Test runner | pytest / unittest | `cargo test` | `go test` | **PARTIAL** — `misa test` runs *model-eval* decls only (`eval Name { model, given, expect, runs }`); there is no unit-test discovery/reporting. An `assert(cond, msg?)` builtin exists (E301 on failure), so `misa run` on assert-files is the current poor-man's test. |
| Package registry | PyPI | crates.io | module proxy | **MISSING** — `misa pkg` works locally only; registry commands explicitly deferred to the public phase |
| Formatter | black / ruff format | rustfmt | gofmt | **HAVE** — `misa fmt` |
| Linter | ruff / flake8 | clippy | go vet | **PARTIAL** — the checker catches type errors with positions; no style, unused-code, or suspicious-pattern lints |
| IDE support | everywhere | rust-analyzer everywhere | gopls everywhere | **PARTIAL** — VS Code extension + LSP only |
| Build caching / incremental | n/a (interpreted) | excellent | excellent (133 ms incremental measured) | **PARTIAL** — runtime objects cached (fib: clean 0.57 s → rebuild 0.33 s; tasks.misa 330 lines: ~0.65–0.92 s), but the program's LLVM module is recompiled whole every time; no per-function incrementality |
| Doc generator | sphinx / pydoc | rustdoc | godoc | **MISSING** — doc comments exist in stdlib (`std/` requires them per PKG.md) but nothing renders them |

### The 3 biggest tooling gaps

**T-1. No debugger.** Every competitor has one. For a human this is
table stakes; for Misa's agent-native positioning it matters doubly —
agents today debug via print statements, which is exactly the slow loop
Misa claims to eliminate.

**T-2. No profiler.** You cannot fix P-1..P-3 without measuring, and
users can't find their own hot spots. Go's pprof (built into the
toolchain, always one import away) is the bar.

**T-3. No real test runner.** `misa test` is a model-eval harness with
a borrowed name. There is no `cargo test` / `go test` equivalent:
no discovery, no fixtures, no failure reporting for plain unit tests.

## Overtake proposals (the "by a mile" part)

**Highest leverage: the first debugger designed for AI agents —
`misa debug --agent`.** Don't build a better pdb; pdb already exists and
is fine for humans. Build what no language has: a debugger whose
primary interface is a JSON-line protocol where an *agent* sets
breakpoints, steps, and dumps **typed variable state as structured
data** (not text to parse). Misa already has everything this needs:
typed values, source positions on every error, structured E-codes.
The loop becomes: agent's program fails → agent attaches, inspects
typed state at the failing line programmatically, fixes, re-runs —
no print-debugging, ever. This directly attacks the iteration-cycle gap
vs Python (benchmark 1's weakest result) and turns Misa's type system
into a debugging superpower. No competitor has this; it's the
tooling equivalent of the cross-platform wedge.

**Second: type-directed unboxing (P-2).** Collapses the two largest
compute gaps in one project. Ship it with the benchmark table in this
file as the before/after proof.

**Third: `misa test` for real** — unit-test discovery (`test_*.misa` /
`fn test_*`), fixtures, structured failure output with positions,
running against both interpreter and AOT. Rename the model-eval
command to `misa eval` to end the confusion.

**Fourth: profiler** — start with a sampling CPU profiler on the AOT
binary (frame-pointer unwinding; the fiber scheduler already owns
timing) plus allocation counting in the runtime. Go-parity can come
later; *any* profiler beats none.

Deliberately NOT proposed: matching rust-analyzer/gopls IDE breadth
(diminishing returns pre-launch), a REPL (nice but low leverage vs the
debugger), doc-generator (mechanical; do it when the registry lands).
