# Phase 6 Self-Hosting — Gap Analysis

**Date:** 2026-10-06. **Status:** analysis only; no compiler code written.

**Goal (roadmap):** "Compiler rewritten in Misa | Misa compiles Misa."

**Scope of "the compiler"** (Python implementation, ~7,645 lines):

| Component | File | Lines | Self-host needs |
|---|---|---|---|
| Lexer | `impl/misa/lexer.py` | 281 | char iteration, string building |
| Parser | `impl/misa/parser.py` | 909 | recursion, AST nodes, error recovery |
| AST | `impl/misa/ast.py` | ~270 | node representation |
| Checker | `impl/misa/checker.py` | 844 | symbol tables, type lattice |
| Interpreter | `impl/misa/interp.py` | 908 | tree-walk eval, environments |
| AOT emitter | `impl/misa/aot/emit.py` | 1,232 | LLVM IR generation (llvmlite today) |
| Build driver | `impl/misa/aot/build.py` | 791 | object emission, linking, NDK/SDK |
| Providers | `impl/misa/providers.py` | 579 | HTTP, JSON, model runtimes |
| Tools | `fmt.py`/`lsp.py`/`pkg.py`/`serve.py` | ~1,700 | (deferrable; not on the critical path) |

---

## 1. Strings — SUFFICIENT (with pain)

**What works today** (verified empirically 2026-10-06):

```misa
let s = "hello"
print(len(s))        // 5  (STDLIB.md §1: len accepts Text, counts chars)
print(s[1])          // "e"  (interp.py: Index on str returns obj[idx])
var out = ""
for c in s {         // interp.py _iterable: str -> list(it), i.e. chars
    out = out + c    // + concatenates when either side is Text
}
print(out)           // "hello"
```

A hand-written lexer is therefore expressible: index-based scanning
(`s[i]`, `len(s)`), char classification via comparison
(`c >= "a" and c <= "z"` — string comparison operators work on Text),
token accumulation via `+`.

**What's missing** (STDLIB.md has no string namespace at all):

| Op | Status | Lexer/parser impact |
|---|---|---|
| slice `s[i:j]` | ❌ missing | must accumulate char-by-char |
| `split` / `join` | ❌ missing | manual loops |
| `substring`/`startsWith`/`contains` | ❌ missing | manual loops |
| `trim` | ❌ missing | manual loops |
| char→code / code→char | ❌ missing | classification by range compare works, but escapes (`\n`, `\u{}`) need code points |
| string→bytes | ❌ missing | file I/O is Text-only (fine for source) |

**Verdict:** a lexer/parser CAN be written today, at roughly 2–3× the
line count of the Python version (no slicing is the main tax).
Recommend adding a minimal `str` namespace (`slice`, `starts_with`,
`split`, `trim`, `ord`/`chr`) before starting — it halves the
self-hosted lexer's size and bug surface. This is new stdlib, not new
syntax.

## 2. Data structures — WORKABLE (records as poor-man's sum types)

**No sum types, no enums, no unions.** `typeExpr` (SYNTAX.md §4) is
`IDENT "?"? | "[" typeExpr "]" "?"? | "Map" "<" … ">" "?"?` — there is
no `A | B`.

**The workaround** (verified): tagged records.

```misa
type Tok { kind: Text, text: Text, line: Int }
let t: Tok = { kind: "ident", text: "foo", line: 1 }
print(t.kind)   // "ident" — works today
```

An AST becomes `type Expr { kind: Text, ... }` with one record type
per node kind carrying all possible fields (unused fields `nil`),
dispatched on `kind` with if/else chains. This is how pre-sum-type
languages (early Go, C) represent ASTs. Cost estimate: the Python
`ast.py` (~40 node classes) becomes ~40 record types + manual
dispatch — mechanical, ~1.5× lines, no expressiveness loss. The
checker already does isinstance-dispatch; the Misa version does
string-dispatch.

`Map<Text, V>` exists (STDLIB.md §4) and suffices for symbol tables
and environments (`Map<Text, Value>`).

**Verdict:** not a blocker. Sum types would be nicer (and are on the
type-system tranche the stdlib summary already calls for), but tagged
records are a complete workaround. Do NOT block Phase 6 on sum types.

