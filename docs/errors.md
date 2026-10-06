# Misa error catalog (the friendly version)

Every Misa diagnostic has a code and a source position. The full
normative list is in the spec; this is the subset you'll actually meet
in your first week, with fixes. All messages below are verbatim from
the toolchain.

## Reading an error

```
2:7: E102: function 'f' takes 1 args, got 2
```

`line:col: CODE: message`. Some errors add a `hint:` line. Runtime
errors print the same shape to stderr and exit 1; uncaught, they abort.

## Syntax (E0xx)

**E001 — syntax error.** The message names the fault:

```
4:7: E001: unbalanced '{' in string: '{' opens interpolation and needs
a matching '}' (a '"' inside '{...}' ends the string — interpolation
cannot nest double-quoted strings)
```

Common causes: unbalanced braces/parens, a `"` inside `{...}`
interpolation (compute the value first, then interpolate it), using
`{` in a JSON literal without `{{` escaping.

## Names (E1xx)

**E101 — undefined name.** You used something that doesn't exist:

```
1:7: E101: undefined name 'nope'
```

Fix: check the spelling, or add the `fn` / `let` / `import`. (Using a
builtin like `print` as a value is E214, not E101.)

**E102 — wrong number of arguments:**

```
2:7: E102: function 'f' takes 1 args, got 2
```

## Types (E2xx) — all caught by `misa check`

**E200 — annotation mismatch:**

```
1:1: E200: cannot assign Text to 'x: Int'
```

The value doesn't fit the declared type. Fix the value or the
annotation.

**E201 — assigning to immutable:**

```
2:1: E201: cannot assign to immutable 'x'
```

`let` bindings can't be reassigned — use `var`.

**E202 — record shape mismatch:**

```
5:15: E202: missing field 'age' for type 'User'
```

Record literals must have exactly the declared fields, with matching
types.

**E211 — argument/return type mismatch:**

```
2:9: E211: argument 'a' wants Int, got Text
```

```
1:1: E211: function 'f' may exit without returning Int
  hint: every code path must end in 'return'
```

The second form is the one newcomers hit most: a non-`Void` function
must `return` on *every* path, including after `try`/`catch`. When in
doubt, use a `var` accumulator and a single trailing `return`.

**E213 — can't infer a lambda parameter:**

```
2:28: E213: cannot infer type of lambda parameter 'x': annotate it
('|x: Int|') or use the lambda where a function type is expected
```

Annotate the parameter (`|x: Int|`) or pass the lambda where a function
type is already known.

**E216 — non-exhaustive `match`:**

```
6:12: E216: non-exhaustive match on 'Color'; missing: Green
```

Add the missing arm or a `_` wildcard.

**E219 — can't infer a type argument:**

```
E219: cannot infer type argument 'T' for 'Result.Err'; give explicit
type arguments ('Result<T>.Err(...)') or annotate the binding
```

Annotate the binding (`let bad: Result<Int> = Result.Err("boom")`) or
pin it (`Result<Int>.Err("boom")`).

**E224 — unknown table/column in SQL:**

```
6:25: E224: unknown column 'nam' (table 'users' has no such column)
```

Your `table` declaration disagrees with your SQL string. Fix the
spelling (or declare the table).

## Runtime (E3xx) — all catchable with `try`/`catch`

**E300 — generic runtime error** (division by zero, bad `int()` parse,
invalid JSON, …):

```
<unknown>: E300: division by zero
```

Catch it:

```misa
try {
    print(1 / 0)
} catch e {
    print("caught {e.code}: {e.message}")
}
// caught E300: division by zero
```

**E301 — `assert` failure.** Your `assert(cond, msg)` found `cond`
false; `msg` is in the message. Fix the code or the assumption.

**E302 — unset environment variable:**

```misa
try {
    env("MISA_DEFINITELY_UNSET_VAR_XYZ")
} catch e {
    print("caught {e.code}")  // caught E302
}
```

**E303 — I/O failure** (`fs.read`/`fs.write`/`process.run`/`db.*`):

```
caught E303   // e.g. fs.read of a missing file
```

Check the path, permissions, or SQL.

**E304 — HTTP failure** (`http.get`/`http.post`): network error or
non-2xx status.

**E305 — missing map key:**

```
2:8: E305: missing map key 'zzz'
```

Indexing a map with an absent key throws. Check membership first or
catch it.

## Models and budgets (E4xx/E5xx) — catchable

- **E400** — the model call failed (transport, bad JSON, schema
  validation after 2 repair retries).
- **E401** — provider misconfigured (bad key, unknown provider, or a
  missing on-device model file under `misa run`).
- **E500** — a declared `budget { ... }` was exceeded. Loosen the
  budget or shrink the call.
- **E501/E502** — on-device tensor shape / signature mismatch.
- **E503** — the needed runtime isn't available (e.g. on-device
  inference in the self-hosted interpreter).
- **E504** — `misa build` target can't run this model file
  (e.g. `.tflite` for iOS).

## Toolchain (E6xx) — not catchable

**E600** — the build itself failed: link errors, a construct the target
doesn't support (`process.run` and `db.*` on iOS/web), a missing model
asset. Read the message — it names the cause.

## The one rule

If `misa check` passes, E0xx–E2xx are behind you. Everything else is a
runtime `Error` value: catch it, match on `e.code`, and handle it like
any other value.
