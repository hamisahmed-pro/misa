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

## Type mapping

| Misa | C | LLVM | ctypes | Notes |
|---|---|---|---|---|
| `Int` | `long` | `i64` | `c_long` | 64-bit on all current targets |
| `Float` | `double` | `double` | `c_double` | |
| `Text` | `char*` | `i8*` | `c_char_p` | Null-terminated; Misa→C copies, C→Misa copies |
| `Bool` | `int` | `i32` | `c_int` | 0/1 |
| `Ptr` | `void*` | `i8*` | `c_void_p` | Opaque; cannot be dereferenced from Misa |
| struct `S` | `struct S` | LLVM struct type | `ctypes.Structure` | Declared via `struct S { ... }`; by value |
| `(A, B) -> R` (param) | `R (*)(A, B)` | ptr to fn type | `CFUNCTYPE` | Misa closure as C callback |
| `Void` | `void` | `void` | `None` | |

**v2 (Overtake Wave 2, 2026-10-06):** structs by value, callbacks,
and varargs are implemented (see below).
**v3 (Overtake final wave, 2026-10-07):** nested structs, unions, and
bitfields (see below) — F1 moves PAR → **LEAD**. Remaining: C++ name
mangling, `safe extern`, automatic binding generation from headers.

### Structs

```misa
extern "c" {
  struct Point { x: Float, y: Float },
  fn scale(p: Point, s: Float) -> Point,
}
let p = Point { x: 3.0, y: 4.0 }
```

- Declared inside the `extern` block (`struct` is a contextual
  keyword — a plain identifier everywhere else).
