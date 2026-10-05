# Misa Tooling (Phase 2)

The Misa toolchain is designed to be driven by humans *and* AI agents with
equal fluency: every diagnostic is structured JSON, formatting is canonical
and byte-stable, and the language server speaks standard LSP.

## CLI surface

| Command | Purpose |
|---|---|
| `misa check <f.misa> [--json]` | Type/name check. `--json` emits `[{code, message, line, col, hint}]` — the machine-readable contract. |
| `misa fmt <f.misa> [--write]` | Canonical formatter (4-space, K&R, one blank line between items). |
| `misa run <f.misa> [--mock m.json]` | Interpret with hot-reload semantics. |
| `misa test <f.misa> [--mock m.json]` | Run `eval` blocks (AI evals as tests). |
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

**4. Evals as tests:** `misa test app.misa --mock mocks.json` runs the
`eval` blocks — the agent's way of proving an AI feature works, in CI.

**Agent loop (canonical):** write → `misa check --json` → fix each
`{code,line,col}` → `misa fmt --write` → `misa test` → done. The 12/12
external cold trials were run almost exactly this way.
