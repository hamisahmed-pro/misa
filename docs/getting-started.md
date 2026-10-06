# Getting started with Misa

Misa is a programming language for writing cross-platform apps. This guide
gets you from zero to a running program in about five minutes.

> **Alpha honesty.** Misa is pre-release software. The interpreter and the
> host (Linux/macOS) compiler are solid and heavily tested. Mobile and web
> targets build, but on-device features have only been verified on
> simulators/emulators so far — see `docs/targets.md`. APIs may change
> between versions.

## Install

Prerequisites: Python 3.10+, a C compiler (`gcc` or `clang`), and `git`.

```bash
git clone <your-misa-repo-url> misa
cd misa
pip install llvmlite==0.50.0      # needed for `misa build` (AOT compiler)
chmod +x impl/bin/misa
export PATH="$PWD/impl/bin:$PATH" # optional, for convenience
```

Check it works:

```bash
misa --help
```

You should see the command list (`run`, `check`, `build`, `test`, `fmt`,
`serve`, `lsp`, `pkg`).

> `misa run` (the interpreter) works with plain Python — no extra
> dependencies. `misa build` needs `llvmlite` and a C compiler.

## Your first program

Create `hello.misa`:

```misa
let name = "Ada"
print("hello {name}")
```

Run it with the interpreter:

```bash
misa run hello.misa
```

```
hello Ada
```

## The three commands you'll use daily

**`misa check`** — type-checks without running. Fast feedback, catches type
errors, unknown names, and bad SQL (see `docs/stdlib.md`):

```bash
misa check hello.misa   # silent = clean
```

**`misa run`** — runs the program with the interpreter. Best for
development: quick startup, full error messages.

**`misa build`** — compiles to a native binary ahead of time (no Python
needed to run the result):

```bash
misa build hello.misa -o hello
./hello
```

```
hello Ada
```

`misa build` runs the same type checker first, then compiles through LLVM.
The interpreter and compiled binaries behave identically — that's a
guarantee the test suite enforces (see `docs/targets.md`).

## Five-minute tour

```misa
// functions with typed parameters and returns
fn double(n: Int) -> Int {
    return n * 2
}

// sum types + pattern matching
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

// generics
fn first<T>(xs: [T]) -> T {
    return xs[0]
}

// errors are values you can catch
fn read_config(path: Text) -> Text {
    var out = "default"
    try {
        out = fs.read(path)
    } catch e {
        print("warn: {e.message}")
    }
    return out
}

print(double(21))              // 42
print(area(Shape.Circle(1.0))) // 3.14
print(first(["a", "b"]))       // a
print(read_config("app.conf")) // warns, prints "default"
```

Save as `tour.misa`, then:

```bash
misa check tour.misa && misa run tour.misa
```

Continue with `docs/tour.md` for the full language walkthrough, or
`docs/stdlib.md` for the library reference.

## Editor support

Misa ships a Language Server (`misa lsp`) with a VS Code extension in
`editors/` — diagnostics, hover types, and completion. Install the
extension from that directory; point it at your `misa` binary.

## Where to go next

| Doc | What it covers |
|---|---|
| `docs/tour.md` | The language, feature by feature, with runnable examples |
| `docs/stdlib.md` | Every builtin and namespace: `fs`, `http`, `str`, `json`, `time`, `math`, `db`, `process`, model calls |
| `docs/ffi.md` | Calling C libraries with `extern` blocks |
| `docs/errors.md` | The errors you'll actually hit, and how to fix them |
| `docs/targets.md` | Building for host, Android, iOS, and web — what works where |
