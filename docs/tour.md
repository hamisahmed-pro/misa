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

## Errors: `try` / `catch` / `throw` / `throws`

Runtime, I/O, HTTP, model, and budget failures surface as catchable
`Error` values (`{code, message}`). Error *codes* are part of the
function signature — the checker verifies them (E227 otherwise):

```misa
fn load(path: Text) -> Text throws E303 {
    var out = ""
    try {
        out = fs.read(path)   // E303: caught, so no `throws` needed...
    } catch e {
        print("failed [{e.code}]: {e.message}")
        out = "fallback"
    }
    return out
}
```

Wait — `fs.read` is inside `try`, so why does `load` declare
`throws E303`? It doesn't need to! `catch` is total: it discharges
every code raised in the `try` body. This version is clean without
the clause:

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

But when the error *escapes* the function, you must declare it:

```misa
fn fetch(u: Text) -> Text throws E304 {
    return http.get(u)   // E304 escapes: declared in the signature
}

fn load2(u: Text) -> Text {
    return fetch(u)      // E227: unhandled E304 — declare or catch
}
```

Custom errors with `throw` (the value must be an `Error` — E212
otherwise; the thrown code is tracked like any other):

```misa
fn need_positive(n: Int) -> Int throws EBAD {
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

The rules in one paragraph: the checker infers every function's
throws set (which E-codes its body may raise). At each call, `throw`,
or `await`, the raised codes must be declared in the enclosing
function's `throws` clause or caught by `try`. Calls are checked;
operators (`/`, `[i]`) are unchecked by design. `misa check --json`
prints the per-function throws graph (`declared` + `inferred`).

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

## Concurrency

Misa's concurrency is **structured**: tasks can't leak, everything is
typed, and timeouts are catchable errors. No OS threads — cooperative
tasks that yield at `await`, channel receives, and `sleep`.

```misa
// spawn a concurrent task; T is inferred from the block's return type
let t: Task<Text> = spawn {
    return http.get("https://api.example.com/a")
}

// ... do other work here ...

// wait for the result (yields until done)
let body: Text = await t
print(body)

// wait with a timeout budget (milliseconds) — expiry is a catchable E306
try {
    let b2 = await t timeout 5000
} catch e {
    print(e.code)  // E306: "task timed out after 5000ms"
}

// sleep without blocking other tasks
sleep(100)
```

**Typed channels** move values between tasks:

```misa
let ch = chan<Int>()
spawn { ch.send(40 + 2) }
let v: Int = ch.recv()   // yields until a value arrives
print(v)                 // 42

close(ch)                // after close, recv drains, then raises E306
```

**Parallel fetch** — the 90% use case in one line (order-preserving;
the first error is raised, catchable):

```misa
let pages: [Text] = fetch_all([
    "https://a.example.com",
    "https://b.example.com",
])
```

Rules that keep you out of trouble:

- A task spawned inside a function is **cancelled when the function
  returns** — its `await`s raise E306 `"task cancelled"`. No orphans.
- A throwing task captures its error; `await` **rethrows it** at the
  awaiter (catchable). A task that is never awaited reports its error
  to stderr at program end — never silent.
- `await` on anything that isn't a `Task` (or a model `TokenStream`)
  is a compile-time E205.
- Scheduling is cooperative round-robin: a task runs until it yields.
  A task that never yields starves the others (same as JavaScript) —
  and tasks share memory, so prefer channels and task results over
  mutable sharing.
- `Task`/`Chan` values can't cross the FFI boundary (E225).

On the web target, `spawn` raises E600 (fibers aren't available there);
everywhere else — interpreter, host binaries, Android, iOS — behaves
identically.

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

## Screens and ephemeral state

A `screen` is a component tree. `navigate(Home())` shows it:

```misa
screen Home(title: Text) {
    Text(text: "hello {title}")
    Button(label: "go", on_press: || { print("tap") })
}

navigate(Home("world"))
```

For ephemeral UI state — the kind React solves with `useState` — use
`state()`:

```misa
screen Counter() {
    let count = state(0)   // per-screen-instance, typed Int
    Text(text: "count: {count}")
    Button(label: "inc", on_press: || { count = count + 1 })
}
```

Assigning to a state binding re-renders the screen on every target
(CLI text, Android, iOS, web). Handlers can freely read and write the
screen's `state()` bindings. A few rules:

- `state()` must be bound with `let`/`var` in the screen body (not in
  component args, handlers, or `for` bodies).
- State is typed: `count = "x"` is a compile error (E211).
- `navigate()` resets state — it is ephemeral. For durable state, use
  SQLite (`db.*`).
- Like React hooks, state slots are source-ordered: don't conditionally
  reorder `state()` declarations between renders.

## Testing

Unit tests live in `*_test.misa` (or `test_*.misa`) files as `test` blocks.
`test` is a contextual keyword — `test` used as a variable or call still
parses normally; only `test <name> {` at the top level is a test block.

```misa
fn add(a: Int, b: Int) -> Int {
    return a + b
}

test addition {
    assert_eq(add(2, 3), 5)
    assert_ne(add(2, 2), 5)
    assert(add(0, 0) == 0)
}
```

- `assert_eq(a, b)` / `assert_ne(a, b)` abort with `E301` and render both
  values: `assert_eq failed: 2 != 3`.
- Each test runs in a **fresh interpreter**: top-level statements re-run
  per test, so shared `var` fixtures reset. A failing test never kills
  the run. Test bodies can see top-level bindings (unlike `fn` bodies).
- `misa test [path]` discovers test files (a directory is walked
  recursively), prints `PASS`/`FAIL` per test plus a summary
  (`12 passed, 2 failed`), and exits non-zero on failure.
- `misa test --json` emits structured results
  (`kind`/`name`/`file`/`line`/`pass`/`message`) — the red/green loop
  contract for agents.
- Model `eval` blocks keep working: `misa test` runs them too, reported
  as `eval` results.

## Command-line arguments

`argv()` returns the program's command-line arguments as `[Text]`,
excluding the program name (like Python's `sys.argv[1:]`):

```misa
for arg in argv() {
    print("arg: " + arg)
}
```

```sh
misa run prog.misa -- hello world   # argv() == ["hello", "world"]
```

Args after `--` go to the program; without `--` the program sees `[]`.
Host AOT binaries receive the real OS argv. On Android/iOS/web there is
no CLI, so `argv()` is `[]` there.

## What's next

- `docs/stdlib.md` — the full library reference
- `docs/ffi.md` — calling C from Misa
- `docs/errors.md` — error catalog
- `docs/targets.md` — compiling for other platforms
