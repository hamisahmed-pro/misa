"""misa debug --agent: the first debugger designed for AI agents.

Don't build a better pdb — build what no language has: a debugger whose
primary interface is a JSON-line protocol where an *agent* sets
breakpoints, steps, and dumps *typed* variable state as structured data
instead of parsing text. Misa already has everything this needs: typed
values, source positions on every AST node, structured E-codes.

Protocol (JSON lines on stdin/stdout; stderr stays human-readable):

  -> {"cmd":"break","file":"...","line":N}   <- {"ok":true,"breakpoint":id}
  -> {"cmd":"clear","id":N}                   <- {"ok":true}
  -> {"cmd":"breakpoints"}                    <- {"ok":true,"breakpoints":[...]}
  -> {"cmd":"run"} | {"cmd":"continue"}       resumes; then events arrive
  -> {"cmd":"step"}                           line step (into calls)
  -> {"cmd":"next"}                           step over
  -> {"cmd":"out"}                            step out of current function
  -> {"cmd":"locals","frame":0}               <- {"ok":true,"locals":[...]}
  -> {"cmd":"eval","expr":"x + 1","frame":0}  <- {"ok":true,"type":"Int","value":"43"}
  -> {"cmd":"stack"}                          <- {"ok":true,"frames":[...]}
  -> {"cmd":"quit"}                           aborts the run

  <- {"event":"started","program":"..."}
  <- {"event":"break","breakpoint":id|null,"file":...,"line":...,"function":...}
  <- {"event":"exit","code":0}
  <- {"event":"error","code":"E...","message":...,"line":...,"col":...,"hint":...}

v1 is interpreter-only (AOT debugging is future work — stated, not faked).
Only the main thread pauses; spawned tasks run free (documented).
Programs that read stdin themselves conflict with the command channel
(documented).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional

from . import ast
from . import interp as _interp
from .errors import MisaError
from .lexer import Lexer
from .parser import Parser, ParseError

MAX_DEPTH = 5
MAX_ITEMS = 50


# --------------------------------------------------------------------------
# typed value rendering: structured data, never text to parse
# --------------------------------------------------------------------------

def dbg_value(v: Any, depth: int = 0) -> Dict[str, Any]:
    """Render a Misa runtime value as {"type","value",...} JSON."""
    if depth > MAX_DEPTH:
        return {"type": "?", "value": "<max depth>"}
    I = _interp  # class references without import cycles
    if v is None:
        return {"type": "Nil", "value": None}
    if isinstance(v, bool):
        return {"type": "Bool", "value": v}
    if isinstance(v, int):
        return {"type": "Int", "value": v}
    if isinstance(v, float):
        return {"type": "Float", "value": v}
    if isinstance(v, str):
        return {"type": "Text", "value": v}
    if isinstance(v, I.StateCell):
        d = dbg_value(v.value, depth)
        d["state_cell"] = True
        return d
    if isinstance(v, list):
        items = [dbg_value(x, depth + 1) for x in v[:MAX_ITEMS]]
        d: Dict[str, Any] = {"type": "List", "value": items,
                             "length": len(v)}
        if len(v) > MAX_ITEMS:
            d["truncated"] = True
        return d
    if isinstance(v, I.MapValue):
        pairs = list(v.data.items())[:MAX_ITEMS]
        d = {"type": "Map",
             "value": {str(k): dbg_value(x, depth + 1)
                       for k, x in pairs},
             "length": len(v.data)}
        if len(v.data) > MAX_ITEMS:
            d["truncated"] = True
        return d
    if isinstance(v, dict):
        pairs = list(v.items())[:MAX_ITEMS]
        return {"type": "Record",
                "value": {str(k): dbg_value(x, depth + 1)
                          for k, x in pairs}}
    if isinstance(v, I.SumValue):
        return {"type": v.type_name, "variant": v.variant,
                "value": [dbg_value(x, depth + 1) for x in v.fields]}
    if isinstance(v, I.FnValue):
        return {"type": "Function", "value": v.decl.name}
    if isinstance(v, I.LambdaValue):
        return {"type": "Function", "value": "<lambda>"}
    if isinstance(v, I.TaskHandle):
        return {"type": "Task", "value": _interp.render_value(v)}
    if isinstance(v, I.Channel):
        return {"type": "Channel", "value": "<chan>"}
    if isinstance(v, I.PtrValue):
        return {"type": "Ptr", "value": hex(v.addr)}
    if isinstance(v, I.MediaHandle):
        return {"type": "Media", "value": f"<{v.kind}: {v.path}>"}
    if isinstance(v, I.TokenStreamHandle):
        return {"type": "Stream", "value": "<stream>"}
    return {"type": type(v).__name__, "value": str(v)}


# --------------------------------------------------------------------------
# interpreter hooks (monkeypatched; interp.py itself is untouched)
# --------------------------------------------------------------------------

_installed = False
_orig_exec_stmt = _interp.Interpreter.exec_stmt
_orig_call_fn = _interp.Interpreter.call_fn
_orig_invoke_lambda = _interp.Interpreter.invoke_lambda_value


def _patched_exec_stmt(self, s, env):
    dbg = getattr(self, "_dbg_session", None)
    if dbg is not None:
        dbg.before_stmt(s, env)
    return _orig_exec_stmt(self, s, env)


def _patched_call_fn(self, decl, args, node):
    dbg = getattr(self, "_dbg_session", None)
    if dbg is None:
        return _orig_call_fn(self, decl, args, node)
    dbg.push_frame(getattr(decl, "name", "<fn>"), node)
    try:
        return _orig_call_fn(self, decl, args, node)
    finally:
        dbg.pop_frame()


def _patched_invoke_lambda(self, lam, args, node):
    dbg = getattr(self, "_dbg_session", None)
    if dbg is None:
        return _orig_invoke_lambda(self, lam, args, node)
    dbg.push_frame("<lambda>", node)
    try:
        return _orig_invoke_lambda(self, lam, args, node)
    finally:
        dbg.pop_frame()


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


class _OutputCapture:
    """Routes program print() output into {"event":"output"} protocol
    frames (agent mode only) so raw program text can never be mistaken
    for a protocol line on stdout."""

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit({"event": "output", "text": line})

    def flush(self):
        if self._buf:
            self._emit({"event": "output", "text": self._buf})
            self._buf = ""


def _human_tv(tv: Dict[str, Any]) -> str:
    """Render a typed-value dict as compact Misa-ish text (human mode)."""
    t = tv.get("type")
    v = tv.get("value")
    if t == "List":
        return "[" + ", ".join(_human_tv(x) for x in v) + "]"
    if t in ("Map", "Record"):
        return "{" + ", ".join(f"{k}: {_human_tv(x)}"
                               for k, x in v.items()) + "}"
    if t == "Text":
        return json.dumps(v)
    if t == "Nil":
        return "nil"
    if t == "Bool":
        return "true" if v else "false"
    if "variant" in tv:
        inner = ", ".join(_human_tv(x) for x in v) if v else ""
        return f"{t}.{tv['variant']}" + (f"({inner})" if inner else "")
    return str(v)


class _DebugQuit(Exception):
    pass


class DebugSession:
    """Runs a Misa program under debugger control.

    The program executes on the calling thread; before each statement the
    `before_stmt` hook may pause into a nested command loop. `mode`
    controls when the next pause happens: "run" (breakpoints only),
    "step" (every statement), "next" (step over calls), "out" (to caller).
    """

    def __init__(self, interp: "_interp.Interpreter",
                 prog: ast.Program,
                 file_of: Dict[int, str],
                 main_path: str):
        install_hooks()
        self.interp = interp
        self.prog = prog
        self.main_path = os.path.abspath(main_path)
        self._main_thread = threading.get_ident()
        # statement id -> absolute file (loader only maps top-level items)
        self._stmt_file: Dict[int, str] = {}
        for item in prog.items:
            f = file_of.get(id(item), self.main_path)
            for n in _walk(item):
                self._stmt_file.setdefault(id(n), f)
        self.breakpoints: Dict[int, tuple] = {}  # id -> (file, line)
        self._bp_by_loc: Dict[tuple, int] = {}
        self._next_bp = 1
        self.frames: List[Dict[str, Any]] = []
        self.mode = "run"
        self._step_depth = 0
        self._pause: Optional[tuple] = None  # (stmt, env) at pause
        self._text_mode = False
        self.program_failed = False
        interp._dbg_session = self  # noqa: SLF001 (hook slot)

    # -- hooking ------------------------------------------------------

    def _file_of(self, node: ast.Node) -> str:
        return self._stmt_file.get(id(node), self.main_path)

    def push_frame(self, name: str, node: Optional[ast.Node]) -> None:
        if threading.get_ident() != self._main_thread:
            return
        self.frames.append({
            "name": name,
            "file": self._file_of(node) if node is not None
            else self.main_path,
            "line": getattr(node, "line", 0),
            "col": getattr(node, "col", 0),
            "env": None,
        })

    def pop_frame(self) -> None:
        if threading.get_ident() != self._main_thread:
            return
        if self.frames:
            self.frames.pop()

    def before_stmt(self, s: ast.Node, env: Any) -> None:
        if threading.get_ident() != self._main_thread:
            return
        if self.frames and self.frames[-1].get("env") is None:
            self.frames[-1]["env"] = env
        f = self._file_of(s)
        line = getattr(s, "line", 0)
        bp_id = self._bp_by_loc.get((f, line))
        depth = len(self.frames)
        pause = False
        hit_bp = None
        if bp_id is not None:
            pause, hit_bp = True, bp_id
        elif self.mode == "step":
            pause = True
        elif self.mode == "next" and depth <= self._step_depth:
            pause = True
        elif self.mode == "out" and depth < self._step_depth:
            pause = True
        if pause:
            self._pause = (s, env)
            self._emit_break(hit_bp, s)
            try:
                if self._text_mode:
                    self._human_loop()
                else:
                    self._agent_loop()
            finally:
                self._pause = None

    # -- events -------------------------------------------------------

    def _emit(self, obj: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _emit_break(self, bp_id: Optional[int], s: ast.Node) -> None:
        if self._text_mode:
            loc = f"{self._file_of(s)}:{getattr(s, 'line', 0)}"
            tag = f"breakpoint {bp_id}" if bp_id else self.mode
            sys.stdout.write(f"[{tag}] {loc} "
                             f"({self.frames[-1]['name']})\n")
            sys.stdout.flush()
            return
        self._emit({
            "event": "break",
            "breakpoint": bp_id,
            "file": self._file_of(s),
            "line": getattr(s, "line", 0),
            "col": getattr(s, "col", 0),
            "function": self.frames[-1]["name"] if self.frames
            else "<main>",
        })

    # -- command handling (shared by agent + human front-ends) --------

    def _frame_env(self, frame_idx: int) -> Any:
        # frame 0 = innermost (current), 1 = caller, ... (pdb convention)
        if not self.frames:
            return self.interp.global_env
        i = len(self.frames) - 1 - frame_idx
        if i < 0 or i >= len(self.frames):
            return None
        env = self.frames[i].get("env")
        return env if env is not None else self.interp.global_env

    def _collect_locals(self, env: Any) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        e = env
        while e is not None:
            for name, cell in e.vars.items():
                if name not in seen:
                    seen[name] = {"name": name,
                                  "mutable": bool(cell[1]),
                                  **dbg_value(cell[0])}
            e = e.parent
        return list(seen.values())

    def _do(self, req: Dict[str, Any]):
        """Execute one command. Returns (response, resume_bool)."""
        cmd = req.get("cmd")
        if cmd == "break":
            f = os.path.abspath(req.get("file", ""))
            line = int(req.get("line", 0))
            key = (f, line)
            if key in self._bp_by_loc:
                return {"ok": True,
                        "breakpoint": self._bp_by_loc[key],
                        "duplicate": True}, False
            bp_id = self._next_bp
            self._next_bp += 1
            self.breakpoints[bp_id] = key
            self._bp_by_loc[key] = bp_id
            return {"ok": True, "breakpoint": bp_id}, False
        if cmd == "clear":
            bp_id = int(req.get("id", -1))
            key = self.breakpoints.pop(bp_id, None)
            if key is None:
                return {"ok": False,
                        "error": {"code": "E300",
                                  "message": f"no breakpoint {bp_id}"}}, \
                    False
            self._bp_by_loc.pop(key, None)
            return {"ok": True}, False
        if cmd == "breakpoints":
            return {"ok": True, "breakpoints": [
                {"id": i, "file": f, "line": ln}
                for i, (f, ln) in sorted(self.breakpoints.items())
            ]}, False
        if cmd in ("run", "continue"):
            self.mode = "run"
            return None, True
        if cmd == "step":
            self.mode = "step"
            return None, True
        if cmd == "next":
            self.mode = "next"
            self._step_depth = len(self.frames)
            return None, True
        if cmd == "out":
            self.mode = "out"
            self._step_depth = len(self.frames)
            return None, True
        if cmd == "locals":
            env = self._frame_env(int(req.get("frame", 0)))
            if env is None:
                return {"ok": False,
                        "error": {"code": "E300",
                                  "message": "no such frame"}}, False
            return {"ok": True, "frame": int(req.get("frame", 0)),
                    "locals": self._collect_locals(env)}, False
        if cmd == "eval":
            env = self._frame_env(int(req.get("frame", 0)))
            if env is None:
                return {"ok": False,
                        "error": {"code": "E300",
                                  "message": "no such frame"}}, False
            try:
                node = Parser(
                    Lexer(str(req.get("expr", "")),
                          "<debug>").tokens(), "<debug>").parse_expr()
                val = self.interp.eval_expr(node, env)
                return {"ok": True, **dbg_value(val)}, False
            except MisaError as e:
                return {"ok": False, "error": e.to_dict()}, False
            except ParseError as e:
                return {"ok": False, "error": {
                    "code": "E100", "message": str(e),
                    "line": e.line, "col": e.col, "hint": ""}}, False
            except Exception as e:  # never leak a Python traceback
                return {"ok": False, "error": {
                    "code": "E300",
                    "message": f"eval failed: {e}"}}, False
        if cmd == "stack":
            n = len(self.frames)
            return {"ok": True, "frames": [
                {"depth": n - 1 - i, "name": fr["name"],
                 "file": fr["file"], "line": fr["line"],
                 "col": fr["col"]}
                for i, fr in enumerate(reversed(self.frames))
            ]}, False
        if cmd == "quit":
            raise _DebugQuit()
        return {"ok": False, "error": {
            "code": "E300",
            "message": f"unknown debug command: {cmd!r}. "
                       f"expected one of break/clear/breakpoints/run/"
                       f"continue/step/next/out/locals/eval/stack/quit"}},
        False

    # -- agent front-end (JSON lines) ---------------------------------

    def _agent_loop(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                raise _DebugQuit()
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                self._emit({"ok": False, "error": {
                    "code": "E300",
                    "message": "debug command must be a JSON object"}})
                continue
            if not isinstance(req, dict):
                self._emit({"ok": False, "error": {
                    "code": "E300",
                    "message": "debug command must be a JSON object"}})
                continue
            try:
                resp, resume = self._do(req)
            except _DebugQuit:
                raise
            except Exception as e:  # never leak a Python traceback
                self._emit({"ok": False, "error": {
                    "code": "E300",
                    "message": f"debugger error: {e}"}})
                continue
            if resp is not None:
                self._emit(resp)
            if resume:
                return

    def interact_agent(self) -> None:
        """Entry point for `misa debug --agent`."""
        self.interp.out = _OutputCapture(self._emit)
        self._emit({"event": "started", "program": self.main_path})
        self.frames.append({"name": "<main>", "file": self.main_path,
                            "line": 1, "col": 1, "env": None})
        try:
            self._agent_loop()  # agent issues break/run/...
            # `run` resumed us here: execute the program.
            self.interp.exec_setup(self.prog)
        except _DebugQuit:
            pass
        except MisaError as e:
            d = e.to_dict()
            self._emit({"event": "error", **d})
            self.program_failed = True
            return
        except RecursionError:
            self._emit({"event": "error", "code": "E300",
                        "message": "call stack overflow "
                                   "(recursion too deep)",
                        "line": 0, "col": 0, "hint": ""})
            self.program_failed = True
            return
        self._emit({"event": "exit", "code": 0})

    # -- human front-end (text) ---------------------------------------

    def _human_help(self) -> None:
        sys.stdout.write(
            "b[reak] file:line  set breakpoint      c[ontinue]/r[un]  resume\n"
            "s[tep]             step into            n[ext]           step over\n"
            "o[ut]              step out             p[rint] expr     eval expr\n"
            "l[ocals]           dump locals          st[ack]         backtrace\n"
            "q[uit]             abort\n")

    def _human_cmd(self, text: str):
        """Translate one human command line into (response_text, resume)."""
        parts = text.strip().split(None, 1)
        if not parts:
            return None, False
        c, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if c in ("b", "break"):
            if ":" not in rest:
                return "usage: b file:line", False
            f, ln = rest.rsplit(":", 1)
            resp, _ = self._do({"cmd": "break", "file": f,
                                "line": int(ln)})
            return (f"breakpoint {resp['breakpoint']} at {f}:{ln}"
                    if resp.get("ok") else f"error: {resp}"), False
        if c in ("c", "continue", "r", "run"):
            return None, True
        if c in ("s", "step"):
            self.mode = "step"
            return None, True
        if c in ("n", "next"):
            self.mode = "next"
            self._step_depth = len(self.frames)
            return None, True
        if c in ("o", "out"):
            self.mode = "out"
            self._step_depth = len(self.frames)
            return None, True
        if c in ("p", "print", "e", "eval"):
            resp, _ = self._do({"cmd": "eval", "expr": rest})
            if resp.get("ok"):
                return f"{resp['type']}: {_human_tv(resp)}", False
            return f"error {resp['error']['code']}: " \
                   f"{resp['error']['message']}", False
        if c in ("l", "locals"):
            resp, _ = self._do({"cmd": "locals"})
            lines = [f"{v['name']}: {v['type']} = {_human_tv(v)}"
                     for v in resp["locals"]]
            return "\n".join(lines) if lines else "(no locals)", False
        if c in ("st", "stack", "bt"):
            resp, _ = self._do({"cmd": "stack"})
            return "\n".join(
                f"#{fr['depth']} {fr['name']} "
                f"{fr['file']}:{fr['line']}" for fr in resp["frames"]), False
        if c in ("q", "quit"):
            raise _DebugQuit()
        if c in ("h", "help", "?"):
            self._human_help()
            return None, False
        return f"unknown command: {c} (h for help)", False

    def _human_loop(self) -> None:
        while True:
            try:
                text = input("(misa-debug) ")
            except EOFError:
                raise _DebugQuit()
            try:
                out, resume = self._human_cmd(text)
            except _DebugQuit:
                raise
            if out:
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
            if resume:
                return

    def interact_human(self) -> None:
        """Entry point for `misa debug` (human text mode)."""
        self._text_mode = True
        sys.stdout.write(f"debugging {self.main_path} "
                         f"(h for help)\n")
        self.frames.append({"name": "<main>", "file": self.main_path,
                            "line": 1, "col": 1, "env": None})
        try:
            self._human_loop()
            self.interp.exec_setup(self.prog)
        except _DebugQuit:
            sys.stdout.write("quit\n")
            return
        except MisaError as e:
            d = e.to_dict()
            sys.stdout.write(
                f"error {d['code']}: {d['message']} "
                f"({d['line']}:{d['col']})"
                f"{' — ' + d['hint'] if d['hint'] else ''}\n")
            self.program_failed = True
            return
        except RecursionError:
            sys.stdout.write("error E300: call stack overflow "
                             "(recursion too deep)\n")
            self.program_failed = True
            return
        sys.stdout.write("program exited (code 0)\n")


def debug_file(path: str, mock_path: Optional[str],
               agent_mode: bool,
               prog_argv: Optional[List[str]] = None) -> int:
    """Load, check, and debug a program. Returns process exit code."""
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
    interp.load(merged)
    session = DebugSession(interp, merged, file_of, path)
    if agent_mode:
        session.interact_agent()
    else:
        session.interact_human()
    return 1 if session.program_failed else 0
