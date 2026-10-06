"""Misa v0.2 static checker: name resolution + pragmatic type checking.

Collects diagnostics (does not raise on first error):
  {code, message, line, col, hint}
"""
from __future__ import annotations
import os
import re
from typing import Dict, List, Optional
from . import ast


PRIMITIVES = {"Text", "Int", "Float", "Bool", "Error", "Void",
              # Phase 7: `Ptr` — opaque C pointer for FFI. Values can be
              # passed back to C and compared to nil, never dereferenced.
              "Ptr"}
MEDIA = {"Photo", "Audio", "Video", "Document"}
SPECIAL = {"TokenStream", "Embedding", "Db"}
# Phase 7 (concurrency): builtin generic type names, exactly 1 type
# argument (like List<T>). `Task<Text>`, `Chan<Int>` are valid
# annotations; bare `Task`/`Chan` is an error (E221, suggesting the
# generic form).
CONCURRENCY_GENERICS = {"Task", "Chan"}
BUILTINS = {
    "print": (["Text"], "Text"),  # print accepts anything; modeled loosely
    "len": (["any"], "Int"),
    "str": (["any"], "Text"),
    "int": (["Text"], "Int"),
    "float": (["Text"], "Float"),
    "assert": (["Bool"], "Text"),
    "env": (["Text"], "Text"),
    "navigate": (["any"], "Text"),
}
BUILTIN_ARITY = {
    "print": (1, 1), "len": (1, 1), "str": (1, 1), "int": (1, 1),
    "float": (1, 1), "assert": (1, 2), "env": (1, 1), "navigate": (1, 1),
}
# namespaced builtins: name -> (param types, return type)
NS_BUILTINS = {
    "fs.read": (["Text"], "Text"),
    "fs.write": (["Text", "Text"], "Void"),
    "http.get": (["Text"], "Text"),
    # Phase 6 prerequisites (self-hosting): string utilities for the
    # self-hosted lexer, and subprocess for the self-hosted AOT driver.
    "str.slice": (["Text", "Int", "Int"], "Text"),
    "str.starts_with": (["Text", "Text"], "Bool"),
    "str.split": (["Text", "Text"], "[Text]"),
    "str.chars": (["Text"], "[Text]"),
    "str.trim": (["Text"], "Text"),
    "str.ord": (["Text"], "Int"),
    "str.chr": (["Int"], "Text"),
    "process.run": (["[Text]"], "record"),
    # Phase 5 (DB): SQLite bindings. `db.exec`/`db.query` take an
    # optional third `args` list (see NS_BUILTIN_ARITY). Bind args are
    # heterogeneous scalars (H-3): Int/Float/Bool/Text/nil, bound natively
    # on both backends — hence [any], not [Text].
    "db.open": (["Text"], "Db"),
    "db.exec": (["Db", "Text", "[any]"], "Int"),
    "db.query": (["Db", "Text", "[any]"], "[record]"),
    "db.close": (["Db"], "Void"),
    # Phase 5 (stdlib2): JSON, HTTP POST, time, math.
    "json.parse": (["Text"], "unknown"),
    "json.stringify": (["any"], "Text"),
    "http.post": (["Text", "Text"], "Text"),
    "http.post_json": (["Text", "any"], "Text"),
    "time.now": ([], "Int"),
    "time.now_ms": ([], "Int"),
    "time.format": (["Int", "Text"], "Text"),
    "time.parse": (["Text", "Text"], "Int"),
    "math.sqrt": (["Float"], "Float"),
    "math.pow": (["Float", "Float"], "Float"),
    "math.abs": (["Float"], "Float"),
    "math.floor": (["Float"], "Int"),
    "math.ceil": (["Float"], "Int"),
    "math.round": (["Float"], "Int"),
}
# Phase 5 (DB): namespaced builtins with optional trailing args:
# name -> (min_arity, max_arity). Absent = exact arity from NS_BUILTINS.
NS_BUILTIN_ARITY = {
    "db.exec": (2, 3),
    "db.query": (2, 3),
}

LAYOUT_COMPONENTS = {"Column", "Row"}
LEAF_COMPONENTS = {
    "Text", "Heading", "Button", "TextInput", "Image", "List",
    "Banner", "CameraButton", "Spacer",
}
ALL_COMPONENTS = LAYOUT_COMPONENTS | LEAF_COMPONENTS

# Phase 5 (DB): minimal SELECT shape for typed db.query validation.
_DB_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+WHERE\s+(?P<where>.+?))?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL)
_DB_WHERE_KW = {"and", "or", "not", "null", "like", "in", "is", "between",
                "true", "false", "nil", "select", "from", "where"}


