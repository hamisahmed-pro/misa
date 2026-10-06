# Phase 7 — FFI: calling C libraries from Misa

**Ordered by Hamis 2026-10-06** (with stdlib growth; showcase app follows
when both are "significant, unprecedented, unmatched").

## Syntax

```misa
extern "c" {
  fn strlen(s: Text) -> Int,
  fn sqrt(x: Float) -> Float,
}

extern "libpq" {
  fn PQconnectdb(conninfo: Text) -> Ptr,
  fn PQstatus(conn: Ptr) -> Int,
  fn PQfinish(conn: Ptr) -> Void,
}

let n = strlen("hello")   // 5
```

- `extern "<lib>" { ... }` declares foreign functions from a shared
  library. `"c"` is the C standard library (no `-l` flag needed).
- Declarations use normal Misa `fn` syntax (name, typed params, return).

## Type mapping (v1)

| Misa | C | Notes |
|---|---|---|
| `Int` | `long` | 64-bit on all current targets |
| `Float` | `double` | |
| `Text` | `char*` | Null-terminated; Misa→C copies, C→Misa copies |
| `Bool` | `int` | 0/1 |
| `Ptr` | `void*` | Opaque; cannot be dereferenced from Misa |
| `Void` | `void` | |

**v1 limits:** no structs by value, no callbacks (Misa fn → C function
pointer), no varargs. These are Phase 8+.

## Semantics

- Text arguments are null-terminated copies; the C side must not retain
  them past the call (documented, unchecked in v1).
- Text returns are copied into Misa-managed memory immediately.
- `Ptr` values are opaque handles — pass them back, compare to nil, free
  via the library's own free function.
- Errors: foreign call crashes are NOT caught (same as C). Document loudly.

## Implementation

- **Interpreter:** Python `ctypes` — `CDLL`, argtypes/restype from the
  Misa signature. ~50 lines.
- **AOT:** llvmlite `declare` the function with the mapped LLVM types;
  link with `-l<lib>` (skip for `"c"`). Text marshalling reuses the
  existing `misa_text_*` runtime helpers.
- **Checker:** `extern` decls registered like `fn` decls; calls check
  arity/types with E102/E211 as normal. `Ptr` is an opaque named type.

## Safety stance

FFI is explicitly unsafe — no sandboxing, no validation of C behavior.
`misa check` verifies *signatures*, not *semantics*. The spec and docs
say this plainly. (A future `safe extern` with result-code checking is
Phase 8+.)

## Exit criteria

- [ ] `extern "c"` strlen/sqrt demo works on interpreter + AOT (host).
- [ ] `extern "libpq"` connects to Postgres (or another real C library
      demo if libpq is unavailable in CI — sqlite3 via FFI as fallback,
      proving the mechanism on a second library).
- [ ] New E-codes (if any) documented; full suite green.
- [ ] Android: `extern` works via NDK libraries (at least `libc` math).

## Deliberately deferred

Structs by value, callbacks, varargs, C++ name mangling, `safe extern`,
automatic binding generation from headers.
