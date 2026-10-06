"""Misa Language Server (Phase 2 tooling).

JSON-RPC 2.0 over stdio with Content-Length framing, per the LSP spec.
Wraps the existing checker (`misa check --json` semantics) and formatter
(`misa fmt`), so editor diagnostics are byte-identical to the CLI.

Usage:  misa lsp
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
import urllib.parse
import urllib.request

# Allow running as `impl/bin/misa lsp` (impl/ on sys.path already) and
# as a standalone module.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))


def log(*args):
    sys.stderr.write("misa-lsp: " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Hover / completion data (derived from spec/SYNTAX.md and spec/STDLIB.md)
# ---------------------------------------------------------------------------

KEYWORD_DOCS = {
    "app": "Top-level app block: `app Name { title: \"...\", ... }`. At most one per program.",
    "provider": "Model provider config: `provider openai { key: env(\"MISA_MODEL_KEY\") }`.",
    "models": "Model alias table: `models { diag: openai(\"DeepSeek-V4-Pro\") }`.",
    "model": "Typed model call: `model name(params) -> RetType with alias.name budget { ... } prompt v1 \"\"\"...\"\"\"`.",
    "type": "Record type declaration: `type Name { field: Text, ... }`.",
    "screen": "UI screen: `screen Name(params) { Column { ... } }`.",
    "fn": "Typed function: `fn name(p: Text) -> Int { ... }`.",
    "import": "Import another file: `import \"lib.misa\"` (relative path).",
    "let": "Immutable binding: `let x = expr` (optionally `let x: Text = ...`).",
    "var": "Mutable binding: `var x = expr`.",
    "if": "Conditional: `if cond { ... } else { ... }`.",
    "else": "Else branch of `if`.",
    "for": "Loop: `for item in list { ... }`.",
    "in": "Membership/iteration: `for x in xs`.",
    "while": "Loop: `while cond { ... }`.",
    "return": "Return from `fn`: `return expr`.",
    "try": "Error handling: `try { ... } catch e { ... }`.",
    "catch": "Catch clause binding the error: `catch e { ... }`.",
    "throw": "Raise an error value: `throw expr` (must be an `Error`).",
    "budget": "Model call budgets: `budget { tokens: 4000, latency: 30s, cost: $0.05 }`.",
    "prompt": "Prompt template with version: `prompt v1 \"\"\"...{param}...\"\"\"`.",
    "with": "Selects provider + model id: `with openai.diag`.",
    "eval": "AI eval block: `eval name { model: m, given: {...}, expect: ..., runs: 3 }`.",
    "test": "Unit test block: `test name { ... }` — run with `misa test`.",
    "given": "Eval inputs: `given: { param: value }`.",
    "expect": "Eval assertion on `result`: `expect: result.confidence > 0.5`.",
    "runs": "Eval repetition count: `runs: 3`.",
    "stream": "Streaming model call: `stream s = diag(x)` (AOT: E402, unsupported).",
    "await": "Await a stream/event (AOT: E402, unsupported).",
    "on": "Event handler: `on s.token { |t| ... }`.",
    "navigate": "Navigate to a screen: `navigate(Home())`.",
    "env": "Read environment variable: `env(\"MISA_MODEL_KEY\")`.",
    "and": "Logical and.", "or": "Logical or.", "not": "Logical negation.",
    "true": "Boolean true.", "false": "Boolean false.", "nil": "Null value.",
}

BUILTIN_DOCS = {
    "print": "`print(x: Text)` — write a line to stdout.",
    "len": "`len(x: [T]) -> Int` — element count; also works on `Text` and `Map`.",
    "str": "`str(x: any) -> Text` — canonical text rendering of any value.",
    "int": "`int(x: Text) -> Int` — parse text; `E300` on failure.",
    "float": "`float(x: Text) -> Float` — parse text; `E300` on failure.",
    "assert": "`assert(cond: Bool, msg: Text)` — abort with `E301` if false.",
    "assert_eq": "`assert_eq(a: any, b: any)` — abort with `E301` (rendering both values) if `a == b` is false.",
    "assert_ne": "`assert_ne(a: any, b: any)` — abort with `E301` if `a == b` is true.",
    "argv": "`argv() -> [Text]` — command-line args minus program name; `[]` on Android/iOS/web.",
    "env": "`env(name: Text) -> Text` — environment variable; `E302` if unset.",
    "fs.read": "`fs.read(path: Text) -> Text` — read a file; throws `Error{code: \"E303\"}`.",
    "fs.write": "`fs.write(path: Text, text: Text)` — write a file; throws `E303`.",
    "http.get": "`http.get(url: Text) -> Text` — HTTP GET; throws `E304` on failure.",
    "Photo": "`Photo(\"img.jpg\")` — media handle for model prompts / `Image`.",
    "Audio": "`Audio(\"a.mp3\")` — media handle.",
    "Video": "`Video(\"v.mp4\")` — media handle.",
    "Document": "`Document(\"d.pdf\")` — media handle.",
    "Map": "`Map<K, V>` — map type with `Text`/`Int` keys.",
    "Text": "Builtin text type.", "Int": "Builtin integer type.",
    "Float": "Builtin float type.", "Bool": "Builtin boolean type.",
    "Error": "Error value type (thrown/caught by `try`/`catch`/`throw`).",
}

COMPONENT_DOCS = {
    "Column": "`Column { ... }` — vertical layout container.",
    "Row": "`Row { ... }` — horizontal layout container.",
    "Spacer": "`Spacer` — flexible empty space.",
    "Text": "`Text(text: \"...\")` — text label.",
    "Heading": "`Heading(text: \"...\")` — large title text.",
    "Banner": "`Banner(text: \"...\", style: \"info\")` — notice banner; style is `info`/`warning`/`error`.",
    "Button": "`Button(label: \"...\", onTap: || { ... })` — tappable button.",
    "TextInput": "`TextInput(id: name, hint: \"...\")` — text field; `name.text -> Text`.",
    "CameraButton": "`CameraButton(onCapture: |photo| { ... })` — photo capture.",
    "Image": "`Image(photo: p)` — display a `Photo` handle.",
    "List": "`List(items: [...])` — list of text items.",
}

HOVER_DOCS = {}
HOVER_DOCS.update(KEYWORD_DOCS)
HOVER_DOCS.update(BUILTIN_DOCS)
HOVER_DOCS.update(COMPONENT_DOCS)

# LSP CompletionItemKind
K_KEYWORD, K_FUNCTION, K_CLASS, K_SNIPPET = 14, 3, 7, 15

SNIPPETS = [
    ("provider openai block", "provider",
     "provider ${1:openai} {\n    key: env(\"${2:MISA_MODEL_KEY}\"),\n}"),
    ("models block", "models",
     "models {\n    ${1:diag}: ${2:openai}(\"${3:DeepSeek-V4-Pro}\"),\n}"),
    ("model declaration", "model",
     "model ${1:name}(${2:notes: Text}) -> ${3:Diagnosis}\n    with ${4:openai}.${5:diag}\n    budget { tokens: ${6:4000}, latency: ${7:30}s, cost: $$${8:0.05} }\n    prompt v1 \"\"\"${9:Describe... {notes}}\"\"\""),
    ("screen scaffold", "screen",
     "screen ${1:Home}() {\n    Column {\n        Heading(text: \"${2:Title}\")\n        $0\n    }\n}"),
    ("eval block", "eval",
     "eval ${1:name} {\n    model: ${2:diag},\n    given: { ${3:notes}: \"${4:...}\" },\n    expect: ${5:result.confidence > 0.5},\n    runs: ${6:3},\n}"),
    ("try/catch", "try",
     "try {\n    $0\n} catch e {\n}"),
]


def completion_items():
    items = []
    for kw in sorted(KEYWORD_DOCS):
        items.append({"label": kw, "kind": K_KEYWORD,
                      "detail": "keyword",
                      "documentation": KEYWORD_DOCS[kw]})
    for name in sorted(BUILTIN_DOCS):
        kind = K_CLASS if name[0].isupper() and "(" not in BUILTIN_DOCS[name][:2] \
            else K_FUNCTION
        items.append({"label": name, "kind": kind,
                      "detail": BUILTIN_DOCS[name].split(" — ")[0],
                      "documentation": BUILTIN_DOCS[name]})
    for name in sorted(COMPONENT_DOCS):
        items.append({"label": name, "kind": K_CLASS,
                      "detail": "screen component",
                      "documentation": COMPONENT_DOCS[name]})
    for label, prefix, body in SNIPPETS:
        items.append({"label": label, "kind": K_SNIPPET,
                      "detail": "snippet", "filterText": prefix,
                      "insertText": body, "insertTextFormat": 2,
                      "documentation": "Misa scaffold: " + label})
    return items


# ---------------------------------------------------------------------------
# JSON-RPC framing
# ---------------------------------------------------------------------------

class Connection:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout

    def _readline(self):
        """Read one \\n-terminated line; None on EOF with no data."""
        line = b""
        while not line.endswith(b"\n"):
            ch = self.stdin.read(1)
            if not ch:
                return None if not line else line
            line += ch
        return line

    def read_message(self):
        """Read one Content-Length-framed message; return dict or None on EOF."""
        length = None
        while True:
            line = self._readline()
            if line is None:
                return None
            text = line.decode("ascii", errors="replace").strip()
            if not text:
                break
            if text.lower().startswith("content-length:"):
                try:
                    length = int(text.split(":", 1)[1].strip())
                except ValueError:
                    length = None
        if length is None:
            log("dropping message without Content-Length")
            return {}
        body = b""
        while len(body) < length:
            chunk = self.stdin.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as ex:
            log("dropping malformed JSON message:", ex)
            return {}

    def write_message(self, msg):
        raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        self.stdout.write(b"Content-Length: %d\r\n\r\n" % len(raw))
        self.stdout.write(raw)
        self.stdout.flush()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def uri_to_path(uri):
    if uri.startswith("file://"):
        return urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    return None


class Server:
    def __init__(self, conn):
        self.conn = conn
        self.docs = {}          # uri -> text
        self.shutdown_requested = False

    # -- diagnostics ------------------------------------------------------
    def check_text(self, uri, text):
        """Run the checker on buffer text; return list of LSP diagnostics."""
        from misa.checker import check_source, check_file
        path = uri_to_path(uri)
        errors = None
        tmp = None
        try:
            if path and os.path.isdir(os.path.dirname(path) or "."):
                # Check via a temp sibling file so relative imports
                # resolve exactly as `misa check` would.
                fd, tmp = tempfile.mkstemp(suffix=".misa",
                                           dir=os.path.dirname(path) or ".",
                                           text=True)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                errors = check_file(tmp)
            else:
                errors = check_source(text, path or "<input>")
        except Exception as ex:
            log("checker crashed:", ex)
            errors = [{"code": "E001", "message": f"checker error: {ex}",
                       "line": 1, "col": 1, "hint": ""}]
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        diags = []
        for e in errors or []:
            line = max(int(e.get("line") or 1) - 1, 0)
            col = max(int(e.get("col") or 1) - 1, 0)
            msg = str(e.get("message", ""))
            hint = e.get("hint")
            if hint:
                msg += f"\nHint: {hint}"
            diags.append({
                "range": {"start": {"line": line, "character": col},
                          "end": {"line": line, "character": col + 1}},
                "severity": 1,  # Error
                "code": str(e.get("code", "")),
                "source": "misa",
                "message": msg,
            })
        return diags

    def publish(self, uri):
        diags = self.check_text(uri, self.docs[uri])
        self.conn.write_message({
            "jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diags},
        })

    # -- formatting -------------------------------------------------------
    def format_text(self, uri):
        from misa.fmt import format_source
        from misa.parser import ParseError
        from misa.lexer import LexError
        text = self.docs.get(uri, "")
        try:
            return format_source(text, uri_to_path(uri) or "<input>")
        except (ParseError, LexError, Exception) as ex:
            log("format skipped:", str(ex)[:120])
            return None

    # -- hover / completion -----------------------------------------------
    @staticmethod
    def word_at(text, line, character):
        lines = text.split("\n")
        if line < 0 or line >= len(lines):
            return ""
        ln = lines[line]
        if character < 0 or character > len(ln):
            return ""
        start = character
        while start > 0 and (ln[start - 1].isalnum() or ln[start - 1] in "_."):
            start -= 1
        end = character
        while end < len(ln) and (ln[end].isalnum() or ln[end] in "_."):
            end += 1
        return ln[start:end]

    # -- dispatch ----------------------------------------------------------
    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return "continue"
        method = msg.get("method", "")
        mid = msg.get("id")
        params = msg.get("params") or {}

        def respond(result=None, error=None):
            if mid is None:
                return
            out = {"jsonrpc": "2.0", "id": mid}
            if error is not None:
                out["error"] = error
            else:
                out["result"] = result
            self.conn.write_message(out)

        try:
            if method == "initialize":
                respond({"capabilities": {
                    "textDocumentSync": 1,  # full
                    "hoverProvider": True,
                    "completionProvider": {"triggerCharacters": ["."]},
                    "documentFormattingProvider": True,
                }, "serverInfo": {"name": "misa-lsp", "version": "0.3"}})
            elif method == "initialized":
                pass  # notification
            elif method == "shutdown":
                self.shutdown_requested = True
                respond(None)
            elif method == "exit":
                return "exit"
            elif method == "textDocument/didOpen":
                doc = params.get("textDocument", {})
                uri = doc.get("uri", "")
                self.docs[uri] = doc.get("text", "")
                self.publish(uri)
            elif method == "textDocument/didChange":
                uri = params.get("textDocument", {}).get("uri", "")
                changes = params.get("contentChanges", [])
                if uri in self.docs and changes:
                    # full sync: last change holds the whole document
                    self.docs[uri] = changes[-1].get("text", "")
                    self.publish(uri)
            elif method == "textDocument/didClose":
                uri = params.get("textDocument", {}).get("uri", "")
                self.docs.pop(uri, None)
                self.conn.write_message({
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": []},
                })
            elif method == "textDocument/formatting":
                uri = params.get("textDocument", {}).get("uri", "")
                formatted = self.format_text(uri)
                if formatted is None or uri not in self.docs:
                    respond(None)
                else:
                    old = self.docs[uri]
                    if formatted == old:
                        respond([])
                    else:
                        lines = old.split("\n")
                        last = len(lines) - 1
                        last_char = len(lines[-1]) if lines else 0
                        respond([{
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": last, "character": last_char},
                            },
                            "newText": formatted,
                        }])
            elif method == "textDocument/hover":
                uri = params.get("textDocument", {}).get("uri", "")
                pos = params.get("position", {})
                word = self.word_at(self.docs.get(uri, ""),
                                    pos.get("line", 0), pos.get("character", 0))
                doc = HOVER_DOCS.get(word)
                if doc:
                    respond({"contents": {"kind": "markdown",
                                          "value": f"**`{word}`** — {doc}"}})
                else:
                    respond(None)
            elif method == "textDocument/completion":
                respond({"isIncomplete": False,
                         "items": completion_items()})
            elif mid is not None:
                respond(None, {"code": -32601, "message": "method not found"})
        except Exception:
            log("handler crashed on", method, ":\n" + traceback.format_exc())
            if mid is not None:
                respond(None, {"code": -32603,
                               "message": "internal error"})
        return "continue"

    def serve(self):
        log("listening on stdio")
        while True:
            msg = self.conn.read_message()
            if msg is None:
                break
            if self.handle(msg) == "exit":
                break
        return 0 if self.shutdown_requested else 0


def main():
    conn = Connection(sys.stdin.buffer, sys.stdout.buffer)
    return Server(conn).serve()


if __name__ == "__main__":
    sys.exit(main())
