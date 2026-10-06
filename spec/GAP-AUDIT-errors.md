# Error-Code Audit — GAP-AUDIT-errors

**Workstream 3: ERROR-PATH AUDIT · 2026-10-06 · auditor: Muse (subagent)**
**Repo:** `~/workspace/misa` · **Spec:** `spec/ERRORS.md` · **CLI:** `python3 impl/bin/misa`

## Method

1. Read `spec/ERRORS.md` fully; extracted every documented E-code
   (E001, E101–E113, E200–E226, E300/E301/E303–E305, E400–E402, E500–E504, E600).
2. Wrote a minimal trigger program per code under `/tmp/errcodes/` (84 files;
   generator: `/tmp/errcodes/gen.py`, driver: `/tmp/errcodes/audit.py`,
   raw results: `/tmp/errcodes/results.json` — 123 CLI invocations).
3. Ran each through `misa check` and `misa run` (plus `misa build` and the
   produced binaries for AOT-time codes). Model codes used `--mock` files.
4. For each code verified: (a) the expected code fires (not another code, not a
   traceback), (b) message matches `spec/ERRORS.md`, (c) `line:col` points at
   the offending construct.
5. **Nothing was fixed.** `selfhost/` was not touched.

Uncaught-error CLI format (consistent everywhere): `{line}:{col}: {CODE}: {message}`
on stderr, exit 1; `<unknown>:` when line/col are 0; an optional second line
`  hint: ...`. `misa check|run|build --json` emits
`[{"code","message","line","col","hint"}]`, exit 1. Consistent across
`check`/`run`/`build` and AOT binaries.

## Result table

