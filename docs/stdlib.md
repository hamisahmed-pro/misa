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
| `fs.temp_file` | `fs.temp_file() -> Text` — new empty temp file; returns its path (`E303` on failure) |
| `fs.temp_dir` | `fs.temp_dir() -> Text` — new empty temp dir; returns its path (`E303` on failure) |

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

### `http.serve` — HTTP server

```misa
http.serve(8080, |req: Request| {
  if req.path == "/hello" {
    return {status: 200, body: "hi " + req.query["name"]}
  }
  return {status: 404, body: "not found"}
})
```

`Request`: `{method, path, query, headers, body}`. `Response`: `{status,
headers, body}` (defaults: `200`, `{}`, `text/plain`). Blocking serve loop;
connections are handled sequentially in v1 — `spawn` the server or per-connection
work for concurrency. Handler errors become `500` without killing the server.
Parser limits: 64KB headers → `431`, 10MB body → `413`, 100 headers max → `431`,
chunked bodies → `501`, malformed → `400`. Platforms: interp ✅, host ✅,
Android/iOS (compiles, device run not yet verified), web ❌ → `E600`.

## `net` — TCP sockets

| Function | Signature |
|---|---|
| `net.connect` | `net.connect(host: Text, port: Int) -> Socket` — `E304` on failure |
| `net.listen` | `net.listen(port: Int) -> Listener` — `port: 0` = ephemeral; `E304` on failure |
| `net.accept` | `lis.accept() -> Socket` — blocks for the next client |
| `net.port` | `lis.port() -> Int` — the bound port |
| `listener.close` | `lis.close()` |
| `socket.send` | `sock.send(data: Text) -> Int` — bytes sent |
| `socket.recv` | `sock.recv(max: Int) -> Text` — `""` on clean EOF (not an error) |
| `socket.close` | `sock.close()` |
| `socket.set_timeout` | `sock.set_timeout(ms: Int)` — recv/send timeout; expiry raises `E304` |

Method style (`sock.send(...)`, `lis.accept()`) or function style
(`socket.send(sock, ...)`, `net.accept(lis)`) — both work. Sockets compose with
`spawn`/`sleep`/`await`: spawn an echo server, `sleep` briefly, then connect.

```misa
let lis = net.listen(0)
let port = lis.port()
let srv = spawn {
  let conn = lis.accept()
  conn.send("echo:" + conn.recv(1024))
  conn.close()
}
sleep(200)
let c = net.connect("127.0.0.1", port)
c.send("hello")
print(c.recv(1024))   // echo:hello
c.close()
await srv
```

Platforms: interp ✅, host ✅, Android/iOS (compiles, device run not yet
verified), web ❌ → `E600`.

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
| `str.join` | `str.join(xs: [Text], sep: Text) -> Text` — inverse of `split` |
| `str.replace` | `str.replace(s: Text, old: Text, new: Text) -> Text` — `E300` on empty `old` |
| `str.ends_with` | `str.ends_with(s: Text, suffix: Text) -> Bool` |
| `str.to_upper` / `str.to_lower` | case mapping — v1 is ASCII-only (`a`–`z`); full Unicode casefold is future work |
| `str.pad_left` / `str.pad_right` | `str.pad_left(s: Text, width: Int, pad: Text) -> Text` — `E300` on empty `pad` |