## 3. Recursion — WORKS

SEMANTICS.md §2: "Recursion is legal; each call gets a fresh local
scope." Verified: `fib(10)` → `55`. The parser (recursive descent)
and a tree-walking interpreter are both expressible. Note: "no
tail-call optimization in the v0.2 interpreter" — deep expressions
could stack-overflow the *host* interpreter during bootstrapping;
keep expression nesting shallow in tests, or add TCO to the Python
interpreter first (small, safe change).

## 4. Higher-order functions — MISSING (real cost)

SEMANTICS.md §2, verbatim: **"Functions are not first-class values
in v0.2 (no function types, no passing `fn` as an argument). Lambdas
remain handler-only."**

Verified empirically:

```misa
let f = |x| x + 1
print(f(5))   // 7:7: E101: undefined function 'f'
```

The lambda binds but is not callable — the name `f` doesn't resolve
as a function.

**Impact on self-hosting:** the Python compiler uses higher-order
patterns pervasively:
- `emit.py`: ~30 `emit_*` methods dispatched on node type — solvable
  with if/else chains on `kind` (mechanical).
- `interp.py`: `LambdaValue` for Misa lambdas — a self-hosted
  interpreter must represent closures as data (record
  `{params, body, env}`) and apply them manually. Expressible, but
  the *host* interpreter's own lambda handling must be correct first.
- Visitor/list utilities (`map`/`filter`) — must be written as
  explicit loops. Mechanical.

**Verdict:** not a hard blocker, but every higher-order idiom becomes
an explicit loop or dispatch chain — estimate +30–50% line count vs
Python. First-class functions are already on the wanted list; landing
them BEFORE Phase 6 would shrink the self-hosted compiler
significantly. Recommended: yes, but not blocking.

## 5. Generics — MISSING (low impact for a compiler)

No generic functions or types (`Map<K,V>` is builtin magic, not
user-generics). A compiler mostly needs concrete types
(`[Tok]`, `Map<Text, Symbol]`, `[Stmt]`); homogeneous lists of
records suffice. The `[T]` list type with `T` = a record type covers
~95% of compiler data structures. **Verdict:** not a blocker.

## 6. I/O — PARTIAL (one hard gap)

| Capability | Status |
|---|---|
| `fs.read(path) -> Text` | ✅ STDLIB.md §2 |
| `fs.write(path, text)` | ✅ STDLIB.md §2 |
| directory listing | ❌ missing |
| argv / stdin | ❌ missing (`env()` exists for env vars — usable for argv-by-convention, ugly) |
| exit codes | ❌ missing (`assert` aborts E301; no clean exit(n)) |
| **subprocess** | ❌ **missing** |

**The hard gap is subprocess.** A self-hosted AOT compiler must turn
its output into a binary. Options:

1. **Emit LLVM IR as text** (`fs.write("out.ll", ir)`) then assemble
   with `clang` — but *something* must invoke clang. Without
   subprocess, the Misa program can't; a Python/shell driver would
   have to, which breaks "Misa compiles Misa" purity (it's
   "Misa emits IR, shell assembles").
2. **Emit machine code directly** — writing an assembler/linker in
   Misa. Absurd for Phase 6.
3. **Add `process.exec` to the stdlib** (new, small, auditable:
   `process.run(cmd: [Text]) -> {code: Int, out: Text}`), then the
   self-hosted compiler shells out to `clang`/`llc` exactly like a
   C compiler driver does. This is the standard bootstrap design
   (cf. early Go, which shelled out to the C assembler).

**Verdict:** add a minimal `process` namespace (6b prerequisite).
Directory listing and argv can be deferred (`env()` + fixed paths
suffice for the bootstrap).

## 7. AOT without llvmlite — the FFI question

Today `emit.py` builds LLVM IR via llvmlite (Python bindings) and
`build.py` links with gcc/NDK. A Misa program cannot import llvmlite.

**Without FFI (Phase 7), the viable design is IR-as-text:**

- The self-hosted emitter constructs LLVM IR *as a string* (string
  building with `+` — adequate; a `TextBuf` helper type keeps it
  linear-ish) and `fs.write`s `out.ll`.
