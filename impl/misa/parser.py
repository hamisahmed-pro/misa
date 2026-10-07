"""Misa v0.1 recursive-descent parser. Produces ast.Program."""
from __future__ import annotations
from typing import List, Optional
from . import ast
from .lexer import Lexer, Token


class ParseError(Exception):
    def __init__(self, message, line=0, col=0):
        super().__init__(message)
        self.line = line
        self.col = col


class Parser:
    def __init__(self, tokens: List[Token], filename: str = "<input>"):
        self.toks = tokens
        self.i = 0
        self.filename = filename

    # -- cursor ---------------------------------------------------------

    def peek(self, n: int = 0) -> Token:
        j = self.i + n
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def advance(self) -> Token:
        t = self.peek()
        if t.kind != "EOF":
            self.i += 1
        return t

    def at(self, kind: str, n: int = 0) -> bool:
        return self.peek(n).kind == kind

    def expect(self, kind: str, what: str) -> Token:
        t = self.peek()
        if t.kind != kind:
            raise ParseError(f"expected {what}, found {t.kind} "
                             f"({t.value!r})", t.line, t.col)
        return self.advance()

    def expect_ident(self, what: str = "identifier") -> Token:
        return self.expect("IDENT", what)

    def skip_semis(self):
        while self.at("SEMI"):
            self.advance()

    def maybe_semi(self):
        if self.at("SEMI"):
            self.advance()

    # -- entry ----------------------------------------------------------

    def parse_program(self) -> ast.Program:
        prog = ast.Program()
        self.skip_semis()
        while not self.at("EOF"):
            prog.items.append(self.parse_item())
            self.skip_semis()
        return prog

    def parse_item(self) -> ast.Node:
        leading = self.peek().leading
        node = self._parse_item_inner()
        node.leading_comments = leading
        return node

    def _parse_item_inner(self) -> ast.Node:
        k = self.peek().kind
        if k == "app":
            return self.parse_app()
        if k == "provider":
            return self.parse_provider()
        if k == "models":
            return self.parse_models()
        if k == "type":
            return self.parse_type_decl()
        if k == "table":
            return self.parse_table_decl()
        if k == "model":
            return self.parse_model_decl()
        if k == "screen":
            return self.parse_screen()
        if k == "eval":
            return self.parse_eval()
        # `test` is a contextual keyword: only `test <name> {` at the top
        # level parses as a test block. Any other use of `test` (call,
        # variable, assignment) falls through to parse_statement.
        if (k == "IDENT" and self.peek().value == "test"
                and self.peek(1).kind == "IDENT" and self.at("{", 2)):
            return self.parse_test_decl()
        # Phase 8 (overtake): `interface` / `impl` are contextual
        # keywords (IDENTs, not lexer keywords) so existing code using
        # those words as identifiers keeps working.
        if (k == "IDENT" and self.peek().value == "interface"
                and self.peek(1).kind == "IDENT"):
            return self.parse_interface_decl()
        if (k == "IDENT" and self.peek().value == "impl"
                and self.peek(1).kind == "IDENT"):
            return self.parse_impl_decl()
        if k == "fn":
            return self.parse_fn_decl()
        if k == "extern":
            return self.parse_extern_block()
        if k == "import":
            return self.parse_import_decl()
        return self.parse_statement()

    # -- declarations ---------------------------------------------------

    def parse_app(self) -> ast.AppDecl:
        t = self.expect("app", "'app'")
        name = self.expect_ident("app name").value
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name")
            self.expect(":", "':'")
            lit = self.parse_literal()
            fields.append((fk.value, lit))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.AppDecl(name=name, fields=fields, line=t.line, col=t.col)

    def parse_provider(self) -> ast.ProviderDecl:
        t = self.expect("provider", "'provider'")
        name = self.expect_ident("provider name").value
        self.expect("{", "'{'")
        key_env = ""
        model_file = ""
        self.skip_semis()
        while not self.at("}"):
            fk_tok = self.peek()
            if fk_tok.kind == "IDENT":
                fname = fk_tok.value
                self.advance()
            elif fk_tok.kind == "model":
                # 'model' is a keyword, but it names a provider field here
                fname = "model"
                self.advance()
            else:
                raise ParseError(
                    f"expected field name, found {fk_tok.kind} "
                    f"({fk_tok.value!r})", fk_tok.line, fk_tok.col)
            fk_line, fk_col = fk_tok.line, fk_tok.col
            self.expect(":", "':'")
            if fname == "key":
                self.expect("env", "'env'")
                self.expect("(", "'('")
                key_env = self.expect("STRING", "string").value
                self.expect(")", "')'")
            elif fname == "model":
                ftok = self.expect("IDENT", "'file'")
                if ftok.value != "file":
                    raise ParseError(f"expected 'file', found "
                                     f"{ftok.value!r}", ftok.line, ftok.col)
                self.expect("(", "'('")
                model_file = self.expect("STRING", "string").value
                self.expect(")", "')'")
            else:
                raise ParseError(f"unknown provider field '{fname}'",
                                 fk_line, fk_col)
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.ProviderDecl(name=name, key_env=key_env,
                                model_file=model_file,
                                line=t.line, col=t.col)

    def parse_models(self) -> ast.ModelsDecl:
        t = self.expect("models", "'models'")
        self.expect("{", "'{'")
        entries = []
        self.skip_semis()
        while not self.at("}"):
            alias = self.expect_ident("model alias").value
            self.expect(":", "':'")
            prov = self.expect_ident("provider name").value
            self.expect("(", "'('")
            mid = self.expect("STRING", "model id string").value
            self.expect(")", "')'")
            entries.append((alias, prov, mid))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.ModelsDecl(entries=entries, line=t.line, col=t.col)

    def parse_table_decl(self) -> ast.TableDecl:
        """Phase 5 (DB): `table users { id: Int @primary, name: Text }`."""
        t = self.expect("table", "'table'")
        name = self.expect_ident("table name").value
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("column name").value
            self.expect(":", "':'")
            te = self.parse_type_expr()
            primary = False
            if self.at("@"):
                self.advance()
                attr = self.expect_ident("attribute name").value
                if attr != "primary":
                    raise ParseError(
                        f"unknown table attribute '@{attr}' "
                        f"(only '@primary' is supported)",
                        self.peek().line, self.peek().col)
                primary = True
            fields.append((fk, te, primary))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.TableDecl(name=name, fields=fields,
                             line=t.line, col=t.col)

    def parse_type_decl(self) -> ast.TypeDecl:
        t = self.expect("type", "'type'")
        name = self.expect_ident("type name").value
        # Phase 5c: type parameters, e.g. `type Box<T> { value: T }`.
        # Phase 5d: inline bounds (`type Box<T: Printable>`) and
        # `where` clauses (`type C<K, V> where K: Hashable`).
        type_params = []
        type_param_bounds = {}
        if self.at("<"):
            for pn, bound in self._parse_type_params_decl():
                type_params.append(pn)
                if bound is not None:
                    type_param_bounds[pn] = bound
        where_bounds = {}
        if self.at("IDENT") and self.peek().value == "where":
            where_bounds = self._parse_where_clause(
                type_params, f"type '{name}'")
        self.expect("{", "'{'")
        fields = []
        variants = []
        is_sum = False
        self.skip_semis()
        if not self.at("}"):
            # Phase 5b: disambiguate record vs sum form by what follows
            # the first name: `name:` -> record field, otherwise a variant.
            first = self.expect_ident("field or variant name").value
            if self.at(":"):
                self.advance()
                te = self.parse_type_expr()
                fields.append((first, te))
                if self.at(","):
                    self.advance()
                self.skip_semis()
                while not self.at("}"):
                    fk = self.expect_ident("field name").value
                    self.expect(":", "':'")
                    te = self.parse_type_expr()
                    fields.append((fk, te))
                    if self.at(","):
                        self.advance()
                    self.skip_semis()
            else:
                is_sum = True
                variants.append(self._finish_variant(first))
                if self.at(","):
                    self.advance()
                self.skip_semis()
                while not self.at("}"):
                    vn = self.expect_ident("variant name").value
                    variants.append(self._finish_variant(vn))
                    if self.at(","):
                        self.advance()
                    self.skip_semis()
        self.expect("}", "'}'")
        return ast.TypeDecl(name=name, fields=fields, is_sum=is_sum,
                            variants=variants, type_params=type_params,
                            type_param_bounds=type_param_bounds,
                            where_bounds=where_bounds,
                            line=t.line, col=t.col)

    def _finish_variant(self, vname: str) -> ast.VariantDecl:
        """Parse the optional `(fields)` after a variant name. Fields may
        be named (`radius: Float`) or positional (`Int`); positional
        fields get synthetic names `_0`, `_1`, ..."""
        t = self.peek()
        fields = []
        if self.at("("):
            self.advance()
            idx = 0
            if not self.at(")"):
                while True:
                    if self.at("IDENT") and self.peek(1).kind == ":":
                        fn = self.expect_ident("field name").value
                        self.expect(":", "':'")
                        te = self.parse_type_expr()
                        fields.append((fn, te))
                    else:
                        te = self.parse_type_expr()
                        fields.append((f"_{idx}", te))
                        idx += 1
                    if self.at(","):
                        self.advance()
                        continue
                    break
            self.expect(")", "')'")
        return ast.VariantDecl(name=vname, fields=fields,
                               line=t.line, col=t.col)

    def expect_gt(self):
        """Consume a '>' closing a Map<...> type. Splits a lexed '>='
        (as in `Map<K,V>=...`) back into '>' and '='."""
        from .lexer import Token
        t = self.peek()
        if t.kind == ">":
            return self.advance()
        if t.kind == ">=":
            self.advance()
            self.toks.insert(self.i, Token("=", "=", t.line, t.col, []))
            return t
        raise ParseError(f"expected '>', found {t.kind} ({t.value!r})",
                         t.line, t.col)

    def parse_type_expr(self) -> ast.TypeExpr:
        t = self.peek()
        # Phase 5a: function type `(Int, Text) -> Bool`, `() -> Void`.
        if self.at("("):
            self.advance()
            params = []
            if not self.at(")"):
                while True:
                    params.append(self.parse_type_expr())
                    if self.at(","):
                        self.advance()
                        continue
                    break
            self.expect(")", "')'")
            self.expect("->", "'->'")
            ret = self.parse_type_expr()
            opt = False
            if self.at("?"):
                self.advance()
                opt = True
            return ast.TypeExpr(is_func=True, func_params=params,
                                func_ret=ret, optional=opt,
                                line=t.line, col=t.col)
        if self.at("["):
            self.advance()
            inner = self.parse_type_expr()
            self.expect("]", "']'")
            opt = False
            if self.at("?"):
                self.advance()
                opt = True
            te = ast.TypeExpr(name=inner.name,
                              is_list=True, optional=opt,
                              line=t.line, col=t.col)
            # Phase 5c: preserve full inner structure for `[Box<Int>]`,
            # `[[T]]`, etc. Plain inners keep the legacy flattened form.
            if inner.is_list or inner.is_map or inner.is_func \
                    or inner.type_args:
                te.list_elem = inner
            return te
        # Phase 5c: applied generic types `Name<A, B>`. `Map<K, V>` keeps
        # its dedicated is_map form (it was the only generic type in
        # v0.2); everything else lands in type_args.
        if self.at("IDENT") and self.peek(1).kind == "<":
            name = self.peek().value
            self.advance()
            args = self._parse_type_args()
            opt = False
            if self.at("?"):
                self.advance()
                opt = True
            if name == "Map":
                if len(args) != 2:
                    raise ParseError(
                        f"Map takes 2 type arguments, got {len(args)}",
                        t.line, t.col)
                return ast.TypeExpr(name="Map", is_map=True,
                                    map_key=args[0], map_val=args[1],
                                    optional=opt, line=t.line, col=t.col)
            return ast.TypeExpr(name=name, type_args=args, optional=opt,
                                line=t.line, col=t.col)
        name = self.expect_ident("type name").value
        opt = False
        if self.at("?"):
            self.advance()
            opt = True
        return ast.TypeExpr(name=name, optional=opt, line=t.line, col=t.col)

    def parse_params(self, allow_varargs=False) -> tuple:
        """Parse `(a: Int, b: Text)`. Returns (params, varargs).

        Overtake Wave 2 (FFI): with allow_varargs, a trailing `...`
        marks C varargs (`fn printf(fmt: Text, ...) -> Int`). `...`
        anywhere else is a parse error.
        """
        self.expect("(", "'('")
        params = []
        varargs = False
        if not self.at(")"):
            while True:
                if self.at("..."):
                    if not allow_varargs:
                        raise ParseError(
                            "varargs '...' are only allowed on extern "
                            "function declarations", self.peek().line,
                            self.peek().col)
                    self.advance()
                    varargs = True
                    break
                pn = self.expect_ident("parameter name").value
                self.expect(":", "':'")
                te = self.parse_type_expr()
                params.append((pn, te))
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect(")", "')'")
        return params, varargs

    def parse_model_decl(self) -> ast.ModelDecl:
        t = self.expect("model", "'model'")
        name = self.expect_ident("model name").value
        params, _ = self.parse_params()
        self.expect("->", "'->'")
        ret = self.parse_type_expr()
        self.skip_semis()
        self.expect("with", "'with'")
        prov = self.expect_ident("provider name").value
        self.expect(".", "'.'")
        alias = self.expect_ident("model alias").value
        self.skip_semis()
        budget = None
        if self.at("budget"):
            budget = self.parse_budget()
            self.skip_semis()
        self.expect("prompt", "'prompt'")
        vt = self.expect_ident("version like v3")
        import re
        m = re.fullmatch(r"v(\d+)", vt.value)
        if not m:
            raise ParseError(f"expected version like v3, found '{vt.value}'",
                             vt.line, vt.col)
        version = int(m.group(1))
        st = self.expect("STRING", "prompt template")
        parts = self.parse_string_parts(st.value, st.line, st.col)
        return ast.ModelDecl(name=name, params=params, ret=ret,
                             provider=prov, alias=alias, budget=budget,
                             prompt_version=version, prompt=parts,
                             line=t.line, col=t.col)

    def parse_budget(self) -> ast.Budget:
        self.expect("budget", "'budget'")
        self.expect("{", "'{'")
        b = ast.Budget()
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("budget field").value
            self.expect(":", "':'")
            if fk == "tokens":
                b.tokens = self.expect("INT", "integer").value
            elif fk == "latency":
                b.latency_ms = self.expect("DURATION", "duration").value
            elif fk == "cost":
                b.cost_usd = self.expect("MONEY", "money amount").value
            else:
                raise ParseError(f"unknown budget field '{fk}'",
                                 self.peek().line, self.peek().col)
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return b

    def parse_screen(self) -> ast.ScreenDecl:
        t = self.expect("screen", "'screen'")
        name = self.expect_ident("screen name").value
        _pp = self.parse_params() if self.at("(") else ([], False)
        params, _ = _pp
        self.expect("{", "'{'")
        body = self.parse_screen_block()
        self.expect("}", "'}'")
        return ast.ScreenDecl(name=name, params=params, body=body,
                              line=t.line, col=t.col)

    def parse_screen_block(self) -> list:
        items = []
        self.skip_semis()
        while not self.at("}"):
            items.append(self.parse_screen_item())
            self.skip_semis()
        return items

    def parse_screen_item(self) -> ast.Node:
        if self.at("if"):
            return self.parse_screen_if()
        if self.at("for"):
            return self.parse_screen_for()
        # Screen state: `let`/`var` in a screen body (e.g.
        # `let count = state(0)` for ephemeral per-screen state).
        k = self.peek().kind
        if k in ("let", "var"):
            return self.parse_let(k == "var")
        return self.parse_component()

    def parse_screen_if(self) -> ast.ScreenIf:
        t = self.expect("if", "'if'")
        cond = self.parse_expr()
        self.expect("{", "'{'")
        then = self.parse_screen_block()
        self.expect("}", "'}'")
        els: list = []
        if self.at("SEMI") and self.peek(1).kind == "else":
            self.advance()
        if self.at("else"):
            self.advance()
            self.expect("{", "'{'")
            els = self.parse_screen_block()
            self.expect("}", "'}'")
        self.maybe_semi()
        return ast.ScreenIf(cond=cond, then=then, els=els,
                            line=t.line, col=t.col,
                            leading_comments=t.leading)

    def parse_screen_for(self) -> ast.ScreenFor:
        t = self.expect("for", "'for'")
        var = self.expect_ident("loop variable").value
        self.expect("in", "'in'")
        it = self.parse_expr()
        self.expect("{", "'{'")
        body = self.parse_screen_block()
        self.expect("}", "'}'")
        self.maybe_semi()
        return ast.ScreenFor(var=var, iter=it, body=body,
                             line=t.line, col=t.col,
                             leading_comments=t.leading)

    def parse_component(self) -> ast.Component:
        leading = self.peek().leading
        t = self.expect_ident("component name")
        args = []
        if self.at("("):
            self.advance()
            if not self.at(")"):
                while True:
                    an = self.expect_ident("argument name").value
                    self.expect(":", "':'")
                    if an == "id":
                        idt = self.expect_ident("component id")
                        args.append((an, ast.Ident(name=idt.value,
                                                  line=idt.line,
                                                  col=idt.col)))
                    else:
                        args.append((an, self.parse_expr()))
                    if self.at(","):
                        self.advance()
                        continue
                    break
            self.skip_semis()
            self.expect(")", "')'")
        children = []
        if self.at("{"):
            self.advance()
            children = self.parse_screen_block()
            self.expect("}", "'}'")
        return ast.Component(name=t.value, args=args, children=children,
                             line=t.line, col=t.col,
                             leading_comments=leading)

    def parse_eval(self) -> ast.EvalDecl:
        t = self.expect("eval", "'eval'")
        name = self.expect_ident("eval name").value
        self.expect("{", "'{'")
        model_name, given, expect, runs = "", [], None, 1
        self.skip_semis()
        while not self.at("}"):
            ft = self.peek()
            if ft.kind == "IDENT":
                fk = self.advance().value
            elif ft.kind in ("model", "given", "expect", "runs"):
                fk = self.advance().kind
            else:
                raise ParseError("expected field name, found "
                                 f"{ft.kind} ({ft.value!r})",
                                 ft.line, ft.col)
            self.expect(":", "':'")
            if fk == "model":
                model_name = self.expect_ident("model name").value
            elif fk == "given":
                given = self.parse_record_fields()
            elif fk == "expect":
                expect = self.parse_expr()
            elif fk == "runs":
                runs = self.expect("INT", "integer").value
            else:
                raise ParseError(f"unknown eval field '{fk}'",
                                 self.peek().line, self.peek().col)
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        if not model_name:
            raise ParseError("eval needs a 'model:' field", t.line, t.col)
        return ast.EvalDecl(name=name, model_name=model_name, given=given,
                            expect=expect, runs=runs,
                            line=t.line, col=t.col)

    def parse_test_decl(self) -> ast.TestDecl:
        t = self.advance()  # the contextual `test` identifier
        name = self.expect("IDENT", "test name").value
        body = self.parse_block()
        return ast.TestDecl(name=name, body=body,
                            line=t.line, col=t.col)

    # -- Phase 8 (overtake): interfaces ---------------------------------

    def _parse_iface_sig(self) -> ast.FnDecl:
        """Parse `fn name(self, p: T, ...) -> Ret throws ...` — the
        signature shared by interface methods (no body) and impl
        methods (with body). Bare `self` (no `: Type`) is allowed and
        defaults to the impl target (filled in by the checker)."""
        ft = self.expect("fn", "'fn'")
        name = self.expect_ident("method name").value
        self.expect("(", "'('")
        params = []
        if not self.at(")"):
            while True:
                pn_tok = self.expect_ident("parameter name")
                pn = pn_tok.value
                if pn == "self" and not self.at(":"):
                    # Bare `self`: type defaults to the impl target.
                    params.append((pn, None))
                else:
                    self.expect(":", "':'")
                    params.append((pn, self.parse_type_expr()))
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect(")", "')'")
        ret = None
        if self.at("->"):
            self.advance()
            ret = self.parse_type_expr()
        # `throws` is contextual (an IDENT here, not a keyword).
        throws: List[str] = []
        throws_line, throws_col = 0, 0
        if self.at("IDENT") and self.peek().value == "throws":
            t_throws = self.advance()
            throws_line, throws_col = t_throws.line, t_throws.col
            while True:
                ctok = self.expect_ident("error code (e.g. E304)")
                throws.append(ctok.value)
                if self.at(","):
                    self.advance()
                    continue
                break
        return ast.FnDecl(name=name, params=params, ret=ret, body=[],
                          throws=throws, throws_line=throws_line,
                          throws_col=throws_col,
                          line=ft.line, col=ft.col)

    def parse_interface_decl(self) -> ast.InterfaceDecl:
        t = self.advance()  # the contextual `interface` identifier
        name = self.expect("IDENT", "interface name").value
        self.expect("{", "'{'")
        methods = []
        self.skip_semis()
        while not self.at("}"):
            sig = self._parse_iface_sig()
            if self.at("{"):
                raise ParseError(
                    f"interface method '{sig.name}' must not have a "
                    f"body (give the implementation in an "
                    f"`impl {name} for ...` block)",
                    self.peek().line, self.peek().col)
            methods.append(sig)
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.InterfaceDecl(name=name, methods=methods,
                                 line=t.line, col=t.col)

    def parse_impl_decl(self) -> ast.ImplDecl:
        t = self.advance()  # the contextual `impl` identifier
        iface = self.expect("IDENT", "interface name").value
        self.expect("for", "'for'")
        target = self.expect("IDENT", "type name").value
        self.expect("{", "'{'")
        methods = []
        self.skip_semis()
        while not self.at("}"):
            sig = self._parse_iface_sig()
            sig.body = self.parse_block()
            # Bare `self` defaults to the impl target type.
            if sig.params and sig.params[0][0] == "self" \
                    and sig.params[0][1] is None:
                te = ast.TypeExpr(name=target,
                                  line=sig.line, col=sig.col)
                sig.params[0] = ("self", te)
            methods.append(sig)
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.ImplDecl(iface=iface, target=target, methods=methods,
                            line=t.line, col=t.col)

    def parse_record_fields(self) -> list:
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name").value
            self.expect(":", "':'")
            fields.append((fk, self.parse_expr()))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return fields

    # -- statements -----------------------------------------------------

    def parse_statement(self) -> ast.Node:
        leading = self.peek().leading
        node = self._parse_statement_inner()
        if not node.leading_comments:
            node.leading_comments = leading
        return node

    def _parse_statement_inner(self) -> ast.Node:
        k = self.peek().kind
        if k in ("let", "var"):
            return self.parse_let(k == "var")
        if k == "if":
            return self.parse_if()
        if k == "for":
            return self.parse_for()
        if k == "while":
            return self.parse_while()
        if k == "return":
            return self.parse_return()
        if k == "try":
            return self.parse_try()
        if k == "throw":
            return self.parse_throw()
        if k == "on":
            return self.parse_on()
        # expression statement or assignment (or stream declaration)
        e = self.parse_expr()
        if isinstance(e, ast.StreamDecl):
            self.maybe_semi()
            return e
        if self.at("="):
            self.advance()
            val = self.parse_expr()
            node = ast.AssignStmt(target=e, value=val,
                                  line=e.line, col=e.col)
        else:
            node = ast.ExprStmt(expr=e, line=e.line, col=e.col)
        self.maybe_semi()
        return node

    def parse_let(self, mutable: bool) -> ast.LetStmt:
        t = self.advance()
        name = self.expect_ident("variable name").value
        ann = None
        if self.at(":"):
            self.advance()
            ann = self.parse_type_expr()
        self.expect("=", "'='")
        val = self.parse_expr()
        self.maybe_semi()
        return ast.LetStmt(name=name, ann=ann, value=val, mutable=mutable,
                           line=t.line, col=t.col)

    def parse_block(self) -> list:
        self.expect("{", "'{'")
        stmts = []
        self.skip_semis()
        while not self.at("}"):
            stmts.append(self.parse_statement())
            self.skip_semis()
        self.expect("}", "'}'")
        return stmts

    def parse_if(self) -> ast.IfStmt:
        t = self.expect("if", "'if'")
        cond = self.parse_expr()
        then = self.parse_block()
        els: list = []
        # allow implicit semicolon between } and else
        if self.at("SEMI") and self.peek(1).kind == "else":
            self.advance()
        if self.at("else"):
            self.advance()
            # W-5: 'else if' desugars to 'else { if ... }' — the else
            # branch holds the nested IfStmt as a single statement.
            # All backends (interp/checker/emit) walk els as a generic
            # statement list, so no AST or backend change is needed.
            if self.at("if"):
                els = [self.parse_if()]
            else:
                els = self.parse_block()
        self.maybe_semi()
        return ast.IfStmt(cond=cond, then=then, els=els,
                          line=t.line, col=t.col)

    def parse_for(self) -> ast.ForStmt:
        t = self.expect("for", "'for'")
        var = self.expect_ident("loop variable").value
        self.expect("in", "'in'")
        it = self.parse_expr()
        body = self.parse_block()
        self.maybe_semi()
        return ast.ForStmt(var=var, iter=it, body=body,
                           line=t.line, col=t.col)

    def parse_while(self) -> ast.WhileStmt:
        t = self.expect("while", "'while'")
        cond = self.parse_expr()
        body = self.parse_block()
        self.maybe_semi()
        return ast.WhileStmt(cond=cond, body=body, line=t.line, col=t.col)

    def parse_return(self) -> ast.ReturnStmt:
        t = self.expect("return", "'return'")
        val = None
        if not self.at("SEMI") and not self.at("}") and not self.at("EOF"):
            val = self.parse_expr()
        self.maybe_semi()
        return ast.ReturnStmt(value=val, line=t.line, col=t.col)

    def parse_fn_decl(self) -> ast.FnDecl:
        t = self.expect("fn", "'fn'")
        name = self.expect_ident("function name").value
        # Phase 5c: type parameters, e.g. `fn first<T>(xs: [T]) -> T`.
        # Phase 8 (overtake): bounds, e.g. `fn f<T: Drawable>(x: T)`.
        type_params = []
        type_param_bounds = {}
        if self.at("<"):
            for pn, bound in self._parse_type_params_decl():
                type_params.append(pn)
                if bound is not None:
                    type_param_bounds[pn] = bound
        params, _pv = self.parse_params()
        if _pv:
            raise ParseError("varargs '...' are only allowed on extern "
                             "function declarations", t.line, t.col)
        ret = None
        if self.at("->"):
            self.advance()
            ret = self.parse_type_expr()
        # Phase 5d: `where` clause, e.g.
        # `fn sort<T>(xs: [T]) -> [T] where T: Comparable`.
        # Contextual (an IDENT here, not a keyword); parsed before
        # `throws` so `... -> T where T: X throws E304 {` reads naturally.
        where_bounds = {}
        if self.at("IDENT") and self.peek().value == "where":
            where_bounds = self._parse_where_clause(
                type_params, f"function '{name}'")
        # Phase 8 (overtake): checked `throws` signatures, e.g.
        # `fn fetch(u: Text) -> Text throws E304, E303`.
        # `throws` is contextual (an IDENT here, not a keyword) so
        # existing code using `throws` as an identifier keeps working.
        throws: List[str] = []
        throws_line, throws_col = 0, 0
        if self.at("IDENT") and self.peek().value == "throws":
            t_throws = self.advance()
            throws_line, throws_col = t_throws.line, t_throws.col
            while True:
                ctok = self.expect_ident("error code (e.g. E304)")
                throws.append(ctok.value)
                if self.at(","):
                    self.advance()
                    continue
                break
        body = self.parse_block()
        return ast.FnDecl(name=name, params=params, ret=ret, body=body,
                          type_params=type_params,
                          type_param_bounds=type_param_bounds,
                          where_bounds=where_bounds,
                          throws=throws,
                          throws_line=throws_line, throws_col=throws_col,
                          line=t.line, col=t.col)

    def parse_extern_block(self) -> ast.ExternDecl:
        """Phase 7: `extern "<lib>" { fn f(...) -> T, ... }`.

        Overtake Wave 2: blocks may also declare C-layout structs
        (`struct Point { x: Float, y: Float }`, contextual keyword —
        `struct` stays a plain identifier everywhere else), and
        function signatures may end params with `...` for C varargs.

        Overtake final wave: `union Value { i: Int, f: Float }`
        (same contextual-keyword treatment) and bitfield widths
        (`a: Int:3`) on struct/union fields.
        """
        t = self.expect("extern", "'extern'")
        lib_tok = self.expect("STRING", "library name string")
        lib = lib_tok.value
        self.expect("{", "'{'")
        fns = []
        structs = []
        self.skip_semis()
        while not self.at("}"):
            pk = self.peek()
            if pk.kind == "IDENT" and pk.value in ("struct", "union") \
                    and self.peek(1).kind == "IDENT":
                structs.append(self.parse_extern_struct(
                    union=(pk.value == "union")))
            else:
                fns.append(self.parse_extern_fn())
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.ExternDecl(lib=lib, fns=fns, structs=structs,
                              line=t.line, col=t.col)

    def _looks_like_struct_lit(self) -> bool:
        """Lookahead guard for struct literals: the current token must be
        `{` followed (past newlines) by `}` or `ident :`. Distinguishes
        `Point { x: 1.0 }` from `match s {`, `for p in items {`, etc."""
        toks = self.toks
        i = self.i + 1
        while i < len(toks) and toks[i].kind == "SEMI":
            i += 1
        if i >= len(toks):
            return False
        if toks[i].kind == "}":
            return True
        return (i + 1 < len(toks) and toks[i].kind == "IDENT"
                and toks[i + 1].kind == ":")

    def parse_struct_lit(self, name: str, t) -> ast.StructLit:
        """`Point { x: 1.0, y: 2.0 }` — C-layout struct construction."""
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name").value
            self.expect(":", "':'")
            fv = self.parse_expr()
            fields.append((fk, fv))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.StructLit(name=name, fields=fields,
                             line=t.line, col=t.col)

    def parse_extern_struct(self, union=False) -> ast.StructDecl:
        """`struct Point { x: Float, y: Float }` inside an extern block.

        Overtake final wave: `union Value { i: Int, f: Float }` when
        union=True, and bitfield widths (`a: Int:3`) on fields.
        """
        kw = "union" if union else "struct"
        t = self.expect_ident("'%s'" % kw)
        name = self.expect_ident("aggregate name").value
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fn_tok = self.expect_ident("field name")
            self.expect(":", "':'")
            te = self.parse_type_expr()
            # Overtake final wave: optional bitfield width `a: Int:3`.
            # After a type expression, `:` here can only start a width
            # (the next field begins with an identifier).
            if self.at(":"):
                self.advance()
                wtok = self.expect("INT", "bitfield width")
                try:
                    te.bitfield_width = int(wtok.value)
                except ValueError:
                    raise ParseError("bitfield width must be an integer",
                                     wtok.line, wtok.col)
            fields.append((fn_tok.value, te))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.StructDecl(name=name, fields=fields,
                              kind=("union" if union else "struct"),
                              line=t.line, col=t.col)

    def parse_extern_fn(self) -> ast.FnDecl:
        ft = self.expect("fn", "'fn'")
        name = self.expect_ident("function name").value
        # Phase 7: accept type params syntactically so the checker
        # can reject them with E225 (v1: no generics across FFI).
        type_params = []
        if self.at("<"):
            for pn, bound in self._parse_type_params_decl():
                if bound is not None:
                    raise ParseError(
                        "interface bounds are not allowed on extern "
                        "functions", self.peek().line, self.peek().col)
                type_params.append(pn)
        # Overtake Wave 2: extern signatures allow trailing `...`
        # for C varargs.
        params, varargs = self.parse_params(allow_varargs=True)
        ret = None
        if self.at("->"):
            self.advance()
            ret = self.parse_type_expr()
        # No body: extern fns are signatures only. A `{` here is a
        # mistake (user wrote a body); reject it plainly.
        if self.at("{"):
            raise ParseError(
                "extern function '%s' must not have a body" % name,
                self.peek().line, self.peek().col)
        return ast.FnDecl(name=name, params=params, ret=ret,
                          body=[], type_params=type_params,
                          varargs=varargs, line=ft.line, col=ft.col)

    def _parse_type_params_decl(self) -> List[Tuple[str, Optional[str]]]:
        """Phase 5c: parse `<T, K>` after a fn/type name. Phase 8
        (overtake): bounds `<T: Drawable>` on functions. Returns
        (name, bound-or-None) pairs."""
        self.expect("<", "'<'")
        params = []
        seen = set()
        if not self.at(">"):
            while True:
                pn = self.expect_ident("type parameter").value
                if pn in seen:
                    raise ParseError(f"duplicate type parameter '{pn}'",
                                     self.peek().line, self.peek().col)
                seen.add(pn)
                bound = None
                if self.at(":"):
                    self.advance()
                    bound = self.expect_ident(
                        "interface bound").value
                params.append((pn, bound))
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect_gt()
        return params

    def _parse_where_clause(self, type_params: List[str],
                            owner: str) -> dict:
        """Phase 5d: parse `where T: Bound1 + Bound2, K: Bound3`.

        `where` is contextual (an IDENT here, not a keyword) so existing
        code using `where` as an identifier keeps working. Returns
        {type_param: [interface, ...]}. Raises ParseError for unknown
        type parameters or duplicate entries; unknown *interfaces* are
        the checker's E229 (it knows the declared interfaces)."""
        self.advance()  # consume 'where'
        bounds = {}
        while True:
            t = self.peek()
            pn = self.expect_ident("type parameter name").value
            if pn not in type_params:
                raise ParseError(
                    f"where clause: '{pn}' is not a type parameter "
                    f"of {owner}", t.line, t.col)
            if pn in bounds:
                raise ParseError(
                    f"where clause: duplicate bounds for '{pn}' "
                    f"in {owner}", t.line, t.col)
            self.expect(":", "':'")
            blist = [self.expect_ident("interface bound").value]
            while self.at("+"):
                self.advance()
                b = self.expect_ident("interface bound").value
                if b in blist:
                    raise ParseError(
                        f"where clause: duplicate bound '{b}' for "
                        f"'{pn}' in {owner}", t.line, t.col)
                blist.append(b)
            # The inline `<T: Bound>` (if any) stays in
            # `type_param_bounds`; the checker merges both forms.
            # (Kept separate so diagnostics can name the right
            # syntax: E200 for inline, E228/E229 for `where`.)
            bounds[pn] = blist
            if self.at(","):
                self.advance()
                continue
            break
        return bounds

    def _parse_type_args(self) -> List[ast.TypeExpr]:
        """Phase 5c: parse `<E1, E2>` type arguments. Returns TypeExprs."""
        self.expect("<", "'<'")
        args = []
        if not self.at(">"):
            while True:
                args.append(self.parse_type_expr())
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect_gt()
        return args

    def parse_import_decl(self) -> ast.ImportDecl:
        t = self.expect("import", "'import'")
        path = self.expect("STRING", "import path").value
        self.maybe_semi()
        return ast.ImportDecl(path=path, line=t.line, col=t.col)

    def parse_try(self) -> ast.TryStmt:
        t = self.expect("try", "'try'")
        body = self.parse_block()
        self.expect("catch", "'catch'")
        var = self.expect_ident("catch variable").value
        handler = self.parse_block()
        return ast.TryStmt(body=body, catch_var=var, handler=handler,
                          line=t.line, col=t.col)

    def parse_throw(self) -> ast.ThrowStmt:
        t = self.expect("throw", "'throw'")
        val = self.parse_expr()
        self.maybe_semi()
        return ast.ThrowStmt(value=val, line=t.line, col=t.col)

    def parse_on(self) -> ast.OnStmt:
        t = self.expect("on", "'on'")
        st = self.expect_ident("stream name")
        stream = ast.Ident(name=st.value, line=st.line, col=st.col)
        self.expect(".", "'.'")
        event = self.expect_ident("event name").value
        self.expect("{", "'{'")
        handler = self.parse_lambda()
        self.expect("}", "'}'")
        self.maybe_semi()
        return ast.OnStmt(stream=stream, event=event, handler=handler,
                          line=t.line, col=t.col)

    def parse_lambda(self) -> ast.Lambda:
        t = self.expect("|", "'|'")
        params = []
        while not self.at("|"):
            pn = self.expect_ident("parameter name").value
            pte = None
            # Phase 5a: typed params `|x: Int|`
            if self.at(":"):
                self.advance()
                pte = self.parse_type_expr()
            params.append((pn, pte))
            if self.at(","):
                self.advance()
        self.expect("|", "'|'")
        if self.at("{"):
            body = self.parse_block()
            return ast.Lambda(params=params, body=body,
                              line=t.line, col=t.col)
        e = self.parse_expr()
        return ast.Lambda(params=params, expr_body=e,
                          line=t.line, col=t.col)

    # -- expressions ----------------------------------------------------

    def parse_expr(self) -> ast.Node:
        return self.parse_or()

    def parse_or(self) -> ast.Node:
        left = self.parse_and()
        while self.at("or"):
            t = self.advance()
            right = self.parse_and()
            left = ast.BinOp(op="or", left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_and(self) -> ast.Node:
        left = self.parse_eq()
        while self.at("and"):
            t = self.advance()
            right = self.parse_eq()
            left = ast.BinOp(op="and", left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_eq(self) -> ast.Node:
        left = self.parse_cmp()
        while self.at("==") or self.at("!="):
            t = self.advance()
            right = self.parse_cmp()
            left = ast.BinOp(op=t.kind, left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_cmp(self) -> ast.Node:
        left = self.parse_bitor()
        while self.at("<") or self.at("<=") or self.at(">") or self.at(">="):
            t = self.advance()
            right = self.parse_bitor()
            left = ast.BinOp(op=t.kind, left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_bitor(self) -> ast.Node:
        left = self.parse_bitxor()
        while self.at("|"):
            t = self.advance()
            right = self.parse_bitxor()
            left = ast.BinOp(op="|", left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_bitxor(self) -> ast.Node:
        left = self.parse_bitand()
        while self.at("^"):
            t = self.advance()
            right = self.parse_bitand()
            left = ast.BinOp(op="^", left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_bitand(self) -> ast.Node:
        left = self.parse_shift()
        while self.at("&"):
            t = self.advance()
            right = self.parse_shift()
            left = ast.BinOp(op="&", left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_shift(self) -> ast.Node:
        left = self.parse_add()
        while self.at("<<") or self.at(">>"):
            t = self.advance()
            right = self.parse_add()
            left = ast.BinOp(op=t.kind, left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_add(self) -> ast.Node:
        left = self.parse_mul()
        while self.at("+") or self.at("-"):
            t = self.advance()
            right = self.parse_mul()
            left = ast.BinOp(op=t.kind, left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_mul(self) -> ast.Node:
        left = self.parse_unary()
        while self.at("*") or self.at("/") or self.at("%"):
            t = self.advance()
            right = self.parse_unary()
            left = ast.BinOp(op=t.kind, left=left, right=right,
                             line=t.line, col=t.col)
        return left

    def parse_unary(self) -> ast.Node:
        if self.at("-") or self.at("not"):
            t = self.advance()
            operand = self.parse_unary()
            return ast.UnOp(op=t.kind, operand=operand,
                            line=t.line, col=t.col)
        if self.at("await"):
            t = self.advance()
            operand = self.parse_unary()
            # Phase 7 (concurrency): `await t timeout <ms>` — `timeout`
            # is a contextual keyword, consumed only when directly
            # adjacent (a newline inserts SEMI first, so `await t`
            # followed by a `timeout(...)` call on the next line is
            # unaffected).
            timeout = None
            pk = self.peek()
            if pk.kind == "IDENT" and pk.value == "timeout":
                self.advance()
                timeout = self.parse_unary()
            return ast.Await(operand=operand, timeout=timeout,
                             line=t.line, col=t.col)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Node:
        node = self.parse_primary()
        while True:
            if self.at("."):
                t = self.advance()
                f = self.expect_ident("field name").value
                if self.at("(") and isinstance(node, ast.Ident):
                    # fs.read("x") -> Call(name="fs.read"); only one level
                    # Phase 5c: `Result<Int>.Ok(42)` carries type_args.
                    ta = getattr(node, "type_args", None) or None
                    node = self.finish_call(f"{node.name}.{f}", t,
                                           type_args=ta)
                else:
                    node = ast.Field(obj=node, field=f,
                                     line=t.line, col=t.col)
            elif self.at("["):
                t = self.advance()
                idx = self.parse_expr()
                self.expect("]", "']'")
                node = ast.Index(obj=node, idx=idx, line=t.line, col=t.col)
            elif self.at("("):
                # Phase 5a: calling the result of an expression,
                # e.g. `mk(9)(1)`. (Plain `f(x)` is built in
                # parse_primary; this only fires on non-Ident nodes.)
                t = self.peek()
                args = self.parse_call_args()
                node = ast.Call(name="", callee=node, args=args,
                                line=t.line, col=t.col)
            else:
                break
        return node

    def parse_spawn(self) -> ast.Spawn:
        """Phase 7 (concurrency): `spawn { stmts }` — a concurrent task
        expression evaluating to Task<T>."""
        t = self.expect("spawn", "'spawn'")
        body = self.parse_block()
        return ast.Spawn(body=body, line=t.line, col=t.col)

    def parse_match(self) -> ast.Match:
        """Phase 5b: `match scrut { Type.Variant(binds) => expr, _ => expr }`."""
        t = self.expect("match", "'match'")
        scrut = self.parse_expr()
        self.expect("{", "'{'")
        arms = []
        self.skip_semis()
        while not self.at("}"):
            arms.append(self.parse_match_arm())
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.Match(scrutinee=scrut, arms=arms,
                         line=t.line, col=t.col)

    def parse_match_arm(self) -> ast.MatchArm:
        t = self.peek()
        wildcard = False
        type_name, variant, binds = "", "", []
        if self.at("IDENT") and self.peek().value == "_":
            self.advance()
            wildcard = True
        else:
            type_name = self.expect_ident("type name").value
            self.expect(".", "'.'")
            variant = self.expect_ident("variant name").value
            if self.at("("):
                self.advance()
                if not self.at(")"):
                    while True:
                        binds.append(
                            self.expect_ident("binding").value)
                        if self.at(","):
                            self.advance()
                            continue
                        break
                self.expect(")", "')'")
        # Phase 5d: optional `if` guard, e.g.
        # `Shape.Circle(r) if r > 0 => ...` or `_ if ok => ...`.
        guard = None
        if self.at("if"):
            self.advance()
            guard = self.parse_expr()
        self.expect("=>", "'=>'")
        body = self.parse_expr()
        return ast.MatchArm(type_name=type_name, variant=variant,
                            binds=binds, body=body, wildcard=wildcard,
                            guard=guard,
                            line=t.line, col=t.col)

    def parse_call_args(self) -> list:
        """Parse `(a, b, ...)`; the opening paren must be next."""
        self.expect("(", "'('")
        args = []
        if not self.at(")"):
            while True:
                args.append(self.parse_expr())
                if self.at(","):
                    self.advance()
                    if self.at(")"):
                        break
                    continue
                break
        self.expect(")", "')'")
        return args

    def parse_primary(self) -> ast.Node:
        t = self.peek()
        # Phase 5b: `match` expression over a sum-typed scrutinee.
        if t.kind == "match":
            return self.parse_match()
        # Phase 7 (concurrency): `spawn { ... }` task expression.
        if t.kind == "spawn":
            return self.parse_spawn()
        if t.kind == "INT":
            self.advance()
            return ast.IntLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "FLOAT":
            self.advance()
            return ast.FloatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "STRING":
            self.advance()
            parts = self.parse_string_parts(t.value, t.line, t.col)
            return ast.StrLit(parts=parts, line=t.line, col=t.col)
        if t.kind == "RAWSTRING":
            # W-6: raw strings are literal — no interpolation parsing.
            self.advance()
            return ast.StrLit(parts=[t.value], raw=True,
                              line=t.line, col=t.col)
        if t.kind == "DURATION":
            self.advance()
            return ast.DurationLit(ms=t.value, line=t.line, col=t.col)
        if t.kind == "MONEY":
            self.advance()
            return ast.MoneyLit(usd=t.value, line=t.line, col=t.col)
        if t.kind in ("true", "false"):
            self.advance()
            return ast.BoolLit(value=(t.kind == "true"),
                               line=t.line, col=t.col)
        if t.kind == "nil":
            self.advance()
            return ast.NilLit(line=t.line, col=t.col)
        if t.kind == "IDENT":
            self.advance()
            # Phase 5c: explicit type arguments — `first<Text>(["x"])`,
            # `Box<Int>(42)`, `Result<Int>.Ok(42)`. Speculative: only a
            # `<...>` followed by `(` or `.` is generic; otherwise it's
            # a comparison (`a < b`) and we backtrack.
            type_args = None
            if self.at("<"):
                saved = self.i
                try:
                    type_args = self._parse_type_args()
                    if not (self.at("(") or self.at(".")):
                        raise ParseError("not a generic application",
                                         t.line, t.col)
                except ParseError:
                    self.i = saved
                    type_args = None
            if self.at("("):
                return self.finish_call(t.value, t, type_args=type_args)
            if type_args is not None:
                # `Result<Int>.Ok(...)` — stash for parse_postfix.
                node = ast.Ident(name=t.value, line=t.line, col=t.col)
                node.type_args = type_args
                return node
            # Overtake Wave 2 (FFI): `Point { x: 1.0, y: 2.0 }`
            # constructs a C-layout struct value. Guarded by lookahead:
            # `IDENT {` also opens `match`/`if`/`while`/`for` bodies
            # (`match s {`, `for p in items {`), so the struct-literal
            # path is only taken when the brace opens a field list
            # (`{ ident :` or `{ }`).
            if self.at("{") and self._looks_like_struct_lit():
                return self.parse_struct_lit(t.value, t)
            return ast.Ident(name=t.value, line=t.line, col=t.col)
        if t.kind in ("navigate", "env"):
            self.advance()
            if self.at("("):
                return self.finish_call(t.value, t)
            raise ParseError(f"'{t.value}' must be called", t.line, t.col)
        if t.kind == "(":
            self.advance()
            e = self.parse_expr()
            self.expect(")", "')'")
            return e
        if t.kind == "[":
            self.advance()
            self.skip_semis()
            items = []
            if not self.at("]"):
                while True:
                    items.append(self.parse_expr())
                    self.skip_semis()
                    if self.at(","):
                        self.advance()
                        self.skip_semis()
                        if self.at("]"):
                            break
                        continue
                    break
            self.expect("]", "']'")
            return ast.ListLit(items=items, line=t.line, col=t.col)
        if t.kind == "{":
            self.advance()
            self.skip_semis()
            # map vs record: STRING key -> map, IDENT key -> record,
            # empty {} -> empty record (annotate for an empty map)
            if self.at("STRING"):
                entries = self.parse_map_fields_rest()
                return ast.MapLit(entries=entries, line=t.line, col=t.col)
            fields = self.parse_record_fields_rest()
            return ast.RecordLit(fields=fields, line=t.line, col=t.col)
        if t.kind == "|":
            return self.parse_lambda()
        if t.kind == "stream":
            self.advance()
            name = self.expect_ident("stream name").value
            self.expect("=", "'='")
            call = self.parse_expr()
            return ast.StreamDecl(name=name, call=call,
                                  line=t.line, col=t.col)
        raise ParseError(f"unexpected {t.kind} ({t.value!r}) in expression",
                         t.line, t.col)

    def finish_call(self, name: str, t: Token,
                  type_args: Optional[list] = None) -> ast.Call:
        args = self.parse_call_args()
        return ast.Call(name=name, args=args,
                        type_args=type_args or [],
                        line=t.line, col=t.col)

    def parse_record_fields_rest(self) -> list:
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name").value
            self.expect(":", "':'")
            fields.append((fk, self.parse_expr()))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return fields

    def parse_map_fields_rest(self) -> list:
        entries = []
        self.skip_semis()
        while not self.at("}"):
            kt = self.expect("STRING", "string key")
            self.expect(":", "':'")
            entries.append((kt.value, self.parse_expr()))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return entries

    def parse_literal(self) -> ast.Node:
        t = self.peek()
        if t.kind in ("INT", "FLOAT", "STRING", "DURATION", "MONEY",
                      "true", "false", "nil"):
            return self.parse_primary()
        raise ParseError(f"expected a literal, found {t.kind}",
                         t.line, t.col)

    # -- string interpolation -------------------------------------------

    def parse_string_parts(self, raw: str, line: int, col: int) -> list:
        parts: list = []
        buf = ""
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "{" and i + 1 < len(raw) and raw[i + 1] == "{":
                buf += "{"
                i += 2
                continue
            if ch == "{":
                depth = 1
                j = i + 1
                while j < len(raw) and depth:
                    if raw[j] == "{":
                        depth += 1
                    elif raw[j] == "}":
                        depth -= 1
                    j += 1
                if depth:
                    # M-5: the common cause is a '"' inside '{...}'
                    # (e.g. "a{"b"}c"): the '"' ends the string at lex
                    # time, so interpolation can never nest
                    # double-quoted strings. Name the real problem.
                    raise ParseError(
                        "unbalanced '{' in string: '{' opens interpolation "
                        "and needs a matching '}' (a '\"' inside '{...}' "
                        "ends the string — interpolation cannot nest "
                        "double-quoted strings)", line, col)
                inner = raw[i + 1:j - 1]
                if buf:
                    parts.append(buf)
                    buf = ""
                expr_line = line + raw[:i].count("\n")
                parts.append(self._parse_inner(inner, expr_line, col))
                i = j
                continue
            buf += ch
            i += 1
        if buf:
            parts.append(buf)
        return parts

    def _parse_inner(self, src: str, line: int, col: int) -> ast.Node:
        toks = Lexer(src, self.filename).tokens()
        p = Parser(toks, self.filename)
        e = p.parse_expr()
        if not p.at("EOF"):
            raise ParseError("unexpected token in interpolation",
                             line, col)
        _shift_lines(e, line - 1)
        return e


def _shift_lines(node: ast.Node, d: int):
    """Shift source lines of a subtree (for interpolated expressions,
    which are parsed as if starting at line 1)."""
    if d == 0 or not isinstance(node, ast.Node):
        return
    node.line += d
    for fname in node.__dataclass_fields__:
        v = getattr(node, fname, None)
        if isinstance(v, ast.Node):
            _shift_lines(v, d)
        elif isinstance(v, list):
            for x in v:
                _shift_lines(x, d)
        elif isinstance(v, tuple):
            for x in v:
                _shift_lines(x, d)


def parse_source(src: str, filename: str = "<input>") -> ast.Program:
    toks = Lexer(src, filename).tokens()
    return Parser(toks, filename).parse_program()
