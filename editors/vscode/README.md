# Misa for VS Code

Language support for [Misa](../../README.md) — syntax highlighting,
real-time diagnostics, hover docs, completions, and formatting, all
powered by the Misa language server (`misa lsp`).

## Install

**Prerequisites:** the `misa` toolchain on your `PATH`
(`impl/bin/misa` in this repo — it must support `misa lsp`).

**Option A — from source (recommended while private):**

```sh
# install the language-client dependency once
cd editors/vscode && npm install --no-save vscode-languageclient
# link the extension into VS Code
ln -s "$PWD" ~/.vscode/extensions/misa-0.3.0
# then: Developer: Reload Window  (or restart VS Code)
```

**Option B — package a .vsix** (needs `vsce`):

```sh
cd editors/vscode && npm install --no-save vscode-languageclient
npx @vscode/vsce package   # produces misa-0.3.0.vsix
code --install-extension misa-0.3.0.vsix
```

## Usage

- Open any `.misa` file: diagnostics appear as you type (same codes as
  `misa check --json`), hover shows docs for keywords/builtins, and
  completions offer keywords, stdlib names, and scaffolds (provider /
  model / screen / eval blocks).
- **Format:** `Misa: Format document` (or your format keybinding) — runs
  the canonical `misa fmt`, byte-identical to the CLI.
- **Commands** (Command Palette → "Misa: …"):
  - *Check current file* — `misa check` in a terminal
  - *Run current file* — `misa run`
  - *Build current file* — `misa build` (native binary)
  - *Restart language server*

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `misa.executablePath` | `"misa"` | Path to the `misa` executable |
| `misa.trace.server` | `"off"` | LSP message tracing (`messages`/`verbose`) |

## Files

- `package.json` — extension manifest (language id `misa`)
- `syntaxes/misa.tmLanguage.json` — TextMate grammar (keywords, types,
  strings incl. `"""` tristings + `{interp}`, numbers incl. `30s`/`$0.05`,
  comments, components, builtins)
- `language-configuration.json` — brackets, comments, auto-closing pairs
- `client/extension.js` — spawns `misa lsp` via vscode-languageclient
