# Misa Tooling (Phase 2)

The Misa toolchain is designed to be driven by humans *and* AI agents with
equal fluency: every diagnostic is structured JSON, formatting is canonical
and byte-stable, and the language server speaks standard LSP.

## CLI surface

| Command | Purpose |
|---|---|
| `misa check <f.misa> [--json]` | Type/name check. `--json` emits `[{code, message, line, col, hint}]` — the machine-readable contract. |
| `misa fmt <f.misa> [--write]` | Canonical formatter (4-space, K&R, one blank line between items). |
| `misa run <f.misa> [--mock m.json]` | Interpret with hot-reload semantics. Args after `--` go to the program's `argv()`. |
| `misa debug [--agent] <f.misa>` | **Agent-native debugger** (see below). `--agent` speaks a JSON-line protocol for AI agents; without it, a human text REPL. |
| `misa profile <f.misa> [--json] [--interval-ms N]` | **Sampling CPU profiler** (see below): flat profile of where the interpreter spends its time. `--json` emits `[{function, file, line, samples, pct}]` for agents. |
| `misa test [path] [--mock m.json] [--json]` | Run unit `test` blocks (`*_test.misa` / `test_*.misa` discovered under directories) plus `eval` blocks. `--json` emits structured results for agents. |
| `misa repl` | **Interactive REPL** (see below): read-eval-print loop with persistent bindings, multi-line input, and `:type` showing the checker's inferred type. |
| `misa build <f.misa> [--target triple]` | LLVM AOT native binary. |
| `misa lsp` | **Language server** (JSON-RPC 2.0 over stdio). Never writes to stdout except protocol frames; logs to stderr. |

## Language server (`misa lsp`)

Capabilities: full-sync diagnostics (driven by the real checker —
identical codes to `misa check --json`), hover docs for every keyword /
stdlib builtin / screen component, completions (keywords, builtins,
components, snippet scaffolds for `provider` / `models` / `model` /
`screen` / `eval` blocks), and whole-document formatting via the real
`misa fmt` (output is byte-identical to the CLI).

Diagnostics resolve imports: for `file://` URIs the buffer is checked via
a temp sibling file, so relative `import "lib.misa"` chains resolve
exactly as `misa check` would. Untitled buffers fall back to single-file
checking (imports report `E112` there — use `misa check` for those).

Lifecycle: `initialize` → `initialized` → … → `shutdown` → `exit`.
Malformed input never crashes the server; it is logged to stderr and the
offending message dropped.

## VS Code extension (`editors/vscode/`)

While Misa is private, install from source:

```sh
cd editors/vscode && npm install --no-save vscode-languageclient
ln -s "$PWD" ~/.vscode/extensions/misa-0.3.0
# then reload VS Code; set misa.executablePath if `misa` isn't on PATH
```

Contents: `package.json` (language id `misa`), TextMate grammar
(`syntaxes/misa.tmLanguage.json` — keywords, types, strings with
`{interpolation}`, `"""` tristings, `30s`/`$0.05` literals, comments,
components, builtins), `language-configuration.json`,
`client/extension.js` (spawns `misa lsp` via vscode-languageclient),
commands *Misa: Check / Run / Build / Format document / Restart server*.

## Driving it from an agent (the flywheel interface)

This is the interface the agent-flywheel strategy rests on: an agent with
only the CLI + LSP can write, check, format, and fix Misa code in a closed
loop with no human in the loop.

**1. Check (structured errors):**
```sh
misa check app.misa --json
# [{"code":"E101","message":"undefined name 'foo'","line":12,"col":5,"hint":"..."}]
```
Error codes are stable (`E001` syntax … `E5xx` budgets); `line`/`col` are
1-based. Fix the reported location, re-check, repeat until `[]`.

**2. Format (canonical form):**
```sh
misa fmt app.misa --write   # the only acceptable form; agents must not hand-format
```

**3. LSP session (editors / long-lived agents):**
Frame every message with `Content-Length: N\r\n\r\n` over stdio.
```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}
← {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"textDocumentSync":1,"hoverProvider":true,"completionProvider":{},"documentFormattingProvider":true},...}}
→ {"jsonrpc":"2.0","method":"textDocument/didOpen","params":{"textDocument":{"uri":"file:///path/app.misa","languageId":"misa","version":1,"text":"..."}}}
← {"jsonrpc":"2.0","method":"textDocument/publishDiagnostics","params":{"uri":"...","diagnostics":[{"range":{"start":{"line":11,"character":4},"end":{"line":11,"character":5}},"severity":1,"code":"E101","source":"misa","message":"..."}]}}
```
(LSP lines/characters are 0-based; the CLI is 1-based.)

Useful requests: `textDocument/hover` (docs for the symbol under the
cursor), `textDocument/completion` (74 items: keywords, builtins,
components, scaffolds), `textDocument/formatting` (returns a single
whole-document `TextEdit`; empty array means already canonical),
`textDocument/didChange` with full text for live re-checks.

