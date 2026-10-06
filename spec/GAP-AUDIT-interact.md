# GAP-AUDIT — workstream 2: feature-interaction matrix + spec-vs-implementation (2026-10-06)

**Scope:** stress-test feature *combinations* (closures/5a, sums+match/5b,
generics/5c, db, FFI, json, str, time, process) on **both** backends
(`misa run` interpreter vs `misa build` AOT), plus verify every concrete
promise in `spec/PHASE5-TYPES.md`, `spec/PHASE5-DB.md`,
`spec/PHASE5-STDLIB2.md`, `spec/PHASE7-FFI.md`, `spec/STDLIB.md`.

**Method:** 25 small `.misa` files (`/tmp/interact/i01.misa`…`i25.misa`),
each run through `misa run` and `misa build` + execute; every spec
example and error code exercised directly. Nothing was fixed; `selfhost/`
untouched.

**Environment:** Linux x86-64 VM, `python3 impl/bin/misa` (v0.3).
Complements `spec/GAP-AUDIT-parent.md` (int overflow, div-by-zero,
`{{`/`}}` asymmetry) — findings here do not duplicate it.

---

## CRITICAL

### C-1: AOT `time.parse` segfaults (exit 139)

Minimal reproduction (`/tmp/specp/tb1.misa`):
```misa
let e = time.parse("2026-10-06 12:00:00", "%Y-%m-%d %H:%M:%S")
print(e)
```
- **Expected:** epoch seconds, exit 0 (spec STDLIB.md 3f: `time.parse(s, fmt) -> Int`).
- **Actual interp:** `1791288000`, exit 0 ✓
- **Actual AOT:** `Segmentation fault`, exit **139** ✗
- Bisected: `time.now()`, `time.now_ms()`, `time.format(...)` all fine on
  AOT; `time.parse` alone crashes. Invalid input is handled
  (`E300: time.parse: bad time string` on AOT), so the crash is in the
  success path — likely the C `strptime`/`mktime` wrapper.
- **Component:** AOT C runtime (time support, `misa_time_parse` area).

---

## HIGH

### H-1: `db.exec`/`db.query` args — checker passes, interp works, AOT crashes (three-way inconsistency)

Reproduction (`/tmp/specp/dbdiv.misa`):
```misa
let db = db.open(":memory:")
db.exec(db, "CREATE TABLE t (name TEXT, n INTEGER)")
db.exec(db, "INSERT INTO t (name, n) VALUES (?, ?)", ["x", 11])
print(db.query(db, "SELECT n FROM t")[0].n)
db.close(db)
```
- `misa check`: **OK**
- Interp: prints `11`, exit 0
- AOT: `<unknown>: E200: expected Text`, exit 1

Three independent defects combine:
1. **Checker unsound:** declared signature is `args: [Text]?`
   (`impl/misa/checker.py:50`), but a *heterogeneous* list literal
   infers `[unknown]` (`checker.py`: `inner = ts.pop() if len(ts)==1 else
   "unknown"`), and `[unknown]` is assignable to `[Text]`. So
   `["x", 11]` passes while `[7]` fails `E211: 'db.exec' wants [Text],
   got [Int]` — enforcement depends on list *content*, not the rule.
   Spec PHASE5-DB.md claims "the checker enforces scalar args" — false.
2. **Interp lenient:** `interp.py db_exec` passes args straight to
   Python sqlite3, which binds `Int`→INTEGER, `Float`→REAL natively.
3. **AOT strict:** `misa_db.c db_bind_args` calls `misa_text_z(a)` on
   every arg → `E200: expected Text` for any non-Text value.

Net: a program that passes `misa check` and runs correctly on the
interpreter **crashes on AOT**. The "identical on interpreter and AOT"
exit criteria (PHASE5-DB.md, PHASE5-STDLIB2.md) are broken for any
non-Text bind arg. (Interaction test **i06** — FFI `strlen` result into
`INSERT` — hits exactly this.)
- **Components:** `impl/misa/checker.py` (list-literal `[unknown]`
  bypass), `impl/misa/interp.py` (`db_exec`/`db_query`), 
...[truncated 14266 chars]
### H-2: AOT `json.stringify` leaks sum-type internals instead of throwing

