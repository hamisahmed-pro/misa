# Misa language+stdlib gap matrix vs Python / Rust / Go

**Date:** 2026-10-06. **Scope:** the language and standard library only.
Measured performance and tooling (LSP, debugger, build times, REPL) are
covered separately in `bench/GAPS-perf-tools.md`.

**Method:** feature-by-feature comparison. Ratings:
- **LAG** — Misa is behind at least one of the three here.
- **PAR** — Misa is roughly equal to the best of the three.
- **LEAD** — Misa is ahead of all three.

Every LAG row carries an **overtake proposal**: not parity ("add regex")
but what "better by a mile" looks like. Sources: `spec/SYNTAX.md`,
`spec/SEMANTICS.md`, `spec/STDLIB.md`, `spec/PHASE7-FFI.md`,
`docs/tour.md`, `docs/stdlib.md` (all read 2026-10-06).

---

## A. Type system

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| A1 | Null safety | `Optional[X]` — unchecked at runtime | `Option<T>` — enforced, no null | Everything nilable — worst of the three | `T?` — `nil` only where declared; checker enforces (E200) | **PAR** (matches Rust; ahead of Go/Python) |
| A2 | Pattern matching | `match` (3.10+) — guards, literals, NOT exhaustive | `match` — exhaustive, guards, literals, the gold standard | `switch` / type-switch only — weakest | `match` on sum types — exhaustive (E216), arm-type agreement (E217). **No guards, no literal patterns** | **PAR** (exhaustiveness beats Go/Python; behind Rust on guards/literals → see A3) |
| A3 | Match guards / literal patterns | ✅ both | ✅ both | n/a | ❌ neither | **LAG** |
| A4 | Generics (mechanism) | `typing` — gradual, unchecked | Monomorphized + trait bounds — best | 1.18+, gcshape stenciling, no specialization | Monomorphized, call-site inference, explicit `f<T>()` pinning, depth cap 32 | **PAR** (with Go; ahead of Python; behind Rust → see A5) |
| A5 | Generic bounds (traits/interfaces) | ABCs / `Protocol` | **Traits** — the model everyone copies | Interfaces (structural) | ❌ **Nothing.** No interfaces, traits, or protocols at all | **LAG** — the single biggest type-system gap |
| A6 | Error handling | Exceptions — unchecked | `Result<T,E>` + `?` — typed, must-handle | `(T, error)` returns — routinely ignored | Catchable `Error{code,message}`, E-codes everywhere, `try/catch/throw`. Machine-readable (`--json`). **No `?` propagation, no throws-in-signature** | **PAR** (more disciplined than Go/Python; less type-level than Rust; LEAD on machine-readability for agents) |
| A7 | Type inference | (unchecked — n/a) | Good (H-M-ish locals) | `:=` | "Inference beyond this is future work" — annotations required in many positions; lambda params and generic call-sites infer | **LAG** |
| A8 | Interfaces / polymorphism | ABCs, Protocol, duck typing | Traits + dyn | Interfaces — structural, zero boilerplate | ❌ None. Only structural *function* types `(Int)->Bool` | **LAG** (same gap as A5, stated separately: it blocks both generic bounds AND everyday polymorphism) |

### Overtake proposals (type system)

- **A3 → Guard-aware exhaustiveness.** Add guards and literal patterns, but make the checker *understand* guards: exhaustiveness analysis that reasons about guard conditions instead of giving up (Rust surrenders on guards and demands `_`). `match` with guards that the checker *proves* exhaustive — better than Rust, not just equal.
- **A5/A8 → Structural interfaces, zero boilerplate.** `interface Printable { fn label(self) -> Text }` — a type conforms *by having the methods* (Go-like structural typing, no `impl` blocks), checked at compile time (Rust-like rigor), with **auto-derived conformance** for declared types (`json.stringify` works on any record via compile-time derivation — serde without a single attribute). Go needs no boilerplate but checks nothing; Rust checks everything but demands boilerplate. Misa takes both halves.
- **A6 → Checked `throws` signatures.** `fn fetch(u: Text) -> Text throws E304` — error *codes* become part of the signature; callers must handle or propagate, verified by `misa check`. Java tried this with checked exceptions and failed on verbosity; Misa avoids it three ways: (1) E-codes are a small closed set per namespace, not an open hierarchy; (2) the throws-set is *inferred* — you write it only to constrain; (3) `misa check --json` emits the full throws-graph per function, which is agent gold. No mainstream language has this. *(This is the highest-leverage proposal in this document — see §H.)*
- **A7 → Inference that explains itself.** Bidirectional local inference plus `misa check --explain`: every inferred type shown with its source span. Rust's inference is a black box; Misa's tells the agent *why*.

