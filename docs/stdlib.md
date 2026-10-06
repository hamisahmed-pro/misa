# Misa standard library reference

Everything here is available with no import. Each entry shows the
signature, a runnable example (all verified against the interpreter),
and platform notes.

**Platform key:** ✅ works · ⚠️ clean `E600` rejection (unsupported on
that target) · ❌ broken/known issue. Targets: interp (`misa run`),
host AOT (Linux/macOS binary), Android (APK), iOS, web (Emscripten).

## Builtins

| Function | Signature | Notes |
|---|---|---|
| `print` | `print(x: any)` | Writes a line to stdout. |
| `len` | `len(x: [T] \| Text \| Map) -> Int` | Element/char/entry count. |
| `str` | `str(x: any) -> Text` | Canonical text rendering. |
| `int` | `int(x: Text) -> Int` | Parses; `E300` on failure. Wraps on overflow. |
| `float` | `float(x: Text) -> Float` | Parses; `E300` on failure. |
| `assert` | `assert(cond: Bool, msg: Text)` | Aborts with `E301` + msg if false. |
| `assert_eq` | `assert_eq(a: any, b: any)` | Aborts with `E301` (`assert_eq failed: <a> != <b>`) if `a == b` is false. |
| `assert_ne` | `assert_ne(a: any, b: any)` | Aborts with `E301` (`assert_ne failed: both are <a>`) if `a == b` is true. |
| `argv` | `argv() -> [Text]` | CLI args minus program name (like `sys.argv[1:]`). Pass via `misa run prog.misa -- a b`; host AOT gets real OS argv; `[]` on Android/iOS/web. |
| `env` | `env(name: Text) -> Text` | Env var; `E302` if unset. |
| `builder` | `builder() -> Builder` | Create a `Text.Builder` for amortized O(1) string appends. |
| `state` | `state(initial: T) -> T` | **Screens only.** Declares ephemeral per-screen-instance state; must be bound with `let`/`var` (`let c = state(0)`). Assigning re-renders the screen. E211 outside a screen body, unbound, in a handler/`for` body, or on type mismatch. |

**`Text.Builder`:** repeated `s = s + x` in a loop is O(n²). Build with a `Builder` instead:

```misa
let b = builder()
b.append("hello")
b.append(42)          // any value; rendered canonically
let s = b.build()     // "hello42"
```

```misa
print(str(42))        // 42
print(len("hello"))   // 5
print(int("42"))      // 42
assert(1 + 1 == 2, "math broke")
assert_eq([1, 2], [1, 2])
print(env("HOME"))    // e.g. /home/you
print(str(argv()))    // e.g. [hello, world] after `misa run prog.misa -- hello world`
```

All platforms ✅ (`argv()` is `[]` on Android/iOS/web).

## Concurrency

Structured, typed, cooperative tasks. `Task<T>` and `Chan<T>` are
builtin generic types.

| Function | Signature | Notes |
|---|---|---|
| `spawn` | `spawn { stmts } -> Task<T>` | Starts a concurrent task; `T` from the block's return type (`Task<Void>` if no `return`). Keyword, not a call. |
| `await` | `await t -> T` / `await t timeout ms -> T` | Waits for a `Task<T>`; rethrows task errors. `timeout` (Int ms) raises catchable **E306** on expiry. E205 on anything else. Keyword, not a call. |
| `sleep` | `sleep(ms: Int)` | Yields the current task for at least `ms` ms. E306 on negative. |
| `chan` | `chan<T>() -> Chan<T>` | Unbounded FIFO channel. |
| `send` | `ch.send(v: T)` | Method on a `Chan<T>`; never blocks. E306 on a closed channel. |
| `recv` | `ch.recv() -> T` | Method on a `Chan<T>`; yields until a value arrives. After `close`, drains then raises catchable **E306**. |
| `close` | `close(ch)` | Closes a channel (idempotent). |
| `fetch_all` | `fetch_all(urls: [Text]) -> [Text]` | Parallel HTTP GET, order-preserving; first error raised (catchable). |

```misa
let t = spawn { return http.get("https://a.example.com") }
let ch = chan<Text>()
spawn { ch.send(await t) }
print(ch.recv())
```

A task spawned in a function is cancelled when the function returns
(awaits inside it raise E306 `"task cancelled"`). Never-awaited task
errors print to stderr at program end. `Task`/`Chan` across FFI is E225.

All platforms ✅ except web: `spawn` raises E600 there.

## `fs` — files

| Function | Signature |
|---|---|
| `fs.read` | `fs.read(path: Text) -> Text` — `E303` on failure |
| `fs.write` | `fs.write(path: Text, text: Text)` — `E303` on failure |

```misa
fs.write("/tmp/note.txt", "hello file")
print(fs.read("/tmp/note.txt"))   // hello file
```