Reproduction (`/tmp/interact/i15.misa`):
```misa
type Shape { Circle(radius: Float), Rect(w: Float, h: Float) }
let s: Shape = Shape.Circle(1.0)
let r = {"shape": s, "n": 3}
print(json.stringify(r))
```
- **Expected:** clean `E300` (spec STDLIB.md 3e: throws for non-JSON values).
- **Actual interp:** `4:11: E300: json.stringify: value of type SumValue is not JSON-representable` ✓
- **Actual AOT:** `{"shape":{"__tag":0,"__fields":[1.0],"__sum_disp":"Shape.Circle"},"n":3}`, exit 0 ✗

Root cause: on AOT, sum values are heap records tagged
`__tag`/`__fields`/`__sum_disp`. `js_value()` in
`impl/misa/aot/rt/misa_json.c` has a `M_TAG_RECORD` case that rejects
closures by scanning for a `__closure_fn` field — but has **no**
equivalent `__sum_disp` exclusion, so sums serialize as plain objects,
leaking the runtime's internal representation into user-visible JSON.
Silent data corruption + backend divergence + internal-name leak.
(`print`/`str` of sums are fine on both — `Shape.Circle(1.0)` — so the
hole is specific to `json.stringify`.)
- **Component:** `impl/misa/aot/rt/misa_json.c` (`js_value`, `M_TAG_RECORD` case).

### H-3: `process.run` missing from the AOT backend entirely

Reproduction (`/tmp/interact/i21.misa`):
```misa
let r = process.run(["echo", "{{\"a\": 1}"])
let v = json.parse(str.trim(r.stdout))
print(v.a)
print(r.code)
```
- Interp: `1`, `0`, exit 0 ✓
- `misa check`: **OK**
- `misa build`: `1:16: E101: undefined function 'process.run'`, build fails ✗

STDLIB.md §3c documents `process.run` as a Phase 6 prerequisite —
"Everything here is available with no import" — but
`impl/misa/aot/emit.py` contains **zero** references to `process.run`
(verified: every other documented builtin — `fs.read/write`,
`http.get/post/post_json`, `env`, `assert` — has an emitter branch).
Consequences:
- Any program using `process.run` passes `misa check` but cannot be
  AOT-built (check/build inconsistency; the E101 message is also
  misleading — the function *is* defined, just not for this target;
  E600 would be honest).
- `selfhost/build.misa` (Phase 6b AOT driver) calls
  `process.run(["clang", ...])` — the self-hosted AOT driver **cannot
  itself be AOT-compiled**.
- **Component:** `impl/misa/aot/emit.py` (missing lowering).

### H-4: Float printing diverges — spec's own example is not byte-identical

- `print(1.0 / 3.0)` → interp `0.3333333333333333` | AOT `0.33333333333333331`
- PHASE5-TYPES.md §5b spec example (`Shape.Circle(1.0)`, `r * 3.14`) →
  interp `3.14` | AOT `3.1400000000000001`

Interp uses shortest-round-trip repr; AOT uses `%.17g`. The Phase 5 exit
criterion — "Spec examples above run identically on interpreter and AOT
(byte-identical stdout/exit/errors)" — is **literally broken by the
spec's own Shape example**. Any float-rendering test or snapshot is
backend-dependent.
- **Component:** AOT C runtime float formatting.

---

## MEDIUM

### M-1: `json.parse` throws E300 on out-of-int64 integers; spec says Float

STDLIB.md §3e: "Numbers become `Int` when they have no fraction or
exponent **and fit in int64, otherwise `Float`**."
```misa
print(json.parse("9223372036854775808"))
```
- **Expected:** `9.223372036854776e18` (Float).
- **Actual (both backends):** `E300` ("integer 9223372036854775808 out of
  Int range" / AOT: "invalid JSON: integer out of Int range" — also
  misleading, since the input *is* valid JSON).
- Consistent across backends, but contradicts the written spec. Either
  the spec or both implementations need to change.
- **Components:** `impl/misa/interp.py` (json parse), `impl/misa/aot/rt/misa_json.c`.

### M-2: `extern` fn as a value → E101 (spec: "registered like fn decls")

