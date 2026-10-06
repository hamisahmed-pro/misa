# Misa Semantics — v0.2

Normative companion to `SYNTAX.md`. v0.2 (2026-10-05); supersedes v0.1.

## 1. Execution model

A program has three phases:

1. **Import resolution.** `import` declarations are resolved
   depth-first, relative to the importing file. Cycles are error `E111`.
2. **Registration.** All declarations (`app`, `provider`, `models`,
   `type`, `model`, `screen`, `eval`, `fn`) from every file are
   registered. Order is irrelevant; forward references and recursion
   are legal.
3. **Execution.** Imported files' top-level statements run first, in
   import order (depth-first), then the main file's statements top to
   bottom, in a single global scope.

Values are garbage-collected. `let` bindings are immutable; `var`
bindings are mutable. Assigning to a `let` binding is error `E201`.

## 2. Functions

```
fn fib(n: Int) -> Int {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}

fn greet(name: Text) {
    print("hi {name}")
}
```

- A `fn` with no `-> typeExpr` returns `Void`; `return` with no value,
  or falling off the end, is legal only for `Void` functions.
- Every code path of a non-`Void` function must end in `return`
  (checked: `E211`). A call to a `Void` function used as a value is
  `E211`.
- Calls are expressions: `fib(10)`. Arity mismatch is `E102`;
  argument type mismatch is `E211`.
- Functions are first-class values (Phase 5a): function types
  `(Int, Text) -> Bool`, lambdas with typed or inferred params,
  `fn` declarations usable as values, and closures capturing their
  defining scope. Builtins (`print`, `str`, `fs.read`, …) are not
  values (`E214`).
- Recursion is legal; each call gets a fresh local scope. There is no
  tail-call optimization in the v0.2 interpreter.

## 3. Modules

`import "utils.misa"` makes the imported file's top-level `fn`,
`type`, `model`, `screen`, `eval`, `provider`, and `models`
declarations visible in the importing file. Lookup is flat: `fib(5)`,
not `utils.fib(5)` — one spelling, no namespaces. A top-level name
defined in more than one reachable file is error `E110`.

## 4. Errors

The builtin type `Error` is `type Error { code: Text, message: Text }`.

```
fn load(path: Text) -> Text {
    try {
        return fs.read(path)
    } catch e {
        print("warn: {e.message}")
        throw { code: "ELOAD", message: "cannot load {path}" }
    }
}
```

- `throw expr`: `expr` must be an `Error` (`E212` otherwise).
- `try { … } catch e { … }`: if the `try` block raises, `e` (an
  `Error`) is bound in the `catch` block and execution continues
  after the statement. If no error is raised, the `catch` block is
  skipped.
- **Every runtime/model/budget error is catchable.** `E3xx`, `E4xx`,
  `E5xx` raised by the interpreter surface as `Error{code, message}`
  values. Uncaught, they abort the program with the v0.1-style
  structured report.
- `catch` does not catch static check errors (`E1xx`/`E2xx`); those
  are compile-time.

## 5. Types

| Misa type | Runtime representation |
|---|---|
| `Text` | Unicode string |
| `Int` | Arbitrary-precision integer |
| `Float` | Double |
| `Bool` | Boolean |
| `Void` | No value; only as a `fn` return type |
| `Error` | Builtin record `{code: Text, message: Text}` |
| `Photo` `Audio` `Video` `Document` | Media handle `{kind, path}` |
| `TokenStream` | Streaming handle (see §9) |
| `[T]` | List |
| `Map<K, V>` | Keyed map; keys `Text` or `Int` |
| `T?` | `T` or `nil` |
| Declared `type` | Record `{field: value}` |

**Type checking (static pass in `misa check`):**

- `E101` undefined name; `E102` arity mismatch on calls; `E110`
  duplicate top-level name across imports; `E111` circular import;
  `E112` missing/unreadable import.
- `E200` annotation mismatch: `let x: Text = 1` is an error.
- `E202` record literal field missing/extra/ mistyped vs declared type.
- `E203` `eval given` keys must exactly match the model's parameter names.
- `E204` `eval expect` must be a `Bool` expression.
- `E205` `await` applied to a non-`TokenStream`.
- `E206` unknown component name in a screen.
- `E207` duplicate `id:` binding within one screen.
- `E208` model return type must be a declared type or a primitive.
- `E211` function argument/return type mismatch; use of a `Void`
  call as a value; missing `return` on some code path.
- `E212` `throw` of a non-`Error` expression.

Inference beyond this is future work. When the checker cannot prove a
program wrong, it stays silent; the interpreter raises typed runtime
errors instead.

## 6. Model calls

```
model diagnose(plant: Photo, notes: Text) -> Diagnosis
    with openai.gpt_mini
    budget { tokens: 2000, latency: 5s, cost: $0.01 }
    prompt v3 """
    You are a botanist. Photo: {plant}. Notes: {notes}.
    Respond with structured data only.
    """
```