**4. Unit tests:** `test <name> { ... }` blocks in `*_test.misa` files;
`misa test [path]` discovers and runs them (fresh interpreter per test,
`--json` for agents). `assert_eq`/`assert_ne` render values on failure.

**5. Evals as tests:** `misa test app.misa --mock mocks.json` runs the
`eval` blocks — the agent's way of proving an AI feature works, in CI.

**Agent loop (canonical):** write → `misa check --json` → fix each
`{code,line,col}` → `misa fmt --write` → `misa test` → done. The 12/12
external cold trials were run almost exactly this way.

## Agent-native debugger (`misa debug --agent`)

The first debugger designed for AI agents. Not a better pdb — a JSON-line
protocol where an agent sets breakpoints, steps, and dumps **typed variable
state as structured data** instead of parsing text. The loop becomes: program
fails → agent attaches, inspects typed state at the failing line
programmatically, forms a hypothesis, verifies it with `eval`, fixes,
re-runs. No print-debugging, ever.

```
misa debug --agent prog.misa        # JSON lines on stdin/stdout
misa debug prog.misa                # human text REPL (same engine)
```

### Protocol

Every line on stdout is exactly one JSON object. Program `print()` output is
wrapped as `{"event":"output","text":...}` so it can never be mistaken for a
protocol line. `stderr` stays human-readable.

**Commands** (agent → debugger):

| Command | Response |
|---|---|
| `{"cmd":"break","file":"...","line":N}` | `{"ok":true,"breakpoint":id}` |
| `{"cmd":"clear","id":N}` | `{"ok":true}` |
| `{"cmd":"breakpoints"}` | `{"ok":true,"breakpoints":[{"id","file","line"}]}` |
| `{"cmd":"run"}` / `{"cmd":"continue"}` | resumes; events follow |
| `{"cmd":"step"}` | step into (one statement) |
| `{"cmd":"next"}` | step over calls |
| `{"cmd":"out"}` | run until the current function returns |
| `{"cmd":"locals","frame":0}` | `{"ok":true,"locals":[{"name","type","value","mutable"}]}` |
| `{"cmd":"eval","expr":"x+1","frame":0}` | `{"ok":true,"type":"Int","value":43}` or `{"ok":false,"error":{E-code…}}` |
| `{"cmd":"stack"}` | `{"ok":true,"frames":[{"depth","name","file","line"}]}` (depth 0 = current) |
| `{"cmd":"quit"}` | aborts the run |

**Events** (debugger → agent):

| Event | Meaning |
|---|---|
| `{"event":"started","program":"..."}` | session ready; set breakpoints, then `run` |
| `{"event":"break","breakpoint":id\|null,"file","line","col","function"}` | paused (breakpoint or step) |
| `{"event":"output","text":"..."}` | one line of program `print()` output |
| `{"event":"exit","code":0}` | program finished |
| `{"event":"error","code","message","line","col","hint"}` | uncaught Misa error (structured E-code) |

**Typed values** (in `locals` and `eval`): `{"type":"Int","value":42}`,
`{"type":"Text","value":"hi"}`, `{"type":"Bool"}`, `{"type":"Float"}`,
`{"type":"Nil"}`, `{"type":"List","value":[...],"length":n}` (nested values
are typed too — never strings to parse), `{"type":"Map"}`,
`{"type":"Record"}`, sum types as `{"type":"Opt","variant":"Some","value":[...]}`,
functions/handles as `{"type":"Function"/"Task"/"Channel"/"Ptr"/…}`.
Lists/maps truncate at 50 items (`"truncated":true`).

### Worked session: an agent catching a real bug

`shop.misa` computes a discounted total. The agent suspects the math and
attaches:

```
→ {"cmd":"break","file":"shop.misa","line":8}
← {"ok": true, "breakpoint": 1}
→ {"cmd":"run"}
← {"event":"break","breakpoint":1,"file":"shop.misa","line":8,"function":"<main>"}
→ {"cmd":"eval","expr":"discount(100, 10)"}
← {"ok": true, "type": "Float", "value": 90.0}
```

The agent expected `Int` — the **typed** dump shows `Float` instantly, no
print-debugging. This turned out to be a genuine checker/interpreter
divergence: the checker infers `Int / Int → Int`, but the interpreter does
float division (`7 / 2 → 3.5`) while the AOT backend emits integer `sdiv`
(→ `3`). Three backends, two answers — filed as a language bug. The
debugger earned its keep on day one.

Stepping and the stack on the same program:

```
→ {"cmd":"break","file":"shop.misa","line":2}     # inside discount()
→ {"cmd":"run"}
← {"event":"break","breakpoint":2,"line":2,"function":"discount"}
→ {"cmd":"locals"}
← {"ok":true,"locals":[{"name":"price","type":"Int","value":100},
                        {"name":"pct","type":"Int","value":10}]}
→ {"cmd":"out"}
← {"event":"break","breakpoint":null,"line":9,"function":"<main>"}
→ {"cmd":"stack"}
← {"ok":true,"frames":[{"depth":1,"name":"discount","file":"shop.misa","line":9},
                        {"depth":0,"name":"<main>","file":"shop.misa","line":1}]}
→ {"cmd":"continue"}
← {"event":"output","text":"owed: 675.0"}
← {"event":"exit","code":0}
```