PHASE7-FFI.md: "`extern` decls registered like `fn` decls" — and 5a
promises `let g = fib` works for `fn` decls.
```misa
extern "c" {
  fn strlen(s: Text) -> Int,
}
let f = strlen
print(f("hello"))
```
- **Expected:** works like `let g = fib`, or at worst E214 (builtins-as-values).
- **Actual (both):** `4:9: E101: undefined name 'strlen'` (+ cascade E215).
  Extern fns are callable but absent from the value namespace.
- **Component:** `impl/misa/checker.py` (name resolution for extern decls).

### M-3: `env()` on unset var raises E300; spec promises E302

STDLIB.md §1: "`env(name: Text) -> Text` — Environment variable; **error
E302 if unset**." Actual on both backends: `E300: env var 'X' is not
set`. `E302` is **never raised anywhere** in the codebase
(zero hits in `interp.py`/`emit.py`/`checker.py`). Dead spec code.
- **Components:** `impl/misa/interp.py`, `impl/misa/aot/emit.py`.

### M-4: PHASE5-DB.md's own code examples do not run (method-style calls)

The spec's API section shows:
```misa
let db = db.open("app.db")
db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
db.exec("INSERT INTO users (name) VALUES (?)", ["Hamis"])
let rows = db.query("SELECT id, name FROM users")
...
db.close()
```
Every one of these fails against the implementation, which requires the
handle as an explicit first argument (`db.exec(db, ...)`,
`db.query(db, ...)`, `db.close(db)`); method-style yields
`E102: 'db.exec' takes 2-3 args, got 1` / `E211: 'db.exec' wants Db, got
Text`. STDLIB.md documents the real signature. **The spec's primary
example is unrunnable.** (Spec bug, not implementation bug — but anyone
implementing from PHASE5-DB.md alone builds the wrong API.)

### M-5: `examples/sums.misa` missing

PHASE5-TYPES.md exit criteria: "`examples/` gains `generics.misa`,
`sums.misa`, `closures.misa`." `generics.misa` ✓, `closures.misa` ✓,
**`sums.misa` does not exist**. (`json_demo.misa` ✓, `diary.misa` ✓.)

### M-6: check/build inconsistency (generalizing H-3)

`misa check` → OK, `misa build` → fail happens for `process.run`
(E101) and would happen for anything else the emitter lacks; E223
(monomorphization depth) likewise fires only at build time. The
checker's "OK" is not a reliable gate for AOT compilation.

---

## LOW / observations

- **L-1 — Unreachable-arm warning missing.** Spec 5b: "Unreachable-arm
  detection is a warning, not an error (v1)." No warning is emitted at
  all: `match o { Opt.None => 1, _ => 2, Opt.Some(n) => n }` checks OK
  silently. Missing feature, harmless.
- **L-2 — E226 symbol-not-found is load-time, not check-time.**
  `misa check` says OK for `extern "c" { fn no_such_symbol_xyz... }`;
  the E226 surfaces when the program loads (`run`/`build`). Spec allows
  "load or call time", so compliant — but `misa check` gives false
  confidence for extern programs.
- **L-3 — FFI Int↔long footgun (spec-inherent).** The mapping is
  Int→C `long`. Declaring a C function that actually returns `int`
  (e.g. `strcmp`) as `-> Int` yields garbage for negatives
  (`strcmp("abc","abd")` → `4294967295`, x86-64 zero-extension of EAX).
  Verified with true-`long` functions (`atol("-5")` → `-5`,
  `labs(-42)` → `42` on both backends) that the mapping itself is
  correct — the hazard is declaring the wrong C width, which "check
  verifies signatures, not semantics" explicitly disclaims. Document
  prominently.
- **L-4 — `<unknown>` source locations.** Interp runtime errors for
  top-level builtin calls (`str.chr`, `str.split`, `process.run`,
  `env`) report `<unknown>:` instead of `line:col`; AOT reports proper
  locations (`1:7:`). Minor diagnostic divergence.
- **L-5 — `json.stringify` error message differs:** interp names the
  offending type ("value of type LambdaValue..."), AOT says only
  "value is not JSON-representable". Cosmetic.
- **L-6 — dynamic↔generic seam.** `first(v)` where `v = json.parse(...)`
  (dynamic) → E219 "cannot infer type argument"; requires explicit
  `first<Int>(v)` or `let v: [Int] = json.parse(...)`. Acceptable per
  E219's design, but `json.parse` → generics always needs the
  annotation/pin two-step.

---

## Appendix A — Interaction matrix (25 tests)

Files: `/tmp/interact/i01.misa` … `i25.misa`. "✓" = correct output on
both backends. Expected behavior was reasoned out before running.

| # | Combination | Interp | AOT | Notes |
|---|---|---|---|---|
| 01 | Closure capturing a generic value (`fn wrap<T>`) | ✓ `42`,`hi` | ✓ | |
| 02 | Generic fn returning a closure (`-> () -> T`) | ✓ `7`,`seven` | ✓ | `-> (() -> T)` parens rejected by parser (E001); bare `-> () -> T` works |
| 03 | `match` on generic sum `Result<T>` | ✓ | ✓ | `Result.Err("bad")` needs annotation or `Result<Text>.Err` (E219 by design) |
| 04 | Sum type with closure payload `Cb.F((Int)->Int)` | ✓ `42`,`-1` | ✓ | |
| 05 | match ⊂ closure ⊂ generic fn (3-level) | ✓ `a`,`99` | ✓ | |
| 06 | FFI `strlen` → db INSERT → query back | ✓ `11` | ✗ **H-1** | `<unknown>: E200: expected Text` |
| 07 | `json.parse` → generic `first<T>` | ✓ `10`,`7` | ✓ | needs `first<Int>(v)` pin or `[Int]` annotation (E219) |
| 08 | `db.query` rows mapped through capturing closure | ✓ | ✓ | `\|r\|` unannotated → E213 by design; annotated `\|name: Text\|` works |
| 09 | Generic sum db round-trip (serialize→text→parse→match) | ✓ `42` | ✓ | needs `Result<Int>.Ok/Err` explicit type args |
| 10 | `str.*` on `json.parse` unicode strings | ✓ | ✓ | slice/len code-point-correct both |
| 11 | Closure counter stored in list, called later | ✓ `1,2,3,3` | ✓ | shared mutable capture works on AOT too |
| 12 | `match` wildcard + generic + closure arm bodies | ✓ `12.56`,`0.0` | ✓ | |
| 13 | Recursive generic sum `Tree<T>` + recursive fn | ✓ `6` | ✓ | no E223 — self-referential, not infinitely nesting |
| 14 | FFI `getenv` → `db.open` path | ✓ | ✓ | |
| 15 | `json.stringify` record containing a sum | E300 clean | ✗ **H-2** | AOT leaks `{"__tag":0,"__fields":[1.0],"__sum_disp":"Shape.Circle"}` |
| 16 | `json.stringify` of a closure | E300 clean | E300 clean | messages differ (L-5) |
| 17 | `time.now()` → db → read back → `time.format` | ✓ `true`,`2026-10-06` | ✓ | |
| 18 | Generic `Box<T>` holding FFI `Ptr` (malloc/free) | ✓ | ✓ | |
| 19 | `match` arm binding shadows outer var | ✓ `6`,`outer` | ✓ | shadowing scoped to arm, correct |
| 20 | json.parse → closure filter → db.exec → query | ✓ `2,apple,fig` | ✓ | |
| 21 | `process.run` output parsed as JSON | ✓ `1`,`0` | ✗ **H-3** | build: `E101: undefined function 'process.run'` |
| 22 | `str.split` on db TEXT with unicode separator `‖` | ✓ `3`,`γδ` | ✓ | |
| 23 | `table` decls + `db.query` with JOIN | ✓ `1`,`ada` | ✓ | checker ignores JOIN (deferred per spec), no crash |
| 24 | `extern` fn as Misa value (`let f = strlen`) | ✗ **M-2** | ✗ **M-2** | `E101: undefined name 'strlen'` both backends |
| 25 | Two generic instantiations, one program, AOT | ✓ `1`,`a`,`1.5` | ✓ | no monomorphization collision |

**Interaction matrix score: 20/25 clean on both backends; 5 findings
(H-1, H-2, H-3, M-2, plus i02's parser paren quirk noted).**

---

## Appendix B — Spec-compliance table

Promise → status (**ok** / **broken** / **partial** / **missing** /
**untested**), with evidence. Quotes are verbatim from the spec.

### spec/PHASE5-TYPES.md

| Promise | Status | Evidence |
|---|---|---|
| Function types `(Int, Text) -> Bool`; `() -> Void` thunks | ok | i02 (`-> () -> T`) runs both backends |
| Lambda `\|x: Int\|`; inference from expected type; E-code when neither | ok | E213 verified: `cannot infer type of lambda parameter 'x'` |
| `let g = fib` — any `fn` usable as a value | ok | `fib(10)` via alias → `55` |
| Closures capture defining scope (interp Env / AOT closure-convert) | ok | i01, i11 (mutable capture shared across calls, both backends) |
| Arity/type mismatch at call sites → E102/E211 | ok | E211 observed repeatedly (e.g. db arg tests) |
| "`str`, `int`, etc. remain builtins, not values (no `let f = print`)" | ok | E214: `'print' is a builtin and cannot be used as a value` |
| Sum syntax `type Shape {...}`, `Shape.Circle(1.0)`, `match` expr, `=>` arms | ok | spec example runs verbatim (modulo H-4 float printing) |
| Exhaustiveness → new E-code without `_` | ok | E216: `non-exhaustive match on 'Shape'; missing: Rect` |
| "Unreachable-arm detection is a warning, not an error (v1)" | **missing** | L-1: no warning emitted at all |
| Fieldless variants `type Opt { None, Some(Int) }` — `Opt.None` | ok | used in i05/i12/i19 |
| `fn first<T>(xs: [T]) -> T`; `type Box<T> { value: T }`; `Box<Int>(42)` | ok | spec example runs verbatim both backends |
| Inference `first([1,2,3])`; explicit `first<Int>(...)` | ok | E219/E220/E221 verified |
| "No constraints in v1 ... `where` clauses are a later phase" | ok | `where` → E001 syntax error (correctly deferred) |
| "Call-site inference ... ambiguity or conflict is a type error (new E-code)" | ok | E219/E220 fire as specified |
| "Generic sum types compose: `type Result<T> { Ok(T), Err(Text) }`" | ok | i03/i09 |
| AOT monomorphization; "Recursion through generics ... cap depth and error cleanly" | ok | E223: `monomorphization depth exceeded for 'f'` at build (explicit-pinning trigger) |
| Exit: "Spec examples above run identically on interpreter and AOT (byte-identical stdout/exit/errors)" | **broken** | H-4: Shape example prints `3.14` vs `3.1400000000000001` |
| Exit: "New E-codes documented in spec/ERRORS.md with tests" | ok | E213–E223 all in ERRORS.md; all fire |
| Exit: "`examples/` gains `generics.misa`, `sums.misa`, `closures.misa`" | **partial** | M-5: `sums.misa` missing |
| Deferred: where-clauses, HKT, variadic generics, pattern guards, TCO | ok | correctly absent |

### spec/PHASE5-DB.md

| Promise | Status | Evidence |
|---|---|---|
| `db.open(path)`; `":memory:"` for tests | ok | used in all db tests |
| `db.exec(db, sql, args?) -> Int` "returns rows changed"; `?` placeholders | **partial** | rows-changed ✓ (`1`); but signature is handle-first, contradicting the spec's own method-style examples (M-4); Int args diverge interp/AOT (H-1) |
| `db.query(db, sql, args?) -> [record]` | ok | incl. args form (qargs test) |
| `db.close(db) -> Void` | ok | |
| "`[any]` renders as `[Text]`/`[Int]`/etc. ... the checker enforces scalar args" | **broken** | H-1: heterogeneous lists infer `[unknown]` and bypass; `[7]` E211s while `["x", 11]` passes |
| `table` decls; SELECT/FROM/WHERE name validation; result record type inference; E-code for bad column | ok | E224 verified for SELECT and WHERE (`unknown column 'nam' in WHERE clause`); `r.name` type-checks; `Text?` → nil |
| "Queries over undeclared tables are dynamic, not errors" | ok | `SELECT a FROM nosuch` checks OK |
| "Deferred: ... JOIN validation ..." | ok | i23: JOIN runs, checker doesn't crash |
| Exit: "`db.*` works identically on interpreter and AOT" | **broken** | H-1 |
| Exit: "`examples/diary.misa`" | ok | exists |
| Deferred: Postgres, migrations, SQLCipher, expression type-checking | ok | correctly absent |

### spec/PHASE5-STDLIB2.md

| Promise | Status | Evidence |
|---|---|---|
| `json.parse`: objects→records, arrays→lists, Int-when-no-fraction, Text/Bool/nil; "Throws `E300`-family on invalid JSON" | **partial** | core ✓; int64-boundary throws instead of Float fallback (M-1) |
| `json.stringify`: "Canonical compact JSON"; "Throws on non-JSON values (e.g. `Db`, functions)" | **partial** | closures/Db throw ✓; sums: interp throws, AOT leaks internals (H-2) |
| `http.post` text body → response body; E304 | **untested** | httpbin.org egress timed out from `misa run` (E304 raised correctly, but no end-to-end verification; Content-Type unverifiable) |
| `http.post_json` stringify + `application/json` | **untested** | same |
| `time.now()` / `time.now_ms()` → Int epoch | ok | both backends (`1791272837` / `...410`) |
| `time.format(epoch, fmt)` strftime-style UTC | ok | `2026-10-06` both |
| `time.parse(s, fmt)` → Int; "throws `E300` on failure" | **broken** | C-1: AOT segfaults on valid input; invalid → E300 ✓ both |
| `math.sqrt/pow/abs/floor/ceil/round`; "round half away from zero"; "abs also accepts Int"; sqrt negative → E300 | ok | `3`,`-3`,`3`,`3.5`,`2`,`3`,`4.0`,`1024.0` identical both; E300 on negative sqrt both |
| Exit: "All four namespaces work identically on interpreter + AOT" | **broken** | C-1 (time.parse), H-4 (float printing) |
| Exit: "`examples/` gains `json_demo.misa`" | ok | exists |

### spec/PHASE7-FFI.md

| Promise | Status | Evidence |
|---|---|---|
| `extern "c" { fn strlen(s: Text) -> Int, ... }`; `"c"` needs no `-l` | ok | strlen/getenv/atol/labs on both backends |
| Type mapping Int→long, Float→double, Text→char*, Bool→int, Ptr→void*, Void→void | ok | `atol("-5")`→`-5`, `labs(-42)`→`42`, `atol("9876543210")` identical both |
| "v1 limits: no structs by value, no callbacks, no varargs" | ok | E225 for type params / non-mappable types |
| "`Ptr` values are opaque — pass them back, compare to nil, free via the library's own free" | ok | `p == nil` → false; malloc/free through `Box<Ptr>` (i18) |
| "Errors: foreign call crashes are NOT caught (same as C)" | **untested** | intentionally not exercised |
| Interp ctypes; AOT llvmlite declare + `-l<lib>` (skip `"c"`) | ok | |
| "Checker: `extern` decls registered like `fn` decls ... E102/E211" | **partial** | calls check ✓; but as-values → E101, unlike `fn` decls (M-2) |
| E225 invalid signatures | ok | empty lib, type params, bad types all E225 |
| E226 load/call failures | ok | `cannot load library 'nonexistentlib_xyz'`; `library 'c' has no symbol 'no_such_symbol_xyz'` (load-time; L-2) |
| Exit: "`extern \"c\"` strlen/sqrt demo on interpreter + AOT" | ok | |
| Exit: "`extern \"libpq\"` ... (or sqlite3 via FFI as fallback)" | **partial** | interp: `sqlite3_libversion()` → `3.45.1` ✓; AOT: **untested** — link fails, no `libsqlite3.so` dev symlink in this VM (environment limitation, not proven broken) |
| Exit: "Android: `extern` works via NDK" | **untested** | no Android toolchain exercise in this audit |
| Deferred: structs/callbacks/varargs/C++ mangling/safe extern/binding gen | ok | correctly absent |