Calling `diagnose(p, n)`:

1. **Render.** The prompt template interpolates `{plant}`/`{notes}`.
   Non-text values render as typed placeholders (`<Photo: img.jpg>`);
   providers may upgrade them (Gemini sends real image bytes, §4).
2. **Request.** The provider is called with `responseMimeType: json`
   (or equivalent) so the model returns a JSON object.
3. **Validate.** The JSON is validated against the declared return type:
   every field present, every field the right primitive/list/record
   shape. Extra fields are ignored.
4. **Repair.** On validation failure the model is re-prompted with the
   validation errors appended, up to **2 retries**. Then error `E400`.
5. **Budgets.** Token usage from the provider's usage report, wall-clock
   latency, and cost from the pricing table (`SEMANTICS.md` appendix)
   are checked against the declared budget. Violation → `E500`
   `BudgetExceeded`, aborting the call. No silent overruns, ever.

`prompt vN`: the version tag is recorded with every call (logs, traces).
Prompt history/rollback tooling is Phase 2.

## 7. Providers

```
provider openai { key: env("OPENAI_KEY") }
provider gemini { key: env("GEMINI_KEY") }
provider mock   { key: env("UNUSED") }      // deterministic test double

models {
    gpt_mini: openai("gpt-5-mini"),
    flash:    gemini("gemini-2.5-flash"),
    fake:     mock("scripted"),
}
```

- `openai`: OpenAI-compatible `POST {base}/chat/completions`
  (`base` defaults to `https://api.openai.com/v1`; override with
  `MISA_OPENAI_BASE_URL`). `response_format: {type: "json_object"}`.
- `gemini`: `POST https://generativelanguage.googleapis.com/v1beta/
  models/{id}:generateContent` with `responseMimeType: application/json`.
  `Photo` args with existing files are sent as `inlineData`.
- `mock`: returns scripted JSON from the file given to
  `misa run --mock mocks.json` / `misa test --mock mocks.json`,
  keyed by model alias. Missing entry → `E401`.
- Keys come from environment variables only. Never from source.
- (Phase 2: OAuth/device flows, command providers, keychain.)

## 8. Evals

```
eval diagnose_healthy_plant {
    given: { plant: Photo("healthy.jpg"), notes: "looks fine" },
    expect: result.confidence > 0.7 and not result.urgent,
    runs: 5,
}
```

`misa test` runs every eval: it calls the model `runs` times with the
`given` arguments, binds each typed result to `result`, and evaluates
`expect`. The eval **passes iff every run satisfies `expect`**.
Output is per-run detail plus a summary line. Non-zero exit on failure —
evals are CI citizens.

## 9. Streams

```
stream s = summarize(doc)
on s.token { |t| preview.append(t) }
let full: Summary = await s
```

- `stream s = f(args)` starts the model call in streaming mode and binds
  the `TokenStream` handle to `s`.
- `on s.token { |t| ... }` registers a handler invoked per text token.
- `await s` blocks until completion, then parses and validates the full
  text exactly like a non-streaming call (§3 steps 3–5), returning the
  typed value.
- v0.1 providers: streaming is implemented for `openai`-compatible
  (SSE). `gemini` streaming and `mock` streaming are Phase 2; using
  `stream` with them is error `E402`.

## 10. Screens

Screens are pure: no statements, only a component tree. Event props
(`onTap`, `onCapture`, …) take lambdas. `id: name` binds a component;
bound components expose typed props (`text` for `TextInput`,
`value` for others — see `STDLIB.md`).

`misa run` prints the component tree of each screen navigated to.
`navigate(Name(args))` switches the current screen. Native rendering
is Phase 4; the tree printed by v0.1 is exactly what Phase 4 will
render, so screen code written today keeps working.

## 11. Uncaught errors

Any error not caught by `try`/`catch` aborts the program with a
structured report: code, message, source span, and (for model errors)
the prompt version and provider usage. `misa run --json` emits the
report as JSON for agent consumers.

Error families: `E1xx` resolution, `E2xx` types, `E3xx` runtime,
`E4xx` model/provider, `E5xx` budget. See §4 for the catchable
`Error` type.

## Appendix A — cost pricing table (estimates, USD per 1K tokens)

Used only for `budget { cost: ... }` enforcement. Marked ESTIMATE;
providers' real prices change.

| Model id pattern | Input | Output |
|---|---|---|
| `gpt-5-mini*` | $0.00015 | $0.0006 |
| `gpt-5*` | $0.00125 | $0.01 |
| `gemini-2.5-flash*` | $0.000075 | $0.0003 |
| `claude-haiku*` | $0.00025 | $0.00125 |
| `deepseek*` | $0.00014 | $0.00028 |
| default | $0.001 | $0.003 |
