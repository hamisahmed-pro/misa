# Phase 8 (overtake): Checked `throws` signatures

Status: IMPLEMENTED (2026-10-06). Highest-leverage proposal from
`bench/GAPS-language.md` §A6/§H.

## The idea

Error *codes* become part of the function signature, enforced by
`misa check`. Nobody has this:

- Rust's `Result<T, E>` forces you to define error enums and still
  doesn't track *which* errors a function can raise.
- Go and Python are unchecked (`(T, error)` is routinely ignored).
- Java tried checked exceptions and died on verbosity (open exception
  hierarchies, no inference).

Misa avoids Java's failure mode three ways:

1. **E-codes are a small closed set per namespace** (`E303` = I/O,
   `E304` = HTTP, …), not an open hierarchy.
2. **The throws set is inferred** — the checker computes it; you write
   `throws` to declare the contract (an upper bound).
3. **`misa check --json` emits the throws graph** (declared + inferred
   per function) — machine-readable contracts, which is agent gold.

## Syntax

```misa
fn fetch(u: Text) -> Text throws E304 {
  return http.get(u)   // E304 declared; body verified against it
}

fn load(u: Text) -> Text {
  return fetch(u)      // E227: unhandled E304 — declare or catch
}

fn safe_load(u: Text) -> Text {
  try { return fetch(u) } catch e { return "" }  // caught: fine
}
```

- `throws` clause: `throws E304` or `throws E304, E303`, after the return
  type and before the body. `throws` is contextual (an `IDENT`, not a
  keyword), so existing code using `throws` as an identifier is unaffected.
- Codes are `E` followed by uppercase letters/digits: builtin `E303`,
  `E304`, … and custom codes like `EBAD`, `ELOAD` (see SEMANTICS.md §4).
  A malformed code (`throws OOPS`) is E227.

## Semantics

For each function, the checker computes two sets:

- **declared**: the `throws` clause as written (empty if absent).
- **inferred**: the E-codes the body may raise, via fixed-point over the
  call graph (callee's declared set wins; else its inferred set).

**Enforcement (E227):** at every call/`throw`/`await` site, the raised
codes must be *declared* by the enclosing function or *caught* by an
enclosing `try` (Misa `catch` is total — it discharges everything).
Otherwise: `unhandled E304 from call to 'fetch' — 'load' must declare
'throws E304' or catch it`.

**Declared is an upper bound.** Over-broad declarations (`throws E304`
on a function that never raises it) are *allowed* — the clause is a
contract, and exact-set enforcement would false-positive on
higher-order functions. Under-declaration always errors.

**Top level is exempt.** Top-level statements are the program boundary
(uncaught errors abort with the runtime report, as before); only
function bodies, lambdas, and screen handlers are verified.

**Lambdas infer.** A let-bound lambda's throws set is inferred from its
body; the call site (`f()`) is checked against it. Inline
(non-let-bound) lambdas in function bodies are unchecked (v1
limitation), as are screen handlers (which are inline lambdas —
requiring try/catch in every UI handler would be the verbosity trap
in the most handler-dense layer).

**Calls are checked; operators are unchecked.** `http.get(u)` forces a
`throws E304` (or a catch); `xs[0]` and `a / b` never do — operator
failures (E300 from division/indexing) are panic-like, à la Rust's
`Result`-vs-`panic` split. This is what keeps the feature from
becoming Java: the common expression-level operations stay quiet.

**`throw` is tracked.** `throw { code: "E303", … }` contributes `E303`
(the literal is read when it's a plain string); `throw e` where `e`
is a caught variable contributes the try body's set; anything else
contributes `E300`.

**`try`/`catch` discharges.** A `try` body never produces E227, no
matter what it calls. The handler's own codes still propagate.

**Concurrency.** `let t = spawn { … }` infers the block's set; `await t`
raises that set plus `E306` (timeout/cancel). `await` on a stream
raises `E400`. `await` on a `Task`-typed parameter raises `E306`.

## Builtin throws table

`http.*` → E304, `fetch_all` → E304 · `fs.*`, `process.run`,
`db.*` → E303 · `json.*`, `int`, `float`, `str.*`, `time.parse`,
`time.format`, `math.*` → E300 · `env` → E302 · `sleep`,
`chan.recv` → E306 · extern fns → E226 · model calls → E400.
Everything else (incl. `print`, `len`, `str`, `assert`) → ∅.

## v1 limitations (documented)

- Calling a function *value* (`let g = fetch; g(u)`) is unchecked.
- Inline (non-let-bound) lambdas in function bodies are unchecked.
- `await` on a task whose set is unknowable (e.g. from a collection)
  contributes only `E306`.
- `selfhost/` does not implement throws (frozen).

## CLI

`misa check --json` includes `throws_graph`: per function,
`{"declared": [...], "inferred": [...]}`.