### spec/STDLIB.md (v0.2 deltas relevant to this audit)

| Promise | Status | Evidence |
|---|---|---|
| `print` accepts any value, canonical rendering | ok | records/maps/sums render identically |
| `len` on `[T]`, `Text` (chars), `Map` (entries) | ok | `len("héllo")`→`5`, `len("🌍🌍")`→`2` both |
| `int`/`float` parse; E300 on failure | ok | `int("abc")` → E300 both |
| `assert` → E301 | **untested** | not exercised |
| `env` → E302 if unset | **broken** | M-3: E300 on both; E302 never raised anywhere |
| `fs.read`/`fs.write` → E303 | **untested** | not exercised |
| `http.get` → E304 | **untested** | network |
| `str.*` code-point semantics both backends; `str.chr` rejects negatives/surrogates/>0x10FFFF with E300 | ok | slice/ord/chr/trim/split/starts_with verified; surrogate → E300 both |
| `str.split` errors on empty sep; `str.ord` errors on empty | ok | E300 both |
| `process.run(cmd: [Text]) -> {code, stdout, stderr}`; E303 when command can't start | **partial** | interp ✓ (`E303: process.run: command not found`); AOT: missing entirely (H-3) |
| `json.parse` "Numbers become `Int` ... and fit in int64, otherwise `Float`" | **broken** | M-1 |
| `json.stringify` "ensure_ascii=False semantics"; throws E300 for functions/closures, `Ptr`, `Db`, `Media`, `Iter`, `Error` | **partial** | unicode ✓ both; closures/Ptr throw ✓; sums leak on AOT (H-2) |
| `time.*` UTC | ok | format/parse round-trip date-correct (interp) |
| `db.query` "INTEGER→Int, REAL→Float, TEXT→Text, NULL→nil; BLOB is an E303 error in v1" | ok | BLOB → `E303: db.query: BLOB columns are not supported in v1` both |
| "All four throw `Error{code: \"E303\"}` on SQLite failures" | ok | bad path open → E303 both; bad SQL → E303 |
| `table` typed queries; "Queries over undeclared tables return dynamic `[record]`" | ok | |
| Map missing key → E305 | ok | both |
| Screen components / media constructors | **untested** | out of scope for this workstream |