Paths resolve against the working directory. `misa run` has your
privileges — don't run untrusted programs. Platforms: interp ✅, host
✅, Android ✅ (app sandbox), iOS ✅ (app sandbox), web ✅.

## `http` — HTTP client

| Function | Signature |
|---|---|
| `http.get` | `http.get(url: Text) -> Text` — `E304` on failure/non-2xx |
| `http.post` | `http.post(url: Text, body: Text) -> Text` — posts as `text/plain` |
| `http.post_json` | `http.post_json(url: Text, v: value) -> Text` — posts as `application/json` |

```misa
let body = http.get("https://example.com")
print(len(body) > 100)   // true
```

30s timeout on all calls. Platforms: interp ✅, host ✅, Android ✅,
iOS ✅ (device run not yet verified), web ✅.

## `str` — strings (code-point semantics)

| Function | Signature |
|---|---|
| `str.slice` | `str.slice(s: Text, i: Int, j: Int) -> Text` — chars `[i, j)`; indices clamp; empty when `i >= j` |
| `str.starts_with` | `str.starts_with(s: Text, prefix: Text) -> Bool` |
| `str.split` | `str.split(s: Text, sep: Text) -> [Text]` — `E300` on empty `sep` |
| `str.trim` | `str.trim(s: Text) -> Text` — strips Unicode whitespace |
| `str.ord` | `str.ord(c: Text) -> Int` — code point of first char; `E300` on empty |
| `str.chr` | `str.chr(code: Int) -> Text` — `E300` on negatives, surrogates, `> 0x10FFFF` |
| `str.chars` | `str.chars(s: Text) -> [Text]` — code points as single-char texts (what `for x in s` iterates) |

```misa
print(str.slice("hello", 1, 4))      // ell
print(str.starts_with("hello", "he")) // true
print(str.split("a,b,c", ","))       // [a, b, c]
print(str.trim("  hi  "))             // hi
print(str.ord("A"))                  // 65
print(str.chr(128512))               // 😀
```

Byte-identical on interpreter and AOT. All platforms ✅.

## `json` — JSON

| Function | Signature |
|---|---|
| `json.parse` | `json.parse(s: Text) -> value` — `E300` on invalid JSON |
| `json.stringify` | `json.stringify(v) -> Text` — `E300` on non-JSON values |

```misa
// note: "{{" escapes a literal { inside Misa strings
let js = "{{" + "\"a\": 1, \"b\": [true, null]}"
let v = json.parse(js)
print(v.a)        // 1
print(v.b[0])     // true
print(v.b[1])     // nil  (JSON null -> Misa nil)
print(json.stringify({x: 1, y: "two"}))  // {"x":1,"y":"two"}
```

Numbers become `Int` when they have no fraction/exponent and fit in
int64, otherwise `Float` (so `99999999999999999999999` parses as
`1e+23`). `json.stringify` rejects functions, `Ptr`, `Db` handles, and
`Error` values with `E300`. All platforms ✅.

## `time` — clock

| Function | Signature |
|---|---|
| `time.now` | `time.now() -> Int` — Unix epoch seconds |
| `time.now_ms` | `time.now_ms() -> Int` — Unix epoch milliseconds |
| `time.format` | `time.format(epoch: Int, fmt: Text) -> Text` — `strftime` directives, UTC |
| `time.parse` | `time.parse(s: Text, fmt: Text) -> Int` — `E300` when `s` doesn't match `fmt` |

```misa
print(time.format(1705276800, "%Y-%m-%d"))  // 2024-01-15
print(time.parse("2024-01-15", "%Y-%m-%d")) // 1705276800
```

All platforms ✅.

## `math`

| Function | Signature |
|---|---|
| `math.sqrt` | `math.sqrt(x: Float) -> Float` — `E300` on negative |
| `math.pow` | `math.pow(x: Float, y: Float) -> Float` |
| `math.abs` | `math.abs(x: Int\|Float) -> Int\|Float` |
| `math.floor` | `math.floor(x: Float) -> Int` — `E300` if out of int64 range |
| `math.ceil` | `math.ceil(x: Float) -> Int` |
| `math.round` | `math.round(x: Float) -> Int` — half away from zero |

```misa
print(math.sqrt(16.0))  // 4.0
print(math.pow(2.0, 10.0)) // 1024.0
print(math.round(2.5))   // 3
```

All platforms ✅.

## `db` — SQLite

Zero-dependency embedded database. `Db` is an opaque handle.

