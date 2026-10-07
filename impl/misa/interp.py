"""Misa v0.2 tree-walking interpreter."""
from __future__ import annotations
import codecs
import calendar
import functools
import gzip
import hashlib
import hmac
import json
import math
import os
import random as _py_random
import socket
import sqlite3
import sys
import tempfile
import zlib
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ast
from .errors import (MisaError, RuntimeError_, UndefinedName,
                     ImmutableAssign, AssertFailed, EnvNotSet, ModelError,
                     SchemaValidationError, BudgetExceeded, ThrownError,
                     IOError_, HTTPError_, MapKeyError, ProviderConfigError,
                     FFIError, TypeMismatch, ConcurrencyError)
from .providers import (Provider, Usage, make_provider, guess_mime,
                        estimate_cost_usd, OnDeviceProvider)


# ---------------------------------------------------------------- integers
# Misa Int is 64-bit two's-complement wrapping (C-1 decision, 2026-10-06).
# Python ints are arbitrary precision, so every int-producing operation
# wraps explicitly to match the AOT backend.

_I64_MASK = 0xFFFFFFFFFFFFFFFF
_I64_HIGH = 0x8000000000000000


def wrap_i64(n: int) -> int:
    n &= _I64_MASK
    return n - 0x10000000000000000 if n >= _I64_HIGH else n


# ---------------------------------------------------------------- values

@dataclass
class MediaHandle:
    kind: str  # Photo | Audio | Video | Document
    path: str


@dataclass
class DbHandle:
    """Phase 5 (DB): an open SQLite connection (opaque `Db` value)."""
    path: str
    conn: Any  # sqlite3.Connection
    # P-3: explicit-transaction state (db.begin/commit/rollback).
    in_txn: bool = False


@dataclass
class TextBuilder:
    """Text.Builder: amortized O(1) string building (perf gap P-1)."""
    parts: list  # list[str]


@dataclass
class SocketHandle:
    """Wave 3 (networking): a connected TCP socket (opaque `Socket`)."""
    sock: Any  # socket.socket
    closed: bool = False
    # Incremental UTF-8 decoder: recv() may split a multi-byte char
    # across reads; decoding incrementally is never lossy.
    decoder: Any = None


@dataclass
class ListenerHandle:
    """Wave 3 (networking): a bound+listening TCP socket (`Listener`)."""
    sock: Any  # socket.socket
    closed: bool = False


@dataclass
class LambdaValue:
    params: List[str]
    body: List[ast.Node]
    expr_body: Optional[ast.Node]
    closure: "Env"


@dataclass
class StateCell:
    """Screen state (`let x = state(v)`): a heap cell holding the current
    value plus a back-reference to the owning screen frame dict, so
    writes mark the frame dirty and the driver re-renders."""
    value: Any
    frame: Optional[dict]  # the screen frame owning this cell


@dataclass
class FnValue:
    """Phase 5a: an `fn` declaration used as a first-class value."""
    decl: ast.FnDecl


@dataclass
class SumValue:
    """Phase 5b: a constructed sum-type variant value."""
    type_name: str
    variant: str
    fields: list  # positional payload values


@dataclass
class PtrValue:
    """Phase 7: an opaque C pointer (FFI `Ptr` value). Only passed
    back to C or compared to nil; never dereferenced from Misa."""
    addr: int  # 0 = null

    def __eq__(self, other):
        if other is None:
            return self.addr == 0
        if isinstance(other, PtrValue):
            return self.addr == other.addr
        return NotImplemented

    def __hash__(self):
        return hash(self.addr)


@dataclass
class IfaceValue:
    """Phase 8 (overtake): an interface-typed value. Carries the
    interface name, the concrete implementing type, and the payload
    (the concrete record value). Method dispatch looks up
    impls[(iface, concrete)]."""
    iface: str
    concrete: str
    payload: object


@dataclass
class TokenStreamHandle:
    decl: ast.ModelDecl
    arg_values: list
    provider: Provider
    model_id: str
    prompt_text: str
    images: list
    timeout_s: float
    handlers: list = field(default_factory=list)
    started: bool = False


# ---------------------------------------------------------------- Phase 7
# (concurrency): one Python thread per task behind the cooperative
# facade. The GIL limits CPU parallelism; I/O parallelizes fully.

# Poll interval (seconds) for unbounded waits (`await` without timeout,
# `recv`, `sleep`). Chunked waiting lets a task observe scope
# cancellation promptly instead of sleeping through it.
_YIELD_POLL_S = 0.05

# Bound on how long run() waits for outstanding tasks at program end
# before cancelling the stragglers. Documented, not derived: long enough
# for normal I/O tails, short enough that a hung task cannot hang the
# CLI forever. All task threads are daemonic, so even a thread stuck in
# a C call (e.g. http.get) dies with the process.
PROGRAM_END_WAIT_S = 10.0

# Per-thread concurrency state: the current task handle (None on the
# main thread) and the stack of function-scope ids on this thread.
_tls = threading.local()


def _scope_stack() -> list:
    st = getattr(_tls, "scope_stack", None)
    if st is None:
        st = []
        _tls.scope_stack = st
    return st


def _tco_stack() -> list:
    """Phase 5d: thread-local stack of in-progress function/lambda
    invocations for tail-call optimization. Entries are
    ("fn", FnDecl) from call_fn or ("lam", LambdaValue) from
    invoke_lambda_value. A `return f(args)` is a tail call only when
    the top entry is ("fn", decl) for that exact decl — i.e. the
    return is directly in f's own body, not inside a nested lambda
    or handler (reusing f's frame there would skip the intermediate
    frames' post-processing)."""
    st = getattr(_tls, "tco_stack", None)
    if st is None:
        st = []
        _tls.tco_stack = st
    return st


class TaskHandle:
    """Phase 7 (concurrency): a spawned task (`spawn { }`)."""

    def __init__(self, label: str = "task"):
        self.label = label
        self.thread: Optional[threading.Thread] = None
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[MisaError] = None  # captured MisaError
        self.cancelled = False  # set by scope exit / program end
        self.parent_scope = 0  # scope id on the spawning thread
        self.awaited = False  # set by await (suppresses stderr report)


class Channel:
    """Phase 7 (concurrency): an unbounded FIFO channel (`chan<T>()`).

    `send` never blocks. `recv` blocks until a value arrives; after
    `close`, `recv` drains the remaining values then raises E306
    "channel closed".
    """

    def __init__(self):
        self.q: deque = deque()
        self.cond = threading.Condition()
        self.closed = False


class MapValue:
    """A Misa Map. Distinct from a record (plain dict) so missing-key
    access can throw E305 instead of silently returning nil."""

    def __init__(self, entries: Optional[dict] = None):
        self.data: dict = dict(entries) if entries else {}

    def __eq__(self, other):
        return isinstance(other, MapValue) and self.data == other.data

    def __len__(self):
        return len(self.data)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"MapValue({self.data!r})"


class RegexValue:
    """Wave 3 (stdlib): a compiled regex (`regex("...")`).

    Wraps misa.regexp.Regex — the same portable engine the checker
    uses for check-time validation, so checked patterns always
    compile here.
    """

    def __init__(self, pattern: str):
        from .regexp import Regex, RegexError
        try:
            self.rx = Regex(pattern)
        except RegexError as ex:
            raise RuntimeError_(f"invalid regex pattern: {ex}")
        self.pattern = pattern


class SetValue:
    """Wave 3 (stdlib): a `Set<T>` value.

    v1: insertion-ordered list + linear scan with Misa equality
    (mirrors the AOT backend exactly; O(n) membership).
    """

    def __init__(self, items=None):
        self.items: list = list(items) if items else []

    def __len__(self):
        return len(self.items)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"SetValue({self.items!r})"


class BytesValue:
    """Final wave (stdlib): opaque binary data (`Bytes`).

    Produced by `compress.gzip` / `compress.gunzip_bytes`; wraps a
    Python `bytes` (mirrors the AOT `M_TAG_BYTES` payload exactly).
    """

    def __init__(self, data: bytes):
        self.data: bytes = bytes(data)

    def __eq__(self, other):
        return isinstance(other, BytesValue) and self.data == other.data

    def __len__(self):
        return len(self.data)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"BytesValue({len(self.data)} bytes)"


class CliParserValue:
    """cli: a `CliParser` — accumulates flag/command declarations.

    Mirrors MisaCliParser in the AOT runtime. flags is a list of dicts:
    {name, metavar (None for bool), help, is_bool, default (None),
     short (None or one char)}. commands is a list of {name, help}.
    """

    def __init__(self, prog: str, desc: str):
        self.prog = prog
        self.desc = desc
        self.flags: list = []
        self.commands: list = []

    def find_flag(self, name: str):
        for f in self.flags:
            if f["name"] == name:
                return f
        return None

    def find_short(self, c: str):
        for f in self.flags:
            if f["short"] == c:
                return f
        return None

    def find_command(self, name: str):
        for c in self.commands:
            if c["name"] == name:
                return c
        return None

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"CliParserValue({self.prog!r})"


class CliArgsValue:
    """cli: a `CliArgs` — one parse result. Mirrors MisaCliArgs."""

    def __init__(self, parser: CliParserValue):
        self.parser = parser
        self.values: dict = {}   # name -> explicit Text value
        self.bools: dict = {}    # name -> True (bool flags only)
        self.command = None      # Text or None
        self.positionals: list = []

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"CliArgsValue({self.parser.prog!r})"


def cli_build_help(p: CliParserValue) -> str:
    """Build the --help text. Mirrors cli_help_into() in misa_rt.c
    exactly — byte-identical output is asserted by AOT parity tests."""
    out = []
    if p.desc:
        out.append(f"{p.prog} - {p.desc}\n\n")
    else:
        out.append(f"{p.prog}\n\n")
    out.append(f"Usage: {p.prog} [flags]")
    if p.commands:
        out.append(" <command> [args]")
    else:
        out.append(" [args]")
    out.append("\n\nFlags:\n")
    specs = []
    for f in p.flags:
        if f["short"]:
            spec = f"-{f['short']}, --{f['name']}"
        else:
            spec = f"--{f['name']}"
        if not f["is_bool"]:
            spec += f" {f['metavar']}"
        specs.append(spec)
    specs.append("-h, --help")
    width = max(len(s) for s in specs)
    for f, spec in zip(p.flags, specs):
        line = f"  {spec:<{width}}  {f['help']}"
        if f["default"] is not None:
            line += f" (default: {f['default']})"
        out.append(line + "\n")
    out.append(f"  {'-h, --help':<{width}}  Show this help and exit\n")
    if p.commands:
        out.append("\nCommands:\n")
        cwidth = max(len(c["name"]) for c in p.commands)
        for c in p.commands:
            out.append(f"  {c['name']:<{cwidth}}  {c['help']}\n")
    return "".join(out)


def _cli_is_flaglike(tok: str) -> bool:
    return tok.startswith("-") and tok != "-" and len(tok) > 1


# Final wave (stdlib): fixed gzip header (mtime=0, XFL=max, OS=Unix)
# shared by the manual framing below and mirrored in the AOT runtime.
_GZ_HDR = bytes([0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
                 0x02, 0x03])


def _gzip_framed(data: bytes) -> bytes:
    """Deterministic gzip of `data`: fixed header, raw deflate at
    level 9, CRC32 + ISIZE (LE) trailer. Byte-identical to the AOT
    backend's `misa_compress_gzip` by construction."""
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = comp.compress(data) + comp.flush()
    trailer = ((zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "little") +
               (len(data) & 0xFFFFFFFF).to_bytes(4, "little"))
    return _GZ_HDR + body + trailer


