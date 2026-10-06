# Misa Error Codes

Every Misa diagnostic carries a code. Ranges:

| Range | Meaning |
|---|---|
| E0xx | Syntax / lexing |
| E1xx | Name resolution |
| E2xx | Static type checking |
| E3xx | Runtime errors |
| E4xx | Model / provider errors |
| E5xx | Budgets, on-device |
| E6xx | Toolchain (AOT/build) |

## E0xx — syntax

| Code | Meaning |
|---|---|
| E001 | Syntax error (lexer/parser). Message describes the fault. |

## E1xx — names

| Code | Meaning |
|---|---|
| E101 | Undefined name or function (`undefined name 'x'`, `undefined function 'f'`). Also: type used as a call, component id misused as a value. |
| E102 | Arity mismatch (`'f' takes 2 args, got 3`). |
| E110 | Duplicate definition across imported files (same name, different files). |
| E111 | Circular import. |
| E112 | Import cannot be resolved / not a `.misa` file. |
| E113 | Bad `misa.pkg` manifest (`invalid TOML in misa.pkg: …`). Raised by the package resolver (L-3: was documented "(reserved)" but actively used). |

## E2xx — static types

| Code | Meaning |
|---|---|
| E200 | Type mismatch on assignment (`cannot assign Text to 'x: Int'`). |
| E201 | Assignment to an immutable binding, invalid target, or component id used as a value. |
| E202 | Structural mismatch (record field type, map key type, list element). |
| E203 | `eval given` keys do not match the model's parameters. |
| E204 | `eval expect` is not a Bool expression. |
| E205 | `await`/`on`/`stream` misused (needs a TokenStream / model call). |
| E206 | Screen DSL misuse (unknown component, children on a leaf component, handler arity). (L-2: the documented `'id:' needs a plain name` branch was dead code — the parser rejects a non-identifier `id:` with E001 first — and has been removed.) |
| E207 | Duplicate component id. |
| E208 | Unknown type name in an annotation. |
| E209 | Unknown provider / model alias / on-device model problem at check time. |
| E210 | Duplicate definition in one file (`duplicate function 'f'`, `redefinition of 'x'`). |
| E211 | Argument/return type mismatch (`argument 'x' wants Int, got Text`). |
| E212 | `throw` of a non-`Error` value. Accepts a `{ code: Text, message: Text }` record *literal* or a caught error — a plain record cannot be assigned to the nominal `Error` type (E200). |
| E213 | **(Phase 5a)** Cannot infer a lambda parameter's type — annotate it (`\|x: Int\|`) or use the lambda where a function type is expected. |
| E214 | **(Phase 5a)** A builtin (`print`, `str`, `fs.read`, …) used as a value. Builtins are not first-class. **(M-7)** Also fires for an `extern` (FFI) function used as a value — extern functions are not first-class either. |
| E215 | **(Phase 5a)** Calling a value that is not a function (`'x' has type 'Int' and is not callable`). |
| E216 | **(Phase 5b)** Non-exhaustive `match` — lists the missing variants (`non-exhaustive match on 'Shape'; missing: Rect`). Add the missing arms or a `_` wildcard. |
| E217 | **(Phase 5b)** `match` arms yield different types (`match arms yield different types ('Int' vs 'Text')`). |
| E218 | **(Phase 5b)** Sum-type misuse: unknown variant, payload arity mismatch on a pattern, bare use of a payload-carrying variant, `match` on a non-sum scrutinee, or an arm targeting a different sum type. |
| E219 | **(Phase 5c)** Cannot infer a type argument (`cannot infer type argument 'T' for 'first'`). Pin it explicitly (`first<Int>(...)`) or annotate lambda parameters. |
| E220 | **(Phase 5c)** Conflicting types for a type variable (`conflicting types for 'T': 'Int' vs 'Text'`). |
| E221 | **(Phase 5c)** Type-argument arity mismatch (`'Box' takes 1 type argument(s), got 2`). |
| E222 | **(Phase 5c)** Unbound type variable — a name used as a type inside a generic declaration that is not one of its type parameters (`unbound type variable 'K'`). |
| E223 | **(Phase 5c)** Monomorphization depth exceeded — recursive generic instantiation in AOT (`f<T>` calling `f<[T]>`); restructure to avoid infinite specialization. |
| E224 | **(Phase 5 DB)** Unknown table or column in a `db.query` SQL string validated against `table` declarations (`unknown column 'nam' (table 'users' has no such column)`). Queries over undeclared tables are dynamic, not errors. |
| E225 | **(Phase 7)** Invalid `extern` signature — empty library name, type parameters on an extern function, or a parameter/return type that cannot cross the FFI boundary (only `Int`, `Float`, `Text`, `Bool`, `Ptr`, `Void` in v1). |
| E226 | **(Phase 7)** FFI failure at load or call time — library cannot be loaded (`cannot load library 'foo': ...`), symbol not found (`library 'm' has no symbol 'bar'`), or the foreign call itself failed. Foreign crashes are NOT caught (same as C). |

## E3xx — runtime

| Code | Meaning |
|---|---|
| E300 | Generic runtime error (div by zero, bad operation, out of memory). |
| E301 | `assert` failure. |
| E302 | **(M-8)** Environment variable not set (`env(name)` when `name` is unset). |
| E303 | I/O error (`fs.read`/`fs.write`/`process.run` failure). Also the code carried by a caught `throw` value's `code` field when user-defined. |
| E304 | HTTP error (`http.get` failure / non-2xx). |
| E305 | Missing map key. |

## E4xx — models

| Code | Meaning |
|---|---|
| E400 | Model call failed (transport, malformed response, schema validation). |
| E401 | Provider configuration error (bad key, unknown provider). A missing on-device model file is E401 under `misa run` (the provider block is misconfigured), but E600 under `misa build` (the file is a build asset that cannot be staged — a packaging failure, not a config check). The split is intentional. |
| E402 | Streaming unsupported in this context (AOT). |

## E5xx — budgets / on-device

| Code | Meaning |
|---|---|
| E500 | Budget exceeded (tokens, cost, latency). |
| E501 | On-device shape mismatch. |
| E502 | On-device signature mismatch. |
| E503 | On-device runtime unavailable / model calls unsupported (e.g. self-hosted interpreter). |
| E504 | On-device target incompatible (e.g. `.tflite` on non-Android). |

## E6xx — toolchain

| Code | Meaning |
|---|---|
| E600 | AOT/build failure (link errors, unsupported construct for the target, missing on-device model asset — see E401 for the intentional run/build split). |

## Catching

`try/catch` catches any `E3xx`–`E5xx` (and user `throw` values) as
`{code, message}` records. Static errors (`E0xx`–`E2xx`) and `E6xx` are
compile-time and cannot be caught — with one exception: **E226** (FFI
failure) is numbered in the E2xx range for historical reasons but fires
at *runtime* (library/symbol/call failure) and **is** catchable, like an
E3xx. In AOT-compiled binaries the same fault surfaces as E600 at link
time instead (E226 never fires from a binary).