(Typed transcript above verified by running `misa debug --agent` on this
exact program; some fields trimmed for readability.)

### Human mode

`misa debug prog.misa` is a thin text wrapper over the same engine:

```
(misa-debug) b shop.misa:2
breakpoint 1 at shop.misa:2
(misa-debug) r
[breakpoint 1] shop.misa:2 (discount)
(misa-debug) p price * pct
Int: 1000
(misa-debug) s
...
```

Commands: `b file:line`, `c`/`r` (continue/run), `s` (step), `n` (next),
`o` (out), `p expr`, `l` (locals), `st` (stack), `q` (quit), `h` (help).

### v1 limitations (stated, not faked)

- **Interpreter only.** AOT debugging is future work; `misa debug` always
  runs the interpreter, even for programs you normally AOT-compile.
- **Main thread only.** `spawn`ed tasks run free; breakpoints inside task
  bodies don't pause (v1).
- **Stdin is the command channel.** Programs that read stdin themselves
  conflict with the debugger in `--agent` mode.
- The program is type-checked before the session starts (`misa check`
  errors go to stderr, exit 1) — you debug runtime behavior, not syntax.

## Sampling profiler (`misa profile`)

A sampling CPU profiler for the interpreter: a timer thread reads the
currently-executing statement every N ms (default 10) and aggregates
into a flat profile — `% time, samples, function, file:line`, sorted
by samples descending.

```
$ misa profile fib.misa
75025
profile: 719 samples (10 ms interval)
samples      %  function                  location
    499   69.4  fib                       fib.misa:2
    219   30.5  fib                       fib.misa:3
      1    0.1  main                      fib.misa:7
```

`misa profile --json fib.misa` emits the agent interface instead:

```json
{"samples": 719, "interval_ms": 10.0, "profile": [
  {"function": "fib", "file": "fib.misa", "line": 2,
   "samples": 499, "pct": 69.4},
  {"function": "fib", "file": "fib.misa", "line": 3,
   "samples": 219, "pct": 30.5}
]}
```

The loop this enables: `misa profile` finds the hot line → the agent
reads the typed code → fixes → re-profiles. The profile above is real
output from `fib(25)` — 100% of samples inside `fib`, lines 2–3.

### v1 limitations (stated, not faked)

- **Interpreter only.** AOT-native profiling (frame-pointer unwinding
  of compiled binaries, `perf`-style external sampling) is future work.
  `misa profile` always runs the interpreter.
- **Main thread only.** `spawn`ed tasks run free and aren't attributed.
- **Sampling overhead.** The per-statement hook slows the interpreter
  while profiling; percentages are trustworthy, absolute times are not
  — use `misa build` + `time` for wall-clock numbers.
- The program is type-checked before profiling (`misa check` errors go
  to stderr, exit 1). A program that fails at runtime still prints the
  profile of what it sampled, then the E-code.

## Interactive REPL (`misa repl`)

A read-eval-print loop with persistent bindings, multi-line input, and
`:type` — which shows the **checker's inferred type** for any
expression, the fastest way to learn the type system.

```
$ misa repl
misa repl (v0.3) — :help for help, :quit to leave
misa> let prices = [10, 20, 30]
misa> map(prices, |p: Int| p * 2)
[20, 40, 60]
misa> :type prices
prices: [Int]
misa> fn total(xs: [Int]) -> Int {
....> var s = 0
....> for x in xs { s = s + x }
....> return s
....> }
misa> total(prices)
60
misa> let x: Int = "hi"
1:1: E200: cannot assign Text to 'x: Int'
misa> :quit
```

How it works: each chunk is appended to the session source and the
whole program is re-checked (the old prefix was clean, so any error is
the new chunk's — reported with chunk-relative lines, then discarded).
A lone bare expression is evaluated and its value printed (`nil`
prints nothing, so `print("hi")` doesn't echo `nil`). `spawn` in one
line and `await` in the next works — one task scope lives for the
whole session and is finished (bounded wait, then cancel) on
`:quit`/EOF, so task errors are never silent.

Commands: `:help`, `:quit`, `:reset` (forget all bindings),
`:type <expr>`. readline history is used when available.

### v1 limitations (stated, not faked)

- `import` parses but does nothing (the loader resolves imports from
  files; the REPL has no files).
- `screen` declarations check but never render — there is no UI driver
  in a REPL; `state()` outside a screen is E211 as usual.
- `let` redefinition is E210, exactly as in a file — use `var` +
  assignment, or `:reset`, to iterate on a binding.
- Model/stream calls need real provider credentials, like everywhere
  else.