def _misa_eq(a: Any, b: Any) -> bool:
    """Misa value equality, mirroring C `val_equal`.

    Numeric cross-type equality (1 == 1.0); Bool is distinct from Int
    (unlike Python's `==`); structural for lists/maps/records.
    Used by Set membership so both backends agree.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_misa_eq(x, y)
                                       for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a.keys()) == set(b.keys()) and all(
            _misa_eq(a[k], b[k]) for k in a)
    if isinstance(a, MapValue) and isinstance(b, MapValue):
        return _misa_eq(a.data, b.data)
    if isinstance(a, SetValue) and isinstance(b, SetValue):
        return len(a.items) == len(b.items) and all(
            any(_misa_eq(x, y) for y in b.items) for x in a.items)
    if isinstance(a, BytesValue) and isinstance(b, BytesValue):
        return a.data == b.data
    return type(a) is type(b) and a == b


# Wave 3 (stdlib): `random.*` uses a dedicated RNG instance so
# `random.seed(n)` gives a reproducible stream without disturbing
# anything else.
_rng = _py_random.Random()


class Env:
    def __init__(self, parent: Optional["Env"] = None):
        self.parent = parent
        self.vars: Dict[str, list] = {}  # name -> [value, mutable]

    def define(self, name: str, value: Any, mutable: bool):
        self.vars[name] = [value, mutable]

    def lookup(self, name: str) -> list:
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.lookup(name)
        raise UndefinedName(f"undefined name '{name}'")

    def assign(self, name: str, value: Any):
        cell = self.lookup(name)
        # Screen state: writing a StateCell mutates the cell (bypassing
        # let-immutability) and marks the owning screen frame dirty so
        # the driver re-renders after the handler returns.
        if isinstance(cell[0], StateCell):
            cell[0].value = value
            if cell[0].frame is not None:
                cell[0].frame["dirty"] = True
            return
        if not cell[1]:
            raise ImmutableAssign(f"cannot assign to immutable '{name}'")
        cell[0] = value


def render_value(v: Any) -> str:
    if v is None:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(render_value(i) for i in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}: {render_value(x)}"
                                for k, x in v.items()) + " }"
    if isinstance(v, MapValue):
        return "{" + ", ".join(f'"{k}": {render_value(x)}'
                                for k, x in v.data.items()) + "}"
    if isinstance(v, MediaHandle):
        return f"<{v.kind}: {v.path}>"
    if isinstance(v, PtrValue):
        return "<ptr>"
    if isinstance(v, TokenStreamHandle):
        return "<stream>"
    if isinstance(v, TaskHandle):
        return "<task>"
    if isinstance(v, Channel):
        return "<chan>"
    # Wave 3 (stdlib).
    if isinstance(v, RegexValue):
        return f"<regex: {v.pattern}>"
    if isinstance(v, CliParserValue):
        return f"<cli-parser: {v.prog}>"
    if isinstance(v, CliArgsValue):
        return "<cli-args>"
    if isinstance(v, SetValue):
        return "{" + ", ".join(render_value(i) for i in v.items) + "}"
    # Final wave (stdlib): opaque bytes render as their length.
    if isinstance(v, BytesValue):
        return f"<bytes: {len(v.data)}>"
    if isinstance(v, LambdaValue):
        return "<lambda>"
    if isinstance(v, FnValue):
        return f"<function {v.decl.name}>"
    if isinstance(v, SumValue):
        if not v.fields:
            return f"{v.type_name}.{v.variant}"
        inner = ", ".join(render_value(x) for x in v.fields)
        return f"{v.type_name}.{v.variant}({inner})"
    # Phase 8 (overtake): interface values render as their payload.
    if isinstance(v, IfaceValue):
        return render_value(v.payload)
    return str(v)


# ---------------------------------------------------------------- interpreter

DEFAULT_TIMEOUT_S = 120.0


class Interpreter:
    def __init__(self, *, mock_scripts: Optional[dict] = None,
                 out=None, main_dir: Optional[str] = None,
                 argv: Optional[list] = None):
        self.mock_scripts = mock_scripts or {}
        self.out = out if out is not None else sys.stdout
        self.main_dir = main_dir  # dir of the entry .misa file, if known
        # Command-line arguments for the program (excluding the program
        # name itself, like Python's sys.argv[1:]).
        self.argv: list = list(argv) if argv else []
        self.file_of: Dict[int, str] = {}  # id(item) -> source file
        self._tokens_warned: set = set()
        self.reset()

    def reset(self):
        self.app: Optional[ast.AppDecl] = None
        self.providers: Dict[str, ast.ProviderDecl] = {}
        self.aliases: Dict[str, tuple] = {}
        self.types: Dict[str, ast.TypeDecl] = {}
        self.models: Dict[str, ast.ModelDecl] = {}
        self.screens: Dict[str, ast.ScreenDecl] = {}
        self.evals: Dict[str, ast.EvalDecl] = {}
        self.tests: Dict[str, ast.TestDecl] = {}
        self.fns: Dict[str, ast.FnDecl] = {}
        # Phase 8 (overtake): interfaces: name -> InterfaceDecl;
        # impls: (iface, target) -> ImplDecl.
        self.interfaces: Dict[str, ast.InterfaceDecl] = {}
        self.impls: Dict[tuple, ast.ImplDecl] = {}
        # Screen state: the current screen frame (dict with name, args,
        # states, dirty, handlers) or None outside a screen.
        self._screen_frame: Optional[dict] = None
        # Phase 7: extern (FFI) functions: name -> (ctypes callable,
        # FnDecl signature). Loaded lazily on first load().
        self.extern_fns: Dict[str, tuple] = {}
        # Overtake Wave 2 (FFI): struct name -> ctypes.Structure
        # subclass; struct name -> StructDecl (for field types).
        self.extern_struct_ctypes: Dict[str, type] = {}
        self.extern_struct_decls: Dict[str, ast.StructDecl] = {}
        # Callback trampolines stay alive for the interpreter's
        # lifetime (v1: a callback must not be invoked after its
        # extern call returns, but GC of the trampoline mid-call
        # would segfault — this prevents that class of crash).
        self._cb_keepalive: list = []
        self.provider_cache: Dict[tuple, Provider] = {}
        self.global_env = Env()
        self.current_screen = None
        # Phase 7 (concurrency): task registry + scope ids. Tasks share
        # the interpreter (and memory — mutable sharing across tasks is
        # the programmer's responsibility, like Go).
        self._tasks: List[TaskHandle] = []
        self._tasks_lock = threading.Lock()
        self._scope_counter = 0

    # -- Phase 7 (concurrency): scopes, cancellation, program end -----

    def _new_scope(self) -> int:
        with self._tasks_lock:
            self._scope_counter += 1
            return self._scope_counter

    def _push_scope(self):
        _scope_stack().append(self._new_scope())

    def _current_scope(self) -> int:
        st = _scope_stack()
        return st[-1] if st else 0

    def _pop_scope(self):
        """Pop the current function scope, cancelling still-running
        children spawned inside it (structured concurrency: no orphans,
        no leaks)."""
        st = _scope_stack()
        if not st:
            return
        sid = st.pop()
        with self._tasks_lock:
            children = [t for t in self._tasks
                        if t.parent_scope == sid and not t.done.is_set()]
        for t in children:
            t.cancelled = True

    @staticmethod
    def _current_task() -> Optional[TaskHandle]:
        return getattr(_tls, "task_handle", None)

    def _check_cancelled(self, node=None):
        """Yield-point check: a cancelled task raises E306 "task
        cancelled" at `await`, `recv`, `sleep` entry, and task start.
        Threads blocked in C calls (e.g. http.get) finish naturally and
        observe the flag at their next yield point."""
        h = self._current_task()
        if h is not None and h.cancelled:
            kw = {"line": node.line, "col": node.col} \
                if node is not None else {}
            raise ConcurrencyError("task cancelled", **kw)

    def _finish_tasks(self):
        """Program end: wait for outstanding tasks bounded by
        PROGRAM_END_WAIT_S, then cancel stragglers. Never-awaited tasks
        whose error is set are reported to stderr (never silent)."""
        st = _scope_stack()
        if st:
            st.pop()  # drop the program scope
        deadline = time.monotonic() + PROGRAM_END_WAIT_S
        pending: list = []
        while True:
            with self._tasks_lock:
                pending = [t for t in self._tasks
                           if not t.done.is_set()]
            if not pending:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        for t in pending:
            t.cancelled = True
        for t in pending:
            # Bounded: a thread stuck in a C call keeps its daemon
            # status and dies with the process; we never join forever.
            t.thread.join(timeout=1.0)
        for t in self._tasks:
            if t.error is not None and not t.awaited:
                print(f"Error: {t.error.code}: {t.error.message}",
                      file=sys.stderr)

    # -- loading ------------------------------------------------------

    def load(self, prog: ast.Program):
        for item in prog.items:
            if isinstance(item, ast.AppDecl):
                self.app = item
            elif isinstance(item, ast.ProviderDecl):
                self.providers[item.name] = item
            elif isinstance(item, ast.ModelsDecl):
                for alias, prov, mid in item.entries:
                    self.aliases[alias] = (prov, mid)
            elif isinstance(item, ast.TypeDecl):
                self.types[item.name] = item
            elif isinstance(item, ast.TableDecl):
                pass  # Phase 5 (DB): check-time only; no runtime state.
            elif isinstance(item, ast.ModelDecl):
                self.models[item.name] = item
            elif isinstance(item, ast.ScreenDecl):
                self.screens[item.name] = item
            elif isinstance(item, ast.EvalDecl):
                self.evals[item.name] = item
            elif isinstance(item, ast.TestDecl):
                self.tests[item.name] = item
            elif isinstance(item, ast.FnDecl):
                self.fns[item.name] = item
            elif isinstance(item, ast.InterfaceDecl):
                self.interfaces[item.name] = item
            elif isinstance(item, ast.ImplDecl):
                self.impls[(item.iface, item.target)] = item
            elif isinstance(item, ast.ExternDecl):
                self._load_extern(item)
            elif isinstance(item, ast.ImportDecl):
                pass  # resolved by the loader before load()

    # -- Phase 7: FFI ---------------------------------------------------
    # Maps Misa extern signatures onto ctypes. Text crosses as
    # null-terminated UTF-8 copies (both directions); Ptr is an opaque
    # address. Load/symbol failures raise FFIError (E226), never a
    # raw traceback.
    #
    # Overtake Wave 2: structs map to ctypes.Structure subclasses
    # (built per declaration in _load_extern); a `(A, B) -> R` param
    # type maps to a CFUNCTYPE callback.

    def _ffi_ctype(self, tname: str):
        import ctypes
        if tname in self.extern_struct_ctypes:
            return self.extern_struct_ctypes[tname]
        if tname in self.extern_struct_decls:
            # Overtake final wave: lazily build the ctypes class
            # (handles nested aggregates and cross-block references).
            return self._build_aggr_ctype(tname, ())
        return {"Int": ctypes.c_long,
                "Float": ctypes.c_double,
                "Text": ctypes.c_char_p,
                "Bool": ctypes.c_int,
                "Ptr": ctypes.c_void_p}[tname]

    def _build_aggr_ctype(self, name: str, seen: tuple):
        """Build (and memoize) the ctypes class for a declared
        struct/union. Recursive for nested aggregates; bitfields use
        unsigned-long units (values are unsigned, 0..2**w-1)."""
        import ctypes
        if name in self.extern_struct_ctypes:
            return self.extern_struct_ctypes[name]
        if name in seen:
            raise FFIError(f"cyclic nesting through '{name}'",
                           line=0, col=0)
        decl = self.extern_struct_decls.get(name)
        if decl is None:
            raise FFIError(f"unknown extern aggregate '{name}'",
                           line=0, col=0)
        base = (ctypes.Union if decl.kind == "union"
                else ctypes.Structure)
        fields = []
        for fname, fte in decl.fields:
            ft = fte.name
            width = fte.bitfield_width
            if width is not None:
                # Bitfield values are unsigned (C `unsigned long`
                # semantics); out-of-range is a clean error at
                # construction.
                fields.append((fname, ctypes.c_ulong, width))
            elif ft in ("Int", "Float", "Bool", "Ptr"):
                ctype = {"Int": ctypes.c_long,
                         "Float": ctypes.c_double,
                         "Bool": ctypes.c_int,
                         "Ptr": ctypes.c_void_p}[ft]
                fields.append((fname, ctype))
            elif ft in self.extern_struct_decls:
                fields.append(
                    (fname, self._build_aggr_ctype(ft, seen + (name,))))
            else:
                raise FFIError(
                    f"extern {decl.kind} '{name}': field '{fname}' has "
                    f"unknown type '{ft}'", line=0, col=0)
        cls = type("MisaAggr_" + name, (base,),
                   {"_fields_": fields, "_misa_name": name})
        self.extern_struct_ctypes[name] = cls
        return cls

    def _ffi_restype(self, tname: str):
        if tname == "Void":
            return None
        return self._ffi_ctype(tname)

    def _ffi_callback_ctype(self, te: ast.TypeExpr):
        """ctypes type for a `(A, B) -> R` callback parameter."""
        import ctypes
        params = [self._ffi_ctype(self._tname_of(te_p))
                  for te_p in te.func_params]
        restype = self._ffi_restype(self._tname_of(te.func_ret))
        return ctypes.CFUNCTYPE(restype, *params)

    @staticmethod
    def _tname_of(te: ast.TypeExpr) -> str:
        if te.is_func:
            ps = ", ".join(Interpreter._tname_of(p)
                           for p in te.func_params)
            return f"({ps}) -> {Interpreter._tname_of(te.func_ret)}"
        return te.name

    def _load_extern(self, decl: ast.ExternDecl):
        import ctypes
        import ctypes.util
        lib = decl.lib
        try:
            if lib == "c":
                cdll = ctypes.CDLL(None)
            elif "/" in lib or lib.endswith((".so", ".dylib", ".dll")):
                # Overtake Wave 2: a direct shared-object path.
                cdll = ctypes.CDLL(lib)
            else:
                resolved = ctypes.util.find_library(lib)
                cdll = ctypes.CDLL(resolved or ("lib" + lib))
        except OSError as ex:
            raise FFIError(f"cannot load library '{lib}': {ex}",
                           line=decl.line, col=decl.col)
        # Overtake Wave 2: build a ctypes.Structure subclass per
        # declared C-layout struct (v1 fields: Int/Float/Bool/Ptr).
        # Overtake final wave: classes are built lazily by _ffi_ctype
        # (recursive: nested structs/unions, bitfields), so only the
        # declarations are recorded here. A struct may reference an
        # aggregate declared in a later extern block.
        for st in decl.structs:
            self.extern_struct_decls[st.name] = st
        for fn in decl.fns:
            try:
                cfn = getattr(cdll, fn.name)
            except AttributeError:
                raise FFIError(
                    f"library '{lib}' has no symbol '{fn.name}'",
                    line=fn.line, col=fn.col)
            # The checker guarantees FFI-mappable types. Callback
            # params (`(A, B) -> R`) become CFUNCTYPE slots; varargs
            # functions declare only their fixed prefix (ctypes
            # passes the tail as given).
            argtypes = []
            for _, te in fn.params:
                if te.is_func:
                    argtypes.append(self._ffi_callback_ctype(te))
                else:
                    argtypes.append(self._ffi_ctype(te.name))
            cfn.argtypes = argtypes
            cfn.restype = self._ffi_restype(
                fn.ret.name if fn.ret else "Void")
            self.extern_fns[fn.name] = (cfn, fn)

    def call_extern(self, name: str, args: list, node) -> Any:
        import ctypes
        cfn, fn = self.extern_fns[name]
        c_args = []
        # Lifetimes: encoded Text bytes for varargs must outlive the
        # call; callback trampolines live in _cb_keepalive.
        keepalive = []
        for (pn, te), a in zip(fn.params, args):
            t = te.name
            if te.is_func:
                # Overtake Wave 2: Misa closure -> C function pointer.
                c_args.append(self._make_callback(te, a, node))
            elif t == "Text":
                c_args.append(a.encode("utf-8"))
            elif t == "Ptr":
                c_args.append(a.addr if isinstance(a, PtrValue) else 0)
            elif t == "Bool":
                c_args.append(1 if a else 0)
            elif t in self.extern_struct_decls:
                # Aggregate by value: the value already is a ctypes
                # Structure/Union instance (built by StructLit eval).
                c_args.append(a)
            else:
                c_args.append(a)
        if fn.varargs:
            # Overtake Wave 2: the tail converts explicitly per the
            # mapping table (ctypes does not convert varargs).
            for a, at in zip(args[len(fn.params):],
                             self._vararg_tnames(fn, args, node)):
                ct = self._ffi_ctype(at)
                if at == "Text":
                    b = a.encode("utf-8")
                    keepalive.append(b)
                    c_args.append(ct(b))
                elif at == "Bool":
                    c_args.append(ct(1 if a else 0))
                elif at == "Ptr":
                    c_args.append(ct(a.addr if isinstance(a, PtrValue)
                                      else 0))
                elif at in self.extern_struct_decls:
                    c_args.append(a)
                else:
                    c_args.append(ct(a))
        try:
            r = cfn(*c_args)
        except Exception as ex:
            raise FFIError(f"extern call '{name}' failed: {ex}",
                           line=node.line, col=node.col)
        ret = fn.ret.name if fn.ret else "Void"
        if ret == "Void":
            return None
        if ret == "Text":
            # ctypes gives bytes (or None for NULL) for c_char_p.
            return r.decode("utf-8") if r is not None else None
        if ret == "Ptr":
            return PtrValue(r or 0)
        if ret == "Bool":
            return bool(r)
        if ret in self.extern_struct_ctypes:
            # Struct by value: ctypes returns an instance.
            return r
        return r

    def _vararg_tnames(self, fn: ast.FnDecl, args: list,
                       node) -> list:
        """Misa type names for the `...` tail of an extern call. The
        checker already validated them; this re-derives them from the
        runtime values for marshalling."""
        out = []
        for a in args[len(fn.params):]:
            if isinstance(a, bool):
                out.append("Bool")
            elif isinstance(a, int):
                out.append("Int")
            elif isinstance(a, float):
                out.append("Float")
            elif isinstance(a, str):
                out.append("Text")
            elif isinstance(a, PtrValue):
                out.append("Ptr")
            else:
                import ctypes as _ct
                if isinstance(a, _ct.Structure):
                    out.append(a._misa_name)
                else:
                    raise FFIError(
                        f"cannot marshal vararg of type "
                        f"{type(a).__name__}", line=node.line,
                        col=node.col)
        return out

    def _make_callback(self, te: ast.TypeExpr, lam, node):
        """Overtake Wave 2: wrap a Misa closure as a C function
        pointer (ctypes CFUNCTYPE). v1 rule: the callback is valid
        only for the duration of the extern call."""
        import ctypes
        if not isinstance(lam, (LambdaValue, FnValue)):
            raise FFIError("callback argument must be a function "
                           "value", line=node.line, col=node.col)
        FUNCTYPE = self._ffi_callback_ctype(te)
        pnames = [self._tname_of(p) for p in te.func_params]
        rname = self._tname_of(te.func_ret)

        def _trampoline(*c_args):
            misa_args = [self._c_to_misa(t, v)
                         for t, v in zip(pnames, c_args)]
            if isinstance(lam, LambdaValue):
                r = self.invoke_lambda_value(lam, misa_args, node)
            else:
                r = self.call_fn(lam.decl, misa_args, node)
            return self._misa_to_c(rname, r, node)

        cfunc = FUNCTYPE(_trampoline)
        self._cb_keepalive.append(cfunc)
        return cfunc

    @staticmethod
    def _c_to_misa(tname: str, v):
        if tname == "Int":
            return int(v)
        if tname == "Float":
            return float(v)
        if tname == "Bool":
            return bool(v)
        if tname == "Text":
            return v.decode("utf-8") if v is not None else ""
        if tname == "Ptr":
            return PtrValue(v or 0)
        return v

    def _misa_to_c(self, tname: str, v, node):
        if tname == "Int":
            return int(v)
        if tname == "Float":
            return float(v)
        if tname == "Bool":
            return 1 if v else 0
        if tname == "Text":
            b = v.encode("utf-8") if isinstance(v, str) else b""
            self._cb_keepalive.append(b)
            return b
        if tname == "Ptr":
            return v.addr if isinstance(v, PtrValue) else 0
        if tname == "Void":
            return None
        raise FFIError(f"cannot return {tname} from a callback",
                       line=node.line, col=node.col)

    def run(self, prog: ast.Program):
        self.load(prog)
        self.exec_setup(prog)

    def exec_setup(self, prog: ast.Program):
        """Execute top-level statements (decls skipped, including
        TestDecl/EvalDecl). Used by run() and by the `misa test` runner,
        which needs setup without re-running the task-reaping tail."""
        # Phase 7: the program scope owns top-level spawns; run() waits
        # for outstanding tasks (bounded) when the program finishes.
        self._push_scope()
        try:
            for item in prog.items:
                if isinstance(item, (ast.AppDecl, ast.ProviderDecl,
                                     ast.ModelsDecl, ast.TypeDecl,
                                     ast.TableDecl,
                                     ast.ModelDecl, ast.ScreenDecl,
                                     ast.EvalDecl, ast.TestDecl,
                                     ast.FnDecl,
                                     ast.ExternDecl,
                                     ast.InterfaceDecl, ast.ImplDecl,
                                     ast.ImportDecl)):
                    continue
                try:
                    self.exec_stmt(item, self.global_env)
                except _ReturnSignal:
                    break
                except RecursionError:
                    # H-5: deep Misa recursion must surface as a clean E300,
                    # never a Python traceback.
                    raise RuntimeError_(
                        "call stack overflow (recursion too deep)",
                        line=getattr(item, "line", 0),
                        col=getattr(item, "col", 0))
        finally:
            self._finish_tasks()

    def run_one_test(self, tdecl: ast.TestDecl):
        """Execute a single `test` block body in a fresh child env.
        A bare `return` counts as a pass. Raises MisaError on failure."""
        env = Env(self.global_env)
        try:
            self.exec_block(tdecl.body, env)
        except _ReturnSignal:
            pass  # early `return` = pass

    # -- statements ---------------------------------------------------

    def exec_stmt(self, s: ast.Node, env: Env):
        if isinstance(s, ast.LetStmt):
            val = self.eval_expr(s.value, env)
            if s.ann is not None and s.ann.is_map \
                    and isinstance(val, dict) \
                    and not isinstance(val, MapValue):
                # annotated empty map: {} parses as an empty record
                val = MapValue(val)
            env.define(s.name, val, s.mutable)
        elif isinstance(s, ast.AssignStmt):
            val = self.eval_expr(s.value, env)
            self.assign_target(s.target, val, env)
        elif isinstance(s, ast.IfStmt):
            branch = s.then if self.truthy(self.eval_expr(s.cond, env)) \
                else s.els
            self.exec_block(branch, Env(env))
        elif isinstance(s, ast.ForStmt):
            it = self.eval_expr(s.iter, env)
            items = self._iterable(it, s)
            for x in items:
                inner = Env(env)
                inner.define(s.var, x, False)
                self.exec_block(s.body, inner)
        elif isinstance(s, ast.WhileStmt):
            while self.truthy(self.eval_expr(s.cond, env)):
                self.exec_block(s.body, Env(env))
        elif isinstance(s, ast.ReturnStmt):
            # Phase 5d: tail-call optimization — `return f(args)`
            # directly in f's own body reuses f's frame instead of
            # recursing (no Python-stack growth). Only when the
            # innermost frame is f itself (not a nested lambda) and
            # the call resolves to that exact decl with matching
            # arity; anything else takes the normal path.
            if s.value is not None:
                tco = _tco_stack()
                if tco and isinstance(s.value, ast.Call) \
                        and s.value.callee is None:
                    kind, ent = tco[-1]
                    if kind == "fn" and self.fns.get(s.value.name) is ent \
                            and len(s.value.args) == len(ent.params):
                        argvs = [self.eval_expr(a, env)
                                 for a in s.value.args]
                        raise _TailCall(ent, argvs)
            val = self.eval_expr(s.value, env) \
                if s.value is not None else None
            raise _ReturnSignal(val)
        elif isinstance(s, ast.TryStmt):
            try:
                self.exec_block(s.body, Env(env))
            except _ReturnSignal:
                raise
            except _TailCall:
                # Phase 5d: TCO is control flow, not an error — never
                # swallowed by a catch handler.
                raise
            except RecursionError:
                # H-5: convert to a catchable E300 so `try/catch` sees a
                # structured error, not a Python traceback. (Handled
                # inline — raising here would skip the handler below.)
                e = RuntimeError_("call stack overflow (recursion too deep)",
                                  line=s.line, col=s.col)
                inner = Env(env)
                inner.define(s.catch_var,
                             {"code": e.code, "message": e.message}, False)
                self.exec_block(s.handler, inner)
            except MisaError as e:
                inner = Env(env)
                inner.define(s.catch_var,
                             {"code": e.code, "message": e.message}, False)
                self.exec_block(s.handler, inner)
        elif isinstance(s, ast.ThrowStmt):
            v = self.eval_expr(s.value, env)
            if not isinstance(v, dict) or "code" not in v \
                    or "message" not in v:
                raise RuntimeError_("throw needs an Error value: a "
                                    "{ code: Text, message: Text } record "
                                    "literal or a caught error",
                                    line=s.line, col=s.col)
            raise ThrownError(str(v["code"]), str(v["message"]),
                             line=s.line, col=s.col)
        elif isinstance(s, ast.OnStmt):
            handle = self.eval_expr(s.stream, env)
            if not isinstance(handle, TokenStreamHandle):
                raise RuntimeError_("'on' needs a stream")
            if s.event != "token":
                raise RuntimeError_(f"unknown stream event '{s.event}'")
            lam = s.handler
            handle.handlers.append(
                LambdaValue([pn for pn, _ in lam.params],
                            lam.body, lam.expr_body, env))
        elif isinstance(s, ast.ExprStmt):
            self.eval_expr(s.expr, env)
        elif isinstance(s, ast.StreamDecl):
            handle = self.make_stream(s.call, env, s)
            env.define(s.name, handle, False)
        else:
            raise RuntimeError_(f"cannot execute {type(s).__name__}")

    def exec_block(self, stmts: list, env: Env):
        for s in stmts:
            self.exec_stmt(s, env)

    def assign_target(self, tgt: ast.Node, val: Any, env: Env):
        if isinstance(tgt, ast.Ident):
            env.assign(tgt.name, val)
        elif isinstance(tgt, ast.Field):
            obj = self.eval_expr(tgt.obj, env)
            if isinstance(obj, dict):
                obj[tgt.field] = val
            else:
                raise RuntimeError_("cannot set field on "
                                    f"{type(obj).__name__}")
        elif isinstance(tgt, ast.Index):
            obj = self.eval_expr(tgt.obj, env)
            idx = self.eval_expr(tgt.idx, env)
            if isinstance(obj, MapValue):
                obj.data[idx] = val
            elif isinstance(obj, list) and isinstance(idx, int):
                obj[idx] = val
            else:
                raise RuntimeError_("cannot index-assign here")
        else:
            raise RuntimeError_("invalid assignment target")

    def _iterable(self, it: Any, node: ast.Node) -> list:
        if isinstance(it, list):
            return it
        if isinstance(it, str):
            return list(it)
        raise RuntimeError_("'for' needs a list or text")

    @staticmethod
    def truthy(v: Any) -> bool:
        if v is None or v is False:
            return False
        if v is True:
            return True
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, (str, list, dict)):
            return len(v) > 0
        return True

    # -- expressions --------------------------------------------------

    def eval_expr(self, e: ast.Node, env: Env) -> Any:
        if isinstance(e, ast.IntLit):
            # C-1: literals wrap like every other int-producing operation
            # (matches AOT, where llvmlite folds out-of-range i64
            # constants to two's complement).
            return wrap_i64(e.value)
        if isinstance(e, ast.FloatLit):
            return e.value
        if isinstance(e, ast.StrLit):
            return "".join(render_value(self.eval_expr(p, env))
                           if isinstance(p, ast.Node) else p
                           for p in e.parts)
        if isinstance(e, ast.BoolLit):
            return e.value
        if isinstance(e, ast.NilLit):
            return None
        if isinstance(e, ast.DurationLit):
            return e.ms
        if isinstance(e, ast.MoneyLit):
            return e.usd
        if isinstance(e, ast.Ident):
            try:
                v = env.lookup(e.name)[0]
                # Screen state: reading a StateCell yields its current
                # value (writes go through Env.assign's cell path).
                if isinstance(v, StateCell):
                    return v.value
                return v
            except UndefinedName:
                # Phase 5a: an `fn` declaration used as a value.
                if e.name in self.fns:
                    return FnValue(self.fns[e.name])
                raise
        if isinstance(e, ast.Call):
            return self.eval_call(e, env)
        if isinstance(e, ast.Field):
            # Phase 5b: bare zero-payload variant, e.g. `Opt.None`.
            if isinstance(e.obj, ast.Ident):
                decl = self.types.get(e.obj.name)
                if decl is not None and decl.is_sum:
                    v = next((x for x in decl.variants
                              if x.name == e.field), None)
                    if v is None:
                        raise RuntimeError_(
                            f"unknown variant '{e.field}' of sum type "
                            f"'{decl.name}'",
                            line=e.line, col=e.col)
                    if v.fields:
                        raise RuntimeError_(
                            f"variant '{decl.name}.{v.name}' needs "
                            f"payload; use "
                            f"'{decl.name}.{v.name}(...)'",
                            line=e.line, col=e.col)
                    return SumValue(decl.name, v.name, [])
            obj = self.eval_expr(e.obj, env)
            # Overtake Wave 2 (FFI): field read on a C-layout struct.
            # Overtake final wave: unions too (ctypes.Union is not a
            # subclass of ctypes.Structure).
            import ctypes as _ct2
            if isinstance(obj, (_ct2.Structure, _ct2.Union)):
                return self.struct_field_get(obj, e.field, e)
            # Phase 8 (overtake): field access on an interface value
            # is E200 statically; this is the runtime backstop.
            if isinstance(obj, IfaceValue):
                raise RuntimeError_(
                    f"cannot access field '.{e.field}' on interface "
                    f"'{obj.iface}' value",
                    line=e.line, col=e.col)
            if isinstance(obj, dict):
                if e.field not in obj:
                    raise RuntimeError_(f"no field '{e.field}'",
                                            line=e.line, col=e.col)
                return obj[e.field]
            raise RuntimeError_("cannot access field on "
                                f"{type(obj).__name__}",
                                line=e.line, col=e.col)
        if isinstance(e, ast.Match):
            return self.eval_match(e, env)
        if isinstance(e, ast.Index):
            obj = self.eval_expr(e.obj, env)
            idx = self.eval_expr(e.idx, env)
            if isinstance(obj, MapValue):
                if idx in obj.data:
                    return obj.data[idx]
                raise MapKeyError(f"missing map key {idx!r}",
                                    line=e.line, col=e.col)
            if isinstance(obj, (list, str)) and isinstance(idx, int):
                try:
                    return obj[idx]
                except IndexError:
                    raise RuntimeError_("index out of range",
                                            line=e.line, col=e.col)
            raise RuntimeError_("cannot index here",
                                    line=e.line, col=e.col)
        if isinstance(e, ast.ListLit):
            return [self.eval_expr(i, env) for i in e.items]
        # Phase 8 (overtake): interface conversions (checker-inserted).
        if isinstance(e, ast.IfaceWrap):
            return IfaceValue(e.iface, e.concrete,
                              self.eval_expr(e.expr, env))
        if isinstance(e, ast.IfaceWrapList):
            return [IfaceValue(e.iface, e.concrete, v)
                    for v in self.eval_expr(e.expr, env)]
        if isinstance(e, ast.MapLit):
            return MapValue({k: self.eval_expr(v, env)
                             for k, v in e.entries})
        if isinstance(e, ast.RecordLit):
            return {k: self.eval_expr(v, env) for k, v in e.fields}
        if isinstance(e, ast.StructLit):
            return self.eval_struct_lit(e, env)
        if isinstance(e, ast.Lambda):
            return LambdaValue([pn for pn, _ in e.params],
                               e.body, e.expr_body, env)
        if isinstance(e, ast.BinOp):
            return self.eval_binop(e, env)
        if isinstance(e, ast.UnOp):
            v = self.eval_expr(e.operand, env)
            if e.op == "not":
                return not self.truthy(v)
            if e.op == "-":
                return wrap_i64(-v) if isinstance(v, int) else -v
            raise RuntimeError_(f"unknown unary {e.op}",
                                    line=e.line, col=e.col)
        if isinstance(e, ast.Await):
            handle = self.eval_expr(e.operand, env)
            if isinstance(handle, TaskHandle):
                ms = self.eval_expr(e.timeout, env) \
                    if e.timeout is not None else None
                return self.await_task(handle, e, ms)
            if not isinstance(handle, TokenStreamHandle):
                raise RuntimeError_("'await' needs a stream",
                                        line=e.line, col=e.col)
            return self.drain_stream(handle, env)
        if isinstance(e, ast.Spawn):
            # Phase 7 (concurrency): returns a TaskHandle immediately;
            # the body runs on its own thread (see spawn_task).
            return self.spawn_task(e.body, env, e)
        if isinstance(e, ast.StreamDecl):
            handle = self.make_stream(e.call, env, e)
            env.define(e.name, handle, False)
            return handle
        raise RuntimeError_(f"cannot evaluate {type(e).__name__}")

    def eval_binop(self, e: ast.BinOp, env: Env) -> Any:
        op = e.op
        if op == "and":
            return self.eval_expr(e.right, env) \
                if self.truthy(self.eval_expr(e.left, env)) else False
        if op == "or":
            lv = self.eval_expr(e.left, env)
            return lv if self.truthy(lv) else self.eval_expr(e.right, env)
        l = self.eval_expr(e.left, env)
        r = self.eval_expr(e.right, env)
        # C-6: runtime type guards (match AOT's E200); the checker catches
        # static cases, this catches dynamic values.
        if op == "+":
            if isinstance(l, str) or isinstance(r, str):
                return render_value(l) + render_value(r)
            if isinstance(l, list) and isinstance(r, list):
                return l + r
            if isinstance(l, int) and isinstance(r, int):
                return wrap_i64(l + r)
            if isinstance(l, (int, float)) and isinstance(r, (int, float)):
                # bool is int subclass; exclude to match checker strictness
                if isinstance(l, bool) or isinstance(r, bool):
                    raise TypeMismatch(
                        f"operator '+' cannot be applied to "
                        f"{type(l).__name__} and {type(r).__name__}")
                return l + r
            raise TypeMismatch(
                f"operator '+' cannot be applied to "
                f"{type(l).__name__} and {type(r).__name__}")
        if op in ("-", "*", "/", "%"):
            if not isinstance(l, (int, float)) or not isinstance(r, (int, float)) \
                    or isinstance(l, bool) or isinstance(r, bool):
                raise TypeMismatch(
                    f"operator '{op}' cannot be applied to "
                    f"{type(l).__name__} and {type(r).__name__}")
        if op == "-":
            if isinstance(l, int) and isinstance(r, int):
                return wrap_i64(l - r)
            return l - r
        if op == "*":
            if isinstance(l, int) and isinstance(r, int):
                return wrap_i64(l * r)
            return l * r
        if op == "/":
            # C-5: clean E300, never a Python traceback (and catchable).
            if isinstance(l, int) and isinstance(r, int):
                if r == 0:
                    raise RuntimeError_("division by zero",
                                         line=e.line, col=e.col)
                # Truncating integer division (LLVM sdiv semantics),
                # matching the checker (Int/Int -> Int) and the AOT
                # backend. The interpreter previously did float
                # division here -- a three-way divergence.
                if l == -(1 << 63) and r == -1:
                    return wrap_i64(l)  # wraps to INT64_MIN
                q = abs(l) // abs(r)
                return wrap_i64(-q if (l < 0) != (r < 0) else q)
            try:
                return l / r
            except ZeroDivisionError:
                raise RuntimeError_("division by zero",
                                     line=e.line, col=e.col)
        if op == "%":
            if isinstance(l, int) and isinstance(r, int):
                if r == 0:
                    raise RuntimeError_("integer modulo by zero",
                                         line=e.line, col=e.col)
                # srem semantics (sign follows dividend), matching AOT.
                # Python's % floors (sign follows divisor) -- diverged.
                m = abs(l) % abs(r)
                return wrap_i64(-m if l < 0 else m)
            try:
                return l % r
            except ZeroDivisionError:
                raise RuntimeError_("integer modulo by zero",
                                     line=e.line, col=e.col)
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        if op in ("<", "<=", ">", ">="):
            # C-6: runtime guard matching AOT's val_cmp (E200): both
            # numeric or both Text; the checker catches static cases,
            # this catches dynamic values. Never a TypeError traceback.
            def _num(x):
                return isinstance(x, (int, float)) \
                    and not isinstance(x, bool)
            if not ((_num(l) and _num(r))
                    or (isinstance(l, str) and isinstance(r, str))):
                raise TypeMismatch(
                    f"operator '{op}' cannot be applied to "
                    f"{type(l).__name__} and {type(r).__name__}")
            if op == "<":
                return l < r
            if op == "<=":
                return l <= r
            if op == ">":
                return l > r
            return l >= r
        raise RuntimeError_(f"unknown operator '{op}'")

    # -- Phase 5b: match ------------------------------------------------

    def eval_match(self, e: ast.Match, env: Env) -> Any:
        sv = self.eval_expr(e.scrutinee, env)
        if not isinstance(sv, SumValue):
            raise RuntimeError_("match scrutinee is not a sum value")
        for arm in e.arms:
            if arm.wildcard or (arm.type_name == sv.type_name
                                and arm.variant == sv.variant):
                child = Env(env)
                for bname, val in zip(arm.binds, sv.fields):
                    child.define(bname, val, False)
                # Phase 5d: `if` guard — the arm fires only when the
                # guard is true; otherwise try the next arm.
                if arm.guard is not None:
                    gv = self.eval_expr(arm.guard, child)
                    if gv is not True and gv is not False:
                        raise RuntimeError_(
                            "match guard must evaluate to Bool",
                            line=arm.guard.line, col=arm.guard.col)
                    if not gv:
                        continue
                return self.eval_expr(arm.body, child)
        raise RuntimeError_(
            f"non-exhaustive match on {sv.type_name}.{sv.variant}")

    # -- calls --------------------------------------------------------

    def eval_call(self, e: ast.Call, env: Env) -> Any:
        args = [self.eval_expr(a, env) for a in e.args]
        # Phase 5a: calling the result of an expression, e.g. `mk(9)(1)`.
        if e.callee is not None:
            v = self.eval_expr(e.callee, env)
            if isinstance(v, LambdaValue):
                return self.invoke_lambda_value(v, args, e)
            if isinstance(v, FnValue):
                return self.call_fn(v.decl, args, e)
            raise RuntimeError_(f"call result is not callable",
                                line=e.line, col=e.col)
        name = e.name
        # M-4: `<handle>.exec/query/close(...)` where the handle variable
        # holds a DbHandle, regardless of its name. (The checker rewrites
        # the checked AST; this covers ASTs that bypass checking.)
        if name not in ("db.exec", "db.query", "db.close",
                        "net.accept", "net.port", "listener.close",
                        "socket.send", "socket.recv", "socket.close",
                        "socket.set_timeout") \
                and "." in name:
            recv, _, field = name.partition(".")
            if field in ("exec", "query", "close"):
                try:
                    rv = env.lookup(recv)[0]
                except UndefinedName:
                    rv = None
                if isinstance(rv, DbHandle):
                    name = "db." + field
                elif isinstance(rv, ListenerHandle) and field == "close":
                    # Wave 3: `lis.close()`.
                    name = "listener.close"
                    args = [rv] + args
                elif isinstance(rv, SocketHandle) and field == "close":
                    # Wave 3: `sock.close()`.
                    name = "socket.close"
                    args = [rv] + args
            elif field in ("send", "recv", "close") \
                    and recv != "chan":
                # Phase 7: `<ch>.send/.recv/.close(...)` where the
                # variable holds a Channel (the checker rewrites the
                # checked AST to the canonical chan.send/chan.recv/close
                # names; this covers ASTs that bypass checking). The
                # canonical names themselves pass through untouched.
                try:
                    rv = env.lookup(recv)[0]
                except UndefinedName:
                    rv = None
                if isinstance(rv, Channel):
                    name = {"send": "chan.send", "recv": "chan.recv",
                            "close": "close"}[field]
                    args = [rv] + args
                elif isinstance(rv, TextBuilder) \
                        and field in ("append", "build"):
                    # Text.Builder: `b.append(s)` / `b.build()` where the
                    # variable holds a TextBuilder (checker rewrites the
                    # checked AST; this covers ASTs that bypass checking).
                    name = "builder." + field
                    args = [rv] + args
                elif isinstance(rv, SocketHandle) \
                        and field in ("send", "recv", "close"):
                    # Wave 3: `sock.send(d)` / `sock.recv(n)` /
                    # `sock.close()` where the variable holds a
                    # SocketHandle.
                    name = "socket." + field
                    args = [rv] + args
            elif field in ("add", "contains", "remove", "union",
                           "intersect", "diff", "to_list", "len"):
                # Wave 3 (stdlib): `s.add(x)` etc. where the variable
                # holds a SetValue (the checker rewrites the checked
                # AST; this covers ASTs that bypass checking).
                try:
                    rv = env.lookup(recv)[0]
                except UndefinedName:
                    rv = None
                if isinstance(rv, SetValue):
                    name = "set." + field
                    args = [rv] + args
            elif field in ("accept", "port", "set_timeout"):
                # Wave 3: `lis.accept()` / `lis.port()` /
                # `sock.set_timeout(ms)` — fields no other handle type
                # uses, so the receiver type alone decides. (The checker
                # rejects cross-type uses on the checked path.)
                try:
                    rv = env.lookup(recv)[0]
                except UndefinedName:
                    rv = None
                if isinstance(rv, ListenerHandle) \
                        and field in ("accept", "port"):
                    name = {"accept": "net.accept",
                            "port": "net.port"}[field]
                    args = [rv] + args
                elif isinstance(rv, SocketHandle) \
                        and field == "set_timeout":
                    name = "socket.set_timeout"
                    args = [rv] + args
            elif field in ("flag", "bool_flag", "short", "command",
                           "parse", "get", "get_bool", "positionals"):
                # cli: `p.flag(..)` / `p.parse(..)` where the variable
                # holds a CliParserValue, and `args.get(..)` /
                # `args.command()` / ... where it holds a CliArgsValue
                # (the checker rewrites the checked AST; this covers
                # ASTs that bypass checking).
                try:
                    rv = env.lookup(recv)[0]
                except UndefinedName:
                    rv = None
                if isinstance(rv, CliParserValue) \
                        and field in ("flag", "bool_flag", "short",
                                      "command", "parse"):
                    name = "cli." + field
                    args = [rv] + args
                elif isinstance(rv, CliArgsValue) \
                        and field in ("get", "get_bool", "command",
                                      "positionals"):
                    name = {"command": "cli.get_command"}.get(
                        field, "cli." + field)
                    args = [rv] + args
        if name in self.models:
            return self.call_model(self.models[name], args, e)
        if name in self.screens:
            return ("screen", name, args)
        # Phase 5b: sum variant construction `Type.Variant(args)`.
        if "." in name:
            head, _, var = name.partition(".")
            decl = self.types.get(head)
            if decl is not None and decl.is_sum:
                v = next((x for x in decl.variants if x.name == var),
                         None)
                if v is None:
                    raise RuntimeError_(
                        f"unknown variant '{var}' of sum type '{head}'")
                if len(args) != len(v.fields):
                    raise RuntimeError_(
                        f"variant '{head}.{var}' takes "
                        f"{len(v.fields)} args, got {len(args)}")
                return SumValue(head, var, args)
        if name in self.fns:
            return self.call_fn(self.fns[name], args, e)
        # Phase 8 (overtake): interface method dispatch. `iface.I.m`
        # is dynamic (receiver is an IfaceValue); `impl.I.T.m` is a
        # static call to the impl method with the payload as `self`.
        if name.startswith("iface.") and name.count(".") == 2:
            _p, iname, mname = name.split(".")
            recv = args[0]
            if not isinstance(recv, IfaceValue):
                raise RuntimeError_(
                    f"interface dispatch needs an interface value, "
                    f"got {type(recv).__name__}",
                    line=e.line, col=e.col)
            impl = self.impls.get((iname, recv.concrete))
            fn = next((m for m in impl.methods if m.name == mname),
                      None) if impl else None
            if fn is None:
                raise RuntimeError_(
                    f"no impl of '{iname}' for '{recv.concrete}' "
                    f"providing '{mname}'",
                    line=e.line, col=e.col)
            return self.call_fn(fn, [recv.payload] + args[1:], e)
        if name.startswith("impl.") and name.count(".") == 3:
            _p, iname, tgt, mname = name.split(".")
            impl = self.impls.get((iname, tgt))
            fn = next((m for m in impl.methods if m.name == mname),
                      None) if impl else None
            if fn is None:
                raise RuntimeError_(
                    f"no impl of '{iname}' for '{tgt}' providing "
                    f"'{mname}'", line=e.line, col=e.col)
            return self.call_fn(fn, args, e)
        # Phase 7: extern (FFI) function calls.
        if name in self.extern_fns:
            return self.call_extern(name, args, e)
        # Phase 5c: generic record construction `Box<Int>(42)`.
        # Erasure: type args are checking-time only; the value is a
        # plain record dict like any other.
        if name in self.types:
            decl = self.types[name]
            if decl.type_params and not decl.is_sum:
                if len(args) != len(decl.fields):
                    raise RuntimeError_(
                        f"'{name}' takes {len(decl.fields)} field "
                        f"arg(s), got {len(args)}")
                return {fn: a for (fn, _), a in zip(decl.fields, args)}
        if name == "navigate":
            return self.do_navigate(args, e)
        if name in ("Photo", "Audio", "Video", "Document"):
            if len(args) != 1 or not isinstance(args[0], str):
                raise RuntimeError_(f"{name}(path) needs a text path")
            return MediaHandle(name, args[0])
        if name == "fs.read":
            try:
                with open(args[0], encoding="utf-8") as f:
                    return f.read()
            except OSError as ex:
                raise IOError_(f"fs.read failed: cannot open '{args[0]}'",
                               line=e.line, col=e.col)
        if name == "fs.write":
            try:
                with open(args[0], "w", encoding="utf-8") as f:
                    f.write(args[1])
                return None
            except OSError as ex:
                raise IOError_(f"fs.write failed: cannot open '{args[0]}'",
                               line=e.line, col=e.col)
        if name == "http.get":
            return self.http_get(args[0], e)
        # -- Phase 5 (stdlib2): JSON, HTTP POST, time, math --------------
        if name == "json.parse":
            return self.json_parse(args[0], e)
        if name == "json.stringify":
            return self.json_stringify(args[0], e)
        if name == "http.post":
            return self.http_post(args[0], args[1], "text/plain", e)
        if name == "http.post_json":
            body = self.json_stringify(args[1], e)
            return self.http_post(args[0], body, "application/json", e)
        # -- Wave 3 (networking): TCP sockets + HTTP server ------------
        if name == "net.connect":
            return self.net_connect(args[0], args[1], e)
        if name == "net.listen":
            return self.net_listen(args[0], e)
        if name == "net.accept":
            return self.net_accept(args[0], e)
        if name == "net.port":
            return self.net_port(args[0], e)
        if name == "listener.close":
            return self.listener_close(args[0], e)
        if name == "socket.send":
            return self.socket_send(args[0], args[1], e)
        if name == "socket.recv":
            return self.socket_recv(args[0], args[1], e)
        if name == "socket.close":
            return self.socket_close(args[0], e)
        if name == "socket.set_timeout":
            return self.socket_set_timeout(args[0], args[1], e)
        if name == "http.serve":
            return self.http_serve(args[0], args[1], e)
        if name == "time.now":
            return int(time.time())
        if name == "time.now_ms":
            return int(time.time() * 1000)
        if name == "time.format":
            return self.time_format(args[0], args[1], e)
        if name == "time.parse":
            return self.time_parse(args[0], args[1], e)
        if name == "math.sqrt":
            return self.math_unary("sqrt", args[0], e, math.sqrt)
        if name == "math.pow":
            return self.math_binary("pow", args[0], args[1], e)
        if name == "math.abs":
            v = args[0]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise RuntimeError_(
                    "math.abs(Float) needs a numeric arg",
                    line=e.line, col=e.col)
            return abs(v)
        if name == "math.floor":
            return self.math_to_int("floor", args[0], e, math.floor)
        if name == "math.ceil":
            return self.math_to_int("ceil", args[0], e, math.ceil)
        if name == "math.round":
            return self.math_to_int("round", args[0], e,
                                    self._round_half_away)
        # -- Phase 5 (DB): SQLite bindings ------------------------------
        if name == "builder":
            return TextBuilder(parts=[])
        if name == "builder.append":
            b, s = args[0], args[1]
            if not isinstance(b, TextBuilder):
                raise TypeMismatch("builder.append needs a Builder "
                                   "(from builder())",
                                   line=e.line, col=e.col)
            b.parts.append(render_value(s))
            return None
        if name == "builder.build":
            b = args[0]
            if not isinstance(b, TextBuilder):
                raise TypeMismatch("builder.build needs a Builder "
                                   "(from builder())",
                                   line=e.line, col=e.col)
            return "".join(b.parts)
        if name == "db.open":
            return self.db_open(args[0], e)
        if name == "db.exec":
            return self.db_exec(args[0], args[1],
                                args[2] if len(args) > 2 else [], e)
        if name == "db.exec_many":
            return self.db_exec_many(args[0], args[1], args[2], e)
        if name == "db.begin":
            return self.db_begin(args[0], e)
        if name == "db.commit":
            return self.db_commit(args[0], e)
        if name == "db.rollback":
            return self.db_rollback(args[0], e)
        if name == "db.query":
            return self.db_query(args[0], args[1],
                                 args[2] if len(args) > 2 else [], e)
        if name == "db.close":
            return self.db_close(args[0], e)
        # -- Phase 6 prerequisites: str namespace + process.run ---------
        if name == "str.slice":
            s, i, j = args[0], args[1], args[2]
            if not isinstance(s, str) or not isinstance(i, int) \
                    or not isinstance(j, int):
                raise RuntimeError_(
                    "str.slice(Text, Int, Int) needs text + ints")
            n = len(s)
            i = max(0, min(i, n))
            j = max(0, min(j, n))
            return s[i:j] if i <= j else ""
        if name == "str.starts_with":
            if not isinstance(args[0], str) \
                    or not isinstance(args[1], str):
                raise RuntimeError_(
                    "str.starts_with(Text, Text) needs text args")
            return args[0].startswith(args[1])
        if name == "str.split":
            if not isinstance(args[0], str) \
                    or not isinstance(args[1], str):
                raise RuntimeError_(
                    "str.split(Text, Text) needs text args")
            if args[1] == "":
                raise RuntimeError_("str.split separator is empty")
            return args[0].split(args[1])
        if name == "str.chars":
            if not isinstance(args[0], str):
                raise RuntimeError_(
                    "str.chars(Text) needs a text arg")
            # list(str) splits by code point — matches `for x in s`
            # and the AOT misa_str_chars.
            return list(args[0])
        if name == "str.trim":
            if not isinstance(args[0], str):
                raise RuntimeError_("str.trim(Text) needs a text arg")
            return args[0].strip()
        if name == "str.ord":
            if not isinstance(args[0], str) or not args[0]:
                raise RuntimeError_(
                    "str.ord(Text) needs a non-empty text arg")
            return ord(args[0][0])
        if name == "str.chr":
            if not isinstance(args[0], int):
                raise RuntimeError_("str.chr(Int) needs an int arg")
            cp = args[0]
            # chr() allows lone surrogates, which then crash on output;
            # reject them here for a clean E300 (mirrors the AOT runtime).
            if cp < 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                raise RuntimeError_(
                    f"str.chr: invalid code point {cp}")
            return chr(cp)
        if name == "process.run":
            return self.process_run(args[0], e)
        # -- Wave 3 (stdlib expansion) ----------------------------------
        if name == "regex":
            if not isinstance(args[0], str):
                raise RuntimeError_("regex(Text) needs a text arg")
            return RegexValue(args[0])
        if name in ("regex.matches", "regex.find", "regex.find_all",
                    "regex.replace", "regex.captures"):
            rx, text = args[0], args[1]
            if not isinstance(rx, RegexValue) \
                    or not isinstance(text, str):
                raise RuntimeError_(
                    f"{name}(Regex, Text) needs a regex and text")
            if name == "regex.matches":
                return rx.rx.matches(text)
            if name == "regex.find":
                return rx.rx.find(text)
            if name == "regex.find_all":
                return rx.rx.find_all(text)
            if name == "regex.captures":
                return rx.rx.captures(text)
            repl = args[2]
            if not isinstance(repl, str):
                raise RuntimeError_(
                    "regex.replace(Regex, Text, Text) needs text args")
            return rx.rx.replace(text, repl)
        if name in ("crypto.sha256", "crypto.sha1", "crypto.md5"):
            if not isinstance(args[0], str):
                raise RuntimeError_(f"{name}(Text) needs a text arg")
            data = args[0].encode("utf-8")
            algo = name.split(".")[1]
            return hashlib.new(algo, data).hexdigest()
        if name == "crypto.hmac_sha256":
            key, msg = args[0], args[1]
            if not isinstance(key, str) or not isinstance(msg, str):
                raise RuntimeError_(
                    "crypto.hmac_sha256(Text, Text) needs text args")
            return hmac.new(key.encode("utf-8"),
                            msg.encode("utf-8"),
                            hashlib.sha256).hexdigest()
        if name == "random.int":
            lo, hi = args[0], args[1]
            if not isinstance(lo, int) or not isinstance(hi, int):
                raise RuntimeError_(
                    "random.int(Int, Int) needs int args")
            if lo > hi:
                raise RuntimeError_(
                    f"random.int: min ({lo}) > max ({hi})")
            return _rng.randint(lo, hi)
        if name == "random.float":
            return _rng.random()
        if name == "random.choice":
            xs = args[0]
            if not isinstance(xs, list):
                raise RuntimeError_(
                    "random.choice([T]) needs a list")
            if not xs:
                raise RuntimeError_(
                    "random.choice: cannot choose from an empty list")
            return _rng.choice(xs)
        if name == "random.shuffle":
            xs = args[0]
            if not isinstance(xs, list):
                raise RuntimeError_(
                    "random.shuffle([T]) needs a list")
            out = list(xs)
            _rng.shuffle(out)
            return out
        if name == "random.seed":
            if not isinstance(args[0], int):
                raise RuntimeError_("random.seed(Int) needs an int")
            _rng.seed(args[0])
            return None
        if name == "time.now_utc":
            return int(time.time())
        if name == "time.to_utc":
            # Epoch timestamps are always UTC; identity, for clarity.
            return args[0]
        if name == "time.from_parts":
            parts = args
            if not all(isinstance(p, int) for p in parts):
                raise RuntimeError_(
                    "time.from_parts needs int args")
            y, mo, d, h, mi, s = parts
            try:
                t = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
            except (OverflowError, ValueError):
                raise RuntimeError_(
                    "time.from_parts: invalid date")
            back = time.gmtime(t)
            if (back.tm_year, back.tm_mon, back.tm_mday,
                    back.tm_hour, back.tm_min,
                    back.tm_sec) != (y, mo, d, h, mi, s):
                raise RuntimeError_(
                    "time.from_parts: invalid date")
            return t
        if name == "time.add":
            return args[0] + args[1]
        if name == "time.add_hours":
            return args[0] + args[1] * 3600
        if name == "time.in_tz":
            # Shift the epoch so UTC formatting shows wall-clock time
            # at the given offset (fractional hours supported).
            return args[0] + int(args[1] * 3600)
        if name == "time.weekday":
            wd = time.gmtime(args[0]).tm_wday
            return ("Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday")[wd]
        if name == "time.is_leap":
            return calendar.isleap(args[0])
        if name == "fs.temp_file":
            try:
                fd, path = tempfile.mkstemp(prefix="misa-")
                os.close(fd)
                return path
            except OSError as ex:
                raise RuntimeError_(f"fs.temp_file failed: {ex}")
        if name == "fs.temp_dir":
            try:
                return tempfile.mkdtemp(prefix="misa-")
            except OSError as ex:
                raise RuntimeError_(f"fs.temp_dir failed: {ex}")
        # -- Final wave (stdlib): compression -------------------------
        # Framing is manual (raw deflate + fixed header/trailer) so
        # output is byte-identical to the AOT backend by construction.
        # Header: 1f 8b 08 00 00000000 02 03 (mtime=0, XFL=max,
        # OS=Unix); level 9; CRC32 + ISIZE (LE) trailer.
        if name == "compress.gzip":
            if not isinstance(args[0], str):
                raise RuntimeError_(
                    "compress.gzip(Text) needs a text arg")
            return BytesValue(_gzip_framed(args[0].encode("utf-8")))
        if name in ("compress.gunzip", "compress.gunzip_bytes"):
            if not isinstance(args[0], BytesValue):
                raise RuntimeError_(
                    f"{name}(Bytes) needs a bytes arg")
            try:
                raw = gzip.decompress(args[0].data)
            except (gzip.BadGzipFile, zlib.error, EOFError,
                    OSError, ValueError) as ex:
                raise RuntimeError_(
                    f"{name} failed: corrupt gzip data ({ex})")
            if name == "compress.gunzip_bytes":
                return BytesValue(raw)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as ex:
                raise RuntimeError_(
                    f"compress.gunzip failed: output is not UTF-8 text "
                    f"({ex}); use compress.gunzip_bytes for binary data")
        if name == "compress.gzip_file":
            src, dst = args[0], args[1]
            if not isinstance(src, str) or not isinstance(dst, str):
                raise RuntimeError_(
                    "compress.gzip_file(Text, Text) needs text args")
            try:
                comp = zlib.compressobj(9, zlib.DEFLATED, -15)
                crc, isize = 0, 0
                with open(src, "rb") as fin, \
                        open(dst, "wb") as fout:
                    fout.write(_GZ_HDR)
                    while True:
                        chunk = fin.read(65536)
                        if not chunk:
                            break
                        crc = zlib.crc32(chunk, crc)
                        isize += len(chunk)
                        fout.write(comp.compress(chunk))
                    fout.write(comp.flush())
                    fout.write((crc & 0xFFFFFFFF).to_bytes(4, "little"))
                    fout.write((isize & 0xFFFFFFFF).to_bytes(4,
                                                             "little"))
                return os.path.getsize(dst)
            except OSError as ex:
                raise IOError_(
                    f"compress.gzip_file failed: {ex}",
                    line=e.line, col=e.col)
        if name == "compress.gunzip_file":
            src, dst = args[0], args[1]
            if not isinstance(src, str) or not isinstance(dst, str):
                raise RuntimeError_(
                    "compress.gunzip_file(Text, Text) needs text args")
            try:
                with gzip.open(src, "rb") as fin, \
                        open(dst, "wb") as fout:
                    while True:
                        chunk = fin.read(65536)
                        if not chunk:
                            break
                        fout.write(chunk)
                return os.path.getsize(dst)
            except (gzip.BadGzipFile, zlib.error, EOFError) as ex:
                raise RuntimeError_(
                    f"compress.gunzip_file failed: corrupt gzip data "
                    f"({ex})")
            except OSError as ex:
                raise IOError_(
                    f"compress.gunzip_file failed: {ex}",
                    line=e.line, col=e.col)
        if name == "set":
            return SetValue()
        if name in ("set.add", "set.contains", "set.remove",
                    "set.union", "set.intersect", "set.diff",
                    "set.to_list", "set.len"):
            s = args[0]
            if not isinstance(s, SetValue):
                raise RuntimeError_(f"{name} needs a Set")
            if name == "set.add":
                if not any(_misa_eq(x, args[1]) for x in s.items):
                    s.items.append(args[1])
                return None
            if name == "set.contains":
                return any(_misa_eq(x, args[1]) for x in s.items)
            if name == "set.remove":
                s.items = [x for x in s.items
                           if not _misa_eq(x, args[1])]
                return None
            if name == "set.to_list":
                return list(s.items)
            if name == "set.len":
                return len(s.items)
            other = args[1]
            if not isinstance(other, SetValue):
                raise RuntimeError_(f"{name} needs two Sets")
            if name == "set.union":
                out = SetValue(s.items)
                for x in other.items:
                    if not any(_misa_eq(y, x) for y in out.items):
                        out.items.append(x)
                return out
            if name == "set.intersect":
                return SetValue([x for x in s.items
                                 if any(_misa_eq(x, y)
                                       for y in other.items)])
            # set.diff
            return SetValue([x for x in s.items
                             if not any(_misa_eq(x, y)
                                       for y in other.items)])
        # -- cli: declarative args parsing -----------------------------
        if name == "cli.parser":
            if not isinstance(args[0], str) or not isinstance(args[1], str):
                raise RuntimeError_("cli.parser(Text, Text) needs text args")
            return CliParserValue(args[0], args[1])
        if name in ("cli.flag", "cli.bool_flag", "cli.short",
                    "cli.command"):
            p = args[0]
            if not isinstance(p, CliParserValue):
                raise RuntimeError_(f"{name} needs a CliParser")
            lc, cc = e.line, e.col
            if name == "cli.flag":
                fname, metavar, fhelp = args[1], args[2], args[3]
                default = args[4] if len(args) > 4 else None
                if p.find_flag(fname):
                    raise RuntimeError_(f"duplicate flag '{fname}'",
                                        line=lc, col=cc)
                p.flags.append({"name": fname, "metavar": metavar,
                                "help": fhelp, "is_bool": False,
                                "default": default, "short": None})
                return None
            if name == "cli.bool_flag":
                fname, fhelp = args[1], args[2]
                if p.find_flag(fname):
                    raise RuntimeError_(f"duplicate flag '{fname}'",
                                        line=lc, col=cc)
                p.flags.append({"name": fname, "metavar": None,
                                "help": fhelp, "is_bool": True,
                                "default": None, "short": None})
                return None
            if name == "cli.short":
                fname, sh = args[1], args[2]
                f = p.find_flag(fname)
                if f is None:
                    raise RuntimeError_(
                        f"cli.short: no such flag '{fname}'",
                        line=lc, col=cc)
                if len(sh) != 1:
                    raise RuntimeError_(
                        f"cli.short: short flag must be one character, "
                        f"got '{sh}'", line=lc, col=cc)
                if sh == "h":
                    raise RuntimeError_(
                        "cli.short: '-h' is reserved for --help",
                        line=lc, col=cc)
                o = p.find_short(sh)
                if o is not None and o is not f:
                    raise RuntimeError_(
                        f"cli.short: '-{sh}' is already the short flag "
                        f"for '--{o['name']}'", line=lc, col=cc)
                f["short"] = sh
                return None
            # cli.command
            cname, chelp = args[1], args[2]
            if p.find_command(cname):
                raise RuntimeError_(f"duplicate command '{cname}'",
                                    line=lc, col=cc)
            p.commands.append({"name": cname, "help": chelp})
            return None
        if name == "cli.parse":
            p, argv = args[0], args[1]
            if not isinstance(p, CliParserValue):
                raise RuntimeError_("cli.parse needs a CliParser")
            if not isinstance(argv, list):
                raise RuntimeError_("cli.parse needs an argv list")
            return self._cli_parse(p, argv, e)
        if name in ("cli.get", "cli.get_bool"):
            a, fname = args[0], args[1]
            if a is None:
                raise RuntimeError_(
                    "no parsed arguments (parse returned nil, "
                    "likely --help)", line=e.line, col=e.col)
            if not isinstance(a, CliArgsValue):
                raise RuntimeError_(f"{name} needs CliArgs")
            f = a.parser.find_flag(fname)
            if f is None:
                raise RuntimeError_(
                    f"unknown flag '{fname}' (not defined on this parser)",
                    line=e.line, col=e.col)
            if name == "cli.get_bool":
                if not f["is_bool"]:
                    raise RuntimeError_(
                        f"flag '{fname}' takes a value; use get, not "
                        f"get_bool", line=e.line, col=e.col)
                return fname in a.bools
            if f["is_bool"]:
                raise RuntimeError_(
                    f"flag '{fname}' is a bool flag; use get_bool, not get",
                    line=e.line, col=e.col)
            if fname in a.values:
                return a.values[fname]
            if f["default"] is not None:
                return f["default"]
            raise RuntimeError_(
                f"flag '--{fname}' was not provided and has no default",
                line=e.line, col=e.col)
        if name == "cli.get_command":
            a = args[0]
            if a is None:
                return None
            if not isinstance(a, CliArgsValue):
                raise RuntimeError_("cli.get_command needs CliArgs")
            return a.command
        if name == "cli.positionals":
            a = args[0]
            if a is None:
                return []
            if not isinstance(a, CliArgsValue):
                raise RuntimeError_("cli.positionals needs CliArgs")
            return list(a.positionals)
        if name in ("map", "filter", "fold", "sort_by"):
            xs = args[0]
            if not isinstance(xs, list):
                raise RuntimeError_(f"{name} needs a list")
            if name == "map":
                f = args[1]
                return [self._call_fn_value(f, [x], e) for x in xs]
            if name == "filter":
                f = args[1]
                return [x for x in xs
                        if self._call_fn_value(f, [x], e) is True]
            if name == "fold":
                f, acc = args[2], args[1]
                for x in xs:
                    acc = self._call_fn_value(f, [acc, x], e)
                return acc
            # sort_by: stable sort via the `less(a, b)` comparator.
            f = args[1]

            def _cmp(a, b, _f=f):
                if self._call_fn_value(_f, [a, b], e) is True:
                    return -1
                if self._call_fn_value(_f, [b, a], e) is True:
                    return 1
                return 0

            return sorted(xs, key=functools.cmp_to_key(_cmp))
        if name == "list.flat":
            xss = args[0]
            if not all(isinstance(xs, list) for xs in xss):
                raise RuntimeError_("list.flat([[T]]) needs a "
                                    "list of lists")
            return [x for xs in xss for x in xs]
        if name == "list.zip":
            xs, ys = args[0], args[1]
            if not isinstance(xs, list) or not isinstance(ys, list):
                raise RuntimeError_("list.zip needs two lists")
            return [[x, y] for x, y in zip(xs, ys)]
        if name == "list.unique":
            xs = args[0]
            if not isinstance(xs, list):
                raise RuntimeError_("list.unique([T]) needs a list")
            out = []
            for x in xs:
                if not any(_misa_eq(x, y) for y in out):
                    out.append(x)
            return out
        if name == "str.join":
            xs, sep = args[0], args[1]
            if not isinstance(xs, list) or not isinstance(sep, str) \
                    or not all(isinstance(x, str) for x in xs):
                raise RuntimeError_(
                    "str.join([Text], Text) needs text args")
            return sep.join(xs)
        if name == "str.replace":
            s, old, new = args[0], args[1], args[2]
            if not all(isinstance(a, str) for a in (s, old, new)):
                raise RuntimeError_(
                    "str.replace(Text, Text, Text) needs text args")
            if old == "":
                raise RuntimeError_(
                    "str.replace: search string is empty")
            return s.replace(old, new)
        if name == "str.to_upper":
            s = args[0]
            if not isinstance(s, str):
                raise RuntimeError_("str.to_upper(Text) needs text")
            # v1: ASCII only (documented); full Unicode casefold is
            # future work.
            return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c
                           for c in s)
        if name == "str.to_lower":
            s = args[0]
            if not isinstance(s, str):
                raise RuntimeError_("str.to_lower(Text) needs text")
            return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c
                           for c in s)
        if name == "str.ends_with":
            if not isinstance(args[0], str) \
                    or not isinstance(args[1], str):
                raise RuntimeError_(
                    "str.ends_with(Text, Text) needs text args")
            return args[0].endswith(args[1])
        if name in ("str.pad_left", "str.pad_right"):
            s, width, pad = args[0], args[1], args[2]
            if not isinstance(s, str) or not isinstance(width, int) \
                    or not isinstance(pad, str):
                raise RuntimeError_(
                    f"{name}(Text, Int, Text) needs text/int/text")
            if not pad:
                raise RuntimeError_(f"{name}: pad string is empty")
            ch = pad[0]
            if name == "str.pad_left":
                return s.rjust(width, ch)
            return s.ljust(width, ch)
        if name == "process.run":
            return self.process_run(args[0], e)
        # -- Phase 7 (concurrency) ------------------------------------
        if name == "sleep":
            return self.task_sleep(args[0], e)
        if name == "chan":
            # type args are checking-time only (erasure), like generics
            return Channel()
        if name == "chan.send":
            return self.chan_send(args[0], args[1], e)
        if name == "chan.recv":
            return self.chan_recv(args[0], e)
        if name == "close":
            return self.chan_close(args[0], e)
        if name == "fetch_all":
            return self.fetch_all(args[0], e)
        # Phase 5a: call through a variable holding a function value.
        if "." not in name:
            try:
                cell = env.lookup(name)
            except UndefinedName:
                cell = None
            if cell is not None:
                v = cell[0]
                if isinstance(v, LambdaValue):
                    return self.invoke_lambda_value(v, args, e)
                if isinstance(v, FnValue):
                    return self.call_fn(v.decl, args, e)
                raise RuntimeError_(
                    f"'{name}' is not callable", line=e.line, col=e.col)
        return self.call_builtin(name, args, e)

    def process_run(self, cmd, node) -> dict:
        """Run a subprocess; return {code, stdout, stderr} as a record.

        Phase 6 prerequisite: the self-hosted AOT compiler driver
        shells out to clang/llc. Timeout is 5 minutes; spawn failures
        and timeouts raise E303 (catchable), mirroring fs.read/write.
        """
        import subprocess
        if not isinstance(cmd, list) or not cmd \
                or not all(isinstance(c, str) for c in cmd):
            raise RuntimeError_(
                "process.run([Text]) needs a non-empty text list",
                line=node.line, col=node.col)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=300)
            return {"code": r.returncode, "stdout": r.stdout,
                    "stderr": r.stderr}
        except FileNotFoundError:
            raise IOError_(f"process.run: command not found: {cmd[0]}",
                           line=node.line, col=node.col)
        except subprocess.TimeoutExpired:
            raise IOError_(f"process.run timed out (300s): {cmd[0]}",
                           line=node.line, col=node.col)
        except OSError as ex:
            raise IOError_(f"process.run failed: {ex}",
                           line=node.line, col=node.col)

    # -- Phase 7 (concurrency) ------------------------------------------
    # One Python thread per task behind the cooperative facade. The GIL
    # limits CPU parallelism; I/O parallelizes fully. Tasks share memory
    # (like Go) — no data-race detector in v1.

    def spawn_task(self, body: list, env: Env, node: ast.Node) -> TaskHandle:
        """`spawn { ... }`: create the handle, start a daemon thread
        running the body in a CHILD ENV capturing the definition-site
        env (like closures), and return the handle immediately."""
        child = Env(env)
        handle = TaskHandle(label="task")
        handle.parent_scope = self._current_scope()

        def _body():
            try:
                self.exec_block(body, child)
            except _ReturnSignal as r:
                return r.value
            return None

        self._start_task(handle, _body)
        return handle

    def _start_task(self, handle: TaskHandle, fn):
        """Register the handle and run fn() on a daemon thread, capturing
        any MisaError into handle.error (a throwing task never leaks a
        traceback; `await` rethrows the captured error)."""
        def _run():
            _tls.task_handle = handle
            _tls.scope_stack = []
            try:
                self._check_cancelled()
                handle.result = fn()
            except RecursionError:
                handle.error = RuntimeError_(
                    "call stack overflow (recursion too deep)")
            except MisaError as e:
                handle.error = e
            except Exception as ex:  # never a raw traceback
                handle.error = RuntimeError_(
                    f"task internal error: {ex}")
            finally:
                handle.done.set()
                _tls.task_handle = None

        with self._tasks_lock:
            self._tasks.append(handle)
        t = threading.Thread(target=_run,
                             name=f"misa-{handle.label}", daemon=True)
        handle.thread = t
        t.start()

    def await_task(self, handle: TaskHandle, node: ast.Node,
                   timeout_ms=None) -> Any:
        """`await t` / `await t timeout ms`. On completion: re-raise the
        captured error (catchable at the awaiter) or return the result.
        With a timeout only the WAIT is aborted — the task keeps running.
        Negative timeout raises E306 "timeout must be non-negative"."""
        handle.awaited = True
        self._check_cancelled(node)  # yield point for the awaiter
        if timeout_ms is not None:
            if isinstance(timeout_ms, bool) \
                    or not isinstance(timeout_ms, (int, float)):
                raise RuntimeError_("await timeout needs an Int",
                                    line=node.line, col=node.col)
            if timeout_ms < 0:
                raise ConcurrencyError("timeout must be non-negative",
                                       line=node.line, col=node.col)
            if not handle.done.wait(timeout_ms / 1000.0):
                raise ConcurrencyError(
                    f"task timed out after {timeout_ms}ms",
                    line=node.line, col=node.col)
        else:
            # Chunked wait so a cancelled awaiter is not stuck forever.
            while not handle.done.wait(_YIELD_POLL_S):
                self._check_cancelled(node)
        if handle.error is not None:
            raise handle.error
        return handle.result

    def task_sleep(self, ms, node: ast.Node) -> None:
        """`sleep(ms)`: blocks only the calling task (chunked, so scope
        cancellation interrupts it). Negative ms raises E306."""
        if isinstance(ms, bool) or not isinstance(ms, (int, float)):
            raise RuntimeError_("sleep(Int) needs an int arg",
                                line=node.line, col=node.col)
        if ms < 0:
            raise ConcurrencyError("sleep: ms must be non-negative",
                                   line=node.line, col=node.col)
        self._check_cancelled(node)
        deadline = time.monotonic() + ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(remaining, _YIELD_POLL_S))
            self._check_cancelled(node)

    @staticmethod
    def _as_channel(v, op: str, node: ast.Node) -> Channel:
        if not isinstance(v, Channel):
            raise RuntimeError_(
                f"{op} needs a channel from chan(), got "
                f"{type(v).__name__}", line=node.line, col=node.col)
        return v

    def chan_send(self, chv, v, node: ast.Node) -> None:
        """`send` never blocks (v1: unbounded FIFO). Sending on a closed
        channel raises E306 "send on closed channel"."""
        ch = self._as_channel(chv, "chan.send", node)
        with ch.cond:
            if ch.closed:
                raise ConcurrencyError("send on closed channel",
                                       line=node.line, col=node.col)
            ch.q.append(v)
            ch.cond.notify()
        return None

    def chan_recv(self, chv, node: ast.Node) -> Any:
        """`recv` blocks until a value arrives. After `close`, it drains
        remaining values then raises E306 "channel closed"."""
        ch = self._as_channel(chv, "chan.recv", node)
        self._check_cancelled(node)  # yield point
        while True:
            with ch.cond:
                if ch.q:
                    return ch.q.popleft()
                if ch.closed:
                    raise ConcurrencyError("channel closed",
                                           line=node.line, col=node.col)
                ch.cond.wait(_YIELD_POLL_S)
            self._check_cancelled(node)

    def chan_close(self, chv, node: ast.Node) -> None:
        ch = self._as_channel(chv, "close", node)
        with ch.cond:
            ch.closed = True  # idempotent; recv drains then errors
            ch.cond.notify_all()
        return None

    def fetch_all(self, urls, node: ast.Node) -> list:
        """`fetch_all([urls])`: one task per URL running http.get,
        awaited in order; results in order; the FIRST error is raised
        (catchable). The threads genuinely parallelize the I/O."""
        if not isinstance(urls, list) \
                or not all(isinstance(u, str) for u in urls):
            raise RuntimeError_("fetch_all([Text]) needs a text list",
                                line=node.line, col=node.col)
        tasks = []
        for u in urls:
            handle = TaskHandle(label="fetch_all")
            handle.parent_scope = self._current_scope()

            def _get(u=u):
                return self.http_get(u, node)

            self._start_task(handle, _get)
            tasks.append(handle)
        results = []
        for i, t in enumerate(tasks):
            t.awaited = True
            try:
                results.append(self.await_task(t, node))
            except MisaError:
                # First error wins; the rest are no longer anyone's
                # result, so cancel + silence them (no stderr noise).
                for rest in tasks[i + 1:]:
                    rest.cancelled = True
                    rest.awaited = True
                raise
        return results

    def call_fn(self, decl: ast.FnDecl, args: list, node: ast.Node) -> Any:
        local = Env(self.global_env)
        for (pn, _), v in zip(decl.params, args):
            local.define(pn, v, False)
        # Phase 7: function entry pushes a scope; exit pops it and
        # cancels still-running children spawned inside this call.
        self._push_scope()
        # Phase 5d: TCO — the frame is reused across tail calls.
        _tco_stack().append(("fn", decl))
        try:
            while True:
                try:
                    self.exec_block(decl.body, local)
                except _TailCall as tc:
                    # Only our own frame reuses itself; a _TailCall
                    # for another decl (impossible by construction,
                    # but be safe) propagates outward.
                    if tc.decl is not decl:
                        raise
                    for (pn, _), v in zip(decl.params, tc.args):
                        local.define(pn, v, False)
                    continue
                except _ReturnSignal as r:
                    return r.value
                return None
        finally:
            _tco_stack().pop()
            self._pop_scope()

    def http_get(self, url: str, node: ast.Node) -> str:
        # L-1: thread the call-site position (was <unknown>).
        lc = {"line": node.line, "col": node.col}
        if not isinstance(url, str) or not url.startswith(
                ("http://", "https://")):
            raise HTTPError_(f"http.get needs an http(s) URL, got {url!r}",
                             **lc)
        req = urllib.request.Request(
            url, headers={"User-Agent": "misa/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise HTTPError_(
                        f"http.get {url} -> HTTP {resp.status}", **lc)
                raw = resp.read()
        except urllib.error.HTTPError as ex:
            raise HTTPError_(f"http.get {url} -> HTTP {ex.code}", **lc)
        except urllib.error.URLError as ex:
            raise HTTPError_(f"http.get failed: {ex.reason}", **lc)
        except TimeoutError:
            raise HTTPError_(f"http.get timed out: {url}", **lc)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPError_(f"http.get {url}: body is not UTF-8 text", **lc)

    def http_post(self, url: str, body: str, content_type: str,
                  node: ast.Node) -> str:
        # L-1: thread the call-site position (was <unknown>).
        lc = {"line": node.line, "col": node.col}
        if not isinstance(url, str) or not url.startswith(
                ("http://", "https://")):
            raise HTTPError_(f"http.post needs an http(s) URL, got {url!r}",
                             **lc)
        req = urllib.request.Request(
            url, data=body.encode("utf-8"),
            headers={"User-Agent": "misa/0.2",
                     "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise HTTPError_(
                        f"http.post {url} -> HTTP {resp.status}", **lc)
                raw = resp.read()
        except urllib.error.HTTPError as ex:
            raise HTTPError_(f"http.post {url} -> HTTP {ex.code}", **lc)
        except urllib.error.URLError as ex:
            raise HTTPError_(f"http.post failed: {ex.reason}", **lc)
        except TimeoutError:
            raise HTTPError_(f"http.post timed out: {url}", **lc)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPError_(f"http.post {url}: body is not UTF-8 text", **lc)

    # -- Wave 3 (networking): TCP sockets + HTTP server -----------------

    @staticmethod
    def _net_lc(node):
        return {"line": node.line, "col": node.col}

    def _sock_open(self, v, op, node):
        if not isinstance(v, SocketHandle) or v.closed:
            raise RuntimeError_(
                f"{op}: expected an open Socket",
                **self._net_lc(node))
        return v

    def _listener_open(self, v, op, node):
        if not isinstance(v, ListenerHandle) or v.closed:
            raise RuntimeError_(
                f"{op}: expected an open Listener",
                **self._net_lc(node))
        return v

    @staticmethod
    def _check_port(port, op, node):
        lc = {"line": node.line, "col": node.col}
        if isinstance(port, bool) or not isinstance(port, int) \
                or port < 0 or port > 65535:
            raise RuntimeError_(
                f"{op}: port must be an Int in 0..65535",
                **lc)
        return port

    def net_connect(self, host, port, node):
        lc = self._net_lc(node)
        if not isinstance(host, str):
            raise RuntimeError_(
                "net.connect(host: Text, port: Int)", **lc)
        port = self._check_port(port, "net.connect", node)
        try:
            s = socket.create_connection((host, port), timeout=30)
        except socket.timeout:
            raise HTTPError_(
                f"net.connect {host}:{port} timed out", **lc)
        except OSError as ex:
            raise HTTPError_(
                f"net.connect {host}:{port} failed: {ex}", **lc)
        return SocketHandle(
            sock=s,
            decoder=codecs.getincrementaldecoder("utf-8")())

    def net_listen(self, port, node):
        lc = self._net_lc(node)
        port = self._check_port(port, "net.listen", node)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            s.listen(128)
        except OSError as ex:
            s.close()
            raise HTTPError_(f"net.listen({port}) failed: {ex}", **lc)
        return ListenerHandle(sock=s)

    def net_accept(self, lis, node):
        lc = self._net_lc(node)
        h = self._listener_open(lis, "net.accept", node)
        try:
            conn, _addr = h.sock.accept()
        except OSError as ex:
            raise HTTPError_(f"net.accept failed: {ex}", **lc)
        return SocketHandle(
            sock=conn,
            decoder=codecs.getincrementaldecoder("utf-8")())

    def net_port(self, lis, node):
        h = self._listener_open(lis, "net.port", node)
        try:
            return h.sock.getsockname()[1]
        except OSError as ex:
            raise HTTPError_(f"net.port failed: {ex}",
                             **self._net_lc(node))

    def listener_close(self, lis, node):
        h = self._listener_open(lis, "listener.close", node)
        h.closed = True
        try:
            h.sock.close()
        except OSError:
            pass
        return None

    def socket_send(self, v, data, node):
        lc = self._net_lc(node)
        h = self._sock_open(v, "socket.send", node)
        if not isinstance(data, str):
            raise RuntimeError_(
                "socket.send(sock, data: Text)", **lc)
        raw = data.encode("utf-8")
        try:
            h.sock.sendall(raw)
        except socket.timeout:
            raise HTTPError_("socket.send timed out", **lc)
        except OSError as ex:
            raise HTTPError_(f"socket.send failed: {ex}", **lc)
        return len(raw)

    def socket_recv(self, v, maxv, node):
        lc = self._net_lc(node)
        h = self._sock_open(v, "socket.recv", node)
        if isinstance(maxv, bool) or not isinstance(maxv, int) \
                or maxv <= 0:
            raise RuntimeError_(
                "socket.recv(sock, max: Int > 0)", **lc)
        try:
            chunk = h.sock.recv(min(maxv, 65536))
        except socket.timeout:
            raise HTTPError_("socket.recv timed out", **lc)
        except OSError as ex:
            raise HTTPError_(f"socket.recv failed: {ex}", **lc)
        if not chunk:
            # Clean EOF: flush the incremental decoder (a truncated
            # multi-byte sequence at EOF decodes to "" rather than
            # raising — v1 is lossy here, never crashing).
            try:
                return h.decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                return ""
        return h.decoder.decode(chunk)

    def socket_close(self, v, node):
        h = self._sock_open(v, "socket.close", node)
        h.closed = True
        try:
            h.sock.close()
        except OSError:
            pass
        return None

    def socket_set_timeout(self, v, ms, node):
        lc = self._net_lc(node)
        h = self._sock_open(v, "socket.set_timeout", node)
        if isinstance(ms, bool) or not isinstance(ms, int):
            raise RuntimeError_(
                "socket.set_timeout(sock, ms: Int)", **lc)
        h.sock.settimeout(ms / 1000.0 if ms >= 0 else None)
        return None

    # -- Wave 3: minimal HTTP/1.1 server (raw sockets, no deps) --------

    # Parser limits (documented in spec/STDLIB.md).
    _HTTP_MAX_HEADERS = 64 * 1024   # bytes of header block
    _HTTP_MAX_BODY = 10 * 1024 * 1024  # Content-Length cap
    _HTTP_MAX_HEADER_COUNT = 100

    _HTTP_REASONS = {
        200: "OK", 201: "Created", 204: "No Content",
        400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
        413: "Content Too Large", 431: "Request Header Fields Too Large",
        500: "Internal Server Error", 501: "Not Implemented",
    }

    @staticmethod
    def _http_urldecode(s):
        out = []
        i = 0
        while i < len(s):
            c = s[i]
            if c == "+":
                out.append(" ")
                i += 1
            elif c == "%" and i + 2 < len(s):
                try:
                    out.append(chr(int(s[i + 1:i + 3], 16)))
                    i += 3
                except ValueError:
                    out.append(c)
                    i += 1
            else:
                out.append(c)
                i += 1
        return "".join(out)

    @classmethod
    def _http_parse_query(cls, qs):
        q = {}
        if qs:
            for part in qs.split("&"):
                if not part:
                    continue
                if "=" in part:
                    k, v = part.split("=", 1)
                else:
                    k, v = part, ""
                q[cls._http_urldecode(k)] = cls._http_urldecode(v)
        return q

    def _http_read_request(self, sock, node):
        """Read + parse one HTTP/1.1 request. Returns (request_dict,
        status_if_error). request_dict has method/path/query/headers/
        body. Malformed input -> (None, 400/413/431/501)."""
        lc = self._net_lc(node)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    return None, 400  # EOF mid-headers
                buf += chunk
                if len(buf) > self._HTTP_MAX_HEADERS:
                    return None, 431
            head, rest = buf.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            try:
                request_line = lines[0].decode("latin-1")
            except UnicodeDecodeError:
                return None, 400
            parts = request_line.split(" ")
            if len(parts) != 3:
                return None, 400
            method, target, version = parts
            if not version.startswith("HTTP/"):
                return None, 400
            if not method or not all(
                    c.isupper() or c == "-" for c in method):
                return None, 400
            headers = {}
            for raw in lines[1:]:
                if len(headers) >= self._HTTP_MAX_HEADER_COUNT:
                    return None, 431
                try:
                    line = raw.decode("latin-1")
                except UnicodeDecodeError:
                    return None, 400
                if ":" not in line:
                    return None, 400
                name, value = line.split(":", 1)
                name = name.strip().lower()
                if not name:
                    return None, 400
                headers[name] = value.strip()  # last wins
            if headers.get("transfer-encoding", "").lower() == "chunked":
                return None, 501  # v1: no chunked bodies
            body = rest
            if "content-length" in headers:
                try:
                    n = int(headers["content-length"])
                except ValueError:
                    return None, 400
                if n < 0 or n > self._HTTP_MAX_BODY:
                    return None, 413
                while len(body) < n:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return None, 400  # EOF mid-body
                    body += chunk
                body = body[:n]
            try:
                body_text = body.decode("utf-8")
            except UnicodeDecodeError:
                return None, 400
            if "?" in target:
                path, qs = target.split("?", 1)
            else:
                path, qs = target, ""
            return {
                "method": method,
                "path": self._http_urldecode(path),
                "query": MapValue(self._http_parse_query(qs)),
                "headers": MapValue(dict(headers)),
                "body": body_text,
            }, 0
        except socket.timeout:
            raise HTTPError_("http.serve: read timed out", **lc)
        except OSError as ex:
            raise HTTPError_(f"http.serve: read failed: {ex}", **lc)

    def _http_write_response(self, sock, resp, node):
        lc = self._net_lc(node)
        status = 200
        headers = {}
        body = ""
        if isinstance(resp, dict):
            if "status" in resp:
                status = resp["status"]
            if "headers" in resp:
                hv = resp["headers"]
                raw = hv.data if isinstance(hv, MapValue) else hv
                if isinstance(raw, dict):
                    headers = {str(k): str(v)
                               for k, v in raw.items()}
            if "body" in resp:
                body = resp["body"]
        if isinstance(status, bool) or not isinstance(status, int):
            status = 500
        if not isinstance(body, str):
            body = str(body)
        body_raw = body.encode("utf-8")
        reason = self._HTTP_REASONS.get(status, "OK")
        out = [f"HTTP/1.1 {status} {reason}\r\n"]
        if not any(k.lower() == "content-type" for k in headers):
            out.append("Content-Type: text/plain; charset=utf-8\r\n")
        for k, v in headers.items():
            out.append(f"{k}: {v}\r\n")
        out.append(f"Content-Length: {len(body_raw)}\r\n")
        out.append("Connection: close\r\n\r\n")
        try:
            sock.sendall("".join(out).encode("latin-1") + body_raw)
        except OSError as ex:
            raise HTTPError_(f"http.serve: write failed: {ex}", **lc)

    def _http_serve_one(self, conn, handler, node):
        """Serve a single connection: parse, invoke handler, respond.
        Handler Misa errors -> 500 (per-connection isolation)."""
        req, err_status = self._http_read_request(conn.sock, node)
        if err_status:
            self._http_write_response(
                conn.sock, {"status": err_status, "body": ""}, node)
            return
        try:
            if isinstance(handler, LambdaValue):
                resp = self.invoke_lambda_value(handler, [req], node)
            else:
                resp = self.call_fn(handler, [req], node)
        except MisaError:
            self._http_write_response(
                conn.sock, {"status": 500, "body": ""}, node)
            return
        self._http_write_response(conn.sock, resp, node)

    def http_serve(self, port, handler, node):
        lc = self._net_lc(node)
        port = self._check_port(port, "http.serve", node)
        if not isinstance(handler, LambdaValue):
            raise RuntimeError_(
                "http.serve(port, handler): handler must be a lambda "
                "`|req: Request| -> Response`", **lc)
        lis = self.net_listen(port, node)
        # Wake the accept() periodically so a cancelled task (structured
        # concurrency: children die at scope exit) stops promptly instead
        # of blocking forever in the C call.
        lis.sock.settimeout(0.5)
        try:
            while True:
                self._check_cancelled(node)
                try:
                    conn, _addr = lis.sock.accept()
                except socket.timeout:
                    continue
                except OSError as ex:
                    raise HTTPError_(
                        f"http.serve: accept failed: {ex}", **lc)
                conn_h = SocketHandle(
                    sock=conn,
                    decoder=codecs.getincrementaldecoder("utf-8")())
                try:
                    self._http_serve_one(conn_h, handler, node)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        except ConcurrencyError:
            # Structured cancellation: the server task was cancelled at
            # scope exit. Swallow E306 here (it is the *expected* way a
            # server stops); anything else propagates.
            pass
        finally:
            try:
                lis.sock.close()
            except OSError:
                pass
            lis.closed = True
        return None

    # -- Phase 5 (stdlib2): JSON ---------------------------------------

    def json_parse(self, s: str, node: ast.Node) -> Any:
        try:
            v = json.loads(s)
        except json.JSONDecodeError as ex:
            raise RuntimeError_(f"json.parse: invalid JSON: {ex}",
                                line=node.line, col=node.col)
        return self._json_to_misa(v, node)

    def _json_to_misa(self, v: Any, node: ast.Node) -> Any:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, str):
            # C-7: reject lone surrogates (AOT does; printing one would
            # traceback-crash). Match str.chr's E300 behavior.
            for ch in v:
                cp = ord(ch)
                if 0xD800 <= cp <= 0xDFFF:
                    raise RuntimeError_(
                        f"json.parse: invalid JSON: lone surrogate",
                        line=node.line, col=node.col)
            return v
        if isinstance(v, int):
            if v > 2**63 - 1 or v < -(2**63):
                # M-6: STDLIB.md §3e — out-of-int64 integers become Float.
                return float(v)
            return v
        if isinstance(v, float):
            return v
        if isinstance(v, list):
            return [self._json_to_misa(i, node) for i in v]
        if isinstance(v, dict):
            return {k: self._json_to_misa(x, node)
                    for k, x in v.items()}
        raise RuntimeError_(f"json.parse: unsupported value {v!r}",
                            line=node.line, col=node.col)

    def json_stringify(self, v: Any, node: ast.Node) -> str:
        try:
            jv = self._misa_to_json(v, node)
            return json.dumps(jv, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as ex:
            raise RuntimeError_(f"json.stringify: {ex}",
                                line=node.line, col=node.col)

    def _misa_to_json(self, v: Any, node: ast.Node) -> Any:
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        if isinstance(v, list):
            return [self._misa_to_json(i, node) for i in v]
        if isinstance(v, dict):
            return {k: self._misa_to_json(x, node)
                    for k, x in v.items()}
        if isinstance(v, MapValue):
            out = {}
            for k, x in v.data.items():
                if not isinstance(k, str):
                    raise RuntimeError_(
                        "json.stringify: map keys must be text",
                        line=node.line, col=node.col)
                out[k] = self._misa_to_json(x, node)
            return out
        raise RuntimeError_(
            f"json.stringify: value of type "
            f"{type(v).__name__} is not JSON-representable",
            line=node.line, col=node.col)

    # -- Phase 5 (stdlib2): time ---------------------------------------

    def time_format(self, epoch: int, fmt: str, node: ast.Node) -> str:
        try:
            return time.strftime(fmt, time.gmtime(epoch))
        except (ValueError, OverflowError, OSError) as ex:
            raise RuntimeError_(f"time.format: {ex}",
                                line=node.line, col=node.col)

    def time_parse(self, s: str, fmt: str, node: ast.Node) -> int:
        import calendar
        try:
            tt = time.strptime(s, fmt)
            return calendar.timegm(tt)
        except ValueError as ex:
            raise RuntimeError_(f"time.parse: {ex}",
                                line=node.line, col=node.col)

    # -- Phase 5 (stdlib2): math ---------------------------------------

    @staticmethod
    def _round_half_away(x: float) -> int:
        import math as _math
        return _math.floor(x + 0.5) if x >= 0 else _math.ceil(x - 0.5)

    def _math_num(self, op: str, v: Any, node: ast.Node) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RuntimeError_(f"math.{op}(Float) needs a numeric arg",
                                line=node.line, col=node.col)
        return float(v)

    def math_unary(self, op: str, v: Any, node: ast.Node,
                   fn) -> float:
        import math as _math
        x = self._math_num(op, v, node)
        try:
            return fn(x)
        except ValueError as ex:
            raise RuntimeError_(f"math.{op}: {ex}",
                                line=node.line, col=node.col)

    def math_binary(self, op: str, a: Any, b: Any,
                    node: ast.Node) -> float:
        x = self._math_num(op, a, node)
        y = self._math_num(op, b, node)
        try:
            return math.pow(x, y)
        except ValueError as ex:
            raise RuntimeError_(f"math.{op}: {ex}",
                                line=node.line, col=node.col)

    def math_to_int(self, op: str, v: Any, node: ast.Node, fn) -> int:
        import math as _math
        x = self._math_num(op, v, node)
        try:
            r = fn(x)
        except (ValueError, OverflowError) as ex:
            raise RuntimeError_(f"math.{op}: {ex}",
                                line=node.line, col=node.col)
        if r > 2**63 - 1 or r < -(2**63):
            raise RuntimeError_(f"math.{op}: result out of Int range",
                                line=node.line, col=node.col)
        return int(r)

    # -- Phase 5 (DB): SQLite ------------------------------------------

    @staticmethod
    def _db_handle(v, op: str, node: ast.Node) -> "DbHandle":
        if not isinstance(v, DbHandle):
            raise RuntimeError_(
                f"{op} needs a Db from db.open(), got "
                f"{type(v).__name__}", line=node.line, col=node.col)
        return v

    def db_open(self, path, node: ast.Node) -> DbHandle:
        if not isinstance(path, str):
            raise RuntimeError_("db.open(path) needs a text path",
                                line=node.line, col=node.col)
        try:
            conn = sqlite3.connect(path)
        except sqlite3.Error as ex:
            raise IOError_(f"db.open failed: {ex}",
                           line=node.line, col=node.col)
        return DbHandle(path=path, conn=conn)

    @staticmethod
    def _db_bind_arg(a, op: str, idx: int, node: ast.Node):
        # H-3: bind args are Int/Float/Bool/Text/nil on both backends
        # (bool is an int subclass; sqlite3 binds it as 0/1, like AOT).
        # Anything else is E200 (matches the AOT runtime).
        if a is None or isinstance(a, (int, float, str)):
            return a
        raise TypeMismatch(
            f"{op}: bind arg {idx} must be Int, Float, Bool, Text, "
            f"or nil, got {type(a).__name__}",
            line=node.line, col=node.col)

    def db_exec(self, dbv, sql, args, node: ast.Node) -> int:
        h = self._db_handle(dbv, "db.exec", node)
        if not isinstance(sql, str):
            raise RuntimeError_("db.exec needs a text SQL statement",
                                line=node.line, col=node.col)
        bound = [self._db_bind_arg(a, "db.exec", i + 1, node)
                 for i, a in enumerate(args)]
        try:
            cur = h.conn.execute(sql, bound)
            # P-3: don't autocommit inside an explicit transaction.
            if not h.in_txn:
                h.conn.commit()
            return cur.rowcount if cur.rowcount >= 0 else 0
        except sqlite3.Error as ex:
            raise IOError_(f"db.exec failed: {ex}",
                           line=node.line, col=node.col)

    def db_exec_many(self, dbv, sql, rows, node: ast.Node) -> int:
        # P-3: bulk inserts via executemany (single prepare).
        h = self._db_handle(dbv, "db.exec_many", node)
        if not isinstance(sql, str):
            raise RuntimeError_("db.exec_many needs a text SQL statement",
                                line=node.line, col=node.col)
        if not isinstance(rows, list):
            raise RuntimeError_("db.exec_many needs a list of rows",
                                line=node.line, col=node.col)
        bound_rows = []
        for i, row in enumerate(rows):
            if not isinstance(row, list):
                raise RuntimeError_(
                    f"db.exec_many: row {i} must be a list",
                    line=node.line, col=node.col)
            bound_rows.append(
                [self._db_bind_arg(a, "db.exec_many", j + 1, node)
                 for j, a in enumerate(row)])
        try:
            cur = h.conn.executemany(sql, bound_rows)
            if not h.in_txn:
                h.conn.commit()
            return cur.rowcount if cur.rowcount >= 0 else 0
        except sqlite3.Error as ex:
            raise IOError_(f"db.exec_many failed: {ex}",
                           line=node.line, col=node.col)

    def db_begin(self, dbv, node: ast.Node) -> None:
        h = self._db_handle(dbv, "db.begin", node)
        try:
            h.conn.execute("BEGIN")
            h.in_txn = True
        except sqlite3.Error as ex:
            raise IOError_(f"db.begin failed: {ex}",
                           line=node.line, col=node.col)
        return None

    def db_commit(self, dbv, node: ast.Node) -> None:
        h = self._db_handle(dbv, "db.commit", node)
        try:
            h.conn.commit()
            h.in_txn = False
        except sqlite3.Error as ex:
            raise IOError_(f"db.commit failed: {ex}",
                           line=node.line, col=node.col)
        return None

    def db_rollback(self, dbv, node: ast.Node) -> None:
        h = self._db_handle(dbv, "db.rollback", node)
        try:
            h.conn.rollback()
            h.in_txn = False
        except sqlite3.Error as ex:
            raise IOError_(f"db.rollback failed: {ex}",
                           line=node.line, col=node.col)
        return None

    def db_query(self, dbv, sql, args, node: ast.Node) -> list:
        h = self._db_handle(dbv, "db.query", node)
        if not isinstance(sql, str):
            raise RuntimeError_("db.query needs a text SQL statement",
                                line=node.line, col=node.col)
        bound = [self._db_bind_arg(a, "db.query", i + 1, node)
                 for i, a in enumerate(args)]
        try:
            cur = h.conn.execute(sql, bound)
            names = [d[0] for d in cur.description or []]
            rows = []
            for tup in cur.fetchall():
                rec = {}
                for name, val in zip(names, tup):
                    rec[name] = self._db_value(val, node)
                rows.append(rec)
            return rows
        except sqlite3.Error as ex:
            raise IOError_(f"db.query failed: {ex}",
                           line=node.line, col=node.col)

    @staticmethod
    def _db_value(val, node: ast.Node):
        # SQLite -> Misa: INTEGER->Int, REAL->Float, TEXT->Text,
        # NULL->nil. BLOB has no Misa equivalent in v1.
        if val is None or isinstance(val, (int, float, str)):
            return val
        if isinstance(val, bytes):
            raise IOError_(
                "db.query: BLOB columns are not supported in v1",
                line=node.line, col=node.col)
        return str(val)

    def db_close(self, dbv, node: ast.Node) -> None:
        h = self._db_handle(dbv, "db.close", node)
        try:
            h.conn.close()
        except sqlite3.Error as ex:
            raise IOError_(f"db.close failed: {ex}",
                           line=node.line, col=node.col)
        return None

    def _cli_parse(self, p: CliParserValue, argv: list,
                   node: ast.Node):
        """Parse argv against p. Mirrors misa_cli_parse() in misa_rt.c
        exactly — same tokens, same errors, same help text. Returns a
        CliArgsValue, or None (nil) after printing --help."""
        lc, cc = node.line, node.col
        args = CliArgsValue(p)
        n = len(argv)
        dashdash = False
        i = 0
        while i < n:
            tok = argv[i]
            if dashdash:
                args.positionals.append(tok)
                i += 1
                continue
            if tok == "--":
                dashdash = True
                i += 1
                continue
            if tok == "-" or not tok.startswith("-"):
                if p.commands and args.command is None:
                    names = [c["name"] for c in p.commands]
                    if tok not in names:
                        raise RuntimeError_(
                            f"unknown command '{tok}' (expected one of: "
                            f"{' '.join(names)})", line=lc, col=cc)
                    args.command = tok
                else:
                    args.positionals.append(tok)
                i += 1
                continue
            if tok.startswith("--"):
                if tok == "--help":
                    self.out.write(cli_build_help(p))
                    return None
                body = tok[2:]
                if "=" in body:
                    name, val = body.split("=", 1)
                    has_eq = True
                else:
                    name, val = body, None
                    has_eq = False
                if not name:
                    raise RuntimeError_(f"malformed flag '{tok}'",
                                        line=lc, col=cc)
                if name == "help":
                    self.out.write(cli_build_help(p))
                    return None
                f = p.find_flag(name)
                if f is None:
                    raise RuntimeError_(
                        f"unknown flag '--{name}' (run '{p.prog} --help' "
                        f"for usage)", line=lc, col=cc)
                if f["is_bool"]:
                    if has_eq:
                        raise RuntimeError_(
                            f"flag '--{name}' takes no value (it's a bool "
                            f"flag)", line=lc, col=cc)
                    args.bools[name] = True
                else:
                    if not has_eq:
                        if i + 1 >= n or _cli_is_flaglike(argv[i + 1]):
                            raise RuntimeError_(
                                f"flag '--{name}' needs a value",
                                line=lc, col=cc)
                        i += 1
                        val = argv[i]
                    args.values[name] = val if val is not None else ""
                i += 1
                continue
            # Short flag(s).
            if tok == "-h":
                self.out.write(cli_build_help(p))
                return None
            rest = tok[1:]
            if "=" in rest:
                pre, post = rest.split("=", 1)
                if len(pre) == 1 and pre != "":
                    f = p.find_short(pre)
                    if f is None:
                        raise RuntimeError_(
                            f"unknown flag '-{pre}' (run '{p.prog} --help' "
                            f"for usage)", line=lc, col=cc)
                    if f["is_bool"]:
                        raise RuntimeError_(
                            f"flag '-{pre}' takes no value (it's a bool "
                            f"flag)", line=lc, col=cc)
                    args.values[f["name"]] = post
                    i += 1
                    continue
            if len(rest) != 1:
                raise RuntimeError_(
                    f"combined short flags like '{tok}' are not supported "
                    f"(use -a -b -c)", line=lc, col=cc)
            f = p.find_short(rest)
            if f is None:
                raise RuntimeError_(
                    f"unknown flag '-{rest}' (run '{p.prog} --help' for "
                    f"usage)", line=lc, col=cc)
            if not f["is_bool"]:
                if i + 1 >= n or _cli_is_flaglike(argv[i + 1]):
                    raise RuntimeError_(
                        f"flag '-{rest}' needs a value", line=lc, col=cc)
                i += 1
                args.values[f["name"]] = argv[i]
            else:
                args.bools[f["name"]] = True
            i += 1
        return args

    def call_builtin(self, name: str, args: list, node: ast.Node) -> Any:
        if name == "state":
            # Screen state: get-or-create the cell for this slot. The
            # checker stamps node.state_slot and guarantees screen
            # context, so reaching here means a legal declaration.
            frame = self._screen_frame
            slot = getattr(node, "state_slot", None)
            if frame is None or slot is None:
                raise RuntimeError_("state() is only usable inside a "
                                     "screen body")
            states = frame["states"]
            cell = states.get(slot)
            if cell is None:
                cell = StateCell(value=args[0], frame=frame)
                states[slot] = cell
            return cell
        if name == "print":
            self.out.write(render_value(args[0]) + "\n")
            return render_value(args[0])
        if name == "len":
            return len(args[0])
        if name == "str":
            return render_value(args[0])
        if name == "int":
            a = args[0]
            # C-1: 64-bit wrapping semantics, matching the AOT backend.
            if isinstance(a, float):
                # Match AOT: out-of-range floats are E300, not wrapped.
                if not (-9.223372036854776e18 < a < 9.223372036854776e18):
                    raise RuntimeError_("float out of Int range")
                try:
                    return wrap_i64(int(a))
                except OverflowError:
                    raise RuntimeError_(f"cannot parse int from {a!r}")
            try:
                v = int(a)
            except (ValueError, TypeError, OverflowError):
                raise RuntimeError_(f"cannot parse int from {a!r}",
                                    line=node.line, col=node.col)
            return wrap_i64(v) if isinstance(v, int) else v
        if name == "float":
            try:
                return float(args[0])
            except (ValueError, TypeError):
                raise RuntimeError_(f"cannot parse float from {args[0]!r}")
        if name == "assert":
            msg = args[1] if len(args) > 1 else "assertion failed"
            if not self.truthy(args[0]):
                raise AssertFailed(render_value(msg),
                                   line=node.line, col=node.col)
            return True
        if name == "assert_eq":
            # Same equality as the `==` operator (Python `==` on the
            # underlying values); the rendered values are the message.
            if not (args[0] == args[1]):
                raise AssertFailed(
                    f"assert_eq failed: {render_value(args[0])} != "
                    f"{render_value(args[1])}",
                    line=node.line, col=node.col)
            return True
        if name == "assert_ne":
            if args[0] == args[1]:
                raise AssertFailed(
                    f"assert_ne failed: both are "
                    f"{render_value(args[0])}",
                    line=node.line, col=node.col)
            return True
        if name == "argv":
            return list(self.argv)
        if name == "env":
            val = os.environ.get(args[0])
            if val is None:
                # M-8: STDLIB.md promises E302 for an unset variable.
                raise EnvNotSet(f"env var '{args[0]}' is not set",
                                line=node.line, col=node.col)
            return val
        raise UndefinedName(f"undefined function '{name}'")

    def do_navigate(self, args: list, node: ast.Node):
        if len(args) != 1 or not (isinstance(args[0], tuple)
                                  and args[0][0] == "screen"):
            raise RuntimeError_("navigate needs a screen call, "
                                "e.g. navigate(Home())")
        _, name, sargs = args[0]
        decl = self.screens[name]
        self.current_screen = name
        self.print_screen(decl, sargs)
        return None

    # -- screens ------------------------------------------------------

    def print_screen(self, decl: ast.ScreenDecl, arg_values: list):
        scope = {}
        for (pn, _), v in zip(decl.params, arg_values):
            scope[pn] = v
        # Screen state: set up the current frame. On a fresh navigation
        # the frame is reset (state starts over); a re-render keeps the
        # existing cells (self._rerendering guards that path).
        if not getattr(self, "_rerendering", False):
            self._screen_frame = {
                "name": decl.name,
                "args": list(arg_values),
                "states": {},
                "dirty": False,
                "handlers": [],
            }
        else:
            frame = self._screen_frame
            frame["handlers"] = []
        self.out.write(f"--- screen {decl.name} ---\n")
        for item in decl.body:
            self.print_screen_item(item, scope, 0, {})

    def print_screen_item(self, node: ast.Node, scope: dict,
                          depth: int, ids: dict):
        if isinstance(node, ast.ScreenIf):
            env = Env()
            for k, v in scope.items():
                env.define(k, v, False)
            branch = node.then if self.truthy(
                self.eval_expr(node.cond, env)) else node.els
            for x in branch:
                self.print_screen_item(x, scope, depth, ids)
            return
        if isinstance(node, ast.ScreenFor):
            env = Env()
            for k, v in scope.items():
                env.define(k, v, False)
            items = self._iterable(self.eval_expr(node.iter, env), node)
            for x in items:
                inner = dict(scope)
                inner[node.var] = x
                for y in node.body:
                    self.print_screen_item(y, inner, depth, ids)
            return
        if isinstance(node, ast.LetStmt):
            # Screen state: `let x = state(v)` creates the cell on first
            # render and reuses it on re-renders (get-or-create).
            env = Env()
            for k, v in scope.items():
                env.define(k, v, False)
            val = self.eval_expr(node.value, env)
            scope[node.name] = val
            return
        self.print_component(node, scope, depth, ids)

    def print_component(self, comp: ast.Component, scope: dict,
                        depth: int, ids: dict):
        pad = "  " * depth
        parts = []
        env = Env()
        for k, v in scope.items():
            env.define(k, v, False)
        for an, val in comp.args:
            if an == "id" and isinstance(val, ast.Ident):
                ids[val.name] = comp.name
                scope[val.name] = {"__component__": comp.name}
                continue
            if isinstance(val, ast.Lambda):
                # Screen state: collect the handler closure so tests can
                # fire it (handlers print as <handler>, as before).
                lam = self.eval_expr(val, env)
                if self._screen_frame is not None:
                    self._screen_frame["handlers"].append(lam)
                parts.append(f"{an}=<handler>")
            else:
                parts.append(f"{an}={render_value(self.eval_expr(val, env))}")
        self.out.write(pad + comp.name +
                       (f"({', '.join(parts)})" if parts else "") + "\n")
        for child in comp.children:
            self.print_screen_item(child, dict(scope), depth + 1, ids)

    def test_fire_handler(self, i: int):
        """Test-only hook: fire the i-th collected handler (in render
        order), then re-render the screen if state was mutated —
        mirroring what the AOT drivers do via
        misa_screen_after_handler()."""
        frame = self._screen_frame
        if frame is None:
            raise RuntimeError_("no screen is currently rendered")
        lam = frame["handlers"][i]
        nargs = len(lam.params)
        self.invoke_lambda_value(lam, [None] * nargs, None)
        if frame["dirty"]:
            frame["dirty"] = False
            self._rerendering = True
            try:
                decl = self.screens[frame["name"]]
                self.print_screen(decl, frame["args"])
            finally:
                self._rerendering = False

    # -- model calls --------------------------------------------------

    @staticmethod
    def _at(err, node):
        """L-1: stamp a source position onto an error that was raised
        without one (e.g. deep inside providers.py). Never clobbers a
        position that is already set."""
        if err.line == 0 and node is not None:
            err.line, err.col = node.line, node.col
        return err

    def get_provider(self, decl: ast.ModelDecl) -> tuple:
        # L-1: model errors carry the model-decl position (was <unknown>).
        lc = {"line": decl.line, "col": decl.col}
        if decl.alias not in self.aliases:
            raise ModelError(f"unknown model alias '{decl.alias}'", **lc)
        prov_name, model_id = self.aliases[decl.alias]
        if prov_name not in self.providers:
            raise ModelError(f"unknown provider '{prov_name}'", **lc)
        pdecl = self.providers[prov_name]
        if pdecl.model_file:
            # on-device: the file (not the name) selects the runtime
            mpath = self._resolve_model_file(pdecl)
            key = (prov_name, decl.alias, mpath)
            if key not in self.provider_cache:
                try:
                    self.provider_cache[key] = OnDeviceProvider(mpath)
                except (OnDeviceRuntimeUnavailable,
                        ProviderConfigError) as ex:
                    # E503/E401 from provider construction (e.g. CoreML
                    # on non-Apple hardware, missing model file).
                    raise self._at(ex, decl)
            return self.provider_cache[key], model_id
        key = (prov_name, decl.alias)
        if key not in self.provider_cache:
            key_env = pdecl.key_env
            self.provider_cache[key] = make_provider(
                prov_name, key_env,
                mock_scripts=self.mock_scripts, mock_alias=decl.alias)
        return self.provider_cache[key], model_id

    def _resolve_model_file(self, pdecl: ast.ProviderDecl) -> str:
        """Resolve a provider's `model: file("...")` against (in order):
        the path as given, the provider block's own file, the entry
        file's directory. Raises E401 when nothing matches."""
        rel = pdecl.model_file
        cands = [os.path.abspath(rel)]
        decl_file = self.file_of.get(id(pdecl))
        if decl_file:
            cands.append(os.path.normpath(
                os.path.join(os.path.dirname(decl_file), rel)))
        if self.main_dir:
            cands.append(os.path.normpath(
                os.path.join(self.main_dir, rel)))
        for c in cands:
            if os.path.exists(c):
                return c
        raise ProviderConfigError(
            f"on-device model file not found: '{rel}'",
            hint="checked: " + ", ".join(cands),
            line=pdecl.line, col=pdecl.col)

    def render_prompt(self, decl: ast.ModelDecl,
                      arg_values: list) -> tuple:
        """Returns (prompt_text, [(path, mime)])."""
        images = []
        str_args = {}
        for (pn, pte), v in zip(decl.params, arg_values):
            if isinstance(v, MediaHandle) and v.kind == "Photo" \
                    and os.path.exists(v.path):
                images.append((v.path, guess_mime(v.path)))
            str_args[pn] = render_value(v)
        text = ""
        for part in decl.prompt:
            if isinstance(part, str):
                # un-escape {{ -> { for final text
                text += part.replace("{{", "{")
            else:
                text += self._render_prompt_expr(part, str_args)
        return text, images

    def _render_prompt_expr(self, e: ast.Node, str_args: dict) -> str:
        if isinstance(e, ast.Ident) and e.name in str_args:
            return str_args[e.name]
        # fallback: evaluate with a tiny env of raw values is overkill;
        # identifiers are the sane case in v0.1
        raise ModelError("prompt interpolation supports parameter "
                         "names only in v0.1",
                         line=e.line, col=e.col)

    def call_model(self, decl: ast.ModelDecl, arg_values: list,
                   node: ast.Node) -> Any:
        if len(arg_values) != len(decl.params):
            raise ModelError(
                f"model '{decl.name}' takes {len(decl.params)} args, "
                f"got {len(arg_values)}")
        provider, model_id = self.get_provider(decl)
        if isinstance(provider, OnDeviceProvider):
            return self.call_ondevice(provider, decl, model_id, arg_values,
                                       node)
        prompt_text, images = self.render_prompt(decl, arg_values)
        timeout_s = DEFAULT_TIMEOUT_S
        if decl.budget and decl.budget.latency_ms:
            timeout_s = min(timeout_s, decl.budget.latency_ms / 1000.0)
        usage = Usage()
        t0 = time.monotonic()
        errors: list = []
        prompt = prompt_text
        value = None
        for _ in range(3):  # initial try + 2 repair retries
            comp = provider.complete(model_id=model_id, prompt=prompt,
                                     images=images, timeout_s=timeout_s)
            usage.input_tokens += comp.usage.input_tokens
            usage.output_tokens += comp.usage.output_tokens
            try:
                data = json.loads(comp.text)
            except json.JSONDecodeError:
                errors = ["response was not valid JSON"]
                prompt = self._repair_prompt(prompt_text, errors)
                continue
            value, errors = self.validate(data, decl.ret, "$")
            if not errors:
                break
            prompt = self._repair_prompt(prompt_text, errors)
        else:
            raise SchemaValidationError(
                f"model '{decl.name}' returned invalid data after "
                f"3 attempts", validation_errors=errors,
                hint="check the return type declaration",
                line=node.line, col=node.col)
        if value is None and errors:
            raise SchemaValidationError(
                f"model '{decl.name}' returned invalid data",
                validation_errors=errors,
                line=node.line, col=node.col)
        latency_ms = int((time.monotonic() - t0) * 1000)
        self.check_budget(decl, usage, latency_ms, model_id)
        return value

    def call_ondevice(self, provider: OnDeviceProvider,
                      decl: ast.ModelDecl, model_id: str,
                      arg_values: list, node: ast.Node) -> Any:
        """Single-shot local inference: no prompt rendering, no repair
        retries. The provider returns the first model output as JSON;
        the declared return type is validated as usual."""
        if decl.budget and decl.budget.tokens is not None:
            warn_key = (provider.model_path, decl.alias)
            if warn_key not in self._tokens_warned:
                print(f"warning: token budget ignored for on-device "
                      f"model '{decl.name}' (local inference uses no "
                      f"tokens)", file=sys.stderr)
                self._tokens_warned.add(warn_key)
        timeout_s = DEFAULT_TIMEOUT_S
        if decl.budget and decl.budget.latency_ms:
            timeout_s = min(timeout_s, decl.budget.latency_ms / 1000.0)
        t0 = time.monotonic()
        try:
            comp = provider.complete(model_id=model_id, prompt="", images=[],
                                     timeout_s=timeout_s, args=arg_values,
                                     params=decl.params, ret=decl.ret)
        except OnDeviceRuntimeUnavailable as ex:
            # L-1: E503 from lazy runtime load (e.g. missing onnxruntime
            # at first inference) gets the call-site position.
            raise self._at(ex, node)
        try:
            data = json.loads(comp.text)
        except json.JSONDecodeError:
            raise SchemaValidationError(
                f"on-device model '{decl.name}' returned invalid data",
                line=node.line, col=node.col)
        value, errors = self.validate(data, decl.ret, "$")
        if errors:
            raise SchemaValidationError(
                f"on-device model '{decl.name}' returned invalid data",
                validation_errors=errors,
                hint="check the return type declaration",
                line=node.line, col=node.col)
        latency_ms = int((time.monotonic() - t0) * 1000)
        # token/cost budgets are vacuous (usage is zero); latency is real
        self.check_budget(decl, comp.usage, latency_ms, model_id)
        return value

    def _repair_prompt(self, prompt_text: str, errors: list) -> str:
        return (prompt_text +
                "\n\nYour previous response failed validation:\n" +
                "\n".join(f"- {e}" for e in errors) +
                "\nRespond with structured data only.")

    def check_budget(self, decl: ast.ModelDecl, usage: Usage,
                     latency_ms: int, model_id: str):
        b = decl.budget
        if not b:
            return
        # L-1: budget errors point at the model decl (was <unknown>).
        lc = {"line": decl.line, "col": decl.col}
        if b.tokens is not None and usage.total_tokens > b.tokens:
            raise BudgetExceeded(
                f"token budget exceeded: used {usage.total_tokens}, "
                f"budget {b.tokens}", **lc)
        if b.latency_ms is not None and latency_ms > b.latency_ms:
            raise BudgetExceeded(
                f"latency budget exceeded: took {latency_ms}ms, "
                f"budget {b.latency_ms}ms", **lc)
        if b.cost_usd is not None:
            cost = estimate_cost_usd(model_id, usage)
            if cost > b.cost_usd:
                raise BudgetExceeded(
                    f"cost budget exceeded: ~${cost:.4f}, "
                    f"budget ${b.cost_usd:.4f} (estimated)", **lc)

    # -- validation ---------------------------------------------------

    def validate(self, data: Any, te: ast.TypeExpr,
                 path: str) -> tuple:
        """Returns (python_value, [error strings])."""
        errors: list = []
        name = te.name
        if te.optional and data is None:
            return None, []
        if te.is_list:
            if not isinstance(data, list):
                return None, [f"{path}: expected list, got "
                               f"{self._jtype(data)}"]
            inner = ast.TypeExpr(name=name, optional=te.optional)
            out = []
            ok = True
            for i, item in enumerate(data):
                v, errs = self.validate(item, inner, f"{path}[{i}]")
                if errs:
                    errors.extend(errs)
                    ok = False
                else:
                    out.append(v)
            return (out, errors) if ok else (None, errors)
        if name == "Text":
            if isinstance(data, str):
                return data, []
            errors.append(f"{path}: expected Text, got "
                          f"{self._jtype(data)}")
            return None, errors
        if name == "Int":
            if type(data) is int:
                return data, []
            errors.append(f"{path}: expected Int, got {self._jtype(data)}")
            return None, errors
        if name == "Float":
            if type(data) in (int, float) and type(data) is not bool:
                return float(data), []
            errors.append(f"{path}: expected Float, got "
                          f"{self._jtype(data)}")
            return None, errors
        if name == "Bool":
            if type(data) is bool:
                return data, []
            errors.append(f"{path}: expected Bool, got "
                          f"{self._jtype(data)}")
            return None, errors
        if name in ("Photo", "Audio", "Video", "Document",
                    "TokenStream", "Embedding"):
            errors.append(f"{path}: models cannot return {name} in v0.1")
            return None, errors
        decl = self.types.get(name)
        if decl is None:
            errors.append(f"{path}: unknown type '{name}'")
            return None, errors
        if not isinstance(data, dict):
            return None, [f"{path}: expected object for type '{name}', "
                           f"got {self._jtype(data)}"]
        out = {}
        ok = True
        for fn, fte in decl.fields:
            if fn not in data:
                if fte.optional:
                    out[fn] = None
                    continue
                errors.append(f"{path}.{fn}: missing field")
                ok = False
                continue
            v, errs = self.validate(data[fn], fte, f"{path}.{fn}")
            if errs:
                errors.extend(errs)
                ok = False
            else:
                out[fn] = v
        return (out, errors) if ok else (None, errors)

    @staticmethod
    def _jtype(data: Any) -> str:
        if data is None:
            return "null"
        if isinstance(data, bool):
            return "boolean"
        if isinstance(data, int):
            return "integer"
        if isinstance(data, float):
            return "number"
        if isinstance(data, str):
            return "string"
        if isinstance(data, list):
            return "array"
        return "object"

    # -- streams --------------------------------------------------------

    def make_stream(self, call: ast.Node, env: Env,
                    node: ast.Node) -> TokenStreamHandle:
        if not (isinstance(call, ast.Call) and call.name in self.models):
            raise RuntimeError_("'stream' must wrap a model call")
        decl = self.models[call.name]
        arg_values = [self.eval_expr(a, env) for a in call.args]
        provider, model_id = self.get_provider(decl)
        prompt_text, images = self.render_prompt(decl, arg_values)
        timeout_s = DEFAULT_TIMEOUT_S
        if decl.budget and decl.budget.latency_ms:
            timeout_s = min(timeout_s, decl.budget.latency_ms / 1000.0)
        return TokenStreamHandle(decl=decl, arg_values=arg_values,
                                 provider=provider, model_id=model_id,
                                 prompt_text=prompt_text, images=images,
                                 timeout_s=timeout_s)

    def drain_stream(self, handle: TokenStreamHandle,
                     env: Env) -> Any:
        t0 = time.monotonic()
        text = ""
        try:
            for token in handle.provider.stream(
                    model_id=handle.model_id,
                    prompt=handle.prompt_text, images=handle.images,
                    timeout_s=handle.timeout_s):
                text += token
                for h in handle.handlers:
                    self.invoke_lambda(h, [token], env)
        except Exception as e:
            # L-1: stream failures point at the model decl (was <unknown>).
            raise ModelError(f"stream failed: {e}",
                             line=handle.decl.line, col=handle.decl.col)
        # validate like a normal call (single repair via complete if needed)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        value, errors = (self.validate(data, handle.decl.ret, "$")
                         if data is not None
                         else (None, ["stream did not produce valid JSON"]))
        if errors:
            # one non-streaming repair attempt
            value = self.call_model(handle.decl, handle.arg_values,
                                    handle.decl)
            return value
        latency_ms = int((time.monotonic() - t0) * 1000)
        # approximate usage for budget: we lack token counts from SSE
        # unless the provider reported them; use text-based estimate
        usage = Usage(input_tokens=max(1, len(handle.prompt_text) // 4),
                      output_tokens=max(1, len(text) // 4),
                      latency_ms=latency_ms)
        self.check_budget(handle.decl, usage, latency_ms,
                          handle.model_id)
        return value

    def eval_struct_lit(self, e: ast.StructLit, env: Env):
        """Overtake Wave 2: `Point { x: 1.0, y: 2.0 }` — build a
        ctypes Structure instance (C layout, by value). The checker
        validated fields; re-check defensively for unchecked ASTs.

        Overtake final wave: nested aggregates (pass the inner
        instance), unions (any subset of fields; the rest zero), and
        bitfields (unsigned values; out-of-range is E300).
        """
        import ctypes
        decl = self.extern_struct_decls.get(e.name)
        if decl is None:
            raise RuntimeError_(f"unknown struct '{e.name}'",
                                line=e.line, col=e.col)
        try:
            cls = self._ffi_ctype(e.name)
        except FFIError as ex:
            raise RuntimeError_(ex.message, line=e.line, col=e.col)
        want = {fname: fte.name for fname, fte in decl.fields}
        widths = {fname: fte.bitfield_width for fname, fte in decl.fields}
        kwargs = {}
        for fname, fv in e.fields:
            if fname not in want:
                raise RuntimeError_(f"struct '{e.name}' has no field "
                                    f"'{fname}'", line=fv.line,
                                    col=fv.col)
            v = self.eval_expr(fv, env)
            ft = want[fname]
            if ft == "Bool":
                v = 1 if v else 0
            elif ft == "Ptr":
                v = v.addr if isinstance(v, PtrValue) else 0
            elif widths[fname] is not None:
                # Unsigned bitfield: clean E300 instead of ctypes'
                # ValueError on out-of-range values.
                w = widths[fname]
                if not isinstance(v, int) or isinstance(v, bool) \
                        or not (0 <= v < (1 << w)):
                    raise RuntimeError_(
                        f"bitfield '{e.name}.{fname}' value {v!r} out "
                        f"of range for :{w} (0..{(1 << w) - 1})",
                        line=fv.line, col=fv.col)
            kwargs[fname] = v
        try:
            return cls(**kwargs)
        except (TypeError, ValueError) as ex:
            raise RuntimeError_(f"bad struct '{e.name}' literal: {ex}",
                                line=e.line, col=e.col)

    def struct_field_get(self, obj, field: str, node) -> Any:
        """Overtake Wave 2: `p.x` on a struct value, converted back
        to Misa values by declared field type."""
        import ctypes
        decl = self.extern_struct_decls.get(obj._misa_name)
        if decl is None:
            raise RuntimeError_("cannot access field on struct value",
                                line=node.line, col=node.col)
        ftype = next((fte.name for fname, fte in decl.fields
                      if fname == field), None)
        if ftype is None:
            raise RuntimeError_(f"struct '{obj._misa_name}' has no "
                                f"field '{field}'", line=node.line,
                                col=node.col)
        v = getattr(obj, field)
        if ftype == "Bool":
            return bool(v)
        if ftype == "Ptr":
            return PtrValue(v or 0)
        return v

    def invoke_lambda(self, lam: LambdaValue, args: list, env: Env):
        return self.invoke_lambda_value(lam, args, None)

    def _call_fn_value(self, v: Any, args: list, node: ast.Node) -> Any:
        """Call a first-class function value (lambda or named fn).

        Used by higher-order builtins (`map`/`filter`/`fold`/`sort_by`).
        """
        if isinstance(v, LambdaValue):
            return self.invoke_lambda_value(v, args, node)
        if isinstance(v, FnValue):
            return self.call_fn(v.decl, args, node)
        raise RuntimeError_(
            f"expected a function, got {render_value(v)}",
            line=node.line, col=node.col)

    def invoke_lambda_value(self, lam: LambdaValue, args: list,
                            node: Optional[ast.Node]):
        inner = Env(lam.closure)
        for p, v in zip(lam.params, args):
            inner.define(p, v, False)
        # Phase 7: like call_fn, a lambda call is a scope (spawns inside
        # are children of this invocation).
        self._push_scope()
        # Phase 5d: mark the lambda frame so a `return f(...)` inside
        # it is NOT treated as a tail call of an outer f (the lambda
        # frame has its own post-processing; see _tco_stack).
        _tco_stack().append(("lam", lam))
        try:
            if lam.expr_body is not None:
                return self.eval_expr(lam.expr_body, inner)
            try:
                self.exec_block(lam.body, inner)
            except _ReturnSignal as r:
                return r.value
            return None
        finally:
            _tco_stack().pop()
            self._pop_scope()


class _ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


class _TailCall(Exception):
    """Phase 5d: tail-call optimization signal. Raised instead of
    recursing for `return f(args)` directly in f's own body; caught
    by f's `call_fn` frame, which rebinds the parameters and
    re-executes the body (no Python-stack growth, no new Env)."""
    def __init__(self, decl, args):
        self.decl = decl
        self.args = args
