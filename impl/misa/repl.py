"""misa repl: an interactive read-eval-print loop for Misa.

Design:
  * One persistent Interpreter for the session; one pushed task scope
    (structured concurrency), so `spawn` in one line and `await` in the
    next works. Tasks are finished (bounded wait, then cancelled) on
    `:quit`/EOF — never silently dropped.
  * Each input chunk is appended to the accumulated source and the
    *whole* program is re-checked with a fresh Checker. The accumulated
    prefix was clean, so any error belongs to the new chunk: report it,
    discard the chunk, keep the session. (O(n^2) re-checking is fine
    for interactive use and is the most correct semantics.)
  * A chunk that is a single bare expression is evaluated and its value
    printed (nil results print nothing, so `print("hi")` doesn't echo
    "nil"). Everything else executes statement-by-statement.
  * Multi-line input: bracket balance is measured on lexer tokens
    (strings/comments can't fool it). Unbalanced -> continuation
    prompt. An unterminated string also continues.
  * `:type expr` shows the *checker's* inferred type via a scope
    captured from a synthetic `let __repl_type_probe__ = (expr)`.

v1 limitations (documented, not faked):
  * `import` is accepted by the parser but does nothing (the loader
    resolves imports from files; the REPL has no files).
  * `screen` declarations check but never render (no driver in a REPL);
    `state()` outside a screen is E211 as usual.
  * Model/stream calls need real provider credentials like everywhere
    else.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import ast
from . import interp as _interp
from .checker import Checker
from .errors import MisaError
from .lexer import Lexer, LexError
from .parser import parse_source, ParseError, Parser

PROBE_NAME = "__repl_type_probe__"

# Mirrors Interpreter.exec_setup: declarations are registered by load(),
# everything else executes.
_DECL_KINDS = (ast.AppDecl, ast.ProviderDecl, ast.ModelsDecl,
               ast.TypeDecl, ast.TableDecl, ast.ModelDecl,
               ast.ScreenDecl, ast.EvalDecl, ast.TestDecl, ast.FnDecl,
               ast.ExternDecl, ast.InterfaceDecl, ast.ImplDecl,
               ast.ImportDecl)

_OPEN = {"{", "(", "["}
_CLOSE = {"}": "{", ")": "(", "]": "["}


class _ScopeCaptureChecker(Checker):
    """Captures the top-level scope check() builds, so `:type` can read
    the checker's inferred type for a synthetic probe binding."""

    def __init__(self):
        super().__init__()
        self.captured_scope = None

    def _check_item(self, item, scope):
        # check() creates one scope dict and threads it through every
        # item, so capturing the reference yields the final scope.
        self.captured_scope = scope
        return super()._check_item(item, scope)


def _balanced(src: str) -> bool:
    """True when brackets balance (lexer tokens; strings/comments are
    opaque to the lexer). Raises LexError on bad input."""
    depth = 0
    for tok in Lexer(src, "<repl>").tokens():
        if tok.kind in _OPEN:
            depth += 1
        elif tok.kind in _CLOSE:
            depth -= 1
            if depth < 0:
                return True  # let the parser report the stray closer
    return depth == 0


def _looks_incomplete_lex_error(e: LexError) -> bool:
    msg = str(e).lower()
    return "unterminated" in msg or "eof" in msg or \
        "end of input" in msg or "end of file" in msg


def _fmt_error(code: str, message: str, line: int, col: int,
               hint: str = "") -> str:
    loc = f"{line}:{col}" if line else "<repl>"
    s = f"{loc}: {code}: {message}"
    if hint:
        s += f"\n  hint: {hint}"
    return s