```misa
print(str.slice("hello", 1, 4))      // ell
print(str.starts_with("hello", "he")) // true
print(str.split("a,b,c", ","))       // [a, b, c]
print(str.trim("  hi  "))             // hi
print(str.ord("A"))                  // 65
print(str.chr(128512))               // 😀
print(str.join(["a", "b"], "-"))     // a-b
print(str.replace("aaa", "a", "bb")) // bbbbbb
print(str.pad_left("42", 5, "0"))    // 00042
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

| Function | Signature |
|---|---|
| `time.now_utc` | `time.now_utc() -> Int` — epoch seconds (always UTC) |
| `time.to_utc` | `time.to_utc(t: Int) -> Int` — identity; epoch timestamps are always UTC |
| `time.from_parts` | `time.from_parts(y, mo, d, h, mi, s: Int) -> Int` — UTC epoch; `E300` on invalid dates (no silent normalization) |
| `time.add` | `time.add(t: Int, seconds: Int) -> Int` |
| `time.add_hours` | `time.add_hours(t: Int, h: Int) -> Int` |
| `time.in_tz` | `time.in_tz(t: Int, offset_hours: Float) -> Int` — shift so UTC formatting shows wall-clock at the offset (v1: offset-based, no tz database, no DST) |
| `time.weekday` | `time.weekday(t: Int) -> Text` — `"Monday"` … `"Sunday"` (UTC) |
| `time.is_leap` | `time.is_leap(year: Int) -> Bool` |

```misa
let t = time.from_parts(2026, 10, 7, 12, 0, 0)
print(time.weekday(t))            // Wednesday
print(time.in_tz(t, 1) - t)      // 3600
```

All platforms ✅.

## `regex` — patterns checked at compile time

`regex("...")` validates the pattern **while checking**: a bad pattern is
`E200` with the index of the problem, not a runtime surprise. (A pattern
built at runtime is validated when `regex()` runs — `E300`.)

```misa
let email = regex("([A-Za-z0-9_]+)@([A-Za-z0-9_]+)\\.([a-z]+)")
print(regex.matches(email, "user@example.com"))  // true
print(regex.find_all(regex("[0-9]+"), "a1 b22")) // [1, 22]
print(regex.replace(email, "u@x.com", "$2@$1"))   // x@u
```

| Function | Signature |
|---|---|
| `regex` | `regex(pattern: Text) -> Regex` |
| `regex.matches` | `regex.matches(re: Regex, text: Text) -> Bool` — whole-text match |
| `regex.find` | `regex.find(re: Regex, text: Text) -> Text?` — first match or `nil` |
| `regex.find_all` | `regex.find_all(re: Regex, text: Text) -> [Text]` |
| `regex.replace` | `regex.replace(re: Regex, text: Text, repl: Text) -> Text` — `$1`–`$9`, `$$` |
| `regex.captures` | `regex.captures(re: Regex, text: Text) -> [Text]?` — groups or `nil` |

Supported: literals, `.`, `^` `$`, `*` `+` `?` `{m,n}`, `(...)`, `|`,
`[...]`, `\n` `\t` and escaped metacharacters. Backtracking matches PCRE
(verified against Python `re`). Not in v1: `\d` `\w` `\s` (use `[...]`),
`(?...)`, backreferences, lazy quantifiers, lookaround — the checker
rejects these with a helpful `E200` instead of miscompiling them.

Both backends run the same algorithm, so patterns behave identically in
`misa run` and compiled binaries.

All platforms ✅.

## `crypto` — hashes

| Function | Signature |
|---|---|
| `crypto.sha256` | `crypto.sha256(data: Text) -> Text` — 64-char hex |
| `crypto.sha1` | `crypto.sha1(data: Text) -> Text` — 40-char hex |
| `crypto.md5` | `crypto.md5(data: Text) -> Text` — 32-char hex |
| `crypto.hmac_sha256` | `crypto.hmac_sha256(key: Text, msg: Text) -> Text` — 64-char hex |

```misa
print(crypto.sha256("hello"))  // 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
```

Self-contained C in the AOT runtime (no OpenSSL); `hashlib` in the
interpreter. Digests agree bit-for-bit. All platforms ✅.

## `compress` — gzip

| Function | Signature |
|---|---|
| `compress.gzip` | `compress.gzip(data: Text) -> Bytes` |
| `compress.gunzip` | `compress.gunzip(data: Bytes) -> Text throws E300` |
| `compress.gunzip_bytes` | `compress.gunzip_bytes(data: Bytes) -> Bytes throws E300` |
| `compress.gzip_file` | `compress.gzip_file(path: Text, out: Text) -> Int throws E303` |
| `compress.gunzip_file` | `compress.gunzip_file(path: Text, out: Text) -> Int throws E303, E300` |

```misa
let b = compress.gzip("hello world")  // <bytes: 31>
print(len(b))                          // 31
print(compress.gunzip(b))              // hello world