---

## B. Stdlib breadth

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| B1 | Regex | `re` (stdlib) | `regex` crate (best-in-class) | `regexp` (stdlib) | ❌ None | **LAG** |
| B2 | Crypto / hashing | `hashlib`, `hmac`, `secrets` | crates (`ring`, `sha2`) | `crypto/*` (stdlib) | ❌ None | **LAG** |
| B3 | Sockets / TCP | `socket` | `std::net` | `net` (stdlib, best) | ❌ None — HTTP client only | **LAG** |
| B4 | Filesystem depth | `pathlib`, `os`, `shutil` | `std::fs` | `os` | `fs.read`/`fs.write` whole-file only. No metadata, permissions, directory listing, watching | **LAG** |
| B5 | Datetime / timezones | `datetime` + `zoneinfo` | `chrono` crate | `time` (stdlib, best) | `time.now`/`now_ms`/`format`/`parse` — **UTC only**, strftime-style | **LAG** |
| B6 | Unicode normalization | `unicodedata` | `unicode-normalization` crate | `x/text` | Code-point ops only (`str.*`) | **LAG** |
| B7 | Compression | `gzip`, `zlib`, `bz2` | `flate2` crate | `compress/*` | ❌ None | **LAG** |
| B8 | Subprocess | `subprocess` (best) | `std::process` | `os/exec` | `process.run` — **interpreter only** (E600 on all AOT targets) | **LAG** (and platform-split, which is worse than missing) |
| B9 | Random | `random`, `secrets` | `rand` crate | `math/rand` | ❌ None | **LAG** |
| B10 | CLI arg parsing | `argparse` | `std::env::args` / `clap` | `flag` / `os.Args` | ❌ **No argv at all** — env-var workaround | **LAG** (flagged: blocks real CLI apps) |
| B11 | Temp files | `tempfile` | `tempfile` crate | `os.CreateTemp` | ❌ None | **LAG** |
| B12 | HTTP depth | `urllib`, `requests` | crates (`reqwest`, `hyper`) | `net/http` client **+ server** | `http.get`/`post`/`post_json` client only. No headers control, no server, 30s fixed timeout | **LAG** |

### Overtake proposals (stdlib)

- **B1 → Compile-time-checked regex.** `let re = regex(r"(\d+)-(\d+)")` — the pattern is validated by `misa check` (invalid pattern = E-code with position, not a runtime `re.error`), and capture groups are *typed* by the pattern (`re.captures(s)` → record with named/positional groups). Python and Rust both fail at runtime (or via proc-macro); Misa fails at check time.
- **B2 → Misuse-resistant crypto.** One-line audited API — `crypto.sha256(data)`, `crypto.hmac(key, data)`, `crypto.random(n)` (OS entropy), `crypto.timing_safe_eq(a, b)` — designed so the easy path is the safe path. Parity on coverage, overtake on safety ergonomics.
- **B3 → Task-native sockets.** `net.listen(":8080")` yields a `Task` per connection; backpressure through `Chan`. Go made this simple; Misa makes it *structured* — connections are child tasks, cancelled when the server scope exits. No leaked connection handlers, ever.
- **B4 → Scope-bound filesystem.** `fs.walk(dir)` as a lazy iterator, `fs.watch(path)` integrated with `spawn`/`await`, and permission-checked writes. Parity on coverage.
- **B5 → Compile-time-checked format strings.** `time.format(t, "%Y-%m-%d %Q")` — `%Q` is rejected by `misa check` with an E-code. Rust's chrono and Python's strftime both fail at *runtime* on bad specifiers. Plus a real timezone-aware `DateTime` type.
- **B6 → `str.normalize(s, "NFC")`** — parity, done right on both backends (byte-identical guarantee).
- **B7 → gzip via bundled miniz** — parity.
- **B8 → Capability-gated subprocess on AOT.** `app { allow: [process] }` — the manifest declares the capability; `misa build` embeds the grant, `misa run` prompts. Deno-like permissions, but declared at compile time and visible in `misa check --json`. Better than all three (none of which have a permission model for subprocess).
- **B9 → Dual RNGs.** `random.secure(n)` (OS entropy) + `random.seeded(seed)` (deterministic — test runs are reproducible by construction). Overtake: determinism-by-default in `misa test`.
- **B10 → Typed argv.** Declare the interface, get parsing + validation + `--help` free:
  ```misa
  args { name: Text, count: Int = 5, verbose: Bool }
  ```
  `misa check` validates the schema; bad CLI input is a catchable E-code; `--help` is generated. argparse/clap without a line of boilerplate, checked at compile time.
