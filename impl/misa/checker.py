"""Misa v0.2 static checker: name resolution + pragmatic type checking.

Collects diagnostics (does not raise on first error):
  {code, message, line, col, hint}
"""
from __future__ import annotations
import os
from typing import Dict, List, Optional
from . import ast


PRIMITIVES = {"Text", "Int", "Float", "Bool", "Error", "Void"}
MEDIA = {"Photo", "Audio", "Video", "Document"}
SPECIAL = {"TokenStream", "Embedding"}
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
    "str.trim": (["Text"], "Text"),
    "str.ord": (["Text"], "Int"),
    "str.chr": (["Int"], "Text"),
    "process.run": (["[Text]"], "record"),
}

LAYOUT_COMPONENTS = {"Column", "Row"}
LEAF_COMPONENTS = {
    "Text", "Heading", "Button", "TextInput", "Image", "List",
    "Banner", "CameraButton", "Spacer",
}
ALL_COMPONENTS = LAYOUT_COMPONENTS | LEAF_COMPONENTS


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
        self.app_seen = False
        self._file_of: Optional[Dict[int, str]] = None
        # Phase 4 (M4): id: name -> component kind for the screen whose
        # handler body is currently being checked (None elsewhere).
        self._cur_id_kinds: Optional[Dict[str, str]] = None

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
            elif isinstance(item, ast.ModelDecl):
                self._dup(self.models, item.name, item, "model")
            elif isinstance(item, ast.ScreenDecl):
                self._dup(self.screens, item.name, item, "screen")
            elif isinstance(item, ast.EvalDecl):
                self._dup(self.evals, item.name, item, "eval")
            elif isinstance(item, ast.FnDecl):
                self._dup(self.fns, item.name, item, "function")
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
        elif isinstance(item, (ast.AppDecl, ast.ProviderDecl,
                               ast.ModelsDecl, ast.TypeDecl,
                               ast.ImportDecl)):
            pass
        else:
            self._check_block([item], scope)

    def _check_fn(self, fn: ast.FnDecl):
        seen_params = set()
        for pn, te in fn.params:
            if pn in seen_params:
                self.error("E210", f"duplicate parameter '{pn}'",
                           te.line, te.col)
            seen_params.add(pn)
            if not self._known_type(te):
                self.error("E208", f"unknown type '{self._tname(te)}'",
                           te.line, te.col)
        ret_name = self._tname(fn.ret) if fn.ret else "Void"
        if fn.ret and not self._known_type(fn.ret):
            self.error("E208", f"unknown type '{self._tname(fn.ret)}'",
                       fn.ret.line, fn.ret.col)
        scope = {pn: (self._tname(te), False) for pn, te in fn.params}
        self._check_block(fn.body, scope, fn_ret=ret_name)
        if ret_name != "Void" and not self._block_returns(fn.body):
            self.error("E211",
                       f"function '{fn.name}' may exit without "
                       f"returning {ret_name}",
                       fn.line, fn.col,
                       hint="every code path must end in 'return'")

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
        if not self._known_type(m.ret):
            self.error("E208",
                       f"unknown return type '{self._tname(m.ret)}'",
                       m.line, m.col)
        for _, te in m.params:
            if not self._known_type(te):
                self.error("E208", f"unknown type '{self._tname(te)}'",
                           te.line, te.col)
        # prompt interpolations may only reference params
        scope = {pn: (self._tname(te), False) for pn, te in m.params}
        for part in m.prompt:
            if isinstance(part, ast.Node):
                self._infer(part, scope)

    def _check_screen(self, s: ast.ScreenDecl):
        for pn, te in s.params:
            if not self._known_type(te):
                self.error("E208", f"unknown type '{self._tname(te)}'",
                           te.line, te.col)
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
                if not isinstance(val, ast.Ident):
                    self.error("E206", "'id:' needs a plain name",
                               val.line, val.col)
                elif val.name in seen_ids:
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
                want = self._tname(s.ann)
                if self._check_annotated(s.ann, s.value, scope):
                    t = want
                elif t != "unknown" and not self._assignable(want, t):
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
            self._infer(s.iter, scope)
            inner = dict(scope)
            inner[s.var] = ("unknown", False)
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
                self.error("E212", "'throw' needs an Error "
                           "({ code: Text, message: Text })",
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
        if te.is_list and isinstance(value, ast.ListLit):
            for item in value.items:
                if isinstance(item, ast.RecordLit):
                    self._check_record_fields(te.name, item, scope)
                else:
                    self.error("E202",
                               f"expected {te.name} record in list",
                               item.line, item.col)
            return True
        if not te.is_list and isinstance(value, ast.RecordLit):
            self._check_record_fields(te.name, value, scope)
            return True
        return False

    def _check_record_fields(self, type_name: str, rec: ast.RecordLit,
                             scope: dict):
        decl = self.types[type_name]
        declared = {fn: fte for fn, fte in decl.fields}
        seen = set()
        for fn, fv in rec.fields:
            seen.add(fn)
            if fn not in declared:
                self.error("E202",
                           f"type '{type_name}' has no field '{fn}'",
                           fv.line, fv.col)
                continue
            fte = declared[fn]
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
                # maybe a model/screen/type used as value?
                if e.name in self.models or e.name in self.screens:
                    return "unknown"
                self.error("E101", f"undefined name '{e.name}'",
                           e.line, e.col)
                return "unknown"
            return scope[e.name][0]
        if isinstance(e, ast.Call):
            return self._infer_call(e, scope)
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
            # Error.code / Error.message on the builtin error type
            if isinstance(e.obj, ast.Ident) and e.obj.name in scope \
                    and scope[e.obj.name][0] == "Error" \
                    and e.field in ("code", "message"):
                return "Text"
            ot = self._infer(e.obj, scope)
            if ot == "Error" and e.field in ("code", "message"):
                return "Text"
            decl = self.types.get(ot)
            if decl:
                for fn, fte in decl.fields:
                    if fn == e.field:
                        return self._tname(fte)
                self.error("E101",
                           f"type '{ot}' has no field '{e.field}'",
                           e.line, e.col)
            return "unknown"
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
            if e.op in ("==", "!=", "<", "<=", ">", ">="):
                return "Bool"
            if e.op == "+" and (lt == "Text" or rt == "Text"):
                return "Text"
            if lt in ("Int", "Float") and rt in ("Int", "Float"):
                return "Float" if "Float" in (lt, rt) else "Int"
            return "unknown"
        if isinstance(e, ast.UnOp):
            t = self._infer(e.operand, scope)
            return "Bool" if e.op == "not" else t
        if isinstance(e, ast.Await):
            t = self._infer(e.operand, scope)
            if t != "TokenStream":
                self.error("E205",
                           f"'await' needs a TokenStream, found {t}",
                           e.line, e.col)
            return "unknown"  # model return type; resolved at runtime
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
            ret = exp_ret if exp_ret is not None else body_t
            if exp_ret is not None and body_t != "unknown" \
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

    def _infer_call(self, e: ast.Call, scope: dict) -> str:
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
        # Phase 5a: per-arg expected types so lambdas infer untyped
        # params from the callee's function type.
        exp_ts = None
        if e.name in self.fns:
            exp_ts = [self._tname(te) for _, te in self.fns[e.name].params]
        elif e.name in scope:
            fp = self._func_parts(scope[e.name][0])
            if fp is not None:
                exp_ts, _ = fp
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
        if e.name in NS_BUILTINS:
            params, ret = NS_BUILTINS[e.name]
            if len(e.args) != len(params):
                self.error("E102",
                           f"'{e.name}' takes {len(params)} args, "
                           f"got {len(e.args)}", e.line, e.col)
            else:
                for want_t, a, at in zip(params, e.args, arg_ts):
                    if at != "unknown" and not self._assignable(want_t, at):
                        self.error("E211",
                                   f"'{e.name}' wants {want_t}, got {at}",
                                   a.line, a.col)
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
            s = f"[{s}]"
        elif te.is_map:
            s = f"Map<{self._tname(te.map_key)}, {self._tname(te.map_val)}>"
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
        base = te.name
        return (base in PRIMITIVES or base in MEDIA or base in SPECIAL
                or base in self.types)

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
        if want.endswith("?") and got == "nil":
            return True
        if want.endswith("?") and got == want[:-1]:
            return True
        # An empty list literal has no element type to infer; it coerces
        # to any list type (so `var xs: [Int] = []` checks).
        if (got == "[unknown]" and want.startswith("[")
                and want.endswith("]")):
            return True
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