---

## Appendix C — Untested areas (honest list)

- `http.get` / `http.post` / `http.post_json` end-to-end (egress to
  httpbin.org timed out; E304 path itself works).
- AOT `extern "sqlite3"` (no dev symlink in this VM) and any
  `extern "libpq"` (no Postgres available).
- Android NDK extern / on-device targets.
- Foreign-call crashes (spec says NOT caught — deliberately not probed).
- `assert`/E301, `fs.read`/`fs.write` (documented, not exercised here).
- Screen DSL, media constructors, model/provider calls.
- The full `tests/run_tests.py` suite (running separately; not used as
  evidence here).
- `misa serve`, LSP, pkg — untouched by this workstream.

## Severity summary

| Severity | Count | IDs |
|---|---|---|
| CRITICAL | 1 | C-1 (AOT `time.parse` segfault) |
| HIGH | 4 | H-1 (db args 3-way inconsistency), H-2 (AOT json sum leak), H-3 (`process.run` missing on AOT), H-4 (float printing divergence) |
| MEDIUM | 6 | M-1 (json int64→Float), M-2 (extern as value), M-3 (env E302), M-4 (DB spec examples unrunnable), M-5 (`sums.misa` missing), M-6 (check/build inconsistency) |
| LOW | 6 | L-1…L-6 |

