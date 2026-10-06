"""Misa v0.2 tree-walking interpreter."""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ast
from .errors import (MisaError, RuntimeError_, UndefinedName,
                     ImmutableAssign, AssertFailed, ModelError,
                     SchemaValidationError, BudgetExceeded, ThrownError,
                     IOError_, HTTPError_, MapKeyError, ProviderConfigError,
                     FFIError)
from .providers import (Provider, Usage, make_provider, guess_mime,
                        estimate_cost_usd, OnDeviceProvider)


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


@dataclass
class LambdaValue:
    params: List[str]
    body: List[ast.Node]
    expr_body: Optional[ast.Node]
    closure: "Env"


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
    if isinstance(v, LambdaValue):
        return "<lambda>"
    if isinstance(v, FnValue):
        return f"<function {v.decl.name}>"
    if isinstance(v, SumValue):
        if not v.fields:
            return f"{v.type_name}.{v.variant}"
        inner = ", ".join(render_value(x) for x in v.fields)
        return f"{v.type_name}.{v.variant}({inner})"
    return str(v)


# ---------------------------------------------------------------- interpreter

DEFAULT_TIMEOUT_S = 120.0


class Interpreter:
    def __init__(self, *, mock_scripts: Optional[dict] = None,
                 out=None, main_dir: Optional[str] = None):
        self.mock_scripts = mock_scripts or {}
        self.out = out if out is not None else sys.stdout
        self.main_dir = main_dir  # dir of the entry .misa file, if known
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
        self.fns: Dict[str, ast.FnDecl] = {}
        # Phase 7: extern (FFI) functions: name -> (ctypes callable,
        # FnDecl signature). Loaded lazily on first load().
        self.extern_fns: Dict[str, tuple] = {}
        self.provider_cache: Dict[tuple, Provider] = {}
        self.global_env = Env()
        self.current_screen = None

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
            elif isinstance(item, ast.FnDecl):
                self.fns[item.name] = item
            elif isinstance(item, ast.ExternDecl):
                self._load_extern(item)
            elif isinstance(item, ast.ImportDecl):
                pass  # resolved by the loader before load()

    # -- Phase 7: FFI ---------------------------------------------------
    # Maps Misa extern signatures onto ctypes. Text crosses as
    # null-terminated UTF-8 copies (both directions); Ptr is an opaque
    # address. Load/symbol failures raise FFIError (E226), never a
    # raw traceback.

    @staticmethod
    def _ffi_ctype(tname: str):
        import ctypes
        return {"Int": ctypes.c_long,
                "Float": ctypes.c_double,
                "Text": ctypes.c_char_p,
                "Bool": ctypes.c_int,
                "Ptr": ctypes.c_void_p}[tname]

    @staticmethod
    def _ffi_restype(tname: str):
        import ctypes
        if tname == "Void":
            return None
        return Interpreter._ffi_ctype(tname)

    def _load_extern(self, decl: ast.ExternDecl):
        import ctypes
        import ctypes.util
        lib = decl.lib
        try:
            if lib == "c":
                cdll = ctypes.CDLL(None)
            else:
                resolved = ctypes.util.find_library(lib)
                cdll = ctypes.CDLL(resolved or ("lib" + lib))
        except OSError as ex:
            raise FFIError(f"cannot load library '{lib}': {ex}",
                           line=decl.line, col=decl.col)
        for fn in decl.fns:
            try:
                cfn = getattr(cdll, fn.name)
            except AttributeError:
                raise FFIError(
                    f"library '{lib}' has no symbol '{fn.name}'",
                    line=fn.line, col=fn.col)
            # The checker guarantees FFI-mappable simple types, so
            # te.name is the whole story here.
            cfn.argtypes = [self._ffi_ctype(te.name)
                            for _, te in fn.params]
            cfn.restype = self._ffi_restype(
                fn.ret.name if fn.ret else "Void")
            self.extern_fns[fn.name] = (cfn, fn)

    def call_extern(self, name: str, args: list, node) -> Any:
        cfn, fn = self.extern_fns[name]
        c_args = []
        for (pn, te), a in zip(fn.params, args):
            t = te.name
            if t == "Text":
                c_args.append(a.encode("utf-8"))
            elif t == "Ptr":
                c_args.append(a.addr if isinstance(a, PtrValue) else 0)
            elif t == "Bool":
                c_args.append(1 if a else 0)
            else:
                c_args.append(a)
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
        return r

    def run(self, prog: ast.Program):
        self.load(prog)
        for item in prog.items:
            if isinstance(item, (ast.AppDecl, ast.ProviderDecl,
                                 ast.ModelsDecl, ast.TypeDecl,
                                 ast.TableDecl,
                                 ast.ModelDecl, ast.ScreenDecl,
                                 ast.EvalDecl, ast.FnDecl,
                                 ast.ExternDecl,
                                 ast.ImportDecl)):
                continue
            try:
                self.exec_stmt(item, self.global_env)
            except _ReturnSignal:
                break

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
            val = self.eval_expr(s.value, env) \
                if s.value is not None else None
            raise _ReturnSignal(val)
        elif isinstance(s, ast.TryStmt):
            try:
                self.exec_block(s.body, Env(env))
            except _ReturnSignal:
                raise
            except MisaError as e:
                inner = Env(env)
                inner.define(s.catch_var,
                             {"code": e.code, "message": e.message}, False)
                self.exec_block(s.handler, inner)
        elif isinstance(s, ast.ThrowStmt):
            v = self.eval_expr(s.value, env)
            if not isinstance(v, dict) or "code" not in v \
                    or "message" not in v:
                raise RuntimeError_("throw needs { code: Text, "
                                    "message: Text }")
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
            return e.value
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
                return env.lookup(e.name)[0]
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
                            f"'{decl.name}'")
                    if v.fields:
                        raise RuntimeError_(
                            f"variant '{decl.name}.{v.name}' needs "
                            f"payload; use "
                            f"'{decl.name}.{v.name}(...)'")
                    return SumValue(decl.name, v.name, [])
            obj = self.eval_expr(e.obj, env)
            if isinstance(obj, dict):
                if e.field not in obj:
                    raise RuntimeError_(f"no field '{e.field}'")
                return obj[e.field]
            raise RuntimeError_("cannot access field on "
                                f"{type(obj).__name__}")
        if isinstance(e, ast.Match):
            return self.eval_match(e, env)
        if isinstance(e, ast.Index):
            obj = self.eval_expr(e.obj, env)
            idx = self.eval_expr(e.idx, env)
            if isinstance(obj, MapValue):
                if idx in obj.data:
                    return obj.data[idx]
                raise MapKeyError(f"missing map key {idx!r}")
            if isinstance(obj, (list, str)) and isinstance(idx, int):
                try:
                    return obj[idx]
                except IndexError:
                    raise RuntimeError_("index out of range")
            raise RuntimeError_("cannot index here")
        if isinstance(e, ast.ListLit):
            return [self.eval_expr(i, env) for i in e.items]
        if isinstance(e, ast.MapLit):
            return MapValue({k: self.eval_expr(v, env)
                             for k, v in e.entries})
        if isinstance(e, ast.RecordLit):
            return {k: self.eval_expr(v, env) for k, v in e.fields}
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
                return -v
            raise RuntimeError_(f"unknown unary {e.op}")
        if isinstance(e, ast.Await):
            handle = self.eval_expr(e.operand, env)
            if not isinstance(handle, TokenStreamHandle):
                raise RuntimeError_("'await' needs a stream")
            return self.drain_stream(handle, env)
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
        if op == "+":
            if isinstance(l, str) or isinstance(r, str):
                return render_value(l) + render_value(r)
            return l + r
        if op == "-":
            return l - r
        if op == "*":
            return l * r
        if op == "/":
            return l / r
        if op == "%":
            return l % r
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        if op == "<":
            return l < r
        if op == "<=":
            return l <= r
        if op == ">":
            return l > r
        if op == ">=":
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
                raise IOError_(f"fs.read failed: {ex}")
        if name == "fs.write":
            try:
                with open(args[0], "w", encoding="utf-8") as f:
                    f.write(args[1])
                return None
            except OSError as ex:
                raise IOError_(f"fs.write failed: {ex}")
        if name == "http.get":
            return self.http_get(args[0])
        # -- Phase 5 (DB): SQLite bindings ------------------------------
        if name == "db.open":
            return self.db_open(args[0], e)
        if name == "db.exec":
            return self.db_exec(args[0], args[1],
                                args[2] if len(args) > 2 else [], e)
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
            try:
                return chr(args[0])
            except (ValueError, OverflowError):
                raise RuntimeError_(
                    f"str.chr: invalid code point {args[0]}")
        if name == "process.run":
            return self.process_run(args[0], e)
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
                "process.run([Text]) needs a non-empty text list")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=300)
            return {"code": r.returncode, "stdout": r.stdout,
                    "stderr": r.stderr}
        except FileNotFoundError:
            raise IOError_(f"process.run: command not found: {cmd[0]}")
        except subprocess.TimeoutExpired:
            raise IOError_(f"process.run timed out (300s): {cmd[0]}")
        except OSError as ex:
            raise IOError_(f"process.run failed: {ex}")

    def call_fn(self, decl: ast.FnDecl, args: list, node: ast.Node) -> Any:
        local = Env(self.global_env)
        for (pn, _), v in zip(decl.params, args):
            local.define(pn, v, False)
        try:
            self.exec_block(decl.body, local)
        except _ReturnSignal as r:
            return r.value
        return None

    def http_get(self, url: str) -> str:
        if not isinstance(url, str) or not url.startswith(
                ("http://", "https://")):
            raise HTTPError_(f"http.get needs an http(s) URL, got {url!r}")
        req = urllib.request.Request(
            url, headers={"User-Agent": "misa/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise HTTPError_(
                        f"http.get {url} -> HTTP {resp.status}")
                raw = resp.read()
        except urllib.error.HTTPError as ex:
            raise HTTPError_(f"http.get {url} -> HTTP {ex.code}")
        except urllib.error.URLError as ex:
            raise HTTPError_(f"http.get failed: {ex.reason}")
        except TimeoutError:
            raise HTTPError_(f"http.get timed out: {url}")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPError_(f"http.get {url}: body is not UTF-8 text")

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

    def db_exec(self, dbv, sql, args, node: ast.Node) -> int:
        h = self._db_handle(dbv, "db.exec", node)
        if not isinstance(sql, str):
            raise RuntimeError_("db.exec needs a text SQL statement",
                                line=node.line, col=node.col)
        try:
            cur = h.conn.execute(sql, list(args))
            h.conn.commit()
            return cur.rowcount if cur.rowcount >= 0 else 0
        except sqlite3.Error as ex:
            raise IOError_(f"db.exec failed: {ex}",
                           line=node.line, col=node.col)

    def db_query(self, dbv, sql, args, node: ast.Node) -> list:
        h = self._db_handle(dbv, "db.query", node)
        if not isinstance(sql, str):
            raise RuntimeError_("db.query needs a text SQL statement",
                                line=node.line, col=node.col)
        try:
            cur = h.conn.execute(sql, list(args))
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

    def call_builtin(self, name: str, args: list, node: ast.Node) -> Any:
        if name == "print":
            self.out.write(render_value(args[0]) + "\n")
            return render_value(args[0])
        if name == "len":
            return len(args[0])
        if name == "str":
            return render_value(args[0])
        if name == "int":
            try:
                return int(args[0])
            except (ValueError, TypeError):
                raise RuntimeError_(f"cannot parse int from {args[0]!r}")
        if name == "float":
            try:
                return float(args[0])
            except (ValueError, TypeError):
                raise RuntimeError_(f"cannot parse float from {args[0]!r}")
        if name == "assert":
            msg = args[1] if len(args) > 1 else "assertion failed"
            if not self.truthy(args[0]):
                raise AssertFailed(render_value(msg))
            return True
        if name == "env":
            val = os.environ.get(args[0])
            if val is None:
                raise RuntimeError_(f"env var '{args[0]}' is not set")
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
                parts.append(f"{an}=<handler>")
            else:
                parts.append(f"{an}={render_value(self.eval_expr(val, env))}")
        self.out.write(pad + comp.name +
                       (f"({', '.join(parts)})" if parts else "") + "\n")
        for child in comp.children:
            self.print_screen_item(child, dict(scope), depth + 1, ids)

    # -- model calls --------------------------------------------------

    def get_provider(self, decl: ast.ModelDecl) -> tuple:
        if decl.alias not in self.aliases:
            raise ModelError(f"unknown model alias '{decl.alias}'")
        prov_name, model_id = self.aliases[decl.alias]
        if prov_name not in self.providers:
            raise ModelError(f"unknown provider '{prov_name}'")
        pdecl = self.providers[prov_name]
        if pdecl.model_file:
            # on-device: the file (not the name) selects the runtime
            mpath = self._resolve_model_file(pdecl)
            key = (prov_name, decl.alias, mpath)
            if key not in self.provider_cache:
                self.provider_cache[key] = OnDeviceProvider(mpath)
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
            hint="checked: " + ", ".join(cands))

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
                         "names only in v0.1")

    def call_model(self, decl: ast.ModelDecl, arg_values: list,
                   node: ast.Node) -> Any:
        if len(arg_values) != len(decl.params):
            raise ModelError(
                f"model '{decl.name}' takes {len(decl.params)} args, "
                f"got {len(arg_values)}")
        provider, model_id = self.get_provider(decl)
        if isinstance(provider, OnDeviceProvider):
            return self.call_ondevice(provider, decl, model_id, arg_values)
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
                hint="check the return type declaration")
        if value is None and errors:
            raise SchemaValidationError(
                f"model '{decl.name}' returned invalid data",
                validation_errors=errors)
        latency_ms = int((time.monotonic() - t0) * 1000)
        self.check_budget(decl, usage, latency_ms, model_id)
        return value

    def call_ondevice(self, provider: OnDeviceProvider,
                      decl: ast.ModelDecl, model_id: str,
                      arg_values: list) -> Any:
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
        comp = provider.complete(model_id=model_id, prompt="", images=[],
                                 timeout_s=timeout_s, args=arg_values,
                                 params=decl.params, ret=decl.ret)
        try:
            data = json.loads(comp.text)
        except json.JSONDecodeError:
            raise SchemaValidationError(
                f"on-device model '{decl.name}' returned invalid data")
        value, errors = self.validate(data, decl.ret, "$")
        if errors:
            raise SchemaValidationError(
                f"on-device model '{decl.name}' returned invalid data",
                validation_errors=errors,
                hint="check the return type declaration")
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
        if b.tokens is not None and usage.total_tokens > b.tokens:
            raise BudgetExceeded(
                f"token budget exceeded: used {usage.total_tokens}, "
                f"budget {b.tokens}")
        if b.latency_ms is not None and latency_ms > b.latency_ms:
            raise BudgetExceeded(
                f"latency budget exceeded: took {latency_ms}ms, "
                f"budget {b.latency_ms}ms")
        if b.cost_usd is not None:
            cost = estimate_cost_usd(model_id, usage)
            if cost > b.cost_usd:
                raise BudgetExceeded(
                    f"cost budget exceeded: ~${cost:.4f}, "
                    f"budget ${b.cost_usd:.4f} (estimated)")

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
            raise ModelError(f"stream failed: {e}")
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

    def invoke_lambda(self, lam: LambdaValue, args: list, env: Env):
        return self.invoke_lambda_value(lam, args, None)

    def invoke_lambda_value(self, lam: LambdaValue, args: list,
                            node: Optional[ast.Node]):
        inner = Env(lam.closure)
        for p, v in zip(lam.params, args):
            inner.define(p, v, False)
        if lam.expr_body is not None:
            return self.eval_expr(lam.expr_body, inner)
        try:
            self.exec_block(lam.body, inner)
        except _ReturnSignal as r:
            return r.value
        return None


class _ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value