// files stream in constant memory; returns byte counts
let n = compress.gzip_file("big.txt", "big.txt.gz")
let m = compress.gunzip_file("big.txt.gz", "big.back.txt")
```

Compressed data lives in the `Bytes` type: an opaque binary buffer with
`len(b)`, `==`/`!=` (byte-wise), rendering as `<bytes: N>`. Corrupt input
is a catchable `E300` (never a crash); `gunzip` additionally requires
UTF-8 output (`E300` otherwise — use `gunzip_bytes` for binary data).

Framing is fully deterministic (fixed header, level 9), so both backends
emit **byte-identical** output. zlib-backed on host/Android; targets
without zlib (web) raise `E600`.

## `cli` — declarative args parsing

| Function | Signature |
|---|---|
| `cli.parser` | `cli.parser(prog: Text, desc: Text) -> CliParser` |
| `cli.flag` | `cli.flag(p: CliParser, name: Text, metavar: Text, help: Text[, default: Text]) -> Void throws E300` |
| `cli.bool_flag` | `cli.bool_flag(p: CliParser, name: Text, help: Text) -> Void throws E300` |
| `cli.short` | `cli.short(p: CliParser, name: Text, short: Text) -> Void throws E300` |
| `cli.command` | `cli.command(p: CliParser, name: Text, help: Text) -> Void throws E300` |
| `cli.parse` | `cli.parse(p: CliParser, argv: [Text]) -> CliArgs? throws E300` |
| `cli.get` | `cli.get(args: CliArgs?, name: Text) -> Text throws E300` |
| `cli.get_bool` | `cli.get_bool(args: CliArgs?, name: Text) -> Bool throws E300` |
| `cli.get_command` | `cli.get_command(args: CliArgs?) -> Text?` |
| `cli.positionals` | `cli.positionals(args: CliArgs?) -> [Text]` |

```misa
let p = cli.parser("tasks", "A tiny task manager")
p.flag("db", "PATH", "Database path", "/tmp/tasks.db")
p.bool_flag("verbose", "Verbose output")
p.short("db", "d")
p.command("add", "Add a task")
p.command("list", "List tasks")
let args = p.parse(argv())          // throws E300; nil after --help
if args == nil { return }
print(args.get("db"))               // value or default
print(args.get_bool("verbose"))     // true/false
print(args.command())               // "add" / "list" / nil
print(args.positionals())           // [Text], excluding the command word
```

`--flag value`, `--flag=value`, `-d value`, `-d=value` are all accepted;
`--` ends flag parsing; a lone `-` is a positional. Bad usage is a
catchable `E300` with a usage hint (`unknown flag '--bogus'`,
`flag '--db' needs a value`, `unknown command 'frobnicate'`, combined
shorts like `-abc` are rejected). `--help`/`-h` prints generated help
(aligned `Flags:`/`Commands:` sections, byte-identical on both backends)
and returns `nil`.

Schema mistakes are caught early: duplicate flags/commands/shorts, empty
flag names, and multi-char shorts are `E300` at definition time (`E200`
at check time for string literals). `get` on an undefined name, on a
bool flag, or on an unset flag without a default is `E300`; `get_bool`
on a value flag is `E300`. All declaring/parsing calls are checked
throws — a function calling them needs `throws E300` (`E227` otherwise).

## `random` — seeded PRNG

| Function | Signature |
|---|---|
| `random.seed` | `random.seed(n: Int)` — seed; same seed ⇒ same sequence **within a backend** |
| `random.int` | `random.int(min: Int, max: Int) -> Int` — inclusive; `E300` if `min > max` |
| `random.float` | `random.float() -> Float` — in `[0, 1)` |
| `random.choice` | `random.choice(xs: [T]) -> T` — `E300` on empty |
| `random.shuffle` | `random.shuffle(xs: [T]) -> [T]` — new shuffled list |

```misa
random.seed(42)
print(random.int(1, 100))
print(random.shuffle([1, 2, 3]))
```

Determinism is per-backend (splitmix64 in AOT, Mersenne Twister in the
interpreter) — the streams differ across backends by necessity. All
platforms ✅.

## Collections — `map`/`filter`/`fold`, `Set<T>`, `list.*`

```misa
print(map([1, 2, 3], |x| x * 2))          // [2, 4, 6]
print(filter([1, 2, 3, 4], |x| x % 2 == 0)) // [2, 4]
print(fold([1, 2, 3, 4], 0, |a, b| a + b))  // 10
print(sort_by(["bb", "a"], |a, b| len(a) < len(b))) // [a, bb]

var xs: [Int] = []
for i in 0..3 { list.push(xs, i) } // amortized O(1) append
print(xs)                         // [0, 1, 2]

let s = set()
s.add(1)
s.add(1)
print(set.len(s))              // 1
print(set.to_list(s))          // [1]
print(list.zip([1, 2], ["a"])) // [[1, a]]
print(list.unique([2, 1, 2]))   // [2, 1]
```

| Function | Signature |
|---|---|
| `map` / `filter` / `fold` / `sort_by` | higher-order list functions (`sort_by` takes a `less` comparator and is stable) |
| `list.push` | mutate a list by appending one value (amortized O(1); aliases observe the append) |
| `list.flat` / `list.zip` / `list.unique` | `[[T]] -> [T]` / pair up / dedupe (first-occurrence order) |
| `set()` | `set() -> Set<T>` — empty set; `s.add(x)` method syntax works too |
| `set.add` / `set.contains` / `set.remove` | mutate / test / delete |
| `set.union` / `set.intersect` / `set.diff` | set algebra (insertion-ordered) |
| `set.to_list` / `set.len` | to list / count (`len(s)` works as well) |

Sets are insertion-ordered with value-equality membership (v1: O(n)).
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
