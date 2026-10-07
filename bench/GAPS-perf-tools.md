# Misa Gap-Hunt: Runtime Performance + Tooling vs Python, Rust, Go

Date: 2026-10-06 (gaps found) / 2026-10-07 (final re-measurement).
All numbers measured (best of 3, wall time) — no estimates.
Misa = AOT binary (`misa build`, host). Python = 3.12.3. Go = 1.24.7
(`~/workspace/go1.24/go/bin`). Rust 1.99.0 (`~/.cargo`).

## 2026-10-07 — final re-measurement (post Wave 1–3 + final wave)

Rebuilt everything from scratch on the current tree (404/404 + 33/33).
Same methodology; all outputs verified byte-identical across languages.

| Benchmark | Misa AOT | Python 3.12 | Go 1.24 | Misa vs Py | Misa vs Go |
|---|---|---|---|---|---|
| fib(35) recursive | 0.221 s | 1.95 s | 0.080 s | **8.8× FASTER** | 2.8× slower |
| sum 10M ints (loop) | 0.268 s | 1.35 s | 0.009 s | **5.0× FASTER** | 30× slower |
| concat 100k (naive `+`) | 7.28 s | 0.207 s | 0.749 s | **35× slower** | 9.7× slower |
| concat 100k (`Text.Builder`) | 0.045 s | 0.036 s (`"".join`) / 0.18 s (`+=`) | 0.003 s | **par with `"".join`**; 4× faster than `+=` | 15× slower |
| JSON parse+traverse (~1 MB) | 0.051 s | 0.058 s | 0.026 s | **1.14× FASTER** | 2.0× slower |
| SQLite 10k inserts | 0.032 s | 0.049 s | n/a | **1.5× FASTER** | — |

Honesty notes on the deltas vs the 2026-10-06 sprint table:
- **Builder vs Python:** the sprint table claimed "4× faster than Python"
  comparing `Text.Builder` (0.04 s) against Python's *naive* `+=` idiom
  (0.18 s). Against Python's *optimal* `"".join` idiom (0.036 s) Misa is
  at parity (1.25× slower). Both numbers are real; the optimal-idiom
  comparison is the honest headline. Builder remains the recommended
  Misa idiom and beats the naive idiom 4–5×.
- **Naive `+` is still 35× slower than Python.** The O(n²) fast path
  helped 2×; the fix is the idiom (Builder), not the operator.
- **SQLite:** the sprint's 0.039 s used `exec_many` with a
  pre-built row list. Re-measured fairly: per-row `db.exec` inside
  `db.begin`/`db.commit` (prepare cache) does 10k inserts in **0.032 s**,
  1.5× faster than Python's `executemany` (0.049 s). (`exec_many` with
  an O(n²)-built row list timed 0.885 s — the list building, not the
  inserts.)
- **JSON is now faster than Python** (1.14×), not just 1.2× slower.
- Python absolutes differ from the 10-06 table (1.60→1.95 s fib) due to
  machine load; ratios were measured contemporaneously and are the
  comparable figures.
- **Go remains 3–30× faster on raw compute** — the honest ceiling.
  Misa beats Python on 4 of 6 benchmarks; Go beats Misa on 5 of 5
  it contests.

### Binary sizes (2026-10-07, current tree — tree-shaking holds)

| Binary | Misa | Rust (stripped) | Go |
|---|---|---|---|
| hello | **176,664 B** | 336,752 B | 2,213,423 B |
| json CLI | **188,976 B** | 440,456 B | 2,947,349 B |
| tasks (SQLite app) | **1,237,120 B** (1.18 MB) | n/a | n/a |

Misa hello grew 172 KB → 177 KB (+2.6%) with the Wave 3 runtime
(regex engine, sockets, crypto) — tree-shaking still holds; still
~2× smaller than Rust, ~12× smaller than Go.

### Startup (2026-10-07)

Misa AOT hello: **9 ms**. Python `print("hello")`: **25 ms**.
Misa ~3× faster (earlier table: 7.7 ms vs 64 ms on a different box;
direction consistent, magnitude varies by machine).

### Build times (2026-10-07)

`examples/tasks.misa` (~330 lines + stdlib): clean build **1.01 s**,
warm rebuild **0.70 s**. (Earlier: 0.65–0.92 s. No per-function
incrementality yet — Go's 133 ms incremental remains the bar.)

### Concurrency (carried forward from 2026-10-06)

`fetch_all` over 10 delayed URLs: interpreter **5.1×** vs sequential
(1.02 s vs 5.21 s), AOT **3.3×** (1.54 s vs 5.02 s). Not re-run 10-07
(network-dependent); the fiber scheduler is covered by `a23_concurrency`
in the green AOT suite.

## 2026-10-06 (evening) — performance sprint results (superseded by the table above; kept for history)

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

## Part 2 — Tooling gap matrix (updated 2026-10-07)

HAVE / PARTIAL / MISSING (Misa column is the one that matters):

| Tool | Python | Rust | Go | Misa |
|---|---|---|---|---|
| Debugger (breakpoints, stepping) | pdb (stdlib) | rust-gdb / lldb | delve | **HAVE** — `misa debug --agent` (JSON protocol, typed dumps — first agent-native debugger) + human text mode |
| Profiler (CPU / memory) | cProfile | perf + flamegraph | pprof (stdlib!) | **HAVE** — `misa profile` (sampling, `--json`); interpreter-only, no memory profiler yet |
| REPL | yes (stdlib) | evcxr (3rd-party) | yaegi (3rd-party) | **HAVE** — `misa repl` (persistent bindings, `:type`, multi-line) |
| Test runner | pytest / unittest | `cargo test` | `go test` | **HAVE** — `misa test` discovers `test NAME {}` blocks + `eval` blocks, `--json`, isolation |
| Package registry | PyPI | crates.io | module proxy | **MISSING** — spec written (`spec/REGISTRY.md`); build blocked on hosting |
| Formatter | black / ruff format | rustfmt | gofmt | **HAVE** — `misa fmt` |
| Linter | ruff / flake8 | clippy | go vet | **PARTIAL** — the checker catches type errors with positions; no style, unused-code, or suspicious-pattern lints |
| IDE support | everywhere | rust-analyzer everywhere | gopls everywhere | **PARTIAL** — VS Code extension + LSP only |
| Build caching / incremental | n/a (interpreted) | excellent | excellent (133 ms incremental measured) | **PARTIAL** — runtime objects cached (tasks.misa: clean 1.01 s → warm 0.70 s), but the program's LLVM module is recompiled whole every time; no per-function incrementality |
| Doc generator | sphinx / pydoc | rustdoc | godoc | **MISSING** — doc comments exist in stdlib but nothing renders them |

### Closed since 2026-10-06

- **T-1 (debugger) → HAVE, and ahead.** `misa debug --agent` is the
  first debugger designed for AI agents (JSON-line protocol, breakpoints,
  stepping, *typed* variable dumps as structured data). Caught a real
  three-way int-division divergence on day one.
- **T-2 (profiler) → HAVE (partial).** `misa profile` samples the
  interpreter with human + `--json` output. No AOT profiling, no memory
  profiler — Go's pprof remains ahead on depth.
- **T-3 (test runner) → HAVE.** `misa test` now discovers `test NAME {}`
  blocks with per-test isolation, `assert_eq`/`assert_ne`, and `--json`
  for agents — *plus* the pre-existing model `eval` blocks in the same
  runner. One runner for code *and* models: nobody else has that
  combination.

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