class ReplSession:
    def __init__(self):
        self.interp = _interp.Interpreter(
            main_dir=os.getcwd(), argv=[])
        # One task scope for the whole session: spawns survive across
        # lines (structured concurrency still applies at :quit/EOF).
        self.interp._push_scope()  # noqa: SLF001
        self.src = ""          # accumulated checked source
        self.n_lines = 0       # lines in self.src
        self.n_items = 0       # top-level items in the last good parse
        self._buf = ""         # current multi-line input being built

    # -- input ----------------------------------------------------------

    def feed_line(self, line: str) -> Optional[str]:
        """Feed one input line. Returns an output string to print when
        the chunk is complete (or an error), None when more input is
        needed. The string "quit" (no newline) signals :quit."""
        if self._buf == "" and line.startswith(":"):
            return self._command(line)
        self._buf += line + "\n"
        try:
            if not _balanced(self._buf):
                return None  # continuation prompt
        except LexError as e:
            if _looks_incomplete_lex_error(e):
                return None
            out = _fmt_error("E100", str(e), e.line, e.col)
            self._buf = ""
            return out
        chunk, self._buf = self._buf, ""
        return self._run_chunk(chunk)

    # -- commands -------------------------------------------------------

    def _command(self, line: str) -> str:
        parts = line.split(None, 1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd == ":quit":
            return "quit"
        if cmd == ":help":
            return (
                "misa repl commands:\n"
                "  :help         this help\n"
                "  :quit         leave the repl (finishes spawned tasks)\n"
                "  :reset        forget all bindings, start fresh\n"
                "  :type <expr>  show the checker's inferred type\n"
                "\n"
                "Anything else is Misa: `let x = 5`, `x + 1`,\n"
                "`fn f() -> Int { return 1 }`, multi-line blocks..."
            )
        if cmd == ":reset":
            # Pop the old session's task scope (cancels its tasks) so
            # the thread-local scope stack stays balanced; daemon
            # threads then die with the process.
            try:
                self.interp._pop_scope()  # noqa: SLF001
            except Exception:
                pass
            self.src = ""
            self.n_lines = 0
            self.n_items = 0
            self.interp = _interp.Interpreter(
                main_dir=os.getcwd(), argv=[])
            self.interp._push_scope()  # noqa: SLF001
            return "reset"
        if cmd == ":type":
            if not arg.strip():
                return _fmt_error("E100", "usage: :type <expr>", 0, 0)
            return self._type_of(arg.strip())
        return _fmt_error(
            "E100", f"unknown command {cmd!r}",
            0, 0, hint="try :help")

    def _type_of(self, expr_text: str) -> str:
        if PROBE_NAME in self.src or PROBE_NAME in expr_text:
            return _fmt_error("E100",
                              "probe name collision (rename your binding)",
                              0, 0)
        probe_src = f"{self.src}\nlet {PROBE_NAME} = ({expr_text})\n"
        try:
            prog = parse_source(probe_src, "<repl>")
        except (ParseError, LexError) as e:
            return _fmt_error("E100", str(e),
                              max(0, e.line - self.n_lines), e.col)
        checker = _ScopeCaptureChecker()
        errors = checker.check(prog)
        if errors:
            # The accumulated prefix was clean: any error is the probe's.
            e = errors[0]
            return _fmt_error(e["code"], e["message"],
                              max(0, e.get("line", 0) - self.n_lines),
                              e.get("col", 0), e.get("hint", ""))
        scope = checker.captured_scope or {}
        entry = scope.get(PROBE_NAME)
        if entry is None:
            return _fmt_error("E100",
                              "could not infer a type", 0, 0)
        return f"{expr_text}: {entry[0]}"

    # -- chunk execution --------------------------------------------------

    def _run_chunk(self, chunk: str) -> str:
        # Single parse of the accumulated program; the new chunk's items
        # are the tail slice. (Parsing the chunk alone could disagree
        # with the accumulated parse at the boundary; one parse keeps
        # line numbers and desugaring consistent.)
        base_lines = self.n_lines
        old_n = self.n_items
        candidate = self.src + chunk
        try:
            prog = parse_source(candidate, "<repl>")
        except (ParseError, LexError) as e:
            return _fmt_error("E100", str(e),
                              max(0, e.line - base_lines), e.col)
        errors = Checker().check(prog)
        if errors:
            # The committed prefix was clean: every error belongs to
            # this chunk.
            return "\n".join(
                _fmt_error(e["code"], e["message"],
                           max(0, e.get("line", 0) - base_lines),
                           e.get("col", 0), e.get("hint", ""))
                for e in errors)
        # Clean: commit the chunk, then execute only the new items.
        new_items = prog.items[old_n:]
        self.src = candidate
        self.n_lines = base_lines + chunk.count("\n")
        self.n_items = len(prog.items)
        try:
            return self._exec_items(new_items, base_lines)
        except MisaError as e:
            d = e.to_dict()
            line = d.get("line", 0)
            return _fmt_error(d["code"], d["message"],
                              max(0, line - base_lines) if line else 0,
                              d.get("col", 0), d.get("hint", ""))
        except RecursionError:
            return _fmt_error("E300",
                              "call stack overflow (recursion too deep)",
                              0, 0)

    def _exec_items(self, items: list, base_lines: int) -> str:
        # A lone bare expression: evaluate and print its value.
        if len(items) == 1 and isinstance(items[0], ast.ExprStmt):
            val = self.interp.eval_expr(items[0].expr,
                                        self.interp.global_env)
            if val is None:
                return ""
            return _interp.render_value(val)
        # Declarations were already validated; register just the new
        # ones, then run the new statements. (Mirrors exec_setup minus
        # its per-program task scope — the session owns one scope.)
        self.interp.load(ast.Program(items=items))
        for item in items:
            if isinstance(item, _DECL_KINDS):
                continue
            try:
                self.interp.exec_stmt(item, self.interp.global_env)
            except _interp._ReturnSignal:  # noqa: SLF001
                break  # `return` at top level: stop the chunk
        return ""

    # -- teardown ---------------------------------------------------------

    def finish(self) -> None:
        """End of session: bounded wait for spawned tasks, then cancel
        stragglers (the interpreter's never-silent rule reports task
        errors to stderr)."""
        try:
            self.interp._finish_tasks()  # noqa: SLF001
        except Exception:
            pass


def repl_loop() -> int:
    """Entry point for `misa repl`. Returns process exit code."""
    try:
        import readline  # noqa: F401  (history; silently absent is fine)
    except ImportError:
        pass
    session = ReplSession()
    sys.stdout.write("misa repl (v0.3) — :help for help, :quit to leave\n")
    sys.stdout.flush()
    try:
        while True:
            prompt = "misa> " if session._buf == "" else "....> "
            try:
                line = input(prompt)
            except EOFError:
                sys.stdout.write("\n")
                break
            out = session.feed_line(line)
            if out is None:
                continue
            if out == "quit":
                break
            if out:
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        session.finish()
    return 0
