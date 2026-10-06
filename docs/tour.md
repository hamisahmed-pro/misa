# Misa language tour

A hands-on walkthrough. Every example below runs — save each block to a
`.misa` file and try `misa check` / `misa run` on it.

## Values and types

```misa
let name = "Ada"          // Text
let age = 36              // Int (64-bit, wraps on overflow)
let pi = 3.14159          // Float (64-bit double)
let ok = true             // Bool
let nothing = nil         // nil — the absence of a value

print(age + 1)            // 37
print(pi * 2)             // 6.28318
print(not ok)             // false
```

`let` bindings are immutable; `var` bindings are mutable:

```misa
var count = 0
count = count + 1
print(count)              // 1
```

Types can be annotated (`let x: Int = 1`); the checker verifies them
(`misa check`). `Int` arithmetic wraps modulo 2^64 on overflow — the
interpreter and compiled binaries agree bit-for-bit.

## String interpolation

Any `"..."` string can embed `{expression}`:

```misa
let name = "Ada"
let n = 3
print("hello {name}, you have {n + 1} messages")
// hello Ada, you have 4 messages
```

`{{` renders a literal `{`. (There is no `}}` escape — a `}` that doesn't
close an interpolation is just a literal character.) Interpolation cannot
nest double-quoted strings: compute the value first, then interpolate it.

### Raw strings

Prefix with `r` to disable interpolation and escapes — braces and
backslashes stay literal. Ideal for JSON templates, regexes, and
Windows paths:

```misa
let name = "Ada"
print(r"hello {name}")          // hello {name}
print(r"""{"user": "{name}"}""")  // {"user": "{name}"}
print(r"C:\new\folder")         // C:\new\folder
```

## Functions

```misa
fn add(a: Int, b: Int) -> Int {
    return a + b
}

fn greet(name: Text) {
    print("hi {name}")
}

print(add(2, 3))   // 5
greet("Ada")       // hi Ada
```

- A `fn` without `-> Type` returns `Void`.
- Every code path of a non-`Void` function must `return` (checked: E211).
- Arity and argument-type mismatches are caught by `misa check`
  (E102 / E211), not at runtime.

## First-class functions and closures

Functions are values. Lambdas use `|params| body`:

```misa
fn apply(f: (Int) -> Int, x: Int) -> Int {
    return f(x)
}

let double = |x: Int| x * 2
print(apply(double, 21))        // 42
print(apply(|x| x + 1, 41))    // 42 — param type inferred from use
```

Lambdas capture their defining scope:

```misa
fn make_adder(n: Int) -> (Int) -> Int {
    return |x| x + n
}

let add5 = make_adder(5)
print(add5(10))   // 15
```

Any `fn` declaration can be used as a value too: `let g = add` gives
`g: (Int, Int) -> Int`. Builtins (`print`, `str`, `fs.read`, …) are *not*
values — using one as a value is E214.

## Sum types and `match`

```misa
type Shape {
    Circle(radius: Float),
    Rect(w: Float, h: Float),
}

fn area(s: Shape) -> Float {
    return match s {
        Shape.Circle(r) => r * r * 3.14,
        Shape.Rect(w, h) => w * h,
    }
}

print(area(Shape.Circle(1.0)))  // 3.14
print(area(Shape.Rect(2.0, 3.0))) // 6.0
```

`match` is an expression — it yields a value. The checker enforces
exhaustiveness: miss a variant and you get E216 (or add a `_` wildcard
arm). Arm types must agree (E217). Variants with no payload:

```misa
type Opt {
    None,
    Some(Int),
}

fn or_zero(o: Opt) -> Int {
    return match o {
        Opt.None => 0,
        Opt.Some(n) => n,
    }
}

print(or_zero(Opt.None))     // 0
print(or_zero(Opt.Some(41))) // 41
```

## Generics