| Function | Signature |
|---|---|
| `db.open` | `db.open(path: Text) -> Db` — `":memory:"` for transient DBs |
| `db.exec` | `db.exec(db: Db, sql: Text, args: [any]?) -> Int` — rows changed; `?` placeholders; statements cached |
| `db.query` | `db.query(db: Db, sql: Text, args: [any]?) -> [record]` — list of column-name → value records |
| `db.close` | `db.close(db: Db) -> Void` |
| `db.exec_many` | `db.exec_many(db: Db, sql: Text, rows: [[any]]) -> Int` — bulk insert: prepare once, total rows changed |
| `db.begin` | `db.begin(db: Db) -> Void` — begin explicit transaction |
| `db.commit` | `db.commit(db: Db) -> Void` — commit |
| `db.rollback` | `db.rollback(db: Db) -> Void` — roll back |

```misa
let db = db.open(":memory:")
db.exec(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
db.exec(db, "INSERT INTO users (name) VALUES (?)", ["Hamis"])
// Bulk inserts: 10× faster than looped db.exec
db.begin(db)
db.exec_many(db, "INSERT INTO users (name) VALUES (?)",
             [["A"], ["B"], ["C"]])
db.commit(db)
let rows = db.query(db, "SELECT id, name FROM users")
for r in rows {
    print("{r.id}: {r.name}")   // 1: Hamis
}
db.close(db)
```

Bind args accept `Int`, `Float`, `Bool`, `Text`, `nil`. All failures
throw catchable `E303`.

**Typed queries.** Declare the schema and the checker validates your SQL
at compile time (E224) and infers the result type:

```misa
table users {
    id: Int,
    name: Text,
}
let db = db.open(":memory:")
db.exec(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
let rows = db.query(db, "SELECT id, name FROM users")
// rows: [{id: Int, name: Text}] — r.name type-checks
```

Queries over undeclared tables return dynamic records (no static
checking, not an error). v1 checks names in SELECT/FROM/WHERE only.

Platforms: interp ✅, host ✅, Android ✅ (SQLite bundled into the APK),
iOS ⚠️ E600, web ⚠️ E600 (host and Android only, for now).

## `process` — subprocesses

| Function | Signature |
|---|---|
| `process.run` | `process.run(cmd: [Text]) -> {code: Int, stdout: Text, stderr: Text}` — `E303` if the command can't start or times out (5 min) |

```misa
let r = process.run(["echo", "hi"])
print(r.code)              // 0
print(str.trim(r.stdout))  // hi
```

**Security:** a program with `process.run` executes arbitrary commands —
same trust level as `fs.write`. Platforms: interp ✅; **all AOT targets
⚠️ E600** (subprocesses in compiled binaries are out of scope).

## Model calls — typed AI

Models are declared with typed parameters, a return type, a provider,
budgets, and a versioned prompt:

```misa
provider mock {
    key: env("UNUSED"),
}

models {
    greeter: mock("greeter"),
}

type Greeting {
    text: Text,
    lang: Text,
}

model greet(name: Text) -> Greeting
    with mock.greeter
    budget { tokens: 500, latency: 10s, cost: $0.01 }
    prompt v1 """
    Greet {name} warmly. Reply with a greeting and its language.
    """

let g = greet("Ada")
print(g.text)   // Hello, Ada!  (from the mock below)
```

```bash
# mocks.json: {"greeter": {"text": "Hello, Ada!", "lang": "English"}}
misa run model.misa --mock mocks.json
```

- **Providers:** `openai` (OpenAI-compatible API), `gemini`, `mock`
  (deterministic test double). Keys come from env vars only — never
  from source.
- **Validation:** the provider must return JSON; it's validated against
  the return type, with up to 2 automatic repair retries. Then `E400`.
- **Budgets:** tokens / latency / cost are enforced — violation is
  catchable `E500`. No silent overruns.
- **Streaming:** `stream s = greet("Ada")`, `on s.token { |t| ... }`,
  `let g: Greeting = await s`. (AOT: `E402` — streaming is
  interpreter-only for now.)
- **On-device:** a provider block can name a local model file instead
  of a key — `provider edge { model: file("./tiny.onnx") }` — and the
  *same* `model` syntax runs offline (`.onnx` → ONNX Runtime,
  `.mlpackage` → CoreML, `.tflite` → TFLite). See `docs/targets.md`.

## `std/` packages

The toolchain bundles pure-Misa packages, importable as
`import "std/..."` (lookup is flat — `sum(...)`, not `lists.sum(...)`):

| Package | Provides (v1) |
|---|---|
| `std/strings` | `trim`, `split`, `join`, `starts_with`, `ends_with`, `contains`, `repeat`, `pad_left`, `pad_right` |
| `std/lists` | `range`, `any`, `all`, `sum` |
| `std/http` | `get_text` |
| `std/json`, `std/result` | reserved for future helpers |

```misa
import "std/lists"
import "std/strings"
print(sum(range(5)))              // 10
print(join(["a", "b", "c"], "-")) // a-b-c
```

Own packages: `misa pkg init` scaffolds one; `misa pkg add <git-url|path>`
installs into `misa_pkgs/`; `misa pkg --help` lists the rest. No central
registry yet (planned for the public phase).
