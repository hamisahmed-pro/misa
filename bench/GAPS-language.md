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
| A5 | Generic bounds (traits/interfaces) | ABCs / `Protocol` | **Traits** — the model everyone copies | Interfaces (structural) | ✅ `interface` + `impl` blocks (2026-10-06): nominal, static zero-cost + dynamic + bounded-generic (`T: Drawable`) dispatch, throws ⊆ rule | **PAR** (matches the model; less mature than Rust's ecosystem of impls) |
| A6 | Error handling | Exceptions — unchecked | `Result<T,E>` + `?` — typed, must-handle | `(T, error)` returns — routinely ignored | ✅ **Checked `throws` signatures** (2026-10-06): `fn f() -> T throws E304` — error *codes* in the signature, enforced by `misa check` (E227), inferred throws-sets, `throws_graph` in `--json`. **Nobody else has this.** | **LEAD** |
| A7 | Type inference | (unchecked — n/a) | Good (H-M-ish locals) | `:=` | "Inference beyond this is future work" — annotations required in many positions; lambda params and generic call-sites infer | **LAG** |
| A8 | Interfaces / polymorphism | ABCs, Protocol, duck typing | Traits + dyn | Interfaces — structural, zero boilerplate | ✅ `interface`/`impl` (2026-10-06) — heterogeneous `[Drawable]` collections dispatch dynamically | **PAR** |

### Overtake proposals (type system)

- **A3 → Guard-aware exhaustiveness.** Add guards and literal patterns, but make the checker *understand* guards: exhaustiveness analysis that reasons about guard conditions instead of giving up (Rust surrenders on guards and demands `_`). `match` with guards that the checker *proves* exhaustive — better than Rust, not just equal.
- **A5/A8 → Structural interfaces, zero boilerplate.** `interface Printable { fn label(self) -> Text }` — a type conforms *by having the methods* (Go-like structural typing, no `impl` blocks), checked at compile time (Rust-like rigor), with **auto-derived conformance** for declared types (`json.stringify` works on any record via compile-time derivation — serde without a single attribute). Go needs no boilerplate but checks nothing; Rust checks everything but demands boilerplate. Misa takes both halves.
- **A6 → Checked `throws` signatures.** `fn fetch(u: Text) -> Text throws E304` — error *codes* become part of the signature; callers must handle or propagate, verified by `misa check`. Java tried this with checked exceptions and failed on verbosity; Misa avoids it three ways: (1) E-codes are a small closed set per namespace, not an open hierarchy; (2) the throws-set is *inferred* — you write it only to constrain; (3) `misa check --json` emits the full throws-graph per function, which is agent gold. No mainstream language has this. *(This is the highest-leverage proposal in this document — see §H.)*
- **A7 → Inference that explains itself.** Bidirectional local inference plus `misa check --explain`: every inferred type shown with its source span. Rust's inference is a black box; Misa's tells the agent *why*.

---

## B. Stdlib breadth

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| B1 | Regex | `re` (stdlib) | `regex` crate (best-in-class) | `regexp` (stdlib) | ✅ `regex("...")` (2026-10-07): **patterns validated at check time** (E200 with index, never a runtime surprise); 988 differential checks vs Python `re`, 0 mismatches | **LEAD** (nobody checks patterns at compile time) |
| B2 | Crypto / hashing | `hashlib`, `hmac`, `secrets` | crates (`ring`, `sha2`) | `crypto/*` (stdlib) | ✅ `crypto.sha256/sha1/md5/hmac_sha256` (2026-10-07, self-contained C, no OpenSSL dep) | **PAR** |
| B3 | Sockets / TCP | `socket` | `std::net` | `net` (stdlib, best) | ✅ `net.*`/`socket.*` (Wave 3, 2026-10-07): connect/listen/accept/send/recv/close/set_timeout, method + function style, `spawn`-native, E304 | **PAR** |
| B4 | Filesystem depth | `pathlib`, `os`, `shutil` | `std::fs` | `os` | `fs.read`/`fs.write` whole-file only. No metadata, permissions, directory listing, watching | **LAG** |
| B5 | Datetime / timezones | `datetime` + `zoneinfo` | `chrono` crate | `time` (stdlib, best) | ✅ `time.now_utc/to_utc/from_parts/add/in_tz/weekday/is_leap` (2026-10-07) — **offset-based, no tz database** | **PAR** (coverage; behind on named zones/DST) |
| B6 | Unicode normalization | `unicodedata` | `unicode-normalization` crate | `x/text` | Code-point ops only (`str.*`) | **LAG** |
| B7 | Compression | `gzip`, `zlib`, `bz2` | `flate2` crate | `compress/*` | ✅ `compress.*` (Final wave, 2026-10-07): gzip/gunzip/gunzip_bytes + streaming file variants, new `Bytes` type (`len`, `==`, `<bytes: N>` rendering), corrupt input = catchable `E300`, byte-identical output on both backends (deterministic framing, verified vs `gzip.compress`) | **PAR** |
| B8 | Subprocess | `subprocess` (best) | `std::process` | `os/exec` | `process.run` — **interpreter only** (E600 on all AOT targets) | **LAG** (and platform-split, which is worse than missing) |
| B9 | Random | `random`, `secrets` | `rand` crate | `math/rand` | ✅ `random.seed/int/float/choice/shuffle` (2026-10-07; determinism per-backend) | **PAR** |
| B10 | CLI arg parsing | `argparse` | `std::env::args` / `clap` | `flag` / `os.Args` | ✅ `cli` module (2026-10-07): declarative typed schema (`cli.parser`/`flag`/`bool_flag`/`short`/`command`), `parse(argv()) -> CliArgs? throws E300`, auto-generated `--help` (byte-identical both backends), catchable E300 with usage hints, check-time E200 for bad literals, checked-throws E227 | **LEAD** (checked schema + typed errors; argparse/clap are runtime-only) |
| B11 | Temp files | `tempfile` | `tempfile` crate | `os.CreateTemp` | ✅ `fs.temp_file()/temp_dir()` (2026-10-07) | **PAR** |
| B12 | HTTP depth | `urllib`, `requests` | crates (`reqwest`, `hyper`) | `net/http` client **+ server** | ✅ `http.serve` (Wave 3, 2026-10-07): raw-socket HTTP/1.1 server, Request/Response records, per-connection error isolation. Client still `http.get`/`post`/`post_json` only (no headers control, 30s fixed timeout) | **PAR** |

### Overtake proposals (stdlib)

- **B1 → Compile-time-checked regex.** `let re = regex(r"(\d+)-(\d+)")` — the pattern is validated by `misa check` (invalid pattern = E-code with position, not a runtime `re.error`), and capture groups are *typed* by the pattern (`re.captures(s)` → record with named/positional groups). Python and Rust both fail at runtime (or via proc-macro); Misa fails at check time.
- **B2 → Misuse-resistant crypto.** One-line audited API — `crypto.sha256(data)`, `crypto.hmac(key, data)`, `crypto.random(n)` (OS entropy), `crypto.timing_safe_eq(a, b)` — designed so the easy path is the safe path. Parity on coverage, overtake on safety ergonomics.
- **B3 → Task-native sockets.** `net.listen(":8080")` yields a `Task` per connection; backpressure through `Chan`. Go made this simple; Misa makes it *structured* — connections are child tasks, cancelled when the server scope exits. No leaked connection handlers, ever.
- **B4 → Scope-bound filesystem.** `fs.walk(dir)` as a lazy iterator, `fs.watch(path)` integrated with `spawn`/`await`, and permission-checked writes. Parity on coverage.
- **B5 → Compile-time-checked format strings.** `time.format(t, "%Y-%m-%d %Q")` — `%Q` is rejected by `misa check` with an E-code. Rust's chrono and Python's strftime both fail at *runtime* on bad specifiers. Plus a real timezone-aware `DateTime` type.
- **B6 → `str.normalize(s, "NFC")`** — parity, done right on both backends (byte-identical guarantee).
- **B7 → gzip via zlib with deterministic framing** — DONE (Final wave, 2026-10-07): `compress.gzip/gunzip/gunzip_bytes` + streaming `gzip_file`/`gunzip_file`, new `Bytes` type, corrupt input = catchable `E300`, byte-identical output on both backends (fixed header, level 9 — verified against CPython's `gzip.compress`). Chose zlib over bundled miniz (universal incl. Android NDK; clean E600 where absent). **PAR** — beyond the original proposal: the `Bytes` type and byte-identity guarantee are the overtake-flavored parts.
- **B8 → Capability-gated subprocess on AOT.** `app { allow: [process] }` — the manifest declares the capability; `misa build` embeds the grant, `misa run` prompts. Deno-like permissions, but declared at compile time and visible in `misa check --json`. Better than all three (none of which have a permission model for subprocess).
- **B9 → Dual RNGs.** `random.secure(n)` (OS entropy) + `random.seeded(seed)` (deterministic — test runs are reproducible by construction). Overtake: determinism-by-default in `misa test`.
- **B10 → Typed argv — DONE (2026-10-07, now LEAD).** The `cli` module delivers the proposal: declare the interface, get parsing + validation + `--help` free:
  ```misa
  let p = cli.parser("prog", "Does things")
  p.flag("name", "NAME", "Who to greet", "world")
  p.bool_flag("verbose", "Verbose output")
  let args = p.parse(argv())  // throws E300; nil after --help
  ```
  `misa check` validates literal schemas (E200); bad CLI input is a catchable E300 with usage hints; `--help` is generated byte-identically on both backends. argparse/clap without a line of boilerplate, checked at compile time.
- **B11 → `fs.tempfile()` auto-deleted on scope exit** — structured cleanup, matching the concurrency design (no other language ties tempfiles to scopes).
- **B12 → `http.serve`.** A one-function HTTP server on the task system (`http.serve(8080, |req| { ... })`), same tiny API as the client. Server + client + structured concurrency in one coherent story — Go needs `net/http` ceremony; Python needs a framework.

---

## C. Strings

Misa has 7 `str.*` functions (`slice`, `starts_with`, `split`, `trim`,
`ord`, `chr`, `chars`) plus 9 more in pure-Misa `std/strings`
(`join`, `contains`, `repeat`, `pad_left`, `pad_right`, …).
Python's `str` has ~45 methods; Rust's `str`/`String` is comparably
rich; Go's `strings` package is solid. Misa now has ~26 (`str.*`:
`slice`, `starts_with`, `ends_with`, `split`, `join`, `replace`,
`trim`, `to_upper/to_lower` (ASCII), `pad_left/pad_right`, `ord`,
`chr`, `chars`, plus 9 in `std/strings`). Still missing: case
conversion beyond ASCII, `find`/`index`, strip variants, format/
templating beyond interpolation, encoding conversions.

| Rating | **LAG** (narrowed 2026-10-07: was 7 builtins, now ~26) |

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
| D1 | map / filter / fold | Comprehensions, `map`/`filter`/`functools.reduce` | Iterator adaptors — best in class | `slices`/`maps` (1.21+) | ✅ `map/filter/fold/sort_by/flat/zip/unique` (2026-10-07) | **PAR** (eager; no lazy iterators → D3) |
| D2 | Sets | `set` (stdlib) | `HashSet` | Map idiom | ✅ `Set<T>` (2026-10-07): add/contains/remove/union/intersect/diff/to_list; O(n) membership, insertion-ordered | **PAR** (semantics; behind on hashed performance) |
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
| F1 | Structs by value | ✅ | ✅ | ✅ | ✅ (Wave 2, 2026-10-06; final wave 2026-10-07: + nested structs, unions, bitfields with C layout) | **LEAD** (cleanest syntax; byte-exact layout differentially verified vs GCC in-suite; dual-backend byte-identical) |
| F2 | Callbacks (Misa fn → C fn pointer) | ✅ | ✅ | ✅ | ✅ (Wave 2, 2026-10-06) | **PAR** (typed closures, no unsafe blocks; easier than Rust/Go) |
| F3 | Varargs | ✅ | ✅ | ✅ | ✅ (Wave 2, 2026-10-06) | **PAR** |
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
| Unit tests | `pytest` — best in class | `cargo test` | `go test` | ✅ `misa test` (2026-10-06): discovers `test NAME {}` blocks + `eval` model blocks in one runner, per-test isolation, `assert_eq`/`assert_ne`, `--json` | **PAR** (pytest more mature; the code+evals unification is unique) |

### Overtake proposal (testing)

**One runner for code *and* models.** `test "name" { … }` blocks discovered by `misa test`, running *alongside* the existing `eval` blocks (model evals), with `--json` machine-readable output for agents, deterministic replay (`--seed`), and property-style tests with model-generated inputs. Nobody else unifies unit tests and AI evals in one runner — and for an AI-native language, the test runner that also grades model behavior is the whole game. This is the highest-leverage *tooling-adjacent* fix: agents need red/green loops, and Misa's should be better than pytest's, not merely equal.

---

## I. Modules and packages

| # | Area | Python | Rust | Go | Misa today | Rating |
|---|---|---|---|---|---|---|
| I1 | Module system | Packages, relative + absolute imports | Modules + crates, `use` | Packages, qualified | Flat imports (`import "./u.misa"`, lookup is `fib()` not `u.fib()`) — one spelling, no namespaces | **LAG** |
| I2 | Package manager / registry | pip + PyPI | cargo + crates.io — best | modules + proxy | `misa pkg` full registry client (publish/add/install/search/yank/audit/vendor, semver, hash-locked `misa.lock` v2) + Misa-written registry server (`/v1/` REST, typed semantic search, throws indexing, verified capabilities, computed breaking changes). Deploying to Render free tier (ephemeral SQLite mitigated by seed script + admin backup). | **PAR** |

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

## Scorecard (updated 2026-10-07 — post Wave 1–3 + final wave)

| Rating | Count | Rows |
|---|---|---|
| **LAG** | 17 | A3, A7, B4, B6, B8, C, D3, E1–E4, F4, G1–G3, I1, I2 |
| **PAR** | 19 | A1, A2, A4, A5, A8, B2, B3, B5, B7, B9, B10, B11, B12, D1, D2, F2, F3, F5, H |
| **LEAD** | 11 | A6 (checked throws), B1 (check-time regex), F1 (FFI structs: nested/unions/bitfields, GCC-verified), + the 8 moat rows in §J |

(Original 2026-10-06: 34 LAG / 7 PAR / 7 LEAD. Flipped since: 12 LAG→PAR,
1 LAG→LEAD, 2 PAR→LEAD. Current: **16 LAG / 20 PAR / 11 LEAD** —
I2 (registry) flipped to PAR with the v1 build.)

---

## Top lags by importance (updated 2026-10-07)

1. **Ecosystem size (I2's count, not its design).** The registry is
   BUILT (`misa pkg` client + Misa-written server, typed semantic
   search, verified capabilities, computed breaking changes —
   design-wise ahead of the incumbents); the package COUNT is still
   ~0 vs 500k/2M. That gap closes with time and the content flywheel,
   not more code.
2. **Metaprogramming (G1–G3).** No macros, no attributes, no
   derivation. The zero-attribute-derivation proposal (G3) is still
   the highest-leverage unbuilt language feature: `json.stringify`
   works on any declared record with no attributes.
3. **Numbers (E1–E4).** No bigint/decimal/bitops. Deliberate for E1
   (64-bit wrapping), but E3 (bitwise ops) is a real gap for
   systems-adjacent code.
4. **Filesystem depth (B4) + subprocess on AOT (B8).** `fs` is
   whole-file only; `process.run` is interpreter-only. Both block
   real-world scripting/CLI apps on native binaries.
5. **Type inference (A7) + match guards (A3).** Inference is still
   annotation-heavy; match still lacks guards/literals. Daily
   ergonomics gaps vs Rust.

Honorable mentions (narrowed but still LAG): C (strings ~26/45),
I1 (flat imports, no namespaces), B6 (unicode normalization),
D3 (no lazy iteration), F4 (no C++).

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