**Bottom line for the parent:** the type-system tranche (5a/5b/5c)
composes remarkably well — 20/25 interaction tests pass byte-identical
on both backends, including closures capturing generics, sums with
closure payloads, recursive generic sums, and FFI pointers in generic
boxes. Every finding is at the *seams*: the db-arg type checker vs both
runtimes (H-1), the AOT JSON serializer vs the AOT sum representation
(H-2), the AOT emitter's missing `process.run` lowering (H-3), float
formatting (H-4), and one segfault in the AOT time parser (C-1).

---

## Appendix D — Supplementary: existing suites (2026-10-06, this audit run)

- `python3 tests/run_tests.py`: **192 passed, 0 failed** (interpreter-focused suite).
- `python3 tests/aot/run_aot_tests.py`: **19/19 AOT parity tests passed**
  (a00–a19 incl. a13_closures, a14_sums, a15_generics, a16_db, a17_ffi,
  a18_stdlib, a19_str).

Both suites are green — which is itself a finding about *coverage*: none
of C-1, H-1, H-2, H-3, H-4, M-1, M-2 is exercised by them (no
`time.parse` success-path on AOT, no non-Text db bind args, no
`json.stringify` of sums, no `process.run` under `misa build`, no float
rendering assertions). The gaps above survived a green suite.