- **B11 → `fs.tempfile()` auto-deleted on scope exit** — structured cleanup, matching the concurrency design (no other language ties tempfiles to scopes).
- **B12 → `http.serve`.** A one-function HTTP server on the task system (`http.serve(8080, |req| { ... })`), same tiny API as the client. Server + client + structured concurrency in one coherent story — Go needs `net/http` ceremony; Python needs a framework.

---

## C. Strings

Misa has 7 `str.*` functions (`slice`, `starts_with`, `split`, `trim`,
`ord`, `chr`, `chars`) plus 9 more in pure-Misa `std/strings`
(`join`, `contains`, `repeat`, `pad_left`, `pad_right`, …).
Python's `str` has ~45 methods; Rust's `str`/`String` is comparably
rich; Go's `strings` package is solid. Missing in Misa: case
conversion, `replace`, `find`/`index`, `join` as a builtin, strip
variants beyond full trim, `format`/templating beyond interpolation,
encoding conversions.

| Rating | **LAG** |

**Overtake proposal → The Unicode-correct string library with proof.**
Complete the namespace to 40+ functions, every one verified
byte-identical across interpreter and AOT (property-tested against
Python's own semantics where they agree), *plus* compile-time-checked
interpolation: `"{user.name} has {n} messages"` type-checks the
interpolated expressions at `misa check` (E-code if `n` isn't in
scope or isn't renderable). Nobody else checks format strings at
compile time across backends — and the byte-identical guarantee is
something no multi-backend language even promises.

---

## D. Collections

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| D1 | map / filter / fold | Comprehensions, `map`/`filter`/`functools.reduce` | Iterator adaptors — best in class | `slices`/`maps` (1.21+) | `std/lists`: `range`, `any`, `all`, `sum` **only** | **LAG** |
| D2 | Sets | `set` (stdlib) | `HashSet` | Map idiom | ❌ None | **LAG** |
| D3 | Lazy iteration | Generators, itertools | Iterators (lazy, zero-cost) | Range-over-func (1.23) | ❌ None — `for` is eager only | **LAG** |

### Overtake proposals (collections)

- **D1/D3 → Fused iterators, guaranteed.** `xs.iter().map(f).filter(g).collect()` — Rust-iterator ergonomics, but the AOT compiler *guarantees* loop fusion (Rust hopes LLVM does it; Misa proves it in the emitter and the docs say so). Lazy by default, one loop in the binary, no allocation between stages.
- **D2 → `Set<T>` with literal syntax** (`{1, 2, 3}` disambiguated from records/maps by element shape) plus set algebra (`|`, `&`, `-`) as operators. Parity on coverage, overtake on literal ergonomics.

---

## E. Numbers

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| E1 | Big integers | Builtin, arbitrary precision | `num-bigint` crate | `math/big` | ❌ 64-bit wrapping (deliberate C-1 decision) | **LAG** (deliberate, but a gap for users who need it) |
| E2 | Decimal arithmetic | `decimal` (stdlib) | `rust_decimal` crate | Third-party | ❌ None | **LAG** |
| E3 | Bit operations | `& \| ^ << >>` | Full + `wrapping_*`/`checked_*` | Full | ❌ **No bitwise operators at all** | **LAG** |
| E4 | Rationals / complex | `fractions`, `cmath` | crates | n/a | ❌ None | **LAG** (minor) |

### Overtake proposals (numbers)

- **E1 → Deliberate, documented, escapable.** Keep 64-bit wrapping as the default (it's the honest systems choice and it's byte-identical across backends — already better-documented than Go's), and add `BigInt` as an explicit opt-in type for the cases that need it. Overtake: the *default* is total (no exceptions, no silent UB — unlike C) and the *escape hatch* is explicit.
- **E2 → First-class decimal literals.** `let price = 1.10d` — exact decimal arithmetic with literal syntax. Python makes you write `Decimal("1.10")` (and `Decimal(1.10)` is a famous footgun); Misa gets it right in the grammar.
- **E3 → Checked bit ops.** Add `& | ^ << >>` where shift-overflow and oversized shifts are *catchable E300*, never UB (C), never debug/release-inconsistent (Rust panics in debug, wraps in release). One consistent rule, both backends, always checked.
- **E4 →** parity via stdlib; low priority.

---

## F. FFI depth

| # | Area | Python (`ctypes`/`cffi`) | Rust (`bindgen`) | Go (`cgo`) | Misa `extern` v1 | Rating |
|---|---|---|---|---|---|---|
| F1 | Structs by value | ✅ | ✅ | ✅ | ❌ | **LAG** |
| F2 | Callbacks (Misa fn → C fn pointer) | ✅ | ✅ | ✅ (hard) | ❌ | **LAG** |
| F3 | Varargs | ✅ | ✅ | ✅ | ❌ | **LAG** |
| F4 | C++ (name mangling, classes) | n/a (C only) | ✅ via bindgen | n/a | ❌ | **LAG** |
| F5 | Declaration ergonomics | Verbose (`argtypes`/`restype` boilerplate) | Build-script ceremony | Comment directives | `extern "c" { fn strlen(s: Text) -> Int }` — cleanest of the four | **PAR** (ergonomics only) |

### Overtake proposals (FFI)

- **F1–F4 → `safe extern` + header import.** Two levels: `extern` (current, explicitly unsafe) and `safe extern`, where the checker enforces result-code checking on every call (the function *must* be used in a checked position — extending the A6 throws-signatures story into FFI). Plus `extern "mylib.h"` — automatic header binding with Misa's type mapping, bindgen-style but with zero build-script ceremony. Safe-by-default FFI with header import in the language core: nobody has this.
- **F5 →** keep the syntax; it's already the best of the four.

---

## G. Metaprogramming

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| G1 | Macros | n/a (decorators instead) | `macro_rules!` + proc macros — best | `go:generate` (weak) | ❌ None | **LAG** |
| G2 | Decorators / attributes | ✅ (pervasive) | ✅ attributes | Struct tags | ❌ None | **LAG** |
| G3 | Derive / reflection-based codegen | `dataclasses` | `#[derive(...)]` (serde!) | `encoding/json` reflection | ❌ None — no reflection at all (`reflect`/`typeof` absent) | **LAG** |

### Overtake proposals (metaprogramming)

- **G3 → Zero-attribute derivation.** Because Misa types are *declared* (not inferred), the compiler already knows every shape: `json.stringify(user)` works on any declared record, `db` row mapping derives from `table` decls, with zero attributes. Rust needs `#[derive(Serialize)]` on everything; Go needs struct tags; Python needs dataclass decorators. Misa needs *nothing* — the declaration *is* the schema. This is the single most under-exploited consequence of Misa's design: **the type declarations are already a machine-readable schema; codegen should fall out for free.** Start with JSON + DB, extend to FFI structs (F1) and mock generation.
- **G1/G2 → `comptime` evaluation.** Compile-time function execution for constants and codegen (`comptime fn`), Zig-style — simpler and more predictable than Rust proc macros, more powerful than decorators. Phase 2.

---

## H. Testing

| Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|
| Unit tests | `pytest` — best in class | `cargo test` | `go test` | ❌ **None.** `misa test` runs *model evals only* (`eval` blocks). `assert()` exists but there is no test discovery, no runner | **LAG** |

### Overtake proposal (testing)

**One runner for code *and* models.** `test "name" { … }` blocks discovered by `misa test`, running *alongside* the existing `eval` blocks (model evals), with `--json` machine-readable output for agents, deterministic replay (`--seed`), and property-style tests with model-generated inputs. Nobody else unifies unit tests and AI evals in one runner — and for an AI-native language, the test runner that also grades model behavior is the whole game. This is the highest-leverage *tooling-adjacent* fix: agents need red/green loops, and Misa's should be better than pytest's, not merely equal.

---

## I. Modules and packages

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| I1 | Module system | Packages, relative + absolute imports | Modules + crates, `use` | Packages, qualified | Flat imports (`import "./u.misa"`, lookup is `fib()` not `u.fib()`) — one spelling, no namespaces | **LAG** |
| I2 | Package manager / registry | pip + PyPI | cargo + crates.io — best | modules + proxy | `misa pkg init/add/install/list/remove` (git/path only). **No registry** (deferred to public phase) | **LAG** |

### Overtake proposals (modules/packages)

- **I1 → Qualified-on-demand imports.** Keep flat-by-default (it's genuinely nicer for small programs and agents), add `import "./u.misa" as u` for the collision cases. Flat when you want it, qualified when you need it — neither Python's mandatory packages nor Go's forced qualification.
- **I2 → Content-addressed, offline-first registry.** Hash-locked builds (`misa.lock` with content hashes, not version ranges), vendoring by default, reproducible builds verified by `misa check`. Stronger reproducibility story than cargo; PyPI can't touch this.

---

## J. Where Misa already leads (for balance)

These are not gaps — they are the moat. Any overtake plan should
*extend* them, not merely defend them.

| Area | Status |
|---|---|
| One file → native CLI + APK + iOS + web | **LEAD** — nobody else does this (measured in `bench/COMPARISONS.md`) |
| Typed model calls with budgets (`model` decls, `E500`) | **LEAD** — unique |
| Machine-readable errors (`misa check --json`, E-codes with positions) | **LEAD** for agent consumers |
| Structured + typed + budgeted concurrency (`spawn`/`Task<T>`/`await t timeout ms`) | **LEAD** on design (no leaked tasks, no silent failures) |
| Exhaustive `match` (E216) | **LEAD** vs Go/Python; PAR vs Rust |
| `T?` null safety | **PAR** with Rust; ahead of Go/Python |
| Tree-shaken binaries (172KB hello — undercuts Rust 337KB, Go 2.1MB, measured) | **LEAD** (measured) |
| Compile-time-checked SQL (`table` decls, E224) | **LEAD** — approaching the ambition of this document's proposals |

---

## Scorecard

| Rating | Count |
|---|---|
| **LAG** | 34 |
| **PAR** | 7 |
| **LEAD** | 7 |

(34 LAG rows: A3, A5, A7, A8, B1–B12, C, D1–D3, E1–E4, F1–F4, G1–G3,
H, I1, I2. PAR: A1, A2, A4, A6, F5, `T?` null safety, +1 partial.
LEAD: the 7 LEAD rows in §J.)

---

## Top 5 lags by importance

1. **No interfaces/traits (A5/A8).** Blocks all abstraction at scale — every stdlib and ecosystem API design hits this wall. Without it, `std/` packages can't offer polymorphic APIs and agent-written libraries will duplicate code.
2. **Stdlib breadth (B1–B12).** The daily "can I build real things?" test. Agents reach for regex, crypto, sockets, timezones, random, and argv constantly; each missing piece is a failed task or an FFI escape hatch.
3. **No unit-test framework (H).** `misa test` only runs model evals. The quality story is half-built, and agents need red/green loops — this is also the highest-leverage tooling-adjacent fix (one runner for code *and* models).
4. **FFI depth (F1–F3).** Structs by value and callbacks block wrapping real C libraries — which is the entire point of FFI. (Postgres-via-FFI, the deferred Phase 5 item, needs F1.)
5. **Thin strings/collections API (C, D1–D3).** Shows up in literally every program; the benchmark papercuts live here. Lowest risk to fix, highest daily friction removed.

---

## Highest-leverage overtake proposal

**Checked `throws` signatures** (A6 overtake):

```misa
fn fetch_user(id: Int) -> User throws E303, E304 {
    let row = db.query(db, "SELECT ...")   // throws E303
    let avatar = http.get(url)             // throws E304
    ...
}
// callers must try/catch or declare throws — verified by misa check
```

Why this one, above all others:

1. **Nobody has it.** Rust's `Result<T,E>` forces you to define error enums and still doesn't track *which* errors; Go and Python are unchecked; Java tried checked exceptions and failed on verbosity. Misa's version avoids Java's failure mode three ways: E-codes are a *small closed set per namespace* (not an open hierarchy), the throws-set is *inferred* (you write it only to constrain), and `misa check --json` emits the full throws-graph per function.
2. **It compounds existing moats.** The E-code system, machine-readable diagnostics, and the agent audience multiply each other: the compiler literally tells the agent which failure modes to handle. "Fewer failure modes" stops being a benchmark observation and becomes a *compile-time guarantee*.
3. **It extends to FFI** (`safe extern` in §F) and **to model calls** (budgets already work this way — `E500` is the precedent).
4. **It's cheap.** The checker already tracks which E-codes each call site can raise (it emits them); threading that set into signatures is bookkeeping, not research.

This is the proposal that turns Misa's biggest philosophical bet —
*errors are structured data, not control flow* — into its biggest
technical moat.
