# Misa FFI — calling C libraries

`extern` blocks declare foreign functions from shared libraries. Both
the interpreter and AOT-compiled binaries can call them.

## Basic example

```misa
extern "c" {
    fn strlen(s: Text) -> Int,
    fn strcmp(a: Text, b: Text) -> Int,
}

extern "m" {
    fn sqrt(x: Float) -> Float,
    fn pow(x: Float, y: Float) -> Float,
}

print(strlen("hello"))      // 5
print(strcmp("abc", "abc")) // 0
print(sqrt(16.0))           // 4.0
print(pow(2.0, 10.0))       // 1024.0
```

- `extern "<lib>" { ... }` — `"<lib>"` names the shared library.
  `"c"` is the C standard library (linked automatically); `"m"` is
  libm. Other libraries link with `-l<lib>` under AOT.
- Declarations use ordinary `fn` syntax. Calls check arity and types
  (E102/E211) exactly like Misa functions.
- Verified identical on interpreter and AOT (host).

## Type mapping

| Misa | C | Notes |
|---|---|---|
| `Int` | `long` | 64-bit on all current targets |
| `Float` | `double` | |
| `Text` | `char*` | Null-terminated; copied both directions |
| `Bool` | `int` | 0/1 |
| `Ptr` | `void*` | Opaque — pass it back, compare with `nil`, free via the library's own free function |
| `Void` | `void` | |

```misa
extern "c" {
    fn malloc(n: Int) -> Ptr,
    fn free(p: Ptr) -> Void,
}

let p = malloc(64)
print(p == nil)  // false
free(p)
```

- `Text` arguments are null-terminated *copies* — the C side must not
  retain them past the call.
- `Text` returns are copied into Misa-managed memory immediately.
- `Ptr` cannot be dereferenced from Misa. There is no pointer
  arithmetic.

## Structs by value

`extern` blocks can declare C-layout structs, usable by value and by
pointer in signatures:

```misa
extern "c" {
    struct Point { x: Float, y: Float },
    fn hypot(x: Float, y: Float) -> Float,
}

let p = Point { x: 3.0, y: 4.0 }   // construct
print(p.x)                          // 3.0 — field read
print(hypot(p.x, p.y))              // 5.0
```

- Field types: `Int`, `Float`, `Bool`, `Ptr`, plus **nested
  structs/unions** and **`Int` bitfields** (`a: Int:3`). In particular
  `Int` fields are C `long` (64-bit) — a C struct with 32-bit `int`
  fields does **not** match; write a shim with `int64_t` fields or
  pass the struct by pointer.
- Construction requires every field (`Point { x: 1.0 }` is E211);
  unknown fields are E101; mistyped fields are E211.
- Structs cross by value with the platform C ABI (≤16-byte structs
  in registers, larger via `sret`/`byval` — handled for you on
  x86-64 and AArch64).
- The library string may be a direct shared-object path
  (`extern "/tmp/mylib.so" { ... }`), which the interpreter loads
  with `dlopen` and AOT links directly — handy for test shims.

### Nested structs

A struct field may be another `extern` struct — layout follows C
rules exactly (alignment padding included), verified byte-for-byte
against GCC in the test suite:

```misa
extern "c" {
    struct Point { x: Int, y: Int },
    struct Outer { tag: Bool, p: Point, n: Int },
}

let o = Outer { tag: true, p: Point { x: 3, y: 4 }, n: 5 }
print(o.p.x)   // 3 — chained field access
```

Cyclic nesting (`struct A { b: B }` / `struct B { a: A }`) is E225 —
C requires complete by-value types.

### Unions

`union` declares a C union: every field starts at offset 0, and all
fields share the same memory. Writing one field and reading another
reinterprets the bits, exactly like C:

```misa
extern "c" {
    union Value { i: Int, f: Float },
}

let v = Value { i: 42 }
print(v.i)   // 42
```

- A union literal may set **any subset** of fields (`Value { i: 1 }`;
  unset bytes are zero).
- Reading a field reinterprets whatever was last written — including
  values returned from C.
- At the FFI boundary, unions are coerced to the platform's register
  classes (verified against `gcc -S`: an `{Int, Float}` union crosses
  in one integer register, an all-`Float` union in an SSE register).

### Bitfields

`field: Int:N` (1 ≤ N ≤ 64) declares an unsigned C bitfield. Fields
pack into 8-byte units, little-endian (bit 0 = LSB), never straddling
a unit — the same rules GCC uses, verified bit-for-bit in the test
suite:

```misa
extern "c" {
    struct Flags { a: Int:3, b: Int:5, mode: Int },
}

let f = Flags { a: 5, b: 17, mode: 1 }
print(f.a)   // 5
```

- Bitfields are **unsigned**: values range `0 .. 2^N - 1`. An
  out-of-range construction value is a catchable **E300** (both
  backends); widths outside 1–64 and non-`Int` bitfields are E225.
- Bitfields are little-endian only in v1 (all Misa targets are
  little-endian: x86-64, AArch64, ARMv7, wasm32).

## Callbacks: Misa closures as C function pointers

A `(A, B) -> R` parameter type means "C function pointer" — pass a
Misa lambda or named function:

```misa
extern "c" {
    fn qsort(base: Ptr, n: Int, sz: Int, cmp: (Ptr, Ptr) -> Int) -> Void,
    fn malloc(n: Int) -> Ptr,
}

let cmp = |a: Ptr, b: Ptr| {
    // ... compare the two elements, return -1/0/1
    return 0
}
qsort(base, n, 8, cmp)
```

- The closure's signature must match the declared callback type
  (E211 otherwise). Named functions work too — but a function that
  calls `extern` functions needs its `throws E226` clause, like
  anywhere else.
- The callback's parameter/return types are the scalar FFI types
  (`Int`, `Float`, `Text`, `Bool`, `Ptr`, `Void` return). Structs
  and nested callbacks in a signature are E225 in v1.
- **Lifetime (v1):** the callback is valid only for the duration of
  the `extern` call. The trampoline stays alive (no GC
  use-after-free), but if C retains the pointer and calls it later,
  behavior is undefined. Callbacks are re-entrant-safe (nested
  extern calls with callbacks work).
- A Misa error raised *inside* a callback cannot unwind through C:
  it is printed and the process aborts (v1) — never silently
  ignored.

## Varargs

C varargs functions declare a `...` tail:

```misa
extern "c" {
    fn snprintf(buf: Ptr, size: Int, fmt: Text, ...) -> Int,
}

let buf = malloc(64)
let n = snprintf(buf, 64, "%d %f %s", 42, 2.5, "hi")
// n == 16, buf holds "42 2.500000 hi"
```

- The fixed prefix checks arity/types as usual; the `...` tail
  needs at least the fixed args (fewer is E102).
- Each vararg's C type derives from its Misa type: `Int`→`long`,
  `Float`→`double`, `Text`→`char*`, `Bool`→`int`, `Ptr`→`void*`
  (C default argument promotion applies). Anything else is E225.
  Structs up to 16 bytes may cross as varargs; larger ones are E225
  (pass by pointer instead).

## Errors

- **E225** — invalid `extern` signature (empty library name, or a type
  that can't cross the boundary):

  ```
  2:27: E225: extern function 'strlen': return has type '[Int]', which
  cannot cross the FFI boundary (supports Int, Float, Text, Bool, Ptr,
  Void, declared structs, and (A, B) -> R callbacks)
  ```

- **E226** — the library can't be loaded, the symbol is missing, or the
  call failed. This one fires at *runtime* and is catchable:

  ```
  1:1: E226: cannot load library 'nosuchlib_xyz': libnosuchlib_xyz:
  cannot open shared object file: No such file or directory
  ```

- **Foreign crashes are NOT caught.** If the C function segfaults, your
  program segfaults — same as C. FFI is explicitly unsafe: `misa check`
  verifies *signatures*, not *semantics*.

- Using an `extern` function as a value (e.g. `let f = strlen`) is
  E214 — extern functions are not first-class.

## The negative-`int` footgun

Misa `Int` maps to C `long` (64-bit). A C function declared to return
`int` (32-bit) gets its 32-bit result *zero-extended* — a negative C
`int` arrives as a large positive number:

```misa
extern "c" {
    fn strcmp(a: Text, b: Text) -> Int,
}
print(strcmp("a", "b"))   // 4294967295, NOT -1
```

C returned `-1`; Misa shows `4294967295`. This is consistent across
backends, but it *will* bite you if you test `< 0` or compare with
`-1`. Patterns that work:

- Never test the raw result with `< 0` — it can never be true.
- Wrap the call in a tiny C shim that normalizes to `0`/`1` (or
  `Bool`) and declare the shim in your `extern` block.
- For "negative on error" C APIs, normalize through a shim returning
  `Bool` before branching.

## Limitations

- No C++ name mangling, no automatic header binding.
- Struct fields: `Int`/`Float`/`Bool`/`Ptr`, nested structs/unions,
  and `Int` bitfields (no `Text` fields — E225).
- Callbacks: scalar signatures only in v1; valid only during the
  extern call; errors inside callbacks abort.
- Varargs: structs over 16 bytes are E225.
- Android: `extern "c"` / `"m"` link via the NDK; other libraries fail
  the link with a clean `E600`. iOS: same via the Xcode template
  (device run not yet verified).
- `process.run`-style subprocess FFI is interpreter-only (E600 under
  AOT).