```misa
fn first<T>(xs: [T]) -> T {
    return xs[0]
}

type Box<T> {
    value: T,
}

print(first([10, 20, 30]))  // 10  (T inferred = Int)
print(first(["a", "b"]))    // a   (T inferred = Text)
print(first<Float>([1.5]))  // 1.5 (pinned explicitly)

let b: Box<Int> = Box<Int>(7)
print(b.value * 2)          // 14
```

Generic sum types compose with `match`:

```misa
type Result<T> {
    Ok(T),
    Err(Text),
}

fn unwrap<T>(r: Result<T>, d: T) -> T {
    return match r {
        Result.Ok(v) => v,
        Result.Err(_) => d,
    }
}

print(unwrap(Result.Ok(7), 0))       // 7
let bad: Result<Int> = Result.Err("boom")
print(unwrap(bad, 0))               // 0
```

Type parameters are unconstrained in v1 (no `where` clauses). The AOT
compiler monomorphizes — one specialization per concrete type tuple.

## Errors: `try` / `catch` / `throw`

Runtime, I/O, HTTP, model, and budget failures surface as catchable
`Error` values (`{code, message}`):

```misa
fn load(path: Text) -> Text {
    var out = ""
    try {
        out = fs.read(path)
    } catch e {
        print("failed [{e.code}]: {e.message}")
        out = "fallback"
    }
    return out
}
```

Custom errors with `throw` (the value must be an `Error` — E212
otherwise):

```misa
fn need_positive(n: Int) -> Int {
    if n <= 0 {
        throw { code: "EBAD", message: "need a positive number" }
    }
    return n
}

try {
    need_positive(-1)
} catch e {
    print("caught {e.code}: {e.message}")
}
// caught EBAD: need a positive number
```

Static errors (E0xx–E2xx) are compile-time and cannot be caught.

## Collections

**Lists** — ordered, `[T]`:

```misa
let xs = [1, 2, 3]
print(xs[0])        // 1
print(len(xs))      // 3
let ys = xs + [4]   // [1, 2, 3, 4]
for x in xs {
    print(x * 10)
}
```

**Maps** — `Map<Text, V>` or `Map<Int, V>`, string keys:

```misa
var scores: Map<Text, Int> = {"ada": 92, "bisi": 71}
print(scores["ada"])  // 92
scores["chidi"] = 58  // insert (needs var)
print(len(scores))    // 3
```

A missing key throws catchable E305. `{}` is an empty *record*; annotate
for an empty map.

**Records** — declared types with named fields:

```misa
type User {
    name: Text,
    age: Int,
}

let u: User = { name: "Ada", age: 36 }
print(u.name)   // Ada
```

Field names and types are checked at compile time (E202 on mismatch).

## Control flow

```misa
let n = 7
if n % 2 == 0 {
    print("even")
} else if n % 3 == 0 {
    print("divisible by 3")
} else {
    print("odd")
}

var i = 0
while i < 3 {
    print(i)
    i = i + 1
}

for c in ["a", "b", "c"] {
    print(c)
}

// `for x in <text>` iterates Unicode code points (like Python).
// For an explicit list, use str.chars:
for c in str.chars("héllo") {
    print(c)  // h, é, l, l, o
}
```

`and` / `or` / `not` are the boolean operators.

## Modules

Split code across files with `import` (path relative to the importing
file):

```misa
// utils.misa
fn shout(s: Text) -> Text {
    return s + "!"
}
```

```misa
// main.misa
import "./utils.misa"
print(shout("hey"))   // hey!
```

Lookup is flat — `shout(...)`, not `utils.shout(...)`. Packages
(`import "my-pkg"`) work the same way; see `misa pkg --help` and
`docs/stdlib.md` for the bundled `std/` packages.

## What's next

- `docs/stdlib.md` — the full library reference
- `docs/ffi.md` — calling C from Misa
- `docs/errors.md` — error catalog
- `docs/targets.md` — compiling for other platforms
