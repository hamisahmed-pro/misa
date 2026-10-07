# Misa Standard Library — v0.2

v0.2 (2026-10-05); supersedes v0.1. Everything here is available with
no import. The `str` and `process` namespaces were added 2026-10-06
as Phase 6 (self-hosting) prerequisites.

## 1. Builtin functions

| Function | Signature | Description |
|---|---|---|
| `print` | `print(x: Text)` | Write a line to stdout. `print` accepts any value; non-text renders canonically. |
| `len` | `len(x: [T]) -> Int` | Number of elements. Also accepts `Text` (chars) and `Map` (entries). |
| `str` | `str(x: any) -> Text` | Canonical text rendering of any value. |
| `int` | `int(x: Text) -> Int` | Parse; error `E300` on failure. |
| `float` | `float(x: Text) -> Float` | Parse; error `E300` on failure. |
| `assert` | `assert(cond: Bool, msg: Text)` | Abort with `E301` and `msg` if `cond` is false. |
| `assert_eq` | `assert_eq(a: any, b: any)` | Abort with `E301` (`assert_eq failed: <a> != <b>`) if `a == b` is false. |
| `assert_ne` | `assert_ne(a: any, b: any)` | Abort with `E301` (`assert_ne failed: both are <a>`) if `a == b` is true. |
| `argv` | `argv() -> [Text]` | Command-line arguments, excluding the program name (like Python's `sys.argv[1:]`). Pass them after `--`: `misa run prog.misa -- foo bar`. Host AOT binaries receive the real OS argv; on Android/iOS/web `argv()` is `[]` (those platforms have no CLI). |
| `env` | `env(name: Text) -> Text` | Environment variable; error `E302` if unset. |
| `builder` | `builder() -> Builder` | Create a `Text.Builder` for amortized O(1) string appends (see below). |

### `Text.Builder` (P-1)

Repeated `s = s + x` in a loop is O(n²). `Text.Builder` appends in amortized O(1) via exponential overallocation:

| Method | Signature | Description |
|---|---|---|
| `append` | `b.append(x: any) -> Void` | Append the canonical rendering of `x`. |
| `build` | `b.build() -> Text` | Concatenate all parts into a single `Text`. |

```misa
let b = builder()
for x in items { b.append(x) }
let s = b.build()
```

## 2. Filesystem (`fs` namespace)

| Function | Signature | Description |
|---|---|---|
| `fs.read` | `fs.read(path: Text) -> Text` | Read a whole file as text; throws `Error{code: "E303"}` on failure. |
| `fs.write` | `fs.write(path: Text, text: Text)` | Write text to a file (creates/overwrites); throws `Error{code: "E303"}` on failure. |
| `fs.temp_file` | `fs.temp_file() -> Text` | Create an empty temp file, return its path; throws `Error{code: "E303"}` on failure. |
| `fs.temp_dir` | `fs.temp_dir() -> Text` | Create an empty temp directory, return its path; throws `Error{code: "E303"}` on failure. |

Paths are resolved against the process working directory. `misa run`
is a developer tool and runs with the user's privileges; do not run
untrusted Misa programs — a program can read and write your files.

## 3. HTTP (`http` namespace)

| Function | Signature | Description |
|---|---|---|
| `http.get` | `http.get(url: Text) -> Text` | GET a URL, return the body as text; throws `Error{code: "E304"}` on network failure or non-2xx status (30s timeout). |
| `http.post` | `http.post(url: Text, body: Text) -> Text` | POST `body` as `text/plain`; returns the response body as text; `E304` on failure (30s timeout). |
| `http.post_json` | `http.post_json(url: Text, v: value) -> Text` | `json.stringify(v)` POSTed as `application/json`; returns the response body; `E304` on failure. |
| `http.serve` | `http.serve(port: Int, handler: fn(Request) -> Response) -> Void` | Run a blocking HTTP/1.1 server on `port`. Each connection is handled sequentially (v1); use `spawn` for concurrency (see example). Throws `E304` on bind failure. See "HTTP server" below. |

## 3b. TCP sockets (`net` namespace) — Wave 3

All socket operations throw `E304` on failure (consistent with the `http.*`
client). Sockets integrate with structured concurrency: a `spawn`ed task can
`accept()` while the main task `connect()`s; child tasks are cancelled when
their scope exits.

| Function | Signature | Description |
|---|---|---|
| `net.connect` | `net.connect(host: Text, port: Int) -> Socket` | TCP-connect to `host:port`; `E304` on failure (DNS, refused, timeout). |
| `net.listen` | `net.listen(port: Int) -> Listener` | Bind `127.0.0.1:port` and listen; `port: 0` picks an ephemeral port (read it back with `lis.port()`). `E304` on failure. |
| `net.accept` | `net.accept(lis: Listener) -> Socket` | Block until a client connects; returns the connected `Socket`. `E304` on failure. Method style: `lis.accept()`. |
| `net.port` | `net.port(lis: Listener) -> Int` | The bound port (useful with `net.listen(0)`). Method style: `lis.port()`. |
| `listener.close` | `listener.close(lis: Listener)` | Close the listener. Method style: `lis.close()`. |
| `socket.send` | `socket.send(s: Socket, data: Text) -> Int` | Send UTF-8 bytes; returns bytes sent. Method style: `s.send(data)`. |
| `socket.recv` | `socket.recv(s: Socket, max: Int) -> Text` | Receive up to `max` bytes, decoded as UTF-8 (incremental decoder handles splits). Returns `""` on clean EOF — never an error. Method style: `s.recv(max)`. |
| `socket.close` | `socket.close(s: Socket)` | Close the socket. Method style: `s.close()`. |
| `socket.set_timeout` | `socket.set_timeout(s: Socket, ms: Int)` | Set recv/send timeout in milliseconds; `0` disables. A timeout raises `E304`. Method style: `s.set_timeout(ms)`. |

### HTTP server (`http.serve`)

```misa
http.serve(8080, |req: Request| {
  return {status: 200, body: "hello " + req.path}
})
```

`Request` is `{method: Text, path: Text, query: Map<Text,Text>,
headers: Map<Text,Text>, body: Text}`. `Response` is `{status: Int,
headers: Map<Text,Text>, body: Text}` — omitted fields default to `200`,
`{}`, and `"text/plain"`.

**Parser limits** (identical on both backends; violations get clean status
codes, never a crash): max 64KB headers → `431`; max 10MB body →
`413`; max 100 headers → `431`; `Transfer-Encoding: chunked` → `501`
(not implemented); malformed request line/headers → `400`. A handler that
throws is isolated per-connection → `500`, and the server keeps running.

**Concurrency:** v1 handles connections sequentially. For concurrent
connections, `spawn` a task per connection inside the handler, or run the
server in a `spawn`ed task alongside clients (see `docs/tour.md`).

**Per-target status:** interpreter ✅ (Python `socket`); Linux/macOS AOT ✅
(POSIX sockets, non-blocking + fiber-scheduler parking); Android ✅/iOS ✅
(compiles — POSIX code is in the runtime — but not yet verified on device);
web ❌ → clean `E600` (browsers cannot open TCP sockets).

## 3b. Strings (`str` namespace) — Phase 6 prerequisite

All six functions use Unicode code-point semantics (not bytes) on both
the interpreter and the AOT backend. `str.chr` rejects negative values,
surrogates (`0xD800`–`0xDFFF`), and values above `0x10FFFF` with `E300`.

| Function | Signature | Description |
|---|---|---|
| `str.slice` | `str.slice(s: Text, i: Int, j: Int) -> Text` | Characters `[i, j)`; indices clamp to `[0, len(s)]`; empty when `i >= j`. |
| `str.starts_with` | `str.starts_with(s: Text, prefix: Text) -> Bool` | True when `s` starts with `prefix`. |
| `str.split` | `str.split(s: Text, sep: Text) -> [Text]` | Split on `sep`; error when `sep` is empty. |
| `str.trim` | `str.trim(s: Text) -> Text` | Strip leading/trailing whitespace. |
| `str.ord` | `str.ord(c: Text) -> Int` | Unicode code point of the first character; error on empty input. |
| `str.chr` | `str.chr(code: Int) -> Text` | Character for a Unicode code point; error on invalid code. |
| `str.join` | `str.join(xs: [Text], sep: Text) -> Text` | Join with separator (the inverse of `str.split`). |
| `str.replace` | `str.replace(s: Text, old: Text, new: Text) -> Text` | Replace all non-overlapping occurrences of `old`; error when `old` is empty. |
| `str.ends_with` | `str.ends_with(s: Text, suffix: Text) -> Bool` | True when `s` ends with `suffix`. |
| `str.to_upper` | `str.to_upper(s: Text) -> Text` | Uppercase. v1 maps ASCII `a`–`z` only (documented); full Unicode case mapping is future work. |
| `str.to_lower` | `str.to_lower(s: Text) -> Text` | Lowercase (ASCII-only in v1, as above). |
| `str.pad_left` | `str.pad_left(s: Text, width: Int, pad: Text) -> Text` | Pad on the left with `pad`'s first char to `width` chars; error when `pad` is empty. |
| `str.pad_right` | `str.pad_right(s: Text, width: Int, pad: Text) -> Text` | Pad on the right (as above). |

## 3c. Processes (`process` namespace) — Phase 6 prerequisite

| Function | Signature | Description |
|---|---|---|
| `process.run` | `process.run(cmd: [Text]) -> record` | Run a command; returns `{code: Int, stdout: Text, stderr: Text}`. Throws `Error{code: "E303"}` when the command cannot be started or times out (5 minutes). **AOT: not supported** — `misa build` rejects it with `E600` (subprocess in compiled binaries is out of scope; the self-hosted build driver runs on the interpreter). **Security note:** a Misa program with `process.run` can execute arbitrary commands — treat it with the same care as `fs.write`. |

## 3e. JSON (`json` namespace) — Phase 5

| Function | Signature | Description |
|---|---|---|
| `json.parse` | `json.parse(s: Text) -> value` | Parse JSON text. Objects become records, arrays become lists, strings become `Text`, `true`/`false` become `Bool`, `null` becomes `nil`. Numbers become `Int` when they have no fraction or exponent and fit in int64, otherwise `Float`. Throws `Error{code: "E300"}` on invalid JSON. |
| `json.stringify` | `json.stringify(v) -> Text` | Canonical compact JSON (`{"a":1}`, `ensure_ascii=False` semantics). Throws `Error{code: "E300"}` for non-JSON values (functions/closures, `Ptr`, `Db` handles, `Media`, `Iter`, `Error`). |

## 3f. Time (`time` namespace) — Phase 5, extended Wave 3

| Function | Signature | Description |
|---|---|---|
| `time.now` | `time.now() -> Int` | Current Unix epoch time in seconds. |
| `time.now_ms` | `time.now_ms() -> Int` | Current Unix epoch time in milliseconds. |
| `time.format` | `time.format(epoch: Int, fmt: Text) -> Text` | Format an epoch timestamp with `strftime`-style directives, in UTC (e.g. `"%Y-%m-%d %H:%M:%S"`). |
| `time.parse` | `time.parse(s: Text, fmt: Text) -> Int` | Parse `s` with `strptime`-style `fmt` (UTC); returns epoch seconds. Throws `Error{code: "E300"}` when `s` does not match `fmt`. |
| `time.now_utc` | `time.now_utc() -> Int` | Current Unix epoch time in seconds (identical to `time.now`; the name documents that epoch timestamps are always UTC). |
| `time.to_utc` | `time.to_utc(t: Int) -> Int` | Identity — epoch timestamps are always UTC; exists for clarity at API boundaries. |
| `time.from_parts` | `time.from_parts(year: Int, month: Int, day: Int, hour: Int, min: Int, sec: Int) -> Int` | Epoch seconds for the given UTC date/time. Throws `Error{code: "E300"}` for invalid dates (e.g. month 13, Feb 30 — no silent normalization). |
| `time.add` | `time.add(t: Int, seconds: Int) -> Int` | `t + seconds`. |
| `time.add_hours` | `time.add_hours(t: Int, h: Int) -> Int` | `t + h * 3600`. |
| `time.in_tz` | `time.in_tz(t: Int, offset_hours: Float) -> Int` | Shift `t` so UTC formatting shows wall-clock time at the given offset (fractional hours OK, e.g. `5.5`). v1 is offset-based — there is no tz database (no DST rules, no named zones). |
| `time.weekday` | `time.weekday(t: Int) -> Text` | English weekday name in UTC (`"Monday"` … `"Sunday"`). |
| `time.is_leap` | `time.is_leap(year: Int) -> Bool` | Gregorian leap-year rule. |

## 3g. Math (`math` namespace) — Phase 5

| Function | Signature | Description |
|---|---|---|
| `math.sqrt` | `math.sqrt(x: Float) -> Float` | Square root; throws `Error{code: "E300"}` for negative `x`. |
| `math.pow` | `math.pow(x: Float, y: Float) -> Float` | `x` raised to `y`. |
| `math.abs` | `math.abs(x: Int\|Float) -> Int\|Float` | Absolute value (same numeric type as input). |
| `math.floor` | `math.floor(x: Float) -> Int` | Round down. |
| `math.ceil` | `math.ceil(x: Float) -> Int` | Round up. |
| `math.round` | `math.round(x: Float) -> Int` | Round half away from zero (`2.5 -> 3`, `-2.5 -> -3`). |

## 3h. Regex (`regex` namespace) — Wave 3

`regex(pattern: Text) -> Regex` compiles a pattern **at check time** when
the pattern is a literal: an invalid pattern is `E200` (with the index of
the problem), not a runtime surprise. A non-literal pattern is validated
at runtime (`E300`).

Supported grammar (v1): literals, `.` (any char except `\n`), `^`/`$`
anchors, `*` `+` `?` `{m}` `{m,}` `{m,n}` (greedy, backtracking), `(...)`
groups, `|` alternation, `[...]` classes (ranges, negation `[^...]`),
escapes for metacharacters (`\.`, `\*`, …) and `\n`/`\t`. Matching is by
Unicode code point on both backends. Backtracking follows PCRE semantics
(leftmost match wins; a later failure backtracks into earlier choices —
verified against Python `re` on the supported subset).

Rejected at check time with guidance: `\d`/`\w`/`\s`/`\b` (hints name the
`[...]` equivalent), `(?...)` groups, backreferences, lazy quantifiers,
lookaround. These are future work, not silent miscompiles.

| Function | Signature | Description |
|---|---|---|
| `regex` | `regex(pattern: Text) -> Regex` | Compile; `E200` at check time for a bad literal pattern, `E300` at runtime for a bad dynamic pattern. |
| `regex.matches` | `regex.matches(re: Regex, text: Text) -> Bool` | True if the whole text matches. |
| `regex.find` | `regex.find(re: Regex, text: Text) -> Text?` | First match, or `nil`. |
| `regex.find_all` | `regex.find_all(re: Regex, text: Text) -> [Text]` | All non-overlapping matches (empty matches advance one char). |
| `regex.replace` | `regex.replace(re: Regex, text: Text, repl: Text) -> Text` | Replace all matches; `$1`–`$9` refer to groups, `$$` is a literal `$`. |
| `regex.captures` | `regex.captures(re: Regex, text: Text) -> [Text]?` | Capture groups of the first match, or `nil`. Unmatched groups are `""`. |

Implementation note: both backends run the *same* backtracking
algorithm — the interpreter runs `impl/misa/regexp.py`, the AOT runtime a
C mirror of it (`misa_rt.c`). There is deliberately no POSIX
`regcomp`/`regexec` or Python `re` underneath, so the two backends cannot
diverge by construction.

## 3i. Crypto (`crypto` namespace) — Wave 3

| Function | Signature | Description |
|---|---|---|
| `crypto.sha256` | `crypto.sha256(data: Text) -> Text` | SHA-256 hex digest (64 chars). |
| `crypto.sha1` | `crypto.sha1(data: Text) -> Text` | SHA-1 hex digest (40 chars). |
| `crypto.md5` | `crypto.md5(data: Text) -> Text` | MD5 hex digest (32 chars). |
| `crypto.hmac_sha256` | `crypto.hmac_sha256(key: Text, msg: Text) -> Text` | HMAC-SHA-256 hex digest. |

The AOT runtime carries self-contained C implementations (no OpenSSL
dependency); the interpreter uses `hashlib`/`hmac`. Digests agree
bit-for-bit across backends.

## 3j. Random (`random` namespace) — Wave 3

| Function | Signature | Description |
|---|---|---|
| `random.seed` | `random.seed(n: Int) -> Void` | Seed the generator. The same seed reproduces the same sequence **within a backend**. |
| `random.int` | `random.int(min: Int, max: Int) -> Int` | Uniform int in `[min, max]` (inclusive); `E300` if `min > max`. |
| `random.float` | `random.float() -> Float` | Uniform float in `[0, 1)`. |
| `random.choice` | `random.choice(xs: [T]) -> T` | Uniform element; `E300` on an empty list. |
| `random.shuffle` | `random.shuffle(xs: [T]) -> [T]` | New list, Fisher-Yates shuffled (input untouched). |

Determinism is per-backend, not cross-backend: the AOT runtime uses
splitmix64, the interpreter Python's Mersenne Twister — no two PRNGs can
produce the same stream, so `random.seed(42); random.int(1,100)` may differ
between `misa run` and a compiled binary. Within one backend the stream is
exactly reproducible. (Documented honestly; cross-backend determinism is
future work.)

## 3d. Database (`db` namespace) — Phase 5

SQLite, zero dependencies. `Db` is an opaque handle type.

| Function | Signature | Description |
|---|---|---|
| `db.open` | `db.open(path: Text) -> Db` | Open (creating if needed) a SQLite database; `":memory:"` for transient DBs. |
| `db.exec` | `db.exec(db: Db, sql: Text, args: [any]?) -> Int` | Run a statement; returns rows changed. `?` placeholders bound from `args` (Int/Float/Bool/Text/nil). Prepared statements are cached per connection. |
| `db.query` | `db.query(db: Db, sql: Text, args: [any]?) -> [record]` | Run a SELECT; returns a list of records (column name → value). INTEGER→Int, REAL→Float, TEXT→Text, NULL→nil; BLOB is an E303 error in v1. |
| `db.close` | `db.close(db: Db) -> Void` | Close the handle (finalizes cached statements). |
| `db.exec_many` | `db.exec_many(db: Db, sql: Text, rows: [[any]]) -> Int` | Bulk exec: prepare once, bind+step each row. Returns total rows changed. |
| `db.begin` | `db.begin(db: Db) -> Void` | Begin an explicit transaction. |
| `db.commit` | `db.commit(db: Db) -> Void` | Commit the explicit transaction. |
| `db.rollback` | `db.rollback(db: Db) -> Void` | Roll back the explicit transaction. |

All eight throw `Error{code: "E303"}` on SQLite failures.

**Typed queries.** Declare schemas with `table`; the checker validates
table/column names in `db.query` SQL at compile time (E224) and infers
the result record type:

```misa
table users {
  id: Int @primary,
  name: Text,
  email: Text?,
}
let rows = db.query(db, "SELECT id, name FROM users")
// rows: [{id: Int, name: Text}] — r.name type-checks
```

Queries over undeclared tables return dynamic `[record]` (no static
checking, not an error). v1 validates names in SELECT/FROM/WHERE only —
no JOIN or expression type-checking.

## 3k. Collections — Wave 3

Higher-order functions take a lambda (or any function value). A
user-defined function with the same name shadows the builtin.

| Function | Signature | Description |
|---|---|---|
| `map` | `map(xs: [T], f: (T) -> U) -> [U]` | Transform each element. |
| `filter` | `filter(xs: [T], f: (T) -> Bool) -> [T]` | Keep elements where `f` returns true. |
| `fold` | `fold(xs: [T], init: U, f: (U, T) -> U) -> U` | Left fold (the one name — there is no `reduce` alias). |
| `sort_by` | `sort_by(xs: [T], less: (T, T) -> Bool) -> [T]` | Stable sort by a less-than comparator (mergesort). |
| `list.flat` | `list.flat(xss: [[T]]) -> [T]` | Concatenate a list of lists. |
| `list.zip` | `list.zip(xs: [T], ys: [U]) -> [[any]]` | Pair up elements; stops at the shorter list. (Pairs are 2-element lists — Misa has no tuple type.) |
| `list.unique` | `list.unique(xs: [T]) -> [T]` | Deduplicate, keeping first-occurrence order. |

### `Set<T>` — Wave 3

`set()` constructs an empty set (`set<Text>()` pins the element type).
Sets are insertion-ordered; `set.to_list` returns elements in insertion
order. Membership uses value equality (`==` semantics). v1 is O(n)
membership — a hashed implementation is future work; semantics are
identical on both backends.

| Function | Signature | Description |
|---|---|---|
| `set` | `set() -> Set<unknown>` | Empty set. Method syntax also works: `s.add(x)`. |
| `set.add` | `set.add(s: Set<T>, x: T) -> Void` | Insert (no-op if present). |
| `set.contains` | `set.contains(s: Set<T>, x: T) -> Bool` | Membership test. |
| `set.remove` | `set.remove(s: Set<T>, x: T) -> Void` | Delete (no-op if absent). |
| `set.union` | `set.union(a: Set<T>, b: Set<T>) -> Set<T>` | All elements of both. |
| `set.intersect` | `set.intersect(a: Set<T>, b: Set<T>) -> Set<T>` | Elements in both. |
| `set.diff` | `set.diff(a: Set<T>, b: Set<T>) -> Set<T>` | Elements in `a` but not `b`. |
| `set.to_list` | `set.to_list(s: Set<T>) -> [T]` | Elements in insertion order. |
| `set.len` | `set.len(s: Set<T>) -> Int` | Element count. `len(s)` also works. |

## 3l. Compression (`compress` namespace) — Final wave

gzip compression backed by zlib (AOT) / `zlib` (interpreter). Framing is
fully deterministic — fixed 10-byte header (`mtime=0`), raw deflate at
level 9, CRC32 + ISIZE trailer — so both backends emit **byte-identical**
output (verified: `gzip.compress(..., compresslevel=9, mtime=0)` parity).

Compressed data lives in the `Bytes` type: an opaque binary buffer.
`Bytes` supports `len(b)`, `==`/`!=` (byte-wise), and renders as
`<bytes: N>`. There is deliberately no indexing/slicing in v1.

| Function | Signature | Description |
|---|---|---|
| `compress.gzip` | `compress.gzip(data: Text) -> Bytes` | gzip the UTF-8 bytes of `data`. |
| `compress.gunzip` | `compress.gunzip(data: Bytes) -> Text throws E300` | Decompress; `E300` on corrupt input or non-UTF-8 output (use `gunzip_bytes` for binary). |
| `compress.gunzip_bytes` | `compress.gunzip_bytes(data: Bytes) -> Bytes throws E300` | Decompress to raw bytes; `E300` on corrupt input. |
| `compress.gzip_file` | `compress.gzip_file(path: Text, out: Text) -> Int throws E303` | Stream-compress file to file (constant memory); returns compressed size. |
| `compress.gunzip_file` | `compress.gunzip_file(path: Text, out: Text) -> Int throws E303, E300` | Stream-decompress file to file; returns decompressed size. |

Without zlib on the target (e.g. web), all five raise `E600` — never a
build break. Android links the NDK's zlib.

## 3m. CLI args parsing (`cli` namespace)

Declarative, typed command-line parsing: declare flags and subcommands on
a `CliParser`, parse `argv()`, and get a typed `CliArgs` back. Parse
failures are catchable `E300` with a usage hint; `--help`/`-h` prints
generated help to stdout and returns `nil` (the program checks and
returns — this keeps `parse` pure-ish and Misa-shaped).

```misa
let p = cli.parser("tasks", "A tiny task manager")
p.flag("db", "PATH", "Database path", "/tmp/tasks.db")  // 4th arg = default
p.bool_flag("verbose", "Verbose output")
p.short("db", "d")                  // -d as alias for --db
p.command("add", "Add a task")
p.command("list", "List tasks")
let args = p.parse(argv())          // throws E300; nil after --help
if args == nil { return }
print(args.get("db"))               // value or default
print(args.get_bool("verbose"))     // true/false
print(args.command())               // "add" / "list" / nil
print(args.positionals())           // [Text], excluding the command word
```

| Function | Signature | Description |
|---|---|---|
| `cli.parser` | `cli.parser(prog: Text, desc: Text) -> CliParser` | New argument parser. |
| `cli.flag` | `cli.flag(p: CliParser, name: Text, metavar: Text, help: Text[, default: Text]) -> Void throws E300` | Value flag `--name <metavar>`; `E300` on duplicate. |
| `cli.bool_flag` | `cli.bool_flag(p: CliParser, name: Text, help: Text) -> Void throws E300` | Boolean flag `--name`; `E300` on duplicate. |
| `cli.short` | `cli.short(p: CliParser, name: Text, short: Text) -> Void throws E300` | One-char alias (`-d` for `--db`); `-h` reserved; `E300` on unknown/duplicate/reserved. |
| `cli.command` | `cli.command(p: CliParser, name: Text, help: Text) -> Void throws E300` | Subcommand; `E300` on duplicate. |
| `cli.parse` | `cli.parse(p: CliParser, argv: [Text]) -> CliArgs? throws E300` | Parse; `E300` on bad usage (see rules); prints help + returns `nil` on `--help`/`-h`. |
| `cli.get` | `cli.get(args: CliArgs?, name: Text) -> Text throws E300` | Flag value, explicit or default; `E300` on unknown name, bool-flag misuse, unset-without-default, or nil receiver. |
| `cli.get_bool` | `cli.get_bool(args: CliArgs?, name: Text) -> Bool throws E300` | `true` iff the bool flag was passed; `E300` on unknown name, value-flag misuse, or nil receiver. |
| `cli.get_command` | `cli.get_command(args: CliArgs?) -> Text?` | Subcommand word, or `nil` (also `nil` on nil receiver). |
| `cli.positionals` | `cli.positionals(args: CliArgs?) -> [Text]` | Non-flag args excluding the command word (`[]` on nil receiver). |

**E300 rules** (exact; each message ends with a usage hint naming the program):

- Unknown long flag: `--bogus` → `unknown flag '--bogus'`.
- Unknown short flag: `-z` → `unknown flag '-z'`.
- Flag needs a value but the next token is missing or looks like a flag: `--db` (at end) or `--db --verbose` → `flag '--db' needs a value`. (Short form: `-d` → `flag '-d' needs a value`.)
- Value given to a bool flag: `--verbose=x` → `takes no value (it's a bool flag)`.
- Malformed flag: `--=x` → `malformed flag '--=x'`.
- Combined short flags: `-abc` → `not supported (use -a -b -c)`.
- Unknown command (first positional when commands are defined): `frobnicate` → `unknown command 'frobnicate' (expected one of: add list)`.
- Schema errors at definition time: duplicate flag/command/short, `short` for an undefined flag, multi-char short, `-h` reservation → `E300` (duplicates and bad literals are additionally `E200` at check time when the name is a string literal).
- `get` on an undefined flag name → `E300` (`unknown flag 'x'`); on a bool flag → `E300` (`use get_bool`); on an unset flag with no default → `E300` (`was not provided and has no default`). `get_bool` on a value flag → `E300` (`use get, not get_bool`). `get`/`get_bool` on a nil receiver → `E300` (`no parsed arguments (parse returned nil, likely --help)`).

**Token rules:** `--flag value`, `--flag=value`, `-d value`, `-d=value`; `--` ends flag parsing (everything after is positional); a lone `-` is a positional; when commands are defined, the first positional is the command word (a `-` there is an unknown command, like any invalid word); later flags may follow positionals; a repeated flag keeps the last value.

**Help format** (byte-identical on both backends):

```
tasks - A tiny task manager

Usage: tasks [flags] <command> [args]

Flags:
  -d, --db PATH  Database path (default: /tmp/tasks.db)
  --verbose      Verbose output
  -h, --help     Show this help and exit

Commands:
  add   Add a task
  list  List tasks
```

`--help` is always listed last in `Flags:`. The `Commands:` section appears only when commands are defined. When no commands are defined, `Usage:` ends with `[args]` and every positional lands in `positionals()`.

**Types:** `CliParser` and `CliArgs` are builtin types (method syntax `p.flag(..)` / `args.get(..)` desugars to `cli.*`, like `set.*`). `parse` returns `CliArgs?`, and `get`/`get_bool`/`get_command`/`positionals` accept `CliArgs?`, so the `if args == nil { return }` pattern typechecks without flow-sensitive narrowing.

**Checked throws:** `parse`, `flag`, `bool_flag`, `short`, `command`, `get`, `get_bool` all declare `throws E300` — calling them from a function without `throws E300` is `E227`.

**Overtake note:** check-time `E200` for empty flag names and multi-char shorts on string literals (like regex pattern validation); computed `--help` with aligned columns on both backends (byte-identical, asserted by AOT parity test `a33_cli`).

## 4. Maps

`Map<K, V>` with `K` = `Text` or `Int`:

```
let scores: Map<Text, Int> = {"ada": 92, "bisi": 71}
print(scores["ada"])   // 92
scores["chidi"] = 58   // insert
```

- Literal keys are strings (`{"k": v}`); a leading `IDENT` key would
  be a record literal instead. `{}` is an empty record; annotate for
  an empty map.
- `m[k]` returns the value; a missing key throws
  `Error{code: "E305"}` (catchable). `m[k] = v` inserts or updates
  (requires a `var` binding, like list element assignment).
- `len(m)` is the entry count.

## 5. Media constructors

Calling a media type name with a path builds a handle:

- `Photo("img.jpg")`, `Audio("a.mp3")`, `Video("v.mp4")`,
  `Document("report.pdf")`

A handle renders in prompts as `<Photo: img.jpg>`. The `gemini`
provider upgrades `Photo` handles whose files exist to real image
parts. (Phase 2: transcoding, thumbnails, OCR/text extraction.)

## 6. Screen components (closed set)

Layout: `Column`, `Row`, `Spacer`
Text: `Text(text:)`, `Heading(text:)`, `Banner(text:, style:)` —
  `style` is one of `"info"`, `"warning"`, `"error"`.
Input: `Button(label:, onTap:)`, `TextInput(id:, hint:)`,
  `CameraButton(onCapture:)`
Media: `Image(photo:)`
Lists: `List(items:)` — `items` is `[Text]` in v0.1 (Phase 2: rows).

Component arguments are named (`label: "Go"`). A block after the
argument list holds child components (layout only; giving children
to a leaf component is error `E206`).

`id: name` binds the component in the screen scope. Bound components
expose: `TextInput.text -> Text`. Event lambdas: `onTap: || { ... }`,
`onCapture: |photo| { ... }`.

## 7. Reserved for future versions

`Embedding` type and vector ops, on-device inference bindings.
(First-class function values shipped in Phase 5a; JSON and
dates/times shipped in Phase 5 stdlib2.)