def _split_top_commas(s: str) -> List[str]:
    """Split on commas not nested inside parentheses."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


class Checker:
    def __init__(self):
        self.errors: List[dict] = []
        self.types: Dict[str, ast.TypeDecl] = {}
        self.models: Dict[str, ast.ModelDecl] = {}
        self.providers: Dict[str, ast.ProviderDecl] = {}
        self.aliases: Dict[str, tuple] = {}  # alias -> (provider, model_id)
        self.screens: Dict[str, ast.ScreenDecl] = {}
        self.evals: Dict[str, ast.EvalDecl] = {}
        self.fns: Dict[str, ast.FnDecl] = {}
        # Phase 7: extern (FFI) functions: name -> FnDecl (signature
        # only), plus name -> library string.
        self.externs: Dict[str, ast.FnDecl] = {}
        self.extern_libs: Dict[str, str] = {}
        self.app_seen = False
        self._file_of: Optional[Dict[int, str]] = None
        # Phase 4 (M4): id: name -> component kind for the screen whose
        # handler body is currently being checked (None elsewhere).
        self._cur_id_kinds: Optional[Dict[str, str]] = None
        # Phase 5c: type parameters in scope while checking a generic
        # fn or type decl (e.g. `T` in `fn first<T>(...)`).
        self._tparams: List[str] = []
        # Phase 5 (DB): declared table schemas, name -> TableDecl.
        self.tables: Dict[str, ast.TableDecl] = {}
        # Phase 5 (DB): counter for synthetic query-result record types.
        self._query_seq = 0

    # -- diagnostics ----------------------------------------------------

    def error(self, code: str, message: str, line: int, col: int,
              hint: str = ""):
        self.errors.append({"code": code, "message": message,
                            "line": line, "col": col, "hint": hint})

    # -- entry ----------------------------------------------------------

    def check(self, prog: ast.Program,
              file_of: Optional[Dict[int, str]] = None) -> List[dict]:
        self._file_of = file_of
        self._collect(prog)
        scope: Dict[str, tuple] = {}  # global scope persists across items
        for item in prog.items:
            self._check_item(item, scope)
        return self.errors

    def _collect(self, prog: ast.Program):
        for item in prog.items:
            if isinstance(item, ast.AppDecl):
                if self.app_seen:
                    self.error("E210", "duplicate 'app' block",
                               item.line, item.col)
                self.app_seen = True
            elif isinstance(item, ast.ProviderDecl):
                self._dup(self.providers, item.name, item, "provider")
            elif isinstance(item, ast.ModelsDecl):
                for alias, prov, mid in item.entries:
                    if alias in self.aliases:
                        self.error("E210",
                                   f"duplicate model alias '{alias}'",
                                   item.line, item.col)
                    self.aliases[alias] = (prov, mid)
            elif isinstance(item, ast.TypeDecl):
                self._dup(self.types, item.name, item, "type")
            elif isinstance(item, ast.TableDecl):
                self._dup(self.tables, item.name, item, "table")
            elif isinstance(item, ast.ModelDecl):
                self._dup(self.models, item.name, item, "model")
            elif isinstance(item, ast.ScreenDecl):
                self._dup(self.screens, item.name, item, "screen")
            elif isinstance(item, ast.EvalDecl):
                self._dup(self.evals, item.name, item, "eval")
            elif isinstance(item, ast.FnDecl):
                self._dup(self.fns, item.name, item, "function")
            elif isinstance(item, ast.ExternDecl):
                for fn in item.fns:
                    self._dup(self.externs, fn.name, fn, "extern function")
                    self.extern_libs[fn.name] = item.lib
            elif isinstance(item, ast.ImportDecl):
                pass  # resolved by the loader before checking

    def _dup(self, table: dict, name: str, item, kind: str):
        prev = table.get(name)
        if prev is not None:
            f1 = self._file_of.get(id(prev)) if self._file_of else None
            f2 = self._file_of.get(id(item)) if self._file_of else None
            if f1 and f2 and f1 != f2:
                self.error("E110",
                           f"duplicate {kind} '{name}' "
                           f"(also defined in {os.path.basename(f1)})",
                           item.line, item.col,
                           hint="imports share one flat namespace")
            else:
                self.error("E210", f"duplicate {kind} '{name}'",
                           item.line, item.col)
        table[name] = item

    # -- items ----------------------------------------------------------

    def _check_item(self, item: ast.Node, scope: dict):
        if isinstance(item, ast.ModelDecl):
            self._check_model(item)
        elif isinstance(item, ast.ScreenDecl):
            self._check_screen(item)
        elif isinstance(item, ast.EvalDecl):
            self._check_eval(item)
        elif isinstance(item, ast.FnDecl):
            self._check_fn(item)
        elif isinstance(item, ast.ExternDecl):
            self._check_extern(item)
        elif isinstance(item, ast.TypeDecl):
            self._check_type_decl(item)
        elif isinstance(item, ast.TableDecl):
            self._check_table_decl(item)
        elif isinstance(item, (ast.AppDecl, ast.ProviderDecl,
                               ast.ModelsDecl,
                               ast.ImportDecl)):
            pass
        else:
            self._check_block([item], scope)

    def _check_type_decl(self, decl: ast.TypeDecl):
        """Phase 5c: validate a type decl's field/variant types. For
        generic decls, the type parameters are in scope (unbound names
        are E222); otherwise an unknown field type is E208 (M-2: plain
        `type T { x: Wat }` must not pass silently)."""
        saved_tparams, self._tparams = self._tparams, list(decl.type_params)
        try:
            if decl.is_sum:
                for v in decl.variants:
                    for _, fte in v.fields:
                        self._check_te(fte, fte.line, fte.col)
            else:
                for _, fte in decl.fields:
                    self._check_te(fte, fte.line, fte.col)
        finally:
            self._tparams = saved_tparams

    def _check_table_decl(self, decl: ast.TableDecl):
        """Phase 5 (DB): validate a table decl's column types."""
        for _, fte, _ in decl.fields:
            self._check_te(fte, fte.line, fte.col)

    # Phase 7: FFI type mapping (v1). Only these Misa types may cross
    # the boundary; everything else (structs, function types, lists,
    # maps, generics) is rejected with E225.
    FFI_TYPES = {"Int", "Float", "Text", "Bool", "Ptr", "Void"}

    def _check_extern(self, decl: ast.ExternDecl):
        """Phase 7: validate an `extern` block. Library name must be a
        non-empty string; each signature may only use FFI-mappable
        types (E225 otherwise); no type parameters (E225); names must
        not collide with Misa functions (E210)."""
        if not decl.lib:
            self.error("E225", "extern library name is empty",
                       decl.line, decl.col)
        for fn in decl.fns:
            if fn.name in self.fns:
                self.error("E210",
                           f"extern function '{fn.name}' collides with "
                           f"a Misa function of the same name",
                           fn.line, fn.col)
                continue
            if fn.type_params:
                self.error("E225",
                           f"extern function '{fn.name}' must not have "
                           f"type parameters (generics are not supported "
                           f"across FFI)",
                           fn.line, fn.col)
            for pn, te in fn.params:
                self._check_ffi_type(te, fn.name, pn)
            if fn.ret is not None:
                self._check_ffi_type(fn.ret, fn.name, "return")

    def _check_ffi_type(self, te: ast.TypeExpr, fn_name: str,
                        where: str):
        t = self._tname(te)
        if t in self.FFI_TYPES:
            return
        self.error("E225",
                   f"extern function '{fn_name}': {where} has type "
                   f"'{t}', which cannot cross the FFI boundary "
                   f"(v1 supports only Int, Float, Text, Bool, Ptr, Void)",
                   te.line, te.col)

    def _check_fn(self, fn: ast.FnDecl):
        seen_params = set()
        # Phase 5c: type parameters are in scope for the signature.
        saved_tparams, self._tparams = self._tparams, list(fn.type_params)
        for pn, te in fn.params:
            if pn in seen_params:
                self.error("E210", f"duplicate parameter '{pn}'",
                           te.line, te.col)
            seen_params.add(pn)
            self._check_te(te, te.line, te.col)
        ret_name = self._tname(fn.ret) if fn.ret else "Void"
        if fn.ret:
            self._check_te(fn.ret, fn.ret.line, fn.ret.col)
        scope = {pn: (self._tname(te), False) for pn, te in fn.params}
        self._check_block(fn.body, scope, fn_ret=ret_name)
        if ret_name != "Void" and not self._block_returns(fn.body):
            # W-1(c): point at the leaking path, not the fn header.
            leak = self._leak_node(fn.body)
            eline, ecol = ((leak.line, leak.col) if leak is not None
                           else (fn.line, fn.col))
            self.error("E211",
                       f"function '{fn.name}' may exit without "
                       f"returning {ret_name}",
                       eline, ecol,
                       hint="every code path must end in 'return'")
        self._tparams = saved_tparams

    @staticmethod
    def _block_returns(stmts: list) -> bool:
        for s in stmts:
            if isinstance(s, ast.ReturnStmt):
                return True
            if isinstance(s, ast.IfStmt) and s.els:
                if Checker._block_returns(s.then) and \
                        Checker._block_returns(s.els):
                    return True
        return False

    @staticmethod
    def _leak_node(stmts: list):
        """W-1(c): find a statement on a path that exits without
        returning, so E211 can point at the offending path instead of
        the function header. Only called when _block_returns is False.
        Returns None when no specific statement can be blamed."""
        for s in stmts:
            if isinstance(s, ast.ReturnStmt):
                continue  # this path returns; the leak is elsewhere
            if isinstance(s, ast.IfStmt) and s.els:
                if Checker._block_returns(s.then) and \
                        Checker._block_returns(s.els):
                    continue
                # the leak is inside whichever branch doesn't return
                if not Checker._block_returns(s.then):
                    return Checker._leak_node(s.then) or s
                return Checker._leak_node(s.els) or s
            # any other statement: the path falls through here
            return s
        return None

    def _check_model(self, m: ast.ModelDecl):
        if m.provider not in self.providers:
            self.error("E209", f"unknown provider '{m.provider}'",
                       m.line, m.col,
                       hint="declare it in a 'provider' block")
        else:
            pdecl = self.providers[m.provider]
            if pdecl.model_file:
                from .providers import ondevice_runtime
                try:
                    ondevice_runtime(pdecl.model_file)
                except Exception as e:
                    self.error("E209",
                               f"provider '{m.provider}': {e}",
                               m.line, m.col,
                               hint="on-device model files end in .onnx, "
                                    ".mlpackage, .mlmodelc, or .tflite")
            elif m.provider not in ("mock", "openai", "gemini"):
                self.error("E209",
                           f"unknown provider kind '{m.provider}'",
                           m.line, m.col,
                           hint="v0.1 providers: mock, openai, gemini")
        if m.alias not in self.aliases:
            self.error("E209", f"unknown model alias '{m.alias}'",
                       m.line, m.col,
                       hint="declare it in the 'models' block")
        else:
            prov, _ = self.aliases[m.alias]
            if prov != m.provider:
                self.error("E209",
                           f"alias '{m.alias}' belongs to provider "
                           f"'{prov}', not '{m.provider}'",
                           m.line, m.col)
        if m.ret is not None:
            self._check_te(m.ret, m.line, m.col)
        elif not self._known_type(m.ret):
            self.error("E208",
                       f"unknown return type '{self._tname(m.ret)}'",
                       m.line, m.col)
        for _, te in m.params:
            self._check_te(te, te.line, te.col)
        # prompt interpolations may only reference params
        scope = {pn: (self._tname(te), False) for pn, te in m.params}
        for part in m.prompt:
            if isinstance(part, ast.Node):
                self._infer(part, scope)

    def _check_screen(self, s: ast.ScreenDecl):
        for pn, te in s.params:
            self._check_te(te, te.line, te.col)
        scope = {pn: (self._tname(te), False) for pn, te in s.params}
        seen_ids = set()
        # Phase 4 (M4): ordered id: binding table (name -> component
        # kind), slots 0..n-1. Ids inside ScreenFor bodies are excluded
        # (no static slot; AOT rejects those with E600).
        bindings: List[tuple] = []
        id_kinds: Dict[str, str] = {}
        for item in s.body:
            self._check_screen_item(item, scope, seen_ids, bindings,
                                    id_kinds, False)
        s.id_bindings = bindings

    def _check_screen_item(self, item: ast.Node, scope: dict,
                           seen_ids: set, bindings: list,
                           id_kinds: dict, in_for: bool):
        if isinstance(item, ast.Component):
            self._check_component(item, scope, seen_ids, bindings,
                                  id_kinds, in_for)
        elif isinstance(item, ast.ScreenIf):
            self._infer(item.cond, scope)
            for x in item.then:
                self._check_screen_item(x, dict(scope), seen_ids,
                                        bindings, id_kinds, in_for)
            for x in item.els:
                self._check_screen_item(x, dict(scope), seen_ids,
                                        bindings, id_kinds, in_for)
        elif isinstance(item, ast.ScreenFor):
            self._infer(item.iter, scope)
            inner = dict(scope)
            inner[item.var] = ("unknown", False)
            for x in item.body:
                self._check_screen_item(x, inner, seen_ids, bindings,
                                        id_kinds, True)

    def _check_component(self, c: ast.Component, scope: dict,
                         seen_ids: set, bindings: list, id_kinds: dict,
                         in_for: bool):
        if c.name not in ALL_COMPONENTS:
            self.error("E206", f"unknown component '{c.name}'",
                       c.line, c.col,
                       hint=f"known: {', '.join(sorted(ALL_COMPONENTS))}")
            return
        if c.name in LEAF_COMPONENTS and c.children:
            self.error("E206",
                       f"component '{c.name}' cannot have children",
                       c.line, c.col)
        for an, val in c.args:
            if an == "id":
                # L-2: the parser (expect_ident) already rejects a
                # non-identifier id: with E001, so `val` is always an
                # Ident here — the old "'id:' needs a plain name" branch
                # was dead code.
                if val.name in seen_ids:
                    self.error("E207",
                               f"duplicate component id '{val.name}'",
                               val.line, val.col)
                else:
                    seen_ids.add(val.name)
                    id_kinds[val.name] = c.name
                    if not in_for:
                        bindings.append((val.name, c.name))
            else:
                # Phase 5a: handler lambdas are checked by
                # _check_handler_body below (with the id: scope); running
                # them through _infer first would check the body without
                # _cur_id_kinds and spuriously fail on `<id>.text`.
                if not isinstance(val, ast.Lambda):
                    self._infer(val, scope)
                # Phase 4 (M4): event-handler bodies are real compiled
                # functions now, so check them with the handler scope:
                # lambda params + screen id: bindings. Closures over
                # other names are not supported (the AOT emitter
                # rejects them with E600).
                if isinstance(val, ast.Lambda):
                    self._check_handler_body(val, scope, id_kinds)
        for child in c.children:
            self._check_screen_item(child, scope, seen_ids, bindings,
                                    id_kinds, in_for)

    def _check_handler_body(self, lam: ast.Lambda, scope: dict,
                            id_kinds: dict):
        if len(lam.params) > 1:
            self.error("E206",
                       f"event handler takes at most 1 param "
                       f"(the event), got {len(lam.params)}",
                       lam.line, lam.col)
            return
        hscope = dict(scope)
        for pn, _ in lam.params:
            hscope[pn] = ("unknown", False)
        prev = self._cur_id_kinds
        self._cur_id_kinds = id_kinds
        try:
            if lam.expr_body is not None:
                self._infer(lam.expr_body, hscope)
            else:
                self._check_block(lam.body, hscope)
        finally:
            self._cur_id_kinds = prev

    def _check_eval(self, e: ast.EvalDecl):
        m = self.models.get(e.model_name)
        if m is None:
            self.error("E101", f"unknown model '{e.model_name}'",
                       e.line, e.col)
            return
        params = [pn for pn, _ in m.params]
        given_keys = [k for k, _ in e.given]
        if sorted(given_keys) != sorted(params):
            self.error("E203",
                       f"eval 'given' keys {given_keys} do not match "
                       f"model params {params}", e.line, e.col)
        scope = {pn: (self._tname(te), False) for pn, te in m.params}
        for _, v in e.given:
            self._infer(v, scope)
        t = self._infer(e.expect, {**scope, "result":
                                   (self._tname(m.ret), False)})
        if t != "Bool":
            self.error("E204", "'expect' must be a Bool expression",
                       e.expect.line, e.expect.col)

    # -- statements -----------------------------------------------------

    def _check_block(self, stmts: list, scope: dict, fn_ret=None,
                     _ret_collect=None):
        for s in stmts:
            self._check_stmt(s, scope, fn_ret, _ret_collect)

    def _check_stmt(self, s: ast.Node, scope: dict, fn_ret=None,
                    _ret_collect=None):
        if isinstance(s, ast.LetStmt):
            if s.name in scope:
                self.error("E210", f"redefinition of '{s.name}'",
                           s.line, s.col)
            # Phase 5a: an annotated lambda gets the annotation as its
            # expected type (untyped params infer from it).
            exp = self._tname(s.ann) if s.ann else None
            t = self._infer(s.value, scope, expected=exp)
            if t == "Void":
                self.error("E211",
                           f"cannot use Void result of '{s.value.name}' "
                           f"as a value" if isinstance(s.value, ast.Call)
                           else "cannot use a Void value here",
                           s.line, s.col)
                t = "unknown"
            if s.ann:
                # M-2: an unknown annotation type is E208 (not a
                # downstream E200 from the failed assignability check).
                ann_ok = self._check_te(s.ann, s.ann.line, s.ann.col)
                want = self._tname(s.ann)
                if self._check_annotated(s.ann, s.value, scope):
                    t = want
                elif ann_ok and t != "unknown" \
                        and not self._assignable(want, t):
                    self.error("E200",
                               f"cannot assign {t} to '{s.name}: {want}'",
                               s.line, s.col)
                    t = want
                else:
                    t = want
            scope[s.name] = (t, s.mutable)
        elif isinstance(s, ast.AssignStmt):
            tgt = s.target
            name = self._assign_target_name(tgt)
            if name is None:
                self.error("E201", "invalid assignment target",
                           s.line, s.col)
            elif name not in scope:
                self.error("E101", f"undefined name '{name}'",
                           s.line, s.col)
            elif not scope[name][1]:
                self.error("E201", f"cannot assign to immutable '{name}'",
                           s.line, s.col)
            self._infer(s.value, scope)
        elif isinstance(s, ast.IfStmt):
            self._infer(s.cond, scope)
            self._check_block(s.then, dict(scope), fn_ret,
                                 _ret_collect=_ret_collect)
            self._check_block(s.els, dict(scope), fn_ret,
                               _ret_collect=_ret_collect)
        elif isinstance(s, ast.ForStmt):
            it = self._infer(s.iter, scope)
            # Phase 5b: bind the loop var to the list's element type
            # when known (was always "unknown").
            vt = "unknown"
            if it.startswith("[") and it.endswith("]"):
                vt = it[1:-1] or "unknown"
            inner = dict(scope)
            inner[s.var] = (vt, False)
            self._check_block(s.body, inner, fn_ret,
                              _ret_collect=_ret_collect)
        elif isinstance(s, ast.WhileStmt):
            self._infer(s.cond, scope)
            self._check_block(s.body, dict(scope), fn_ret,
                                   _ret_collect=_ret_collect)
        elif isinstance(s, ast.ReturnStmt):
            if fn_ret is None:
                if s.value is not None:
                    t = self._infer(s.value, scope)
                    if _ret_collect is not None:
                        _ret_collect.append(t)
            elif s.value is None:
                if fn_ret != "Void":
                    self.error("E211", "missing return value "
                               f"(function returns {fn_ret})",
                               s.line, s.col)
            else:
                # Phase 5a: a returned lambda infers untyped params
                # from the function's declared return type.
                t = self._infer(s.value, scope, expected=fn_ret)
                if fn_ret == "Void":
                    self.error("E211", "cannot return a value from "
                               "a Void function", s.line, s.col)
                elif t != "unknown" and not self._assignable(fn_ret, t):
                    # L-6: inside a generic fn, `return x` (x: T) against
                    # `-> K` is not a definition-site error — both are
                    # unbound type variables settled at the call site
                    # (E219/E220). Emitting E211 here produced a spurious
                    # leading error before the real E219.
                    if fn_ret in self._tparams and t in self._tparams:
                        pass
                    else:
                        self.error("E211",
                                   f"return wants {fn_ret}, got {t}",
                                   s.line, s.col)
        elif isinstance(s, ast.TryStmt):
            self._check_block(s.body, dict(scope), fn_ret,
                                   _ret_collect=_ret_collect)
            inner = dict(scope)
            inner[s.catch_var] = ("Error", False)
            self._check_block(s.handler, inner, fn_ret,
                              _ret_collect=_ret_collect)
        elif isinstance(s, ast.ThrowStmt):
            if not self._is_error_value(s.value, scope):
                # L-4: the old message ("an Error ({ code: Text,
                # message: Text })") promised a shape that cannot be
                # assigned to the nominal Error type (E200). `throw`
                # takes a record *literal* or a caught error value.
                self.error("E212", "'throw' needs an Error value: a "
                           "{ code: Text, message: Text } record literal "
                           "or a caught error",
                           s.line, s.col)
        elif isinstance(s, ast.OnStmt):
            t = self._infer(s.stream, scope)
            if t != "TokenStream":
                self.error("E205",
                           f"'on' needs a TokenStream, found {t}",
                           s.line, s.col)
            if s.event != "token":
                self.error("E206", f"unknown stream event '{s.event}'",
                           s.line, s.col,
                           hint="streams only emit 'token'")
        elif isinstance(s, ast.ExprStmt):
            self._infer(s.expr, scope)
        elif isinstance(s, ast.StreamDecl):
            if not (isinstance(s.call, ast.Call)
                    and s.call.name in self.models):
                self.error("E205",
                           "'stream' must wrap a model call",
                           s.line, s.col)
            else:
                self._check_model_call(s.call, scope)
            scope[s.name] = ("TokenStream", False)
        else:
            self.error("E001", f"cannot check {type(s).__name__}",
                       s.line, s.col)

    def _check_annotated(self, te: ast.TypeExpr, value: ast.Node,
                         scope: dict) -> bool:
        """Validate a value against an annotation with a declared type.
        Returns True when the annotation named a declared record type
        (caller then skips the generic assignability check)."""
        if te.is_map and te.name == "Map":
            want_v = self._tname(te.map_val)
            if isinstance(value, ast.MapLit):
                if te.map_key.name != "Text":
                    self.error("E202",
                               "map literal keys are Text; "
                               f"annotation wants Map<{te.map_key.name}, …>",
                               value.line, value.col)
                    return True
                for k, v in value.entries:
                    t = self._infer(v, scope)
                    if t != "unknown" and not self._assignable(want_v, t):
                        self.error("E202",
                                   f"map value wants {want_v}, got {t}",
                                   v.line, v.col)
                return True
            if isinstance(value, ast.RecordLit) and not value.fields:
                return True  # annotated empty map
            return False
        if te.name not in self.types:
            return False
        # Phase 5c: bindings for generic record literals
        # (`let b: Box<Int> = { value: 42 }`).
        bindings: Dict[str, str] = {}
        decl0 = self.types[te.name]
        if decl0.type_params and te.type_args \
                and len(te.type_args) == len(decl0.type_params):
            bindings = {p: self._tname(a)
                        for p, a in zip(decl0.type_params, te.type_args)}
        if te.is_list and isinstance(value, ast.ListLit):
            for item in value.items:
                if isinstance(item, ast.RecordLit):
                    self._check_record_fields(te.name, item, scope,
                                              bindings)
                else:
                    self.error("E202",
                               f"expected {te.name} record in list",
                               item.line, item.col)
            return True
        if not te.is_list and isinstance(value, ast.RecordLit):
            self._check_record_fields(te.name, value, scope, bindings)
            return True
        return False

    def _check_record_fields(self, type_name: str, rec: ast.RecordLit,
                             scope: dict,
                             bindings: Optional[Dict[str, str]] = None):
        decl = self.types[type_name]
        declared = {fn: fte for fn, fte in decl.fields}
        bindings = bindings or {}
        seen = set()
        for fn, fv in rec.fields:
            seen.add(fn)
            if fn not in declared:
                self.error("E202",
                           f"type '{type_name}' has no field '{fn}'",
                           fv.line, fv.col)
                continue
            fte = declared[fn]
            # Phase 5c: substitute generic bindings.
            if bindings:
                fte = self._subst_te(fte, bindings)
            # contextual typing: nested record literal against a declared
            # record type checks recursively instead of inferring "record"
            if isinstance(fv, ast.RecordLit) and not fte.is_list \
                    and fte.name in self.types:
                self._check_record_fields(fte.name, fv, scope)
                continue
            if isinstance(fv, ast.ListLit) and fte.is_list \
                    and fte.name in self.types:
                for item in fv.items:
                    if isinstance(item, ast.RecordLit):
                        self._check_record_fields(fte.name, item, scope)
                    else:
                        self.error("E202",
                                   f"field '{fn}' wants [{fte.name}] record",
                                   item.line, item.col)
                continue
            t = self._infer(fv, scope)
            want = self._tname(fte)
            if t != "unknown" and not self._assignable(want, t):
                self.error("E202",
                           f"field '{fn}' wants {want}, got {t}",
                           fv.line, fv.col)
        for fn in declared:
            if fn not in seen and not declared[fn].optional:
                self.error("E202",
                           f"missing field '{fn}' for type '{type_name}'",
                           rec.line, rec.col)

    def _is_error_value(self, v: ast.Node, scope: dict) -> bool:
        """True if v is an Error: the builtin Error type, a declared
        type shaped like one, or a matching record literal."""
        t = self._infer(v, scope)
        if t == "Error":
            return True
        if t in self.types:
            decl = self.types[t]
            fields = {fn: self._tname(fte) for fn, fte in decl.fields}
            if fields == {"code": "Text", "message": "Text"}:
                return True
        if isinstance(v, ast.RecordLit):
            shape = {}
            for fn, fv in v.fields:
                shape[fn] = self._infer(fv, scope)
            if shape == {"code": "Text", "message": "Text"}:
                return True
        return False

    def _map_val_type(self, map_t: str) -> Optional[str]:
        # "Map<K, V>" -> V  (single-level; nested maps stay opaque)
        if map_t.startswith("Map<") and map_t.endswith(">"):
            inner = map_t[4:-1]
            depth = 0
            for i, ch in enumerate(inner):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                elif ch == "," and depth == 0:
                    return inner[i + 1:].strip()
        return None

    def _assign_target_name(self, tgt) -> Optional[str]:
        if isinstance(tgt, ast.Ident):
            return tgt.name
        if isinstance(tgt, (ast.Field, ast.Index)):
            return self._assign_target_name(tgt.obj)
        return None

    # -- expressions ----------------------------------------------------

    def _infer(self, e: ast.Node, scope: dict,
               expected: Optional[str] = None) -> str:
        if isinstance(e, ast.IntLit):
            return "Int"
        if isinstance(e, ast.FloatLit):
            return "Float"
        if isinstance(e, ast.StrLit):
            for p in e.parts:
                if isinstance(p, ast.Node):
                    self._infer(p, scope)
            return "Text"
        if isinstance(e, ast.BoolLit):
            return "Bool"
        if isinstance(e, ast.NilLit):
            return "nil"
        if isinstance(e, ast.DurationLit):
            return "Int"
        if isinstance(e, ast.MoneyLit):
            return "Float"
        if isinstance(e, ast.Ident):
            # Phase 4 (M4): inside an event-handler body, a bare
            # component id is not a value (widget handles are not Misa
            # values); only `<id>.text` readback is supported.
            if self._cur_id_kinds is not None \
                    and e.name in self._cur_id_kinds:
                self.error("E201",
                           f"component id '{e.name}' cannot be used as a "
                           f"value (use '{e.name}.text' to read a "
                           f"TextInput)",
                           e.line, e.col)
                return "unknown"
            if e.name not in scope:
                # Phase 5a: an `fn` decl used as a value has its
                # function type `(params) -> ret`.
                if e.name in self.fns:
                    fn = self.fns[e.name]
                    # Phase 5c: generic fns keep a marker so calls
                    # through the variable still infer.
                    if fn.type_params:
                        return f"generic({fn.name})"
                    ps = ", ".join(self._tname(te)
                                   for _, te in fn.params)
                    r = self._tname(fn.ret) if fn.ret else "Void"
                    return f"({ps}) -> {r}"
                # Phase 5a: builtins are not values.
                if e.name in BUILTINS or e.name in NS_BUILTINS:
                    self.error("E214",
                               f"'{e.name}' is a builtin and cannot be "
                               f"used as a value",
                               e.line, e.col)
                    return "unknown"
                # M-7: extern (FFI) functions are not first-class
                # values either — E214, not a misleading E101.
                if e.name in self.externs:
                    self.error("E214",
                               f"'{e.name}' is an extern function and "
                               f"cannot be used as a value",
                               e.line, e.col)
                    return "unknown"
                # maybe a model/screen/type used as value?
                if e.name in self.models or e.name in self.screens:
                    return "unknown"
                self.error("E101", f"undefined name '{e.name}'",
                           e.line, e.col)
                return "unknown"
            return scope[e.name][0]
        if isinstance(e, ast.Call):
            return self._infer_call(e, scope, expected)
        if isinstance(e, ast.Field):
            # Phase 4 (M4): `<id>.text` readback inside handler bodies.
            # Only TextInput.text exists in the first slice.
            if isinstance(e.obj, ast.Ident) \
                    and self._cur_id_kinds is not None \
                    and e.obj.name in self._cur_id_kinds:
                kind = self._cur_id_kinds[e.obj.name]
                if e.field == "text" and kind == "TextInput":
                    return "Text"
                self.error("E101",
                           f"component id '{e.obj.name}' ({kind}) has no "
                           f"readable field '.{e.field}' "
                           f"(only TextInput.text is supported)",
                           e.line, e.col)
                return "unknown"
            # Phase 5a: namespaced builtins are not values either.
            if isinstance(e.obj, ast.Ident):
                dotted = f"{e.obj.name}.{e.field}"
                if dotted in NS_BUILTINS:
                    self.error("E214",
                               f"'{dotted}' is a builtin and cannot be "
                               f"used as a value",
                               e.line, e.col)
                    return "unknown"
            # Phase 5b: bare zero-payload variant, e.g. `Opt.None`.
            if isinstance(e.obj, ast.Ident):
                decl = self.types.get(e.obj.name)
                if decl is not None and decl.is_sum:
                    # Phase 5c: thread expected for generic nil.
                    return self._check_bare_variant(decl, e, expected)
            # Error.code / Error.message on the builtin error type
            if isinstance(e.obj, ast.Ident) and e.obj.name in scope \
                    and scope[e.obj.name][0] == "Error" \
                    and e.field in ("code", "message"):
                return "Text"
            ot = self._infer(e.obj, scope)
            if ot == "Error" and e.field in ("code", "message"):
                return "Text"
            # Phase 5c: field access on applied generic types.
            base, args = self._split_applied(ot)
            decl = self.types.get(base)
            if decl:
                bindings: Dict[str, str] = {}
                if decl.type_params and len(args) == len(decl.type_params):
                    bindings = dict(zip(decl.type_params, args))
                for fn, fte in decl.fields:
                    if fn == e.field:
                        return self._tname(self._subst_te(fte, bindings))
                self.error("E101",
                           f"type '{ot}' has no field '{e.field}'",
                           e.line, e.col)
            return "unknown"
        if isinstance(e, ast.Match):
            # Phase 5b: `match` expression over a sum type.
            return self._infer_match(e, scope)
        if isinstance(e, ast.Index):
            ot = self._infer(e.obj, scope)
            self._infer(e.idx, scope)
            if ot.startswith("[") and ot.endswith("]"):
                return ot[1:-1]
            if ot.startswith("Map<"):
                vt = self._map_val_type(ot)
                if vt is not None:
                    return vt
            return "unknown"
        if isinstance(e, ast.MapLit):
            vts = set()
            for _, v in e.entries:
                vts.add(self._infer(v, scope))
            vt = vts.pop() if len(vts) == 1 else "unknown"
            return f"Map<Text, {vt}>"
        if isinstance(e, ast.ListLit):
            ts = {self._infer(i, scope) for i in e.items}
            inner = ts.pop() if len(ts) == 1 else "unknown"
            return f"[{inner}]"
        if isinstance(e, ast.RecordLit):
            for _, v in e.fields:
                self._infer(v, scope)
            return "record"
        if isinstance(e, ast.Lambda):
            return self._infer_lambda(e, scope, expected)
        if isinstance(e, ast.BinOp):
            lt = self._infer(e.left, scope)
            rt = self._infer(e.right, scope)
            if e.op in ("and", "or"):
                return "Bool"
            if e.op in ("==", "!="):
                # Universal equality: allowed on any operand types
                # (both backends agree).
                return "Bool"
            if e.op in ("<", "<=", ">", ">="):
                # C-6: ordering comparisons need both-numeric or
                # both-Text operands (matches AOT val_cmp); "unknown"
                # (dynamic) passes through to the runtime guard.
                if lt != "unknown" and rt != "unknown":
                    numeric = ("Int", "Float")
                    both_numeric = lt in numeric and rt in numeric
                    both_text = lt == "Text" and rt == "Text"
                    if not both_numeric and not both_text:
                        self.error(
                            "E211",
                            f"operator '{e.op}' cannot be applied to "
                            f"{lt} and {rt}", e.line, e.col)
                        return "unknown"
                return "Bool"
            # C-6: validate arithmetic operand types. Known-incompatible
            # types are E211; "unknown" (dynamic) is allowed through.
            if e.op == "+":
                if lt == "Text" or rt == "Text":
                    return "Text"
                if lt.startswith("[") and rt.startswith("["):
                    return lt if lt == rt else "unknown"
            if e.op in ("+", "-", "*", "/", "%"):
                numeric = ("Int", "Float", "unknown")
                if lt not in numeric or rt not in numeric:
                    self.error("E211",
                               f"operator '{e.op}' cannot be applied to "
                               f"{lt} and {rt}", e.line, e.col)
                    return "unknown"
            if lt in ("Int", "Float") and rt in ("Int", "Float"):
                return "Float" if "Float" in (lt, rt) else "Int"
            return "unknown"
        if isinstance(e, ast.UnOp):
            t = self._infer(e.operand, scope)
            return "Bool" if e.op == "not" else t
        if isinstance(e, ast.Spawn):
            # Phase 7 (concurrency): `spawn { ... }` — the block's
            # return type is collected exactly like a lambda block
            # body (all returns must agree); it becomes T in `Task<T>`.
            # A block with no `return` is `Task<Void>`. The block may
            # capture enclosing variables (like lambdas), so it shares
            # the enclosing scope — no extra restriction.
            ret_ts = []
            self._check_block(e.body, dict(scope), fn_ret=None,
                              _ret_collect=ret_ts)
            if ret_ts:
                rt = (ret_ts[0]
                      if all(t == ret_ts[0] for t in ret_ts)
                      else "unknown")
            else:
                rt = "Void"
            return f"Task<{rt}>"
        if isinstance(e, ast.Await):
            t = self._infer(e.operand, scope)
            # Phase 7 (concurrency): `await t timeout <ms>` — the
            # timeout must be Int (a negative value is a runtime
            # check, not a static one).
            if e.timeout is not None:
                tt = self._infer(e.timeout, scope)
                if tt != "unknown" and tt != "Int":
                    self.error("E211",
                               f"'await' timeout wants Int, got {tt}",
                               e.timeout.line, e.timeout.col)
            if t == "TokenStream":
                return "unknown"  # model return type; resolved at runtime
            base, args = self._split_applied(t)
            if base == "Task" and len(args) == 1:
                return args[0]
            self.error("E205",
                       f"'await' needs a Task or TokenStream, found {t}",
                       e.line, e.col)
            return "unknown"
        if isinstance(e, ast.StreamDecl):
            return "TokenStream"
        self.error("E001", f"cannot infer {type(e).__name__}",
                   e.line, e.col)
        return "unknown"

    def _infer_lambda(self, lam: ast.Lambda, scope: dict,
                      expected: Optional[str]) -> str:
        """Phase 5a: infer a lambda's function type.

        Typed params (`|x: Int|`) stand alone; untyped params take their
        type from the expected function type at the use site (E213 when
        neither is available). The return type comes from the expected
        type when present, else from the body.
        """
        exp_params, exp_ret = None, None
        if expected:
            fp = self._func_parts(expected)
            if fp is not None:
                exp_params, exp_ret = fp
        ptypes = []
        for i, (pn, pte) in enumerate(lam.params):
            if pte is not None:
                ptypes.append(self._tname(pte))
            elif exp_params is not None and i < len(exp_params):
                ptypes.append(exp_params[i])
            else:
                self.error("E213",
                           f"cannot infer type of lambda parameter "
                           f"'{pn}': annotate it ('|{pn}: Int|') or use "
                           f"the lambda where a function type is expected",
                           lam.line, lam.col)
                ptypes.append("unknown")
        # duplicate param names
        seen = set()
        for pn, _ in lam.params:
            if pn in seen:
                self.error("E210", f"duplicate parameter '{pn}'",
                           lam.line, lam.col)
            seen.add(pn)
        lscope = dict(scope)
        for (pn, _), pt in zip(lam.params, ptypes):
            lscope[pn] = (pt, False)
        if lam.expr_body is not None:
            body_t = self._infer(lam.expr_body, lscope)
            # Phase 5c: "unknown" expected return means "infer it".
            if exp_ret is not None and exp_ret != "unknown":
                ret = exp_ret
            else:
                ret = body_t
            if exp_ret is not None and exp_ret != "unknown" \
                    and body_t != "unknown" \
                    and not self._assignable(exp_ret, body_t):
                self.error("E211",
                           f"lambda returns {body_t}, expected {exp_ret}",
                           lam.expr_body.line, lam.expr_body.col)
        else:
            # block body: single pass checks statements and collects
            # return types (no double inference).
            ret_ts = []
            self._check_block(lam.body, lscope,
                              fn_ret=exp_ret if exp_ret is not None
                              else None,
                              _ret_collect=ret_ts)
            if exp_ret is not None:
                ret = exp_ret
            elif ret_ts:
                ret = (ret_ts[0] if all(t == ret_ts[0] for t in ret_ts)
                       else "unknown")
            else:
                ret = "Void"
        ps = ", ".join(ptypes)
        return f"({ps}) -> {ret}"

    def _infer_call(self, e: ast.Call, scope: dict,
                    expected: Optional[str] = None) -> str:
        # M-4: `<handle>.exec/query/close(...)` where the handle variable
        # has type Db resolves to the `db.*` builtin regardless of the
        # variable's name (the handle is still passed explicitly as the
        # first arg). Rewriting e.name here propagates to the
        # interpreter and the AOT emitter, which key off the same AST.
        if e.callee is None and e.name not in NS_BUILTINS \
                and "." in e.name:
            recv, _, field = e.name.partition(".")
            if field in ("exec", "query", "close") and recv in scope \
                    and scope[recv][0] == "Db":
                e.name = f"db.{field}"
        # Phase 7 (concurrency): `<ch>.send(v)` / `<ch>.recv()` /
        # `<ch>.close()` where the receiver variable's type starts with
        # `Chan<` rewrites e.name in place to the canonical `chan.send`
        # / `chan.recv` / `close` (propagating to the interpreter and
        # the AOT emitter, which key off the same AST — same trick as
        # M-4 above). The receiver is prepended as the first arg so the
        # checked AST carries the canonical runtime signatures
        # `chan.send(chan, value)` / `chan.recv(chan)` / `close(chan)`.
        if e.callee is None and e.name not in NS_BUILTINS \
                and "." in e.name:
            recv, _, field = e.name.partition(".")
            if recv in scope and scope[recv][0].startswith("Chan<") \
                    and field in ("send", "recv", "close"):
                e.name = {"send": "chan.send", "recv": "chan.recv",
                          "close": "close"}[field]
                e.args.insert(0, ast.Ident(name=recv, line=e.line,
                                           col=e.col))
        # Phase 5a: calling the result of an expression, e.g. `mk(9)(1)`.
        if e.callee is not None:
            ct = self._infer(e.callee, scope)
            fp = self._func_parts(ct)
            if fp is None:
                self.error("E215",
                           f"call result has type '{ct}' and is not callable",
                           e.line, e.col)
                return "unknown"
            fps, fret = fp
            arg_ts = []
            for i, a in enumerate(e.args):
                exp = fps[i] if i < len(fps) else None
                arg_ts.append(self._infer(a, scope, expected=exp))
            if len(e.args) != len(fps):
                self.error("E102",
                           f"function takes {len(fps)} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for want_t, a, at in zip(fps, e.args, arg_ts):
                    # skip when either side is "unknown" (cascades from a
                    # prior error, e.g. E213 untyped lambda param)
                    if at != "unknown" and want_t != "unknown" \
                            and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"argument wants {want_t}, got {at}",
                                   a.line, a.col)
            return fret
        # Phase 5c: generic fn — inference/pinning lives in
        # _infer_generic_call (handles its own expected types).
        if e.name in self.fns and self.fns[e.name].type_params:
            return self._infer_generic_call(self.fns[e.name], e, scope)
        # Phase 5a: per-arg expected types so lambdas infer untyped
        # params from the callee's function type.
        # Phase 5b: sum variant payload types serve the same role.
        # Phase 5c: a variable holding a generic fn value, `let g = f`.
        exp_ts = None
        if e.name in self.fns:
            exp_ts = [self._tname(te) for _, te in self.fns[e.name].params]
        elif e.name in scope and scope[e.name][0].startswith("generic("):
            gname = scope[e.name][0][len("generic("):-1]
            if gname in self.fns:
                return self._infer_generic_call(self.fns[gname], e,
                                                scope)
        elif e.name in scope:
            fp = self._func_parts(scope[e.name][0])
            if fp is not None:
                exp_ts, _ = fp
        elif "." in e.name:
            head, _, var = e.name.partition(".")
            decl = self.types.get(head)
            if decl is not None and decl.is_sum:
                v = next((x for x in decl.variants if x.name == var),
                         None)
                if v is not None:
                    exp_ts = [self._tname(te) for _, te in v.fields]
        arg_ts = []
        for i, a in enumerate(e.args):
            exp = exp_ts[i] if exp_ts and i < len(exp_ts) else None
            arg_ts.append(self._infer(a, scope, expected=exp))
        if e.name in self.models:
            self._check_model_call(e, scope)
            return self._tname(self.models[e.name].ret)
        if e.name in self.screens:
            want = len(self.screens[e.name].params)
            if len(e.args) != want:
                self.error("E102",
                           f"screen '{e.name}' takes {want} args, "
                           f"got {len(e.args)}", e.line, e.col)
            return "screen"
        # Phase 5b: sum variant construction `Type.Variant(args)`.
        if "." in e.name:
            head, _, var = e.name.partition(".")
            decl = self.types.get(head)
            if decl is not None and decl.is_sum:
                return self._check_variant_construct(decl, var, e, scope,
                                                     arg_ts, expected)
        if e.name in self.fns:
            fn = self.fns[e.name]
            want = len(fn.params)
            if len(e.args) != want:
                self.error("E102",
                           f"function '{e.name}' takes {want} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for (pn, te), a, at in zip(fn.params, e.args, arg_ts):
                    want_t = self._tname(te)
                    if at != "unknown" and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"argument '{pn}' wants {want_t}, "
                                   f"got {at}", a.line, a.col)
            return self._tname(fn.ret) if fn.ret else "Void"
        # Phase 7: extern (FFI) function calls — same arity/type
        # checking as Misa functions.
        if e.name in self.externs:
            fn = self.externs[e.name]
            want = len(fn.params)
            if len(e.args) != want:
                self.error("E102",
                           f"extern function '{e.name}' takes {want} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for (pn, te), a, at in zip(fn.params, e.args, arg_ts):
                    want_t = self._tname(te)
                    # Phase 7 (concurrency): Task/Chan values may not
                    # cross the FFI boundary (E225), even as arguments.
                    if at != "unknown" \
                            and (at.startswith("Task<")
                                 or at.startswith("Chan<")):
                        self.error("E225",
                                   f"cannot pass {at} to extern function "
                                   f"'{e.name}': Task/Chan values cannot "
                                   f"cross the FFI boundary",
                                   a.line, a.col)
                    elif at != "unknown" \
                            and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"argument '{pn}' wants {want_t}, "
                                   f"got {at}", a.line, a.col)
            return self._tname(fn.ret) if fn.ret else "Void"
        # Phase 5a: calling a variable holding a function value.
        if e.name in scope:
            st = scope[e.name][0]
            fp = self._func_parts(st)
            if fp is None:
                self.error("E215",
                           f"'{e.name}' has type '{st}' and is not callable",
                           e.line, e.col)
                return "unknown"
            fps, fret = fp
            if len(e.args) != len(fps):
                self.error("E102",
                           f"function '{e.name}' takes {len(fps)} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for want_t, a, at in zip(fps, e.args, arg_ts):
                    # skip when either side is "unknown" (cascades from a
                    # prior error, e.g. E213 untyped lambda param)
                    if at != "unknown" and want_t != "unknown" \
                            and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"argument wants {want_t}, got {at}",
                                   a.line, a.col)
            return fret
        # Phase 7 (concurrency): `chan` / `sleep` / `fetch_all` /
        # `close` / `chan.send` / `chan.recv`. Placed after user
        # functions and variables so a user-defined `sleep` etc.
        # shadows the builtin, like the other builtin families.
        if e.name == "chan":
            # `chan<T>()` — exactly 1 type argument (E221, matching
            # the Call type_args arity reporting for generics), no
            # value args, returns Chan<T>.
            if len(e.type_args) != 1:
                self.error("E221",
                           f"'chan' takes 1 type argument(s), "
                           f"got {len(e.type_args)}", e.line, e.col)
                return "unknown"
            ta = e.type_args[0]
            self._check_te(ta, ta.line, ta.col)
            if e.args:
                self.error("E102",
                           f"'chan' takes 0 args, got {len(e.args)}",
                           e.line, e.col)
            return f"Chan<{self._tname(ta)}>"
        if e.name == "sleep":
            if len(e.args) != 1:
                self.error("E102",
                           f"'sleep' takes 1 arg, got {len(e.args)}",
                           e.line, e.col)
            elif arg_ts[0] != "unknown" \
                    and not self._assignable("Int", arg_ts[0]):
                self.error("E211",
                           f"'sleep' wants Int, got {arg_ts[0]}",
                           e.args[0].line, e.args[0].col)
            return "Void"
        if e.name == "fetch_all":
            if len(e.args) != 1:
                self.error("E102",
                           f"'fetch_all' takes 1 arg, got {len(e.args)}",
                           e.line, e.col)
            elif arg_ts[0] != "unknown" \
                    and not self._assignable("[Text]", arg_ts[0]):
                self.error("E211",
                           f"'fetch_all' wants [Text], got {arg_ts[0]}",
                           e.args[0].line, e.args[0].col)
            return "[Text]"
        if e.name == "close":
            if len(e.args) != 1:
                self.error("E102",
                           f"'close' takes 1 arg, got {len(e.args)}",
                           e.line, e.col)
                return "Void"
            ct = arg_ts[0]
            cbase, cargs = self._split_applied(ct)
            if ct != "unknown" \
                    and not (cbase == "Chan" and len(cargs) == 1):
                self.error("E211",
                           f"'close' wants a Chan<T> channel, got {ct}",
                           e.args[0].line, e.args[0].col)
            return "Void"
        if e.name == "chan.send":
            # Canonical name for `ch.send(v)` (see the M-4-style
            # rewrite at the top of _infer_call).
            if len(e.args) != 2:
                self.error("E102",
                           f"'chan.send' takes 2 args, "
                           f"got {len(e.args)}", e.line, e.col)
                return "Void"
            ct, vt = arg_ts
            cbase, cargs = self._split_applied(ct)
            if ct != "unknown" \
                    and not (cbase == "Chan" and len(cargs) == 1):
                self.error("E211",
                           f"'chan.send' needs a Chan<T> channel, "
                           f"got {ct}", e.args[0].line, e.args[0].col)
                return "Void"
            elem = cargs[0]
            if vt != "unknown" and elem != "unknown" \
                    and not self._assignable(elem, vt):
                self.error("E211",
                           f"'chan.send' wants {elem}, got {vt}",
                           e.args[1].line, e.args[1].col)
            return "Void"
        if e.name == "chan.recv":
            # Canonical name for `ch.recv()`.
            if len(e.args) != 1:
                self.error("E102",
                           f"'chan.recv' takes 1 arg, "
                           f"got {len(e.args)}", e.line, e.col)
                return "unknown"
            ct = arg_ts[0]
            cbase, cargs = self._split_applied(ct)
            if ct != "unknown" \
                    and not (cbase == "Chan" and len(cargs) == 1):
                self.error("E211",
                           f"'chan.recv' needs a Chan<T> channel, "
                           f"got {ct}", e.args[0].line, e.args[0].col)
                return "unknown"
            return cargs[0]
        if e.name in NS_BUILTINS:
            params, ret = NS_BUILTINS[e.name]
            lo, hi = NS_BUILTIN_ARITY.get(e.name, (len(params),
                                                  len(params)))
            if not (lo <= len(e.args) <= hi):
                self.error("E102",
                           f"'{e.name}' takes {lo}-{hi} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for want_t, a, at in zip(params, e.args, arg_ts):
                    if at != "unknown" and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"'{e.name}' wants {want_t}, got {at}",
                                   a.line, a.col)
            # Phase 5 (DB): typed query results against `table` decls.
            if e.name == "db.query" and len(e.args) >= 2:
                typed = self._check_db_query(e, arg_ts)
                if typed is not None:
                    return typed
            return ret
        if e.name in BUILTINS:
            lo, hi = BUILTIN_ARITY[e.name]
            if not (lo <= len(e.args) <= hi):
                self.error("E102",
                           f"'{e.name}' takes {lo}-{hi} args, "
                           f"got {len(e.args)}", e.line, e.col)
            return BUILTINS[e.name][1]
        if e.name in MEDIA:
            if len(e.args) != 1:
                self.error("E102",
                           f"{e.name}(path) takes 1 arg, "
                           f"got {len(e.args)}", e.line, e.col)
            return e.name
        # Phase 5c: generic record construction `Box<Int>(42)`.
        if e.name in self.types and not self.types[e.name].is_sum \
                and self.types[e.name].type_params:
            return self._check_generic_construct(self.types[e.name], e,
                                                scope)
        if e.name in self.types:
            self.error("E101",
                       f"type '{e.name}' is not callable "
                       f"(media types excepted)", e.line, e.col)
            return "unknown"
        self.error("E101", f"undefined function '{e.name}'",
                   e.line, e.col)
        return "unknown"

    def _check_model_call(self, e: ast.Call, scope: dict):
        m = self.models[e.name]
        want = len(m.params)
        if len(e.args) != want:
            self.error("E102",
                       f"model '{e.name}' takes {want} args, "
                       f"got {len(e.args)}", e.line, e.col)

    # -- Phase 5 (DB): typed queries ------------------------------------

    def _check_db_query(self, e: ast.Call,
                        arg_ts: list) -> Optional[str]:
        """Validate `db.query(db, sql)` against `table` decls.

        Returns the synthetic result type string (e.g. `[$query_0]`)
        when the SQL is a plain string literal over a declared table,
        else None (dynamic `[record]`, no static checking).
        Unknown tables/columns are E224.
        """
        sql_arg = e.args[1]
        # Only plain (non-interpolated) string literals are statically
        # checkable: StrLit.parts is [str] for plain strings.
        if not isinstance(sql_arg, ast.StrLit) \
                or len(sql_arg.parts) != 1 \
                or not isinstance(sql_arg.parts[0], str):
            return None
        parsed = self._parse_select(sql_arg.parts[0])
        if parsed is None:
            return None
        table_name, cols, where_cols = parsed
        decl = self.tables.get(table_name)
        if decl is None:
            # Undeclared table: dynamic records, not an error.
            return None
        col_types = {fn: fte for fn, fte, _ in decl.fields}
        # Validate WHERE identifiers (v1: name validation only).
        for wc in where_cols:
            if wc not in col_types:
                self.error("E224",
                           f"unknown column '{wc}' in WHERE clause "
                           f"(table '{table_name}' has no such column)",
                           sql_arg.line, sql_arg.col)
        # Resolve the select list.
        if cols == ["*"]:
            sel = [(fn, fte) for fn, fte, _ in decl.fields]
        else:
            sel = []
            for cname, _alias in cols:
                if cname not in col_types:
                    self.error("E224",
                               f"unknown column '{cname}' "
                               f"(table '{table_name}' has no such column)",
                               sql_arg.line, sql_arg.col)
                    continue
                sel.append((cname, col_types[cname]))
        # Synthesize a record type for the result rows so field
        # access type-checks through the existing machinery.
        self._query_seq += 1
        tname = f"$query_{self._query_seq}"
        self.types[tname] = ast.TypeDecl(
            name=tname,
            fields=[(fn, fte) for fn, fte in sel],
            line=e.line, col=e.col)
        return f"[{tname}]"

    @staticmethod
    def _parse_select(sql: str):
        """Minimal SELECT parser (v1). Returns
        (table, [(col, alias)], [where_cols]) or None when the SQL
        isn't a plain `SELECT ... FROM ... [WHERE ...]`."""
        m = _DB_SELECT_RE.match(sql)
        if not m:
            return None
        table = m.group("table")
        raw_cols = m.group("cols").strip()
        if raw_cols == "*":
            cols = ["*"]
        else:
            cols = []
            for part in _split_top_commas(raw_cols):
                part = part.strip()
                if not part:
                    return None
                # `t.col`, `col AS alias`, `col alias`
                toks = part.split()
                first = toks[0]
                cname = first.rpartition(".")[2]
                alias = None
                if len(toks) >= 3 and toks[1].upper() == "AS":
                    alias = toks[2]
                elif len(toks) == 2:
                    alias = toks[1]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cname):
                    return None  # expressions: not v1-checkable
                cols.append((cname, alias or cname))
        where_cols = []
        where = m.group("where")
        if where:
            # strip string literals so 'O''Brien' doesn't yield identifiers
            no_str = re.sub(r"'(?:[^']|'')*'", " ", where)
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", no_str):
                if tok.lower() not in _DB_WHERE_KW \
                        and not tok[0].isdigit():
                    wc = tok.rpartition(".")[2]
                    if wc not in where_cols:
                        where_cols.append(wc)
        return table, cols, where_cols

    # -- Phase 5b: sum types ------------------------------------------

    @staticmethod
    def _variant_of(decl: ast.TypeDecl, vname: str):
        return next((v for v in decl.variants if v.name == vname), None)

    def _check_variant_construct(self, decl: ast.TypeDecl, var: str,
                                 e: ast.Call, scope: dict,
                                 arg_ts: list,
                                 expected: Optional[str] = None) -> str:
        """Check `Type.Variant(args)`; return the sum type name
        (Phase 5c: the applied name, e.g. `Result<Int>`)."""
        v = self._variant_of(decl, var)
        if v is None:
            self.error("E218",
                       f"unknown variant '{var}' of sum type "
                       f"'{decl.name}'", e.line, e.col)
            return "unknown"
        if len(e.args) != len(v.fields):
            self.error("E102",
                       f"variant '{decl.name}.{var}' takes "
                       f"{len(v.fields)} args, got {len(e.args)}",
                       e.line, e.col)
            return decl.name
        # Phase 5c: generic sum — bind type args from explicit pinning,
        # payload unification, and/or the expected type.
        if decl.type_params:
            return self._check_generic_variant(decl, v, e, scope,
                                               arg_ts, expected)
        for (fn, fte), a, at in zip(v.fields, e.args, arg_ts):
            want_t = self._tname(fte)
            if at != "unknown" and want_t != "unknown" \
                    and not self._assignable(want_t, at):
                self.error("E211",
                           f"variant payload '{fn}' wants {want_t}, "
                           f"got {at}", a.line, a.col)
        return decl.name

    def _check_generic_variant(self, decl: ast.TypeDecl,
                               v: ast.VariantDecl, e: ast.Call,
                               scope: dict, arg_ts: list,
                               expected: Optional[str]) -> str:
        """Phase 5c: `Result.Ok(42)` / `Result<Int>.Ok(42)`. Returns the
        applied type string, e.g. `Result<Int>`."""
        tparams = decl.type_params
        bindings: Dict[str, str] = {}
        if e.type_args:
            if len(e.type_args) != len(tparams):
                self.error("E221",
                           f"'{decl.name}' takes {len(tparams)} type "
                           f"argument(s), got {len(e.type_args)}",
                           e.line, e.col)
                return "unknown"
            for p, a in zip(tparams, e.type_args):
                self._check_te(a, a.line, a.col)
                bindings[p] = self._tname(a)
        for (fn, fte), a, at in zip(v.fields, e.args, arg_ts):
            err = self._unify_te(fte, self._parse_tstr(at), tparams,
                                 bindings)
            if err:
                self.error("E220", err, a.line, a.col)
        # The expected type (`let r: Result<Int> = ...`) can bind any
        # remaining variables.
        if expected:
            base, args = self._split_applied(expected)
            if base == decl.name and len(args) == len(tparams):
                for p, a in zip(tparams, args):
                    if p not in bindings:
                        bindings[p] = a
        for p in tparams:
            if p not in bindings:
                self.error("E219",
                           f"cannot infer type argument '{p}' for "
                           f"'{decl.name}.{v.name}'; give explicit type "
                           f"arguments ('{decl.name}<{p}>.{v.name}(...)' ) "
                           f"or annotate the binding", e.line, e.col)
                return "unknown"
        # Phase 5c: annotate for AOT monomorphization.
        e.type_args = [self._parse_tstr(bindings[p]) for p in tparams]
        for (fn, fte), a, at in zip(v.fields, e.args, arg_ts):
            want_t = self._tname(self._subst_te(fte, bindings))
            if at == "unknown":
                continue
            # Phase 5c: a bare nil of a generic sum (`L.Nil`, or `L<T>`
            # with unbound vars, as the payload of `L<Int>`) is
            # compatible — it carries no T.
            wbase, _ = self._split_applied(want_t)
            abase, aargs = self._split_applied(at)
            gdecl = self.types.get(wbase)
            if gdecl is not None and gdecl.type_params \
                    and abase == wbase \
                    and (not aargs or all(x in gdecl.type_params
                                          for x in aargs)):
                continue
            if not self._assignable(want_t, at):
                self.error("E211",
                           f"variant payload '{fn}' wants {want_t}, "
                           f"got {at}", a.line, a.col)
        args = ", ".join(bindings[p] for p in tparams)
        return f"{decl.name}<{args}>"

    def _check_bare_variant(self, decl: ast.TypeDecl,
                            e: ast.Field,
                            expected: Optional[str] = None) -> str:
        """Check a bare `Type.Variant` field access (no payload)."""
        v = self._variant_of(decl, e.field)
        if v is None:
            self.error("E218",
                       f"unknown variant '{e.field}' of sum type "
                       f"'{decl.name}'", e.line, e.col)
            return "unknown"
        if v.fields:
            self.error("E218",
                       f"variant '{decl.name}.{v.name}' takes "
                       f"{len(v.fields)} payload arg(s); construct it as "
                       f"'{decl.name}.{v.name}(...)'", e.line, e.col)
            return "unknown"
        # Phase 5c: a bare nil of a generic sum takes the expected
        # applied type when available (`let x: L<Int> = L.Nil`).
        if decl.type_params and expected:
            base, args = self._split_applied(expected)
            if base == decl.name and len(args) == len(decl.type_params):
                return expected
        return decl.name

    def _infer_match(self, e: ast.Match, scope: dict) -> str:
        st = self._infer(e.scrutinee, scope)
        # Phase 5c: applied sum types — `match` on `Result<Int>`.
        base, args = self._split_applied(st)
        decl = self.types.get(base)
        if decl is None or not decl.is_sum:
            self.error("E218",
                       f"match scrutinee must be a sum type, got '{st}'",
                       e.scrutinee.line, e.scrutinee.col)
            return "unknown"
        bindings: Dict[str, str] = {}
        if decl.type_params:
            if len(args) != len(decl.type_params):
                # Scrutinee type isn't fully applied; bindings stay empty
                # and field types keep their variables (best effort).
                pass
            else:
                bindings = dict(zip(decl.type_params, args))
        seen = set()
        has_wildcard = False
        arm_ts = []
        for arm in e.arms:
            if arm.wildcard:
                has_wildcard = True
                t = self._infer(arm.body, scope)
                arm_ts.append(t)
                continue
            if arm.type_name != decl.name:
                self.error("E218",
                           f"match arm on '{arm.type_name}.{arm.variant}' "
                           f"does not match scrutinee type '{decl.name}'",
                           arm.line, arm.col)
                arm_ts.append(self._infer(arm.body, scope))
                continue
            v = self._variant_of(decl, arm.variant)
            if v is None:
                self.error("E218",
                           f"unknown variant '{arm.variant}' of sum type "
                           f"'{decl.name}'", arm.line, arm.col)
                arm_ts.append(self._infer(arm.body, scope))
                continue
            if len(arm.binds) != len(v.fields):
                self.error("E218",
                           f"pattern '{decl.name}.{v.name}' binds "
                           f"{len(arm.binds)}, variant has "
                           f"{len(v.fields)} payload field(s)",
                           arm.line, arm.col)
            child = dict(scope)
            for bname, (fn, fte) in zip(arm.binds, v.fields):
                # Phase 5c: substitute the scrutinee's type arguments.
                bt = self._tname(self._subst_te(fte, bindings))
                child[bname] = (bt, False)
            seen.add(arm.variant)
            arm_ts.append(self._infer(arm.body, child))
        if not has_wildcard:
            missing = [v.name for v in decl.variants
                       if v.name not in seen]
            if missing:
                self.error("E216",
                           f"non-exhaustive match on '{decl.name}'; "
                           f"missing: {', '.join(missing)}",
                           e.line, e.col)
        ret = "unknown"
        for t in arm_ts:
            if t == "unknown":
                continue
            if ret == "unknown":
                ret = t
            elif t != ret:
                self.error("E217",
                           f"match arms yield different types "
                           f"('{ret}' vs '{t}')", e.line, e.col)
                return "unknown"
        return ret

    # -- type helpers ---------------------------------------------------

    def _tname(self, te: Optional[ast.TypeExpr]) -> str:
        if te is None:
            return "unknown"
        # Phase 5a: function types render `(Int, Text) -> Bool`
        if te.is_func:
            ps = ", ".join(self._tname(p) for p in te.func_params)
            r = self._tname(te.func_ret)
            s = f"({ps}) -> {r}"
            if te.optional:
                s += "?"
            return s
        s = te.name
        if te.is_list:
            # Phase 5c: full inner structure when preserved.
            if te.list_elem is not None:
                s = f"[{self._tname(te.list_elem)}]"
            else:
                s = f"[{s}]"
        elif te.is_map:
            s = f"Map<{self._tname(te.map_key)}, {self._tname(te.map_val)}>"
        elif te.type_args:
            # Phase 5c: applied generic types render `Box<Int>`.
            args = ", ".join(self._tname(a) for a in te.type_args)
            s = f"{s}<{args}>"
        if te.optional:
            s += "?"
        return s

    def _known_type(self, te: Optional[ast.TypeExpr]) -> bool:
        if te is None:
            return False
        # Phase 5a: function types — every param and the return must be known
        if te.is_func:
            return (te.func_ret is not None
                    and all(self._known_type(p) for p in te.func_params)
                    and self._known_type(te.func_ret))
        if te.is_map:
            return (te.map_key is not None and te.map_val is not None
                    and te.map_key.name in ("Text", "Int")
                    and not te.map_key.is_list and not te.map_key.is_map
                    and self._known_type(te.map_val))
        if te.is_list:
            # Phase 5c: full inner structure when preserved.
            if te.list_elem is not None:
                return self._known_type(te.list_elem)
            return self._known_type(ast.TypeExpr(name=te.name))
        # Phase 5c: applied generic types `Box<Int>`.
        if te.type_args:
            # Phase 7 (concurrency): builtin `Task<T>` / `Chan<T>`
            # require exactly 1 type argument.
            if te.name in CONCURRENCY_GENERICS:
                return (len(te.type_args) == 1
                        and all(self._known_type(a)
                                for a in te.type_args))
            decl = self.types.get(te.name)
            if decl is None or not decl.type_params:
                return False
            if len(te.type_args) != len(decl.type_params):
                return False
            return all(self._known_type(a) for a in te.type_args)
        base = te.name
        # Phase 5c: type parameters in scope are known types.
        if base in self._tparams:
            return True
        return (base in PRIMITIVES or base in MEDIA or base in SPECIAL
                or base in self.types)

    def _check_te(self, te: Optional[ast.TypeExpr], line: int,
                  col: int) -> bool:
        """Phase 5c: validate a type expression, emitting precise errors:
        E221 for type-argument arity mismatch, E222 for unbound type
        variables (inside a generic decl), E208 otherwise. Returns True
        when valid."""
        if te is None:
            return True
        if self._known_type(te):
            return True
        # Phase 5c: applied generic type with wrong arity.
        if te.type_args:
            # Phase 7 (concurrency): `Task`/`Chan` take exactly 1.
            if te.name in CONCURRENCY_GENERICS \
                    and len(te.type_args) != 1:
                self.error(
                    "E221",
                    f"'{te.name}' takes 1 type argument(s), "
                    f"got {len(te.type_args)}", line, col)
                return False
            decl = self.types.get(te.name)
            if decl is not None and decl.type_params \
                    and len(te.type_args) != len(decl.type_params):
                self.error(
                    "E221",
                    f"'{te.name}' takes {len(decl.type_params)} type "
                    f"argument(s), got {len(te.type_args)}", line, col)
                return False
        # Phase 7 (concurrency): bare `Task`/`Chan` without a type
        # argument is not a type — suggest the generic form.
        if not te.is_list and not te.is_map and not te.is_func \
                and not te.type_args and te.name in CONCURRENCY_GENERICS:
            self.error("E221",
                       f"'{te.name}' takes 1 type argument(s), got 0 "
                       f"(write {te.name}<T>, e.g. {te.name}<Text>)",
                       line, col)
            return False
        # Phase 5c: bare unknown name inside a generic decl → unbound
        # type variable (E222); otherwise E208.
        if not te.is_list and not te.is_map and not te.is_func \
                and not te.type_args and te.name:
            if self._tparams:
                self.error("E222",
                           f"unbound type variable '{te.name}'",
                           line, col)
                return False
        # Recurse to find nested problems (so `[Box<Int, Text>]`
        # reports E221, not E208).
        if te.is_func:
            ok = True
            for p in te.func_params:
                ok = self._check_te(p, line, col) and ok
            if te.func_ret is not None:
                ok = self._check_te(te.func_ret, line, col) and ok
            return ok
        if te.is_map:
            ok = self._check_te(te.map_key, line, col)
            return self._check_te(te.map_val, line, col) and ok
        if te.is_list and te.list_elem is not None:
            return self._check_te(te.list_elem, line, col)
        if te.type_args:
            ok = True
            for a in te.type_args:
                ok = self._check_te(a, line, col) and ok
            return ok
        tname = self._tname(te)
        hint = (" — 'any' is not a valid annotation: omit the type and "
                "Misa will infer it"
                if tname in ("any", "value", "unknown") else "")
        self.error("E208", f"unknown type '{tname}'{hint}",
                   line, col)
        return False

    # -- Phase 5c: generics -------------------------------------------

    def _parse_tstr(self, s: str) -> ast.TypeExpr:
        """Parse a checker type string (e.g. `[Box<Int>]`, `(T) -> K`)
        back into a TypeExpr for structural unification."""
        toks = self._tstr_toks(s)
        te, pos = self._tstr_expr(toks, 0)
        return te

    @staticmethod
    def _tstr_toks(s: str):
        toks, i = [], 0
        while i < len(s):
            c = s[i]
            if c.isspace():
                i += 1
            elif c in "[],()<>?":
                toks.append(c)
                i += 1
            elif c == "-" and s[i:i + 2] == "->":
                toks.append("->")
                i += 2
            else:
                j = i
                while j < len(s) and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                toks.append(s[i:j])
                i = j
        return toks

    def _tstr_expr(self, toks, pos):
        """Parse one type; returns (TypeExpr, new_pos)."""
        if toks[pos] == "(":
            # function type `(A, B) -> C`
            pos += 1
            params = []
            if toks[pos] != ")":
                while True:
                    p, pos = self._tstr_expr(toks, pos)
                    params.append(p)
                    if toks[pos] == ",":
                        pos += 1
                        continue
                    break
            pos += 1  # )
            assert toks[pos] == "->"
            pos += 1
            ret, pos = self._tstr_expr(toks, pos)
            te = ast.TypeExpr(is_func=True, func_params=params,
                              func_ret=ret)
        elif toks[pos] == "[":
            pos += 1
            inner, pos = self._tstr_expr(toks, pos)
            assert toks[pos] == "]"
            pos += 1
            te = ast.TypeExpr(name=inner.name, is_list=True)
            # preserve structure like the source parser does
            if inner.is_list or inner.is_map or inner.is_func \
                    or inner.type_args:
                te.list_elem = inner
            else:
                # copy inner flags for plain names
                te.name = inner.name
        else:
            name = toks[pos]
            pos += 1
            if name == "Map" and pos < len(toks) and toks[pos] == "<":
                pos += 1
                k, pos = self._tstr_expr(toks, pos)
                assert toks[pos] == ","
                pos += 1
                v, pos = self._tstr_expr(toks, pos)
                assert toks[pos] == ">"
                pos += 1
                te = ast.TypeExpr(name="Map", is_map=True,
                                  map_key=k, map_val=v)
            elif pos < len(toks) and toks[pos] == "<":
                pos += 1
                args = []
                while True:
                    a, pos = self._tstr_expr(toks, pos)
                    args.append(a)
                    if toks[pos] == ",":
                        pos += 1
                        continue
                    break
                assert toks[pos] == ">"
                pos += 1
                te = ast.TypeExpr(name=name, type_args=args)
            else:
                te = ast.TypeExpr(name=name)
        if pos < len(toks) and toks[pos] == "?":
            te.optional = True
            pos += 1
        return te, pos

    def _is_tvar(self, te: ast.TypeExpr, tparams) -> bool:
        """Is this TypeExpr a bare type variable?"""
        return (te.name in tparams and not te.is_list and not te.is_map
                and not te.is_func and not te.type_args
                and not te.optional)

    def _unify_te(self, pte: ast.TypeExpr, ate: ast.TypeExpr,
                  tparams, bindings: dict) -> Optional[str]:
        """Unify a parameter TypeExpr (possibly with type variables)
        against an argument TypeExpr. Binds variables in `bindings`
        ({name: type_string}). Returns an error string on conflict,
        else None."""
        # "unknown" (cascading from a prior error) carries no info.
        if self._tname(ate) == "unknown":
            return None
        if self._is_tvar(pte, tparams):
            name = pte.name
            at = self._tname(ate)
            # "unknown" (prior error) or self-reference carries no info.
            if at == "unknown" or at == name:
                return None
            if name in bindings:
                if bindings[name] != at:
                    return (f"conflicting types for '{name}': "
                            f"'{bindings[name]}' vs '{at}'")
            else:
                bindings[name] = at
            return None
        if pte.is_list and ate.is_list:
            pe = (pte.list_elem if pte.list_elem is not None
                  else ast.TypeExpr(name=pte.name))
            ae = (ate.list_elem if ate.list_elem is not None
                  else ast.TypeExpr(name=ate.name))
            return self._unify_te(pe, ae, tparams, bindings)
        if pte.is_map and ate.is_map:
            e = self._unify_te(pte.map_key, ate.map_key, tparams,
                               bindings)
            if e:
                return e
            return self._unify_te(pte.map_val, ate.map_val, tparams,
                                  bindings)
        if pte.is_func and ate.is_func \
                and len(pte.func_params) == len(ate.func_params):
            for pp, ap in zip(pte.func_params, ate.func_params):
                e = self._unify_te(pp, ap, tparams, bindings)
                if e:
                    return e
            if pte.func_ret is not None and ate.func_ret is not None:
                return self._unify_te(pte.func_ret, ate.func_ret,
                                      tparams, bindings)
            return None
        if pte.type_args and ate.type_args and pte.name == ate.name \
                and len(pte.type_args) == len(ate.type_args):
            for pa, aa in zip(pte.type_args, ate.type_args):
                e = self._unify_te(pa, aa, tparams, bindings)
                if e:
                    return e
            return None
        # Concrete types: no binding; mismatches are E211 elsewhere.
        return None

    def _subst_te(self, te: ast.TypeExpr,
                  bindings: Dict[str, str]) -> ast.TypeExpr:
        """Substitute type variables using {name: type_string}."""
        import copy
        if self._is_tvar(te, bindings.keys()):
            return self._parse_tstr(bindings[te.name])
        te = copy.copy(te)
        if te.is_func:
            te.func_params = [self._subst_te(p, bindings)
                              for p in te.func_params]
            if te.func_ret is not None:
                te.func_ret = self._subst_te(te.func_ret, bindings)
        elif te.is_map:
            te.map_key = self._subst_te(te.map_key, bindings)
            te.map_val = self._subst_te(te.map_val, bindings)
        elif te.is_list:
            elem = te.list_elem if te.list_elem is not None \
                else ast.TypeExpr(name=te.name)
            new_elem = self._subst_te(elem, bindings)
            if (not new_elem.is_list and not new_elem.is_map
                    and not new_elem.is_func and not new_elem.type_args
                    and not new_elem.optional):
                # plain name: keep the legacy flattened form
                te.name = new_elem.name
                te.list_elem = None
            else:
                te.list_elem = new_elem
        elif te.type_args:
            te.type_args = [self._subst_te(a, bindings)
                            for a in te.type_args]
        return te

    def _split_applied(self, t: str):
        """Split an applied type string `Box<Int>` into (base, [args]).
        Returns (t, []) when not applied."""
        if "<" not in t or not t.endswith(">"):
            # handle optional: `Box<Int>?`
            if t.endswith("?"):
                base, args = self._split_applied(t[:-1])
                return base, args
            return t, []
        depth, start = 0, -1
        for i, ch in enumerate(t):
            if ch == "<":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == ">":
                depth -= 1
        if start < 0:
            return t, []
        base = t[:start]
        inner = t[start + 1:-1]
        args, depth, cur = [], 0, ""
        for ch in inner:
            if ch in "<([":
                depth += 1
            elif ch in ">)]":
                depth -= 1
            if ch == "," and depth == 0:
                args.append(cur.strip())
                cur = ""
            else:
                cur += ch
        args.append(cur.strip())
        return base, args

    def _infer_generic_call(self, fn: ast.FnDecl, e: ast.Call,
                            scope: dict) -> str:
        """Phase 5c: call a generic fn. Type arguments come from explicit
        `f<T>(...)` pinning or are inferred by unifying argument types
        against parameter types (E219 ambiguity / E220 conflict)."""
        tparams = fn.type_params
        bindings: Dict[str, str] = {}
        had_conflict = False
        explicit = bool(e.type_args)
        if e.type_args:
            if len(e.type_args) != len(tparams):
                self.error("E221",
                           f"'{fn.name}' takes {len(tparams)} type "
                           f"argument(s), got {len(e.type_args)}",
                           e.line, e.col)
                return "unknown"
            for p, a in zip(tparams, e.type_args):
                self._check_te(a, a.line, a.col)
                bindings[p] = self._tname(a)
        # Infer argument types. Non-lambda args first (they ground the
        # bindings); lambda args then get substituted expected types.
        # (Skipped when the caller pinned types explicitly — bindings
        # are fixed, so we go straight to the assignability check.)
        arg_ts: list = [None] * len(e.args)
        lam_idx = {i for i, a in enumerate(e.args)
                   if isinstance(a, ast.Lambda)}
        for i, a in enumerate(e.args):
            if i not in lam_idx:
                arg_ts[i] = self._infer(a, scope)
        if not explicit:
            for i, a in enumerate(e.args):
                if i in lam_idx or i >= len(fn.params):
                    continue
                _, pte = fn.params[i]
                err = self._unify_te(pte, self._parse_tstr(arg_ts[i]),
                                     tparams, bindings)
                if err:
                    self.error("E220", err, a.line, a.col)
                    had_conflict = True
            for i in sorted(lam_idx):
                if i >= len(fn.params):
                    continue
                _, pte = fn.params[i]
                exp_te = self._subst_te(pte, bindings)
                # Phase 5c: if the expected return is an unbound type var,
                # let the lambda infer its own return (unification binds the
                # var afterwards) instead of E211-ing here.
                if exp_te.is_func and exp_te.func_ret is not None \
                        and self._is_tvar(exp_te.func_ret, tparams):
                    import copy
                    exp_te = copy.copy(exp_te)
                    exp_te.func_ret = ast.TypeExpr(name="unknown")
                exp = self._tname(exp_te)
                arg_ts[i] = self._infer(e.args[i], scope, expected=exp)
                err = self._unify_te(pte, self._parse_tstr(arg_ts[i]),
                                     tparams, bindings)
                if err:
                    self.error("E220", err, e.args[i].line,
                               e.args[i].col)
                    had_conflict = True
        else:
            # Explicit pinning: lambdas still get expected types.
            for i in sorted(lam_idx):
                if i >= len(fn.params):
                    continue
                _, pte = fn.params[i]
                exp = self._tname(self._subst_te(pte, bindings))
                arg_ts[i] = self._infer(e.args[i], scope, expected=exp)
        if len(e.args) != len(fn.params):
            self.error("E102",
                       f"function '{fn.name}' takes {len(fn.params)} "
                       f"args, got {len(e.args)}", e.line, e.col)
            return "unknown"
        for p in tparams:
            if p not in bindings:
                self.error("E219",
                           f"cannot infer type argument '{p}' for "
                           f"'{fn.name}'; pin it explicitly "
                           f"('{fn.name}<{p}>(...)' or annotate the "
                           f"lambda parameters)", e.line, e.col)
                return "unknown"
        # Phase 5c: annotate the call with concrete type args for AOT
        # monomorphization (explicit pinning already set them).
        e.type_args = [self._parse_tstr(bindings[p]) for p in tparams]
        for (pn, pte), a, at in zip(fn.params, e.args, arg_ts):
            want_t = self._tname(self._subst_te(pte, bindings))
            if not had_conflict and at != "unknown" \
                    and not self._assignable(want_t, at):
                self.error("E211",
                           f"argument '{pn}' wants {want_t}, got {at}",
                           a.line, a.col)
        if fn.ret:
            return self._tname(self._subst_te(fn.ret, bindings))
        return "Void"

    def _check_generic_construct(self, decl: ast.TypeDecl, e: ast.Call,
                                 scope: dict) -> str:
        """Phase 5c: construct a generic record type — `Box<Int>(42)`
        or `Box(42)` with inference. Returns the applied type string."""
        tparams = decl.type_params
        bindings: Dict[str, str] = {}
        if e.type_args:
            if len(e.type_args) != len(tparams):
                self.error("E221",
                           f"'{decl.name}' takes {len(tparams)} type "
                           f"argument(s), got {len(e.type_args)}",
                           e.line, e.col)
                return "unknown"
            for p, a in zip(tparams, e.type_args):
                self._check_te(a, a.line, a.col)
                bindings[p] = self._tname(a)
        if len(e.args) != len(decl.fields):
            self.error("E102",
                       f"'{decl.name}' takes {len(decl.fields)} field "
                       f"arg(s), got {len(e.args)}", e.line, e.col)
            return "unknown"
        arg_ts = [self._infer(a, scope) for a in e.args]
        for (fn, fte), a, at in zip(decl.fields, e.args, arg_ts):
            err = self._unify_te(fte, self._parse_tstr(at), tparams,
                                 bindings)
            if err:
                self.error("E220", err, a.line, a.col)
        for p in tparams:
            if p not in bindings:
                self.error("E219",
                           f"cannot infer type argument '{p}' for "
                           f"'{decl.name}'; give explicit type arguments "
                           f"('{decl.name}<{p}>(...)' )", e.line, e.col)
                return "unknown"
        # Phase 5c: annotate for AOT monomorphization.
        e.type_args = [self._parse_tstr(bindings[p]) for p in tparams]
        for (fn, fte), a, at in zip(decl.fields, e.args, arg_ts):
            want_t = self._tname(self._subst_te(fte, bindings))
            if at != "unknown" and not self._assignable(want_t, at):
                self.error("E211",
                           f"field '{fn}' wants {want_t}, got {at}",
                           a.line, a.col)
        args = ", ".join(bindings[p] for p in tparams)
        return f"{decl.name}<{args}>"

    @staticmethod
    def _func_parts(t: str):
        """Phase 5a: if t is a `(P, ...) -> R` function type string,
        return (param_type_strings, ret_string). Else None."""
        if not t.startswith("("):
            return None
        depth = 0
        for i, ch in enumerate(t):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    rest = t[i + 1:].strip()
                    if not rest.startswith("->"):
                        return None
                    ret = rest[2:].strip()
                    inner = t[1:i].strip()
                    if not inner:
                        return [], ret
                    params, depth2, cur = [], 0, ""
                    for ch2 in inner:
                        if ch2 == "(":
                            depth2 += 1
                        elif ch2 == ")":
                            depth2 -= 1
                        if ch2 == "," and depth2 == 0:
                            params.append(cur.strip())
                            cur = ""
                        else:
                            cur += ch2
                    params.append(cur.strip())
                    return params, ret
        return None

    def _assignable(self, want: str, got: str) -> bool:
        if want == got:
            return True
        # Phase 5 (stdlib2): "any" accepts any value (json.stringify,
        # http.post_json — the dynamic value universe, like print).
        if want == "any":
            return True
        # Phase 5a: structural function subtyping — params
        # contravariant, return covariant.
        wf = self._func_parts(want)
        gf = self._func_parts(got)
        if wf is not None or gf is not None:
            if wf is None or gf is None:
                return False
            wps, wr = wf
            gps, gr = gf
            if len(wps) != len(gps):
                return False
            for wp, gp in zip(wps, gps):
                if not self._assignable(gp, wp):
                    return False
            return self._assignable(wr, gr)
        if want == "Float" and got == "Int":
            return True
        # Phase 7: nil is a valid Ptr (null pointer).
        if want == "Ptr" and got == "nil":
            return True
        if want.endswith("?") and got == "nil":
            return True
        if want.endswith("?") and got == want[:-1]:
            return True
        # An empty list literal has no element type to infer; it coerces
        # to any list type (so `var xs: [Int] = []` checks).
        if (got == "[unknown]" and want.startswith("[")
                and want.endswith("]")):
            return True
        # H-3: list covariance — [Text] is assignable to [any], etc.
        # (db.exec/db.query bind args accept heterogeneous scalars).
        if (want.startswith("[") and want.endswith("]")
                and got.startswith("[") and got.endswith("]")
                and "," not in want[1:-1] and "," not in got[1:-1]):
            return self._assignable(want[1:-1], got[1:-1])
        return False


def check_source(src: str, filename: str = "<input>") -> List[dict]:
    from .parser import parse_source, ParseError
    try:
        prog = parse_source(src, filename)
    except ParseError as pe:
        return [{"code": "E001", "message": str(pe),
                 "line": pe.line, "col": pe.col, "hint": ""}]
    except Exception as ex:  # lexer errors surface here
        line = getattr(ex, "line", 0)
        col = getattr(ex, "col", 0)
        return [{"code": "E001", "message": str(ex),
                 "line": line, "col": col, "hint": ""}]
    for item in prog.items:
        if isinstance(item, ast.ImportDecl):
            return [{"code": "E112",
                     "message": f"cannot resolve import '{item.path}' "
                                "from a source string",
                     "line": item.line, "col": item.col,
                     "hint": "use misa check <file.misa> for programs "
                             "with imports"}]
    return Checker().check(prog)


def check_file(path: str) -> List[dict]:
    """Check a .misa file, resolving its import chain."""
    from .loader import load
    merged, load_errors, _, file_of = load(path)
    if merged is None:
        return load_errors
    errors = list(load_errors)
    errors.extend(Checker().check(merged, file_of=file_of))
    return errors
