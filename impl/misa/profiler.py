"""misa profile: a sampling CPU profiler for the Misa interpreter.

Don't fake it: this is a *sampling* profiler on the interpreter (timer
thread reads the currently-executing statement every N ms and
aggregates). AOT-native profiling (frame-pointer unwinding of compiled
binaries, or `perf`-style external sampling) is future work — stated,
not faked.

Usage:
  misa profile prog.misa [--json] [--mock mocks.json]
             [--interval-ms N] [-- args...]

Output (human): a flat profile sorted by samples, descending:
  samples, % of total, function, file:line.
Output (--json): {"samples": N, "interval_ms": M, "profile":
  [{function, file, line, samples, pct}]} — the agent interface.

The hooks use the same monkeypatch pattern as `misa/debugger.py`
(interp.py itself is untouched), so sibling tooling can't collide:
profiler and debugger never run in the same process.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import ast
from . import interp as _interp
from .errors import MisaError

Key = Tuple[str, str, int]  # (function, file, line)


# --------------------------------------------------------------------------
# interpreter hooks (monkeypatched; interp.py itself is untouched)
# --------------------------------------------------------------------------

_installed = False
_orig_exec_stmt = _interp.Interpreter.exec_stmt
_orig_call_fn = _interp.Interpreter.call_fn
_orig_invoke_lambda = _interp.Interpreter.invoke_lambda_value


def _patched_exec_stmt(self, s, env):
    prof = getattr(self, "_prof_session", None)
    if prof is not None:
        prof.note_stmt(s)
    return _orig_exec_stmt(self, s, env)


def _patched_call_fn(self, decl, args, node):
    prof = getattr(self, "_prof_session", None)
    if prof is None:
        return _orig_call_fn(self, decl, args, node)
    prof.push_frame(getattr(decl, "name", "<fn>"))
    try:
        return _orig_call_fn(self, decl, args, node)
    finally:
        prof.pop_frame()


def _patched_invoke_lambda(self, lam, args, node):
    prof = getattr(self, "_prof_session", None)
    if prof is None:
        return _orig_invoke_lambda(self, lam, args, node)
    prof.push_frame("<lambda>")
    try:
        return _orig_invoke_lambda(self, lam, args, node)
    finally:
        prof.pop_frame()


def install_hooks() -> None:
    global _installed
    if _installed:
        return
    _interp.Interpreter.exec_stmt = _patched_exec_stmt
    _interp.Interpreter.call_fn = _patched_call_fn
    _interp.Interpreter.invoke_lambda_value = _patched_invoke_lambda
    _installed = True


def _walk(node: Any):
    """Yield node and every descendant AST node."""
    seen = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Node):
            if id(n) in seen:
                continue
            seen.add(id(n))
            yield n
            for v in vars(n).values():
                stack.append(v)
        elif isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, dict):
            stack.extend(n.values())


class ProfileSession:
    """Runs a Misa program under the sampling profiler.

    The program executes on the calling (main) thread. `note_stmt` is
    called before every statement and records the current location;
    a sampler thread wakes every `interval_ms` and tallies it.
    Only the main thread is sampled (spawned tasks run free) — a v1
    limitation, documented, not faked.
    """

    def __init__(self, interp: "_interp.Interpreter",
                 prog: ast.Program,
                 file_of: Dict[int, str],
                 main_path: str,
                 interval_ms: float = 10.0):
        install_hooks()
        self.interp = interp
        self.interval_ms = interval_ms
        self.main_path = os.path.abspath(main_path)
        self._main_thread = threading.get_ident()
        # statement id -> absolute file (loader only maps top-level items)
        self._stmt_file: Dict[int, str] = {}
        for item in prog.items:
            f = file_of.get(id(item), self.main_path)
            for n in _walk(item):
                self._stmt_file.setdefault(id(n), f)
        self._frames: List[str] = ["<main>"]
        self._current: Key = ("<main>", self.main_path, 1)
        self.counts: Dict[Key, int] = {}
        self.total = 0
        self._stop = threading.Event()
        self._sampler: Optional[threading.Thread] = None
        self.program_failed = False
        interp._prof_session = self  # noqa: SLF001 (hook slot)

    # -- hooking --------------------------------------------------------

    def _file_of(self, node: ast.Node) -> str:
        return self._stmt_file.get(id(node), self.main_path)

    def push_frame(self, name: str) -> None:
        if threading.get_ident() != self._main_thread:
            return
        self._frames.append(name)

    def pop_frame(self) -> None:
        if threading.get_ident() != self._main_thread:
            return
        if len(self._frames) > 1:
            self._frames.pop()

    def note_stmt(self, s: ast.Node) -> None:
        if threading.get_ident() != self._main_thread:
            return
        try:
            func = self._frames[-1]
        except IndexError:
            func = "<main>"
        self._current = (func, self._file_of(s),
                         getattr(s, "line", 0))

    # -- sampling -------------------------------------------------------

    def _sample(self) -> None:
        key = self._current  # atomic read under the GIL
        self.counts[key] = self.counts.get(key, 0) + 1
        self.total += 1

    def _sampler_loop(self) -> None:
        interval = self.interval_ms / 1000.0
        while not self._stop.wait(interval):
            self._sample()

    def start(self) -> None:
        self._sampler = threading.Thread(target=self._sampler_loop,
                                         daemon=True,
                                         name="misa-profiler")
        self._sampler.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2.0)
        # One last sample so tiny programs aren't always empty.
        if self.total == 0:
            self._sample()

    # -- reporting ------------------------------------------------------

    def rows(self) -> List[Dict[str, Any]]:
        total = self.total or 1
        out = [{"function": func, "file": f, "line": ln,
                "samples": n, "pct": round(100.0 * n / total, 1)}
               for (func, f, ln), n in self.counts.items()]
        out.sort(key=lambda r: (-r["samples"], r["function"],
                                r["file"], r["line"]))
        return out

    def report_human(self) -> str:
        lines = [f"profile: {self.total} samples "
                 f"({self.interval_ms:g} ms interval)"]
        if self.total == 0:
            lines.append("(no samples: the program finished before the "
                         "first tick)")
            return "\n".join(lines)
        lines.append(f"{'samples':>7}  {'%':>5}  "
                     f"{'function':<24}  location")
        for r in self.rows():
            loc = f"{r['file']}:{r['line']}"
            lines.append(f"{r['samples']:>7}  {r['pct']:>5.1f}  "
                         f"{r['function']:<24}  {loc}")
        return "\n".join(lines)

    def report_json(self) -> str:
        return json.dumps({"samples": self.total,
                           "interval_ms": self.interval_ms,
                           "profile": self.rows()}, indent=2)

    # -- run ------------------------------------------------------------

    def run_program(self, prog: ast.Program) -> None:
        self.start()
        try:
            self.interp.load(prog)
            self.interp.exec_setup(prog)
        except MisaError:
            raise
        except RecursionError:
            from .errors import RuntimeError_ as _RE
            raise _RE("call stack overflow (recursion too deep)")
        finally:
            self.stop()
            # Detach so a reused interpreter doesn't keep profiling.
            self.interp._prof_session = None  # noqa: SLF001


def profile_file(path: str, mock_path: Optional[str],
                 as_json: bool, interval_ms: float,
                 prog_argv: Optional[List[str]] = None) -> int:
    """Load, check, and profile a program. Returns process exit code."""
    from .checker import Checker
    from .loader import load as load_with_imports
    mock_scripts: dict = {}
    if mock_path:
        with open(mock_path) as f:
            mock_scripts = json.load(f)
    merged, load_errors, _order, file_of = load_with_imports(path)
    if merged is None:
        for e in load_errors:
            sys.stderr.write(str(e) + "\n")
        return 1
    errors = load_errors + Checker().check(merged, file_of=file_of)
    if errors:
        for e in errors:
            sys.stderr.write(
                f"{e.get('line', 0)}:{e.get('col', 0)}: "
                f"{e.get('code')}: {e.get('message')}\n")
        return 1
    interp = _interp.Interpreter(
        mock_scripts=mock_scripts,
        main_dir=os.path.dirname(os.path.abspath(path)),
        argv=prog_argv or [])
    interp.file_of = file_of
    session = ProfileSession(interp, merged, file_of, path,
                             interval_ms=interval_ms)
    failed = False
    try:
        session.run_program(merged)
    except MisaError as e:
        d = e.to_dict()
        failed = True
    if as_json:
        sys.stdout.write(session.report_json() + "\n")
    else:
        sys.stdout.write(session.report_human() + "\n")
    if failed:
        sys.stderr.write(
            f"{d.get('line', 0)}:{d.get('col', 0)}: "
            f"{d.get('code')}: {d.get('message')}\n")
        return 1
    return 0