- v1 fields: `Int`, `Float`, `Bool`, `Ptr` only (E225 otherwise).
  The mapping is uniform: `Int` fields are C `long`, so a C struct
  with 32-bit `int` fields does not match (extends the L-7 footgun;
  document, don't special-case).
- Construction `S { f: v, ... }` requires every field (E211 if
  missing); unknown fields E101; mistyped E211; duplicate E210.
- Field read `p.x` type-checks (E101 on unknown field).
- ABI: ≤16-byte structs cross in registers; larger returns use the
  explicit `sret` form and larger params the `byval` form (x86-64
  SysV / AAPCS64). Raw LLVM struct args mislower (registers instead
  of stack) — `byval` is required; verified against GCC 13 output.
- Interpreter: `ctypes.Structure` subclasses (exact C layout).
- AOT: values are heap byte buffers (`M_TAG_STRUCT`) boxed at the
  boundary; field reads compile to GEP+load.

### Nested structs (v3)

```misa
extern "c" {
  struct Point { x: Int, y: Int },
  struct Outer { tag: Bool, p: Point, n: Int },
}
let o = Outer { tag: true, p: Point { x: 3, y: 4 }, n: 5 }
print(o.p.x)   // 3
```

- A field may be any other declared struct/union (E225 on unknown
  types). Layout is recursive C layout: nested aggregates contribute
  their own size/alignment, padding included.
- Construction nests literals; field access chains (`o.p.x`)
  type-checks through the nesting.
- Cyclic by-value nesting is E225 (C requires complete types).
- Interpreter: nested `ctypes.Structure` classes. AOT: nested LLVM
  struct types; inner values marshal by unboxing the MVal*.

### Unions (v3)

```misa
extern "c" {
  union Value { i: Int, f: Float },
}
let v = Value { i: 42 }   // any subset of fields; unset bytes are zero
print(v.i)                // 42 — read reinterprets last write
```

- `union` is a contextual keyword (identifier everywhere else).
- Every field is at offset 0; size = max field size; alignment = max
  field alignment. Reading a field reinterprets the stored bits
  (write `i`, read `f`), exactly like C — including values that
  crossed from C.
- A union-typed variable may be read but a union literal may set any
  subset of fields (structs still require every field, E211).
- ABI subtlety, verified against `gcc -S`: a bare LLVM byte array
  mislowers (passed on the stack) while C passes small unions in
  registers. The backend coerces unions at the FFI boundary to the
  SysV per-eightbyte register classes (`impl/misa/ffi_layout.py`:
  `union_chunk_classes`): `{Int, Float}` → one integer register,
  all-`Float` → one SSE register, 16-byte unions → two registers.
  Unions over 16 bytes use the same `byval`/`sret` path as structs.
- Interpreter: `ctypes.Union` (native). AOT: in-memory byte array
  with bitcast field accessors; coerced type at calls.

### Bitfields (v3)

```misa
extern "c" {
  struct Flags { a: Int:3, b: Int:5, mode: Int },
}
let f = Flags { a: 5, b: 17, mode: 1 }
```

- Syntax `field: Int:N`, 1 ≤ N ≤ 64. Non-`Int` bitfields and widths
  outside 1–64 are E225 (`:0` and unnamed bitfields are E225).
- Packing follows GCC: consecutive bitfields share 8-byte (`Int`)
  units, little-endian (bit 0 = LSB), never straddling a unit; a
  non-bitfield field closes the open unit. In unions each bitfield
  starts at bit 0 of offset 0 (matches GCC).
- Bitfields are **unsigned** (`0 .. 2^N - 1`); an out-of-range
  construction value is a catchable **E300** on both backends (no
  silent truncation, no sign extension).
- Endianness: little-endian only in v1 — all Misa targets (x86-64,
  AArch64, ARMv7, wasm32) are little-endian. Documented; no E600
  path needed.
- There is no struct field *assignment* in Misa, so bitfields need
  only construction packing + read extraction. Interpreter:
  `ctypes` bitfield tuples. AOT: shift+mask insert/extract on the
  unit word.
- Layout authority: `impl/misa/ffi_layout.py` is the single source
  of truth, shared by checker, interpreter, and AOT. The test suite
  compiles a C reference with GCC and compares every offset, size,
  alignment, and bit position (`ffi3: byte-exact C layout vs GCC`).

### Callbacks

```misa
extern "c" {
  fn qsort(base: Ptr, n: Int, sz: Int, cmp: (Ptr, Ptr) -> Int) -> Void,
}
```

- `(A, B) -> R` in parameter position = C function pointer.
  Signature parts are scalar FFI types (E225 otherwise); callbacks
  as return types are E225 (v1).
- Call sites: the argument must be a function value (lambda or named
  fn) with a matching signature (E211 otherwise).
- Interpreter: `ctypes.CFUNCTYPE` trampoline; kept alive for the
  interpreter's lifetime.
- AOT: one generated trampoline per signature with a global closure
  slot; the call site saves/sets/restores the slot (nesting-safe).
  A Misa error inside a callback is fatal in v1 (reported + abort —
  it cannot unwind through C frames).
- v1 rule: callbacks are valid only for the duration of the extern
  call (documented; escaping is undefined).

### Varargs

```misa
extern "c" {
  fn printf(fmt: Text, ...) -> Int,
}
```

- `...` is lexed as its own token; only legal on extern signatures
  (parse error on Misa functions).
- Calls need at least the fixed args (E102); each tail arg's C type
  derives from its Misa type (E225 otherwise); `Task`/`Chan`
  rejected (E225).
- Interpreter: tail converted explicitly (ctypes does not convert
  varargs).
- AOT: `var_arg` LLVM function type; tail kinds from literals,
  FFI-typed variables, struct literals, and extern calls (E225 when
  undeterminable — bind to a typed variable first).
- Structs over 16 bytes are E225 as varargs (the indirect form is
  not expressible for `...` tails in v1).

**v1 limits (remaining):** C++ name mangling, `safe extern`,
automatic binding generation from headers.

**C-`int` width footgun (L-7):** Misa `Int` maps to C `long` (64-bit), so a
C function declared to return `int` (32-bit) has its 32-bit result
zero-extended into the 64-bit Misa `Int`. A negative C `int` therefore
arrives as a large positive number — e.g. `strcmp("a", "b")` returns C
`-1`, which surfaces in Misa as `4294967295`, not `-1` (verified
identical on interpreter and AOT). It is consistent — but it is *your*
bug if you compare the result against `-1` or test `< 0`. Patterns:
- Never test the raw result with `< 0`; it will never be true.
- Wrap the call in a tiny C shim that normalizes to `0`/`1` (or another
  unambiguous encoding) and declare the shim in the `extern` block.
- For C APIs whose contract is "negative on error / non-negative on
  success", normalize through a shim returning `Bool` before branching.

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

C++ name mangling, `safe extern`, automatic binding generation from
headers.

## Exit criteria (v2)

- [x] Structs by value: declared in `extern` blocks, constructed as
      `S { ... }`, cross by value on interpreter + AOT (host),
      correct C ABI incl. >16-byte `sret`/`byval` (verified vs GCC).
- [x] Callbacks: `(Ptr, Ptr) -> Int` params take Misa closures;
      end-to-end via libc `qsort` on interpreter + AOT.
- [x] Varargs: `...` declarations, checked fixed prefix, mapped tail;
      end-to-end via `snprintf` on interpreter + AOT.
- [x] Clean E-codes for everything the backends can't do (E225/E211/
      E101/E102); full suites green (356 + 28 AOT parity).

## Exit criteria (v3 — overtake final wave, 2026-10-07)

- [x] Nested structs: recursive C layout (padding/alignment),
      nested literals, chained field access — interpreter + AOT
      byte-identical, round-trip through C by value.
- [x] Unions: same-memory fields, write/read reinterpretation,
      any-subset literals; SysV register-class coercion at the FFI
      boundary verified against `gcc -S` (a bare LLVM byte array
      mislowers to the stack — found and fixed).
- [x] Bitfields: `Int:N` syntax, GCC packing rules (8-byte LE units,
      no straddle), unsigned with catchable E300 on out-of-range
      construction values, E225 on bad widths/non-Int.
- [x] Byte-exact layout proof: the suite compiles a C reference with
      GCC and compares every offset/size/alignment/bit position
      against `impl/misa/ffi_layout.py`
      (`ffi3: byte-exact C layout vs GCC`); `tests/aot/prog/a30_ffi3`
      proves interpreter/AOT/C agreement end to end.
- [x] Full suites green (411 + 33 AOT parity).