- A `process.run(["clang", "out.ll", ...])` step assembles/links
  (requires §6's `process` addition).
- The emitted IR must be correct LLVM — the author must know the
  IR grammar. Mitigation: the Python emitter can gain
  `--emit-ir-text` mode first, producing reference `.ll` files the
  Misa emitter diffs against (golden tests).

**What this means for phase ordering:** Phase 6 does NOT need Phase 7's
FFI if it takes the IR-as-text route. The roadmap order (6 before 7)
is coherent under this design. FFI later lets the emitter call
LLVM's C API directly (faster, no clang dependency) — a Phase 7+
optimization, not a Phase 6 prerequisite.

**Caveat:** the C runtime (`impl/misa/aot/rt/*.c`) stays C —
self-hosting the *compiler* doesn't require rewriting the runtime
(Go kept its runtime in C for years; Rust still links C). The
`build.py` driver logic (flags per target, NDK paths) becomes Misa
string/table logic + `process.run`.

## 8. Minimal additions, ordered by dependency

### 6a — self-hosted frontend + interpreter (no new syntax)

1. **`str` namespace** (stdlib only): `slice(s, i, j)`,
   `starts_with`, `split`, `trim`, `ord`/`chr`. *Halves lexer size.*
2. **TCO or explicit depth guard** in the Python interpreter
   (robustness for deeply nested bootstrap programs).
3. *(optional, high value)* **First-class functions**: function
   types + callable values. Shrinks the self-hosted compiler ~30%.
   Already wanted independently.

Then: `misa-lexer.misa` → `misa-parser.misa` → `misa-checker.misa` →
`misa-interp.misa`, each validated by differential testing against
the Python implementation (same inputs → same ASTs/errors/behavior).
**6a exit:** the Misa interpreter runs the Misa test suite
(`tests/run_tests.py` equivalents) with identical results.

### 6b — self-hosted AOT (needs two stdlib additions)

4. **`process.run`** (stdlib): run a command, capture
   `{code: Int, stdout: Text, stderr: Text}`. *The* enabling
   primitive for a compiler driver.
5. **`--emit-ir-text`** mode on the Python emitter (dev tooling):
   golden `.ll` references for differential testing.
6. *(optional)* **FFI** (pulled forward from Phase 7) OR stay with
   IR-as-text + clang. Recommend IR-as-text for 6b; FFI is a later
   optimization.

Then: `misa-emit.misa` (AST → LLVM IR text) + `misa-build.misa`
(driver: flags, clang invocation), validated by compiling the Misa
test programs and diffing behavior vs Python-emitted binaries.
**6b exit (== Phase 6 exit):** `misa-build.misa`, *run by the
self-hosted interpreter*, compiles a Misa program to a working
native binary. Full circle: Misa compiles Misa.

### Explicitly NOT needed for Phase 6

- Sum types / generics (workarounds exist; nice-to-have).
- Rewriting the C runtime in Misa (stays C, like Go/Rust bootstraps).
- `misa fmt`/`lsp`/`pkg`/`serve` in Misa (tools, not the compiler).
- Model providers in Misa (the compiler doesn't call models).
- Directory listing, stdin, argv, exit codes (deferrable).

---

## Critical blockers (10-line summary)

1. **No subprocess** — a self-hosted AOT compiler cannot invoke
   clang/llc; add minimal `process.run` (stdlib-only change).
2. **No string slicing/split/starts_with** — lexer becomes 2–3×
   longer without them; add a small `str` namespace first.
3. **Lambdas aren't first-class** (verified: `f(5)` → E101) —
   every higher-order idiom becomes an explicit loop (+30–50%
   lines); landing first-class functions first would shrink the
   work substantially.
4. **No sum types** — ASTs become tagged records + string dispatch;
   mechanical, not blocking.
5. **llvmlite unusable from Misa** — emitter must generate LLVM IR
   *as text* and shell out to clang (needs #1); FFI is a later
   optimization, so Phase 6 doesn't depend on Phase 7.
6. Recursion works, `text[i]`/`for c in text`/`len(text)` work —
   the frontend is fully expressible today.
7. Recommended staging: **6a** frontend+interpreter in Misa
   (differential-tested vs Python), then **6b** IR-text emitter +
   build driver; exit = Misa-built Misa compiler produces a
   working native binary.