| E-code | reachable? | correct code? | message accurate? | position correct? | notes |
|---|---|---|---|---|---|
| E001 | ✓ | ✓ | ✓ | ✓ | Empty file → exit 0 (valid empty program). `}`, unterminated string, `fn` w/o body → clean E001 with helpful messages, no tracebacks |
| E101 | ✓ | ✓ | ✓ | ✓ | `1:7` points at the name |
| E102 | ✓ | ✓ | ✓ | ✓ | `'len' takes 1-1 args, got 2` |
| E110 | ✓ | ✓ | ✓ | ✓ | names the other file: `(also defined in u1.misa)` |
| E111 | ✓ | ✓ | ✓ | **✗** | `F1`: position is `<unknown>` though the `import` statement has a location |
| E112 | ✓ | ✓ | ~ | **✗** | `F2`: `cannot read import` → `<unknown>`; `import "data.txt"` → misleading `invalid package name 'data.txt'` |
| E113 | ✓ | ✓ | ✓ | ✓ | `F3`: spec says "(reserved)" but the code actively uses it for bad `misa.pkg` manifests |
| E200 | ✓ | ✓ | ✓ | ✓ | matches spec example pattern |
| E201 | ✓ | ✓ | ✓ | ✓ | `2:1` on the assignment |
| E202 | ✓ | ✓ | ✓ | ✓ | `2:17` on the bad field |
| E203 | ✓ | ✓ | ✓ | ✓ | `12:1` on the `eval` decl |
| E204 | ✓ | ✓ | ✓ | ✓ | `15:13` on the non-Bool expr |
| E205 | ✓ | ✓ | ✓ | ✓ | all three: `await 5`, `on s.token` (s:Int), `stream s = f()` non-model call |
| E206 | ✓ | ✓ | ✓ | ✓ | `F4`: the documented `'id:' needs a plain name` branch is **dead code** — the parser rejects non-ident `id:` with E001 first |
| E207 | ✓ | ✓ | ✓ | ✓ | `2:15` on the duplicate |
| E208 | ✓ | **✗** | ~ | ✓ | `F5`: fires only for fn-param/model-return annotations. `let`/`var` annotations → E200; plain `type` fields → silently accepted; generic `type` fields → E222 |
| E209 | ✓ | ✓ | ✓ | ✓ | `unknown provider kind 'wat'` |
| E210 | ✓ | ✓ | ✓ | ✓ | incl. extern-name collision |
| E211 | ✓ | ✓ | ✓ | ✓ | arg and return variants |
| E212 | ✓ | ✓ | ~ | ✓ | `F6`: all 6 non-Error values (Int/Text/nil/list/bool/wrong-shape record) → E212; but the message's `Error ({code,message})` shape can't be assigned to a declared `Error` (→ E200) |
| E213 | ✓ | ✓ | ✓ | ✓ | message matches spec's remediation advice verbatim |
| E214 | ✓ | ✓ | ✓ | ✓ | `print`, `str`, `len`, `fs.read` all fire it |
| E215 | ✓ | ✓ | ✓ | ✓ | matches spec wording exactly |
| E216 | ✓ | ✓ | ✓ | ✓ | `missing: Rect` listed as spec'd |
| E217 | ✓ | ✓ | ✓ | ✓ | `('Text' vs 'Int')` as spec'd |
| E218 | ✓ | ✓ | ✓ | ✓ | unknown variant; bare payload variant; non-sum scrutinee; wrong sum type; bind-count mismatch — all fire E218 |
| E219 | ✓ | ✓ | ✓ | ✓ | `F7` (info): the canonical trigger also emits a leading E211; a cleaner trigger fires E219 alone |
| E220 | ✓ | ✓ | ✓ | ✓ | `4:14` on the call |
| E221 | ✓ | ✓ | ✓ | ✓ | type- and fn-type-arg arity both fire |
| E222 | ✓ | ✓ | ✓ | ✓ | also fires a follow-on E211 for the bad body (acceptable) |
| E223 | ✓ | ✓ | ✓ | ✓ | needs `misa build`; trigger: `fn f<T>(x: T) -> T { let y = f([x]); return x }` → `2:13: E223: monomorphization depth exceeded for 'f' (recursive generic instantiation)` |
| E224 | ✓ | ✓ | ✓ | ✓ | `6:25` inside the SQL string; message matches spec example |
| E225 | ✓ | ✓ | ✓ | ✓ | `2:12` on the param; lists the v1 FFI types |
| E226 | ✓ | ✓ | ✓ | ✓ | `F8`: fires at **run time** and **is caught by try/catch**, but is classified E2xx (static) and the spec's Catching section says only E3xx–E5xx are catchable. In AOT the same fault is E600 (link failed) — E226 never fires from a binary |
| E300 | ~ | **✗** | n/a | **✗** | `F9` **CRITICAL**: `int("abc")` → clean E300, but `1/0`, `1%0` (the spec's own examples!) and `1-"a"` → raw Python tracebacks (`ZeroDivisionError`/`TypeError`). Checker passes `1-"a"` silently |
| E301 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>` though the `assert` statement has a position |
| E303 | ✓ | ✓ | ~ | **✗** | `F10` (`<unknown>`); `F11`: interp message uses OS strerror, AOT binary says `cannot open` — same code, different text. AOT binary reports `1:3` (better than interp) |
| E304 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>` though the call site is known |
| E305 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>` though the indexing expr is known |
| E400 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>`; message + hint accurate |
| E401 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>`; `F12`: missing on-device model file is E401 at run but **E600** at build |
| E402 | ✓ | ✓ | ✓ | ✓ | `15:1` on the `stream` decl; build-only as spec'd |
| E500 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>`; catchable, binds correctly |
| E501 | ? | ? | ? | ? | **untestable in this environment** (no `onnxruntime`); code path exists in `providers.py` |
| E502 | ? | ? | ? | ? | **untestable in this environment** (no `onnxruntime`); code path exists in `providers.py` |
| E503 | ✓ | ✓ | ✓ | **✗** | `F10`: `<unknown>`; reachable via CoreML file (no Apple hw) and via TFLite without `ai-edge-litert` |
| E504 | ✓ | ✓ | ✓ | ✓ | build-only; `1:1` on the provider block; message names target triple |
| E600 | ✓ | ✓ | ✓ | ✓ | reachable via `navigate("nope")` → `4:1: E600: navigate needs a screen call, e.g. navigate(Home())`; also link failures. `F12`: missing on-device model file is E600 at build vs E401 at run |

**Counts:** 47 documented codes (E113 counted as documented-reserved) · **tested 47** ·
**clean pass 29** · **with findings 16** · **untestable here 2** (E501, E502 — need
`onnxruntime`, not installed).

## Severity-rated findings

### CRITICAL

**F9a — `1 / 0` and `1 % 0` produce a raw Python traceback instead of E300.**
The spec's own E300 description is "Generic runtime error (**div by zero**, …)".
Repro (`/tmp/errcodes/e300_div.misa`):
```
print(1 / 0)
```
`misa run` → exit 1, stderr is a full traceback ending in:
```
File ".../impl/misa/interp.py", line 552, in eval_binop
    return l / r
           ~~^~~
ZeroDivisionError: division by zero
```
Expected: `1:9: E300: division by zero` (which is exactly what the **AOT binary**
emits for the same program: `1:9: E300: division by zero`, exit 1). Root cause:
`eval_binop` does unguarded `l / r`, `l % r`. Consequence: `try/catch` cannot
catch division/modulo by zero either (`ZeroDivisionError` is not a `MisaError`;
verified — the `try { print(1/0) }` case in `/tmp/errcodes/e_catch.misa`
tracebacks after correctly printing E303/E304/E305 for the earlier cases).

**F9b — `1 - "a"` passes `misa check`, then tracebacks at run.**
Repro (`/tmp/errcodes/e300_sub.misa`): `print(1 - "a")` → `misa check` exits 0
(`OK`); `misa run` → traceback ending `TypeError: unsupported operand type(s)
for -: 'int' and 'str'`. Expected: a check-time type error (E211-family) or a
run-time `E300` ("bad operation" per spec). The checker's `BinOp` inference
(`checker.py` ~L918) never validates operand types, and `eval_binop` never
wraps `TypeError`. Same presumably applies to `<`/`>`/`==` on mismatched types.

**F9c — on-device `.onnx` model call without `onnxruntime` installed tracebacks.**
Repro (`/tmp/errcodes/e_onnx.misa`; `/tmp/errcodes/x.onnx` is a placeholder file):
```
provider od {
    model: file("x.onnx"),
}
models { m: od("m"), }
model mm() -> Text with od.m prompt v1 """hi"""
print(mm())
```
`misa run` → traceback ending:
```
File ".../impl/misa/providers.py", line 321, in _ort_session
    import onnxruntime as ort
ModuleNotFoundError: No module named 'onnxruntime'
```
Expected: a clean `E503` (the TFLite path handles its missing dependency
correctly: `E503: TFLite models need the 'ai-edge-litert' package (pip install
ai-edge-litert)`). The `.onnx` branch in `_ort_session` does a bare
`import onnxruntime` with no `ImportError` guard, unlike `_tflite_interp`.

### MEDIUM

**F5 — E208 (unknown type name) fires for the wrong code, or not at all,
depending on annotation position.** Only **fn params** (`fn f(x: Wat)`) and
**model return types** produce E208 (`unknown type 'Wat'`). Elsewhere:
- `let x: Wat = 1` / `var y: Wat = 1` → **E200** `cannot assign Int to 'x: Wat'`
  (wrong code; the real fault is the unknown type, and the message blames the value).
- `type T { x: Wat }` (plain record) → **check passes silently, exit 0** —
  no error at all (`_check_type_decl` skips validation for non-generic, non-sum decls).
- `type Box<T> { value: Wat }` (generic) → **E222** `unbound type variable 'Wat'`
  (wrong code — `Wat` is not a type variable, it's an unknown name).
  Expected in all three: E208.

**F8 — E226 is documented as E2xx (static) but behaves as a runtime,
catchable error.** Spec table places E226 under "E2xx — static types", and the
"Catching" section says "try/catch catches any E3xx–E5xx … Static errors
(E0xx–E2xx) … cannot be caught." In fact E226 fires at run time (lazy
`ctypes` load on first call: `2:3: E226: library 'c' has no symbol
'nosuchsym_xyz'`) and **is caught** by try/catch
(`/tmp/errcodes/e_catch_226.misa` prints `E226 | library 'c' has no symbol
'nosuchsym_xyz'`). Either the code belongs in E3xx or the Catching section
needs an E226 carve-out. Related: in AOT the identical fault (missing lib or
missing symbol) surfaces as **E600** `link failed:` at build time — E226 can
never fire from a compiled binary. That mapping is spec-consistent for E600
("link errors") but means FFI fault codes diverge interp-vs-AOT.

### LOW

**F10 — twelve runtime codes report `<unknown>` position though the source
location is obvious.** E301 (`assert`), E300 (`int("abc")`), E303, E304, E305,
E400, E401, E500, E503 (all `<unknown>:`), plus loader codes E111/E112-missing.
The offending call/assert statement is always available in the interpreter.
Inconsistent with E226 (carries `2:3`/`1:1`), with thrown errors (carry the
`throw` site, e.g. `1:1: E303: boom`), and with the AOT binaries, which report
*better* positions than the interpreter for the same fault
(`1:7: E300: cannot parse int from 'abc'`, `1:3: E303: fs.read failed: …`).

**F4 — E206 message `'id:' needs a plain name` is unreachable dead code.**
`checker.py:430` guards `not isinstance(val, ast.Ident)`, but the parser
(`parser.py:505`) does `idt = self.expect_ident("component id")` — any
non-ident (`id: 5`, `id: "a"`) dies at parse time with
`E001: expected component id, found INT (5)`. The checker branch can never fire.
(E206 itself is fine via unknown component / handler arity / unknown stream event.)

**F3 — E113 documented "(reserved)" but actively raised.** `pkg.py` raises E113
for bad manifests (`invalid TOML in misa.pkg: …`, verified via
`/tmp/errcodes/e113/`), and `loader.py`'s docstring says "Bad manifest ->
E113". The spec table should document it instead of "(reserved)".

**F1 — E111 position `<unknown>`.** `circular import: a.misa -> b.misa -> a.misa`
is emitted by the loader with line/col 0,0 although the offending `import`
statement has a known location.

**F2 — E112 rough edges.** (a) `cannot read import '…'` (missing file) → position
`<unknown>` though the `import` line is known. (b) `import "data.txt"` (no `./`
prefix, not `.misa`) is routed to package resolution and reports
`E112: invalid package name 'data.txt'` + hint about lowercase/digits/hyphens —
misleading; the `./`-relative branch reports the far better
`E112: import path must end in .misa: './data.txt'`.

**F6 — E212's message describes an `Error` shape you cannot actually write.**
Message: `'throw' needs an Error ({ code: Text, message: Text })`. But
`let e: Error = { code: "E303", message: "via var" }` → 
...[truncated 4383 chars]