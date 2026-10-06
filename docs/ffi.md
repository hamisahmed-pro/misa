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

## Errors

- **E225** — invalid `extern` signature (empty library name, or a type
  that can't cross the boundary):

  ```
  2:27: E225: extern function 'strlen': return has type '[Int]', which
  cannot cross the FFI boundary (v1 supports only Int, Float, Text,
  Bool, Ptr, Void)
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

## Limitations (v1)

- No structs by value, no callbacks (Misa fn → C function pointer),
  no varargs, no C++ name mangling, no automatic header binding.
- Android: `extern "c"` / `"m"` link via the NDK; other libraries fail
  the link with a clean `E600`. iOS: same via the Xcode template
  (device run not yet verified).
- `process.run`-style subprocess FFI is interpreter-only (E600 under
  AOT).
