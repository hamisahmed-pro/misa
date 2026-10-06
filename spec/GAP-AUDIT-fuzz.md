# GAP AUDIT — Workstream 1: Differential Fuzzing + Robustness

Date: 2026-10-06. Auditor: adversarial subagent (workstream 1).
Method: stress-testing, not code review. **No fixes applied. `selfhost/` untouched.**

Tooling: `spec/fuzz_diff.py` (new, kept with this audit) with three modes:
- `diff` — generate random Misa programs, run interpreter vs AOT, compare
  stdout/exit-code exactly (stderr compared loosely: the E-code must agree;
  position prefixes may differ).
- `parser` — 250 random token soups through `misa check`; any Python
  traceback = bug.
- `robust` — hand-written limit probes (deep recursion, large strings, 10k db
  rows, 200-deep JSON, unicode edges, int64/float boundaries) on both backends.

Repro artifacts: `/tmp/fuzz_bugs/` — canonical minimal repros at top level
(`min_*.misa` + `.report.txt`); pilot-run bugs under `pilot-seed11/`;
per-program `fuzz_N.{misa,report.txt}`.

---

## Counts

| Metric | Value |
|---|---|
| Differential programs generated | 641 (13 families + targeted modsign/intbound) |
| Pass (exact stdout + exit agreement) | 541 |
| Divergences | **100** → 96× H-2 (float rendering), 4× H-1 (int64 overflow) |
| Interpreter tracebacks in fuzz run | 0 (generator avoids div-by-zero by design) |
| AOT tracebacks in fuzz run | 0 |
| Timeouts (both backends) | 16 — generator artifact (see below), not a product bug |
| Parser soups | 250 → **0 crashes** (247 E001, 2 E101, 1 E214 — all clean) |
| Robustness probes | 13 probes × 2 backends (table below) |
| Confirmation re-run (fixed generator, seed 2) | 74 programs: 59 pass, 15 diverge (11× H-2, 4× H-1 — only known classes), **0 timeouts**, 0 crashes |

**Zero-divergence families** (full agreement): `db` (30), `ffi` (20),
`generic` (40), `invalid` (30 — run/build agree on all checker E-codes),
`json` (40), `modsign` (4 — Python-style floored `%` on both backends).

---

## Findings

### HIGH

#### H-1. Int64 overflow: interpreter silently bigints, AOT silently wraps
- **Repro** (`/tmp/fuzz_bugs/min_int64_overflow.misa`):
  ```misa
  print(9223372036854775807 + 1)
  print(9223372036854775807 * 2)
  print(-(-9223372036854775808))
  print(1000000 * 1000000 * 1000000 * 1000000)
  ```
- **Expected**: per spec, `Int` is int64 ("fit in int64"). `json.parse` and
  `math.*` raise `E300` ("out of Int range") on overflow — verified agreeing
  on BOTH backends.
- **Actual (interp)**: `9223372036854775808`, `18446744073709551614`,
  `9223372036854775808`, `1000000000000000000000000` — silent Python bigints.
- **Actual (AOT)**: `-9223372036854775808`, `-2`, `-9223372036854775808`,
  `2003764205206896640` — silent two's-complement wrap.
- **Affected**: interp `eval_binop` (`impl/misa/interp.py`, no range check on
  `+`/`-`/`*`); AOT C runtime (`int64_t` arithmetic, no overflow check).
- Both backends are wrong and they disagree. Arithmetic should raise E300
  like `math.*`/`json.parse` already do. Silent wrap in AOT is a correctness
  hazard for money/math code.

#### H-2. Float rendering: AOT prints `%.17g`, interpreter prints shortest repr
- **Repro** (`/tmp/fuzz_bugs/min_float_render.misa`):
  ```misa
  print(0.1 + 0.2)   // interp: 0.1         aot: 0.10000000000000001
  print(str(0.1))     // interp: 0.1         aot: 0.10000000000000001
  print("v={0.1}")    // interp: v=0.1       aot: v=0.10000000000000001
  print([0.1, 0.2])   // interp: [0.1, 0.2]  aot: [0.10000000000000001, ...]
  ```
  Also in scientific notation: `-4.974598456630829e-05` vs
  `-4.9745984566308289e-05` (same double, different repr).
- **Affected**: AOT C runtime float→text rendering (`print`, `str()`,
  interpolation, list/record rendering).
- **Note**: `json.stringify({"x": 0.1})` AGREES (`{"x":0.1}`) on both — JSON
  uses a separate correct code path. Purely the display path.
- Dominated the raw count (96/100 divergences) but is a single root cause.

#### H-3. `json.parse` accepts lone surrogates (interp); printing one traceback-crashes
- **Repro** (`/tmp/fuzz_bugs/min_json_lone_surrogate.misa`; builds the JSON
  text `"\ud800"` programmatically since Misa has no `\u` escapes):
  ```misa
  let q = str.chr(34)
  let bs = str.chr(92)
  let j = q + bs + "ud800" + q
  let x = json.parse(j)
  print(x)
  ```
- **Actual (interp)**: `json.parse` ACCEPTS the lone surrogate; `print(x)`
  then dies with `UnicodeEncodeError: 'utf-8' codec can't encode character
  '\ud800'` — full Python traceback.
- **Actual (AOT)**: clean `E300: json.parse: invalid JSON: lone surrogate`.
- Two divergences in one: parse acceptance + traceback-on-print vs clean E300.
- `str.chr` already rejects surrogates with E300 on both backends, so the
  intended semantics is rejection — interp's `json.parse` is the outlier.

### MEDIUM

#### M-1. Interpreter: uncaught `ZeroDivisionError` traceback on division by zero
- **Repro**: `print(1/0)` or `print(1.0/0.0)`.
- **Expected**: clean `E300` (what AOT produces: `1:8: E300: division by zero`,
  exit 1).
- **Actual (interp)**: full Python traceback (`ZeroDivisionError` at
  `impl/misa/interp.py:552`, `return l / r`), leaking internals.
- **Affected**: interp `eval_binop` — `/` (and `%` with zero divisor) miss the
  `try/except` other runtime errors have.
- Found by manual probing; the fuzzer avoids zero divisors by construction,
  so this class is under-fuzzed (see "not covered").

#### M-2. Deep recursion: interpreter `RecursionError` traceback at depth ~100–200; AOT fine to 100k
- Linear-recursion probe: depth 100 OK on both; depth 200/500/1000/10000 →
  interp `RecursionError: maximum recursion depth exceeded` (Python
  traceback, not a clean E-code); AOT prints correctly up to 100000.
- Behavioral divergence on identical programs, plus the traceback instead
  of a clean resource error. (The selfhost harness "bumps host recursion
  limit" — known limitation, but it surfaces as a crash.)

#### M-3. Parser: nested plain-quote strings inside interpolation rejected with wrong error
- **Repro**: `print("x={("a" < "b")}")` → `1:7: E001: unbalanced '{' in string`
  (the braces ARE balanced).
- `print("x={(1 < 2)}")` works; `print("x={\"a\"}")` (escaped quotes) works;
  only *plain* `"` nested strings fail. `print("x={("tab\\there")}")` →
  `E001: unexpected character '\'` — escapes also mishandled there.
- Clean E001 (no crash) but the message is wrong and the accept/reject line
  (escaped ok, plain not) is incoherent.

#### M-4. `db.exec`/`db.query` only resolve when the handle variable is named `db`
- **Repro**:
  ```misa
  let mydb = db.open(":memory:")
  mydb.exec(mydb, "CREATE TABLE t (x INTEGER)")  // E101: undefined function 'mydb.exec'
  ```
  Only `db.exec(db, ...)` works (cf. `examples/diary.misa`).
- `db.exec` is namespace-style resolution, not a method call — the receiver
  name is load-bearing, documented nowhere. Users hit E101 with any other
  name. (Found while building the fuzzer's db family.)

### LOW

#### L-1. Error-message wording differs between backends (same E-code — not flagged)
- `json.parse("9223372036854775808")`: interp `E300: json.parse: integer
  9223372036854775808 out of Int range` vs AOT `E300: json.parse: invalid
  JSON: integer out of Int range`.
- `math.sqrt(-1.0)`: interp `E300: math.sqrt: math domain error` vs AOT
  `E300: math.sqrt: domain error (negative input)`.
- `json.stringify(inf)`: both E300, different text.
- Runtime positions: interp `<unknown>: E300: ...` vs AOT `1:8: E300: ...`
  (interp has no positions — harder to locate errors).

#### L-2. `strcmp` negative results render as unsigned 32-bit on BOTH backends
- `strcmp("abc", "abd")` → `4294967295` (i.e. `(uint32)-1`) identically on
  interp and AOT. Consistent, not a divergence — but a C `int -1` should
  surely be `-1`. Shared FFI int-marshaling flaw.

#### L-3. Interpreter much slower than AOT on print-heavy loops (perf note)
- Generated program printing ~444k lines: interp 19.3s wall vs AOT <0.1s.
  Not a correctness bug; sets expectations for interp use.

#### L-4. Language facts learned while fuzzing (not bugs)
- No `break`/`continue` (E101 `undefined name 'break'`); `if` is
  statement-only (`E001: unexpected if ('if') in expression`); `match` IS an
  expression; no nested `fn` declarations; no `;` separators; shadowing
  rejected entirely (E210 even in nested blocks); float literals have no
  scientific notation (`1e308` → E001); string escapes are only
  `\n \t \\ \"` (no `\u`, no `\U`); no `0x` hex int literals.

---

## Robustness probe results

| Probe | Interp | AOT | Verdict |
|---|---|---|---|
| recursion depth 100 | ok `100` | ok `100` | agree |
| recursion depth 200/500/1000/10000 | `RecursionError` traceback | ok | **DIVERGE (M-2)** |
| recursion depth 100000 | (not run — interp already fails) | ok `100000` | AOT robust |
| fib(20) | `10946` | `10946` | agree |
| string build/len/slice/json (620KB) | ok | ok | agree |
| 10k db rows: insert + COUNT + SUM | `10000` / `49995000` | same | agree |
| 200-deep nested JSON parse+stringify+equality | ok | ok | agree |
| unicode: café/emoji, `len`=code points, `str.ord/chr` astral, json+db round-trip | ok | ok | agree |
| lone surrogate `"\ud800"` via `json.parse` | accepts, then `UnicodeEncodeError` traceback on print | `E300` reject | **DIVERGE (H-3)** |
| int64 boundaries | bigints | wraps | **DIVERGE (H-1)** |
| float edges: `pow(10,308)`→`1e+308`, `*10`→`inf`, `sqrt(-1)` | `E300` | `E300` (wording differs) | agree on code |
| `inf` through `json.stringify` | `E300` | `E300` (wording differs) | agree on code |

---

## What was NOT covered (honest gaps)

- **Deliberate runtime-error programs** (div/mod by zero, OOB indexing,
  `int("zz")`, missing map keys): the generator avoids them by construction
  to stay deterministic; M-1 was found by hand. A follow-up should fuzz
  *failing* programs and compare E-codes.
- **`http.*` / `process.*` / `fs.*`**: excluded (network/side effects).
- **Screens, model calls, budgets**: excluded (not AOT-comparable).
- **AOT cross-targets** (Android, wasm): host target only.
- **`extern` beyond libc `strlen`/`strcmp`**: safe read-only calls only.
- **Signals, broken pipes, OOM**: not probed. Recursion stops at 100k
  (AOT); neither backend was pushed to memory exhaustion.
- The generator is valid-by-construction, so *checker rejection paths*
  beyond the 30-program `invalid` family are lightly covered.
- 16 fuzz timeouts were generator-created infinite loops (loop bodies
  reassigned the loop's own counter/flag vars); both backends hung
  identically — agreement, not a product bug. Fixed in `fuzz_diff.py`
  (control vars are now read-only to the generator); the 74-program
  confirmation re-run had **0 timeouts**, proving the fix.

---

## Fuzzer-infra lessons (for reruns)

- `/tmp` is a 512MB tmpfs: AOT binaries (~0.7–1.8MB each) filled it mid-run;
  the first full run died with `OSError: [Errno 28]`, and its tail of `E600`
  "divergences" were **link failures from the full disk**, not real bugs
  (all rebuild fine with space). `fuzz_diff.py` now deletes each binary +
  `.misa` + `.db` after use and guards `save_bug`.
- Print-heavy programs can push the interpreter past the 30s timeout while
  AOT finishes instantly (L-3); the generator now budgets prints
  (`LOOP_BOUND=12`, `PRINT_BUDGET=4000`).
- `misa check` vs `run`/`build` agree on all 30 invalid-family E-codes —
  the checker is shared, as expected.
