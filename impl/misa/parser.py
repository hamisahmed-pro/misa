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
        if k == "model":
            return self.parse_model_decl()
        if k == "screen":
            return self.parse_screen()
        if k == "eval":
            return self.parse_eval()
        if k == "fn":
            return self.parse_fn_decl()
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
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name")
            if fk.value != "key":
                raise ParseError(f"unknown provider field '{fk.value}'",
                                 fk.line, fk.col)
            self.expect(":", "':'")
            self.expect("env", "'env'")
            self.expect("(", "'('")
            var = self.expect("STRING", "string").value
            self.expect(")", "')'")
            key_env = var
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.ProviderDecl(name=name, key_env=key_env,
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

    def parse_type_decl(self) -> ast.TypeDecl:
        t = self.expect("type", "'type'")
        name = self.expect_ident("type name").value
        self.expect("{", "'{'")
        fields = []
        self.skip_semis()
        while not self.at("}"):
            fk = self.expect_ident("field name").value
            self.expect(":", "':'")
            te = self.parse_type_expr()
            fields.append((fk, te))
            if self.at(","):
                self.advance()
            self.skip_semis()
        self.expect("}", "'}'")
        return ast.TypeDecl(name=name, fields=fields, line=t.line, col=t.col)

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
        if self.at("["):
            self.advance()
            inner = self.parse_type_expr()
            self.expect("]", "']'")
            opt = False
            if self.at("?"):
                self.advance()
                opt = True
            return ast.TypeExpr(name=inner.name,
                                is_list=True, optional=opt,
                                line=t.line, col=t.col)
        # Map<K, V> — the only generic type in v0.2
        if self.at("IDENT") and self.peek().value == "Map" \
                and self.peek(1).kind == "<":
            self.advance()
            self.advance()  # <
            key = self.parse_type_expr()
            self.expect(",", "','")
            val = self.parse_type_expr()
            self.expect_gt()
            opt = False
            if self.at("?"):
                self.advance()
                opt = True
            return ast.TypeExpr(name="Map", is_map=True,
                                map_key=key, map_val=val, optional=opt,
                                line=t.line, col=t.col)
        name = self.expect_ident("type name").value
        opt = False
        if self.at("?"):
            self.advance()
            opt = True
        return ast.TypeExpr(name=name, optional=opt, line=t.line, col=t.col)

    def parse_params(self) -> list:
        self.expect("(", "'('")
        params = []
        if not self.at(")"):
            while True:
                pn = self.expect_ident("parameter name").value
                self.expect(":", "':'")
                te = self.parse_type_expr()
                params.append((pn, te))
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect(")", "')'")
        return params

    def parse_model_decl(self) -> ast.ModelDecl:
        t = self.expect("model", "'model'")
        name = self.expect_ident("model name").value
        params = self.parse_params()
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
        params = self.parse_params() if self.at("(") else []
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
        params = self.parse_params()
        ret = None
        if self.at("->"):
            self.advance()
            ret = self.parse_type_expr()
        body = self.parse_block()
        return ast.FnDecl(name=name, params=params, ret=ret, body=body,
                          line=t.line, col=t.col)

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
            params.append(self.expect_ident("parameter name").value)
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
        left = self.parse_add()
        while self.at("<") or self.at("<=") or self.at(">") or self.at(">="):
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
            return ast.Await(operand=operand, line=t.line, col=t.col)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Node:
        node = self.parse_primary()
        while True:
            if self.at("."):
                t = self.advance()
                f = self.expect_ident("field name").value
                if self.at("(") and isinstance(node, ast.Ident):
                    # fs.read("x") -> Call(name="fs.read"); only one level
                    node = self.finish_call(f"{node.name}.{f}", t)
                else:
                    node = ast.Field(obj=node, field=f,
                                     line=t.line, col=t.col)
            elif self.at("["):
                t = self.advance()
                idx = self.parse_expr()
                self.expect("]", "']'")
                node = ast.Index(obj=node, idx=idx, line=t.line, col=t.col)
            else:
                break
        return node

    def parse_primary(self) -> ast.Node:
        t = self.peek()
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
            if self.at("("):
                return self.finish_call(t.value, t)
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

    def finish_call(self, name: str, t: Token) -> ast.Call:
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
        return ast.Call(name=name, args=args, line=t.line, col=t.col)

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
                    raise ParseError("unbalanced '{' in string", line, col)
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
