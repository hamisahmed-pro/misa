"""Misa v0.2 canonical formatter. `format_source` is idempotent:
formatting twice yields identical output (tested).
"""
from __future__ import annotations
from . import ast


PREC = {"or": 1, "and": 2, "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4,
        ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}


class Formatter:
    def __init__(self):
        self.out: list = []
        self.ind = 0

    # -- primitives -----------------------------------------------------

    def w(self, s: str):
        self.out.append("    " * self.ind + s)

    def emit_leading(self, node: ast.Node):
        for c in node.leading_comments:
            self.w(c)

    def text(self) -> str:
        return "\n".join(self.out) + "\n"

    # -- top level ------------------------------------------------------

    def format_program(self, prog: ast.Program) -> str:
        first = True
        for item in prog.items:
            if not first:
                self.out.append("")
            first = False
            self.format_item(item)
        return self.text()

    _DECLS = (ast.AppDecl, ast.ProviderDecl, ast.ModelsDecl, ast.TypeDecl,
              ast.TableDecl,
              ast.ModelDecl, ast.ScreenDecl, ast.EvalDecl, ast.FnDecl,
              ast.ExternDecl, ast.ImportDecl, ast.TestDecl,
              ast.InterfaceDecl, ast.ImplDecl)

    def fmt_iface_sig(self, m: ast.FnDecl) -> str:
        """Format an interface/impl method signature."""
        ps = []
        for n, te in m.params:
            if te is None:
                ps.append(n)  # bare `self`
            else:
                ps.append(f"{n}: {self.fmt_type(te)}")
        head = f"fn {m.name}({', '.join(ps)})"
        if m.ret is not None:
            head += f" -> {self.fmt_type(m.ret)}"
        if m.throws:
            head += f" throws {', '.join(m.throws)}"
        return head

    def format_item(self, item: ast.Node):
        # statements emit their own leading comments via format_stmt
        if not isinstance(item, Formatter._DECLS):
            self.format_stmt(item)
            return
        self.emit_leading(item)
        if isinstance(item, ast.AppDecl):
            self.w(f"app {item.name} {{")
            self.ind += 1
            for k, v in item.fields:
                self.w(f"{k}: {self.fmt_expr(v)},")
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.ProviderDecl):
            self.w(f"provider {item.name} {{")
            self.ind += 1
            if item.key_env:
                self.w(f'key: env("{item.key_env}"),')
            if item.model_file:
                self.w(f'model: file("{item.model_file}"),')
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.ModelsDecl):
            self.w("models {")
            self.ind += 1
            for alias, prov, mid in item.entries:
                self.w(f'{alias}: {prov}("{mid}"),')
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.TypeDecl):
            # Phase 5c: type parameters.
            tp = f"<{', '.join(item.type_params)}>" if item.type_params \
                else ""
            self.w(f"type {item.name}{tp} {{")
            self.ind += 1
            if item.is_sum:
                # Phase 5b: sum variants.
                for v in item.variants:
                    if v.fields:
                        fs = ", ".join(
                            f"{fn}: {self.fmt_type(fte)}"
                            for fn, fte in v.fields)
                        self.w(f"{v.name}({fs}),")
                    else:
                        self.w(f"{v.name},")
            else:
                for fn, fte in item.fields:
                    self.w(f"{fn}: {self.fmt_type(fte)},")
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.TableDecl):
            # Phase 5 (DB): table declarations.
            self.w(f"table {item.name} {{")
            self.ind += 1
            for fn, fte, primary in item.fields:
                attr = " @primary" if primary else ""
                self.w(f"{fn}: {self.fmt_type(fte)}{attr},")
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.ModelDecl):
            params = ", ".join(f"{n}: {self.fmt_type(t)}"
                               for n, t in item.params)
            self.w(f"model {item.name}({params}) -> "
                   f"{self.fmt_type(item.ret)}")
            self.ind += 1
            self.w(f"with {item.provider}.{item.alias}")
            if item.budget:
                b = item.budget
                parts = []
                if b.tokens is not None:
                    parts.append(f"tokens: {b.tokens}")
                if b.latency_ms is not None:
                    parts.append(f"latency: {self.fmt_duration(b)}")
                if b.cost_usd is not None:
                    parts.append(f"cost: ${b.cost_usd:g}")
                self.w("budget { " + ", ".join(parts) + " }")
            prompt = self.fmt_prompt(item.prompt)
            self.w(f"prompt v{item.prompt_version} {prompt}")
            self.ind -= 1
        elif isinstance(item, ast.ScreenDecl):
            params = ", ".join(f"{n}: {self.fmt_type(t)}"
                               for n, t in item.params)
            head = f"screen {item.name}"
            if params:
                head += f"({params})"
            self.w(head + " {")
            self.ind += 1
            for child in item.body:
                self.format_screen_item(child)
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.EvalDecl):
            self.w(f"eval {item.name} {{")
            self.ind += 1
            self.w(f"model: {item.model_name},")
            given = ", ".join(f"{k}: {self.fmt_expr(v)}"
                              for k, v in item.given)
            self.w("given: { " + given + " },")
            self.w(f"expect: {self.fmt_expr(item.expect)},")
            self.w(f"runs: {item.runs},")
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.TestDecl):
            self.w(f"test {item.name} {{")
            self.ind += 1
            for stmt in item.body:
                self.format_stmt(stmt)
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.InterfaceDecl):
            self.w(f"interface {item.name} {{")
            self.ind += 1
            for m in item.methods:
                self.w(self.fmt_iface_sig(m))
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.ImplDecl):
            self.w(f"impl {item.iface} for {item.target} {{")
            self.ind += 1
            for m in item.methods:
                self.w(self.fmt_iface_sig(m) + " {")
                self.ind += 1
                for st in m.body:
                    self.format_stmt(st)
                self.ind -= 1
                self.w("}")
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.FnDecl):
            params = ", ".join(f"{n}: {self.fmt_type(t)}"
                               for n, t in item.params)
            # Phase 5c: type parameters. Phase 8: interface bounds.
            if item.type_params:
                tps = []
                for tp in item.type_params:
                    b = item.type_param_bounds.get(tp)
                    tps.append(f"{tp}: {b}" if b else tp)
                tp = f"<{', '.join(tps)}>"
            else:
                tp = ""
            head = f"fn {item.name}{tp}({params})"
            if item.ret is not None:
                head += f" -> {self.fmt_type(item.ret)}"
            # Phase 8 (overtake): checked `throws` signatures.
            if item.throws:
                head += f" throws {', '.join(item.throws)}"
            self.w(head + " {")
            self.ind += 1
            for st in item.body:
                self.format_stmt(st)
            self.ind -= 1
            self.w("}")
        elif isinstance(item, ast.ImportDecl):
            self.w(f'import "{item.path}"')
        elif isinstance(item, ast.ExternDecl):
            # Phase 7: `extern "<lib>" { fn f(...) -> T, ... }`.
            # Overtake Wave 2: struct declarations and `...` varargs.
            self.w(f'extern "{item.lib}" {{')
            self.ind += 1
            for st in item.structs:
                fields = ", ".join(
                    f"{fn_}: {self.fmt_type(fte)}"
                    for fn_, fte in st.fields)
                self.w(f"struct {st.name} {{ {fields} }},")
            for fn in item.fns:
                params = ", ".join(
                    f"{pn}: {self.fmt_type(te)}" for pn, te in fn.params)
                if fn.varargs:
                    params = (params + ", ...") if params else "..."
                ret = f" -> {self.fmt_type(fn.ret)}" if fn.ret else ""
                self.w(f"fn {fn.name}({params}){ret},")
            self.ind -= 1
            self.w("}")

    def fmt_duration(self, b: ast.Budget) -> str:
        ms = b.latency_ms
        if ms % 1000 == 0:
            return f"{ms // 1000}s"
        return f"{ms}ms"

    def fmt_prompt(self, parts: list) -> str:
        # re-emit as triple-quoted string
        body = ""
        for p in parts:
            if isinstance(p, str):
                body += p.replace("{", "{{")
            else:
                body += "{" + self.fmt_expr(p) + "}"
        return '"""\n' + body + '"""'

    def fmt_type(self, te: ast.TypeExpr) -> str:
        # Phase 5a: function types
        if te.is_func:
            ps = ", ".join(self.fmt_type(p) for p in te.func_params)
            s = f"({ps}) -> {self.fmt_type(te.func_ret)}"
            if te.optional:
                s += "?"
            return s
        s = te.name
        if te.is_list:
            # Phase 5c: full inner structure when preserved.
            if te.list_elem is not None:
                s = f"[{self.fmt_type(te.list_elem)}]"
            else:
                s = f"[{s}]"
        elif te.is_map:
            s = (f"Map<{self.fmt_type(te.map_key)}, "
                 f"{self.fmt_type(te.map_val)}>")
        elif te.type_args:
            # Phase 5c: applied generic types.
            args = ", ".join(self.fmt_type(a) for a in te.type_args)
            s = f"{s}<{args}>"
        if te.optional:
            s += "?"
        return s

    # -- components -----------------------------------------------------

    def format_screen_item(self, node: ast.Node):
        self.emit_leading(node)
        if isinstance(node, ast.Component):
            self.format_component(node)
        elif isinstance(node, ast.ScreenIf):
            self.w(f"if {self.fmt_expr(node.cond)} {{")
            self.ind += 1
            for x in node.then:
                self.format_screen_item(x)
            self.ind -= 1
            if node.els:
                self.w("} else {")
                self.ind += 1
                for x in node.els:
                    self.format_screen_item(x)
                self.ind -= 1
                self.w("}")
            else:
                self.w("}")
        elif isinstance(node, ast.ScreenFor):
            self.w(f"for {node.var} in {self.fmt_expr(node.iter)} {{")
            self.ind += 1
            for x in node.body:
                self.format_screen_item(x)
            self.ind -= 1
            self.w("}")
        elif isinstance(node, ast.LetStmt):
            kw = "var" if node.mutable else "let"
            ann = f": {self.fmt_type(node.ann)}" if node.ann else ""
            self.w(f"{kw} {node.name}{ann} = "
                   f"{self.fmt_expr(node.value)}")
        else:
            raise ValueError(f"cannot format {type(node).__name__}")

    def format_component(self, c: ast.Component):
        args = []
        has_block_lambda = False
        for an, val in c.args:
            if an == "id" and isinstance(val, ast.Ident):
                args.append(("simple", f"id: {val.name}"))
            elif isinstance(val, ast.Lambda) and val.body:
                has_block_lambda = True
                args.append(("lambda", an, val))
            else:
                args.append(("simple", f"{an}: {self.fmt_expr(val)}"))
        if not has_block_lambda:
            simple = [s for kind, s in args]
            head = c.name + (f"({', '.join(simple)})" if simple else "")
            self._component_head_tail(c, head)
            return
        # multi-line layout when any arg is a block lambda
        self.w(f"{c.name}(")
        self.ind += 1
        for i, a in enumerate(args):
            last = i == len(args) - 1
            if a[0] == "simple":
                self.w(a[1] + ("," if not last else ""))
            else:
                _, an, lam = a
                params = ", ".join(lam.params)
                self.w(f"{an}: |{params}| {{")
                self.ind += 1
                for st in lam.body:
                    self.format_stmt(st)
                self.ind -= 1
                self.w("}" + ("," if not last else ""))
        self.ind -= 1
        self._component_close(c)

    def _component_head_tail(self, c: ast.Component, head: str):
        if c.children:
            self.w(head + " {")
            self.ind += 1
            for child in c.children:
                self.format_screen_item(child)
            self.ind -= 1
            self.w("}")
        else:
            self.w(head)

    def _component_close(self, c: ast.Component):
        if c.children:
            self.w(") {")
            self.ind += 1
            for child in c.children:
                self.format_screen_item(child)
            self.ind -= 1
            self.w("}")
        else:
            self.w(")")

    # -- statements -----------------------------------------------------

    def format_stmt(self, s: ast.Node):
        self.emit_leading(s)
        if isinstance(s, ast.LetStmt):
            kw = "var" if s.mutable else "let"
            ann = f": {self.fmt_type(s.ann)}" if s.ann else ""
            self.w(f"{kw} {s.name}{ann} = {self.fmt_expr(s.value)}")
        elif isinstance(s, ast.AssignStmt):
            self.w(f"{self.fmt_expr(s.target)} = {self.fmt_expr(s.value)}")
        elif isinstance(s, ast.IfStmt):
            self.w(f"if {self.fmt_expr(s.cond)} {{")
            self.ind += 1
            for x in s.then:
                self.format_stmt(x)
            self.ind -= 1
            if s.els:
                self.w("} else {")
                self.ind += 1
                for x in s.els:
                    self.format_stmt(x)
                self.ind -= 1
                self.w("}")
            else:
                self.w("}")
        elif isinstance(s, ast.ForStmt):
            self.w(f"for {s.var} in {self.fmt_expr(s.iter)} {{")
            self.ind += 1
            for x in s.body:
                self.format_stmt(x)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ast.WhileStmt):
            self.w(f"while {self.fmt_expr(s.cond)} {{")
            self.ind += 1
            for x in s.body:
                self.format_stmt(x)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ast.ReturnStmt):
            if s.value is not None:
                self.w(f"return {self.fmt_expr(s.value)}")
            else:
                self.w("return")
        elif isinstance(s, ast.TryStmt):
            self.w("try {")
            self.ind += 1
            for x in s.body:
                self.format_stmt(x)
            self.ind -= 1
            self.w(f"}} catch {s.catch_var} {{")
            self.ind += 1
            for x in s.handler:
                self.format_stmt(x)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ast.ThrowStmt):
            self.w(f"throw {self.fmt_expr(s.value)}")
        elif isinstance(s, ast.OnStmt):
            lam = s.handler
            params = ", ".join(lam.params)
            self.w(f"on {self.fmt_expr(s.stream)}.{s.event} "
                   f"{{ |{params}|")
            self.ind += 1
            for st in lam.body:
                self.format_stmt(st)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ast.ExprStmt):
            self.w(self.fmt_expr(s.expr))
        elif isinstance(s, ast.StreamDecl):
            self.w(f"stream {s.name} = {self.fmt_expr(s.call)}")
        else:
            raise ValueError(f"cannot format {type(s).__name__}")

    # -- expressions ----------------------------------------------------

    def fmt_expr(self, e: ast.Node, parent_prec: int = 0) -> str:
        if isinstance(e, ast.IntLit):
            return str(e.value)
        if isinstance(e, ast.FloatLit):
            return repr(e.value)
        if isinstance(e, ast.StrLit):
            return self.fmt_str(e)
        if isinstance(e, ast.BoolLit):
            return "true" if e.value else "false"
        if isinstance(e, ast.NilLit):
            return "nil"
        if isinstance(e, ast.DurationLit):
            ms = e.ms
            return f"{ms // 1000}s" if ms % 1000 == 0 else f"{ms}ms"
        if isinstance(e, ast.MoneyLit):
            return f"${e.usd:g}"
        if isinstance(e, ast.Ident):
            return e.name
        if isinstance(e, ast.Call):
            args = ", ".join(self.fmt_expr(a) for a in e.args)
            # Phase 5c: explicit type arguments. For `Result<Int>.Ok`,
            # they belong on the head.
            ta = ""
            name = e.name
            if e.type_args:
                ta = "<" + ", ".join(self.fmt_type(a)
                                     for a in e.type_args) + ">"
                if "." in name:
                    head, _, rest = name.partition(".")
                    name = f"{head}{ta}.{rest}"
                    ta = ""
            return f"{name}{ta}({args})"
        if isinstance(e, ast.Field):
            return f"{self.fmt_expr(e.obj, 9)}.{e.field}"
        if isinstance(e, ast.Index):
            return f"{self.fmt_expr(e.obj, 9)}[{self.fmt_expr(e.idx)}]"
        if isinstance(e, ast.ListLit):
            return "[" + ", ".join(self.fmt_expr(i) for i in e.items) + "]"
        if isinstance(e, ast.RecordLit):
            inner = ", ".join(f"{k}: {self.fmt_expr(v)}"
                              for k, v in e.fields)
            return "{ " + inner + " }"
        if isinstance(e, ast.StructLit):
            # Overtake Wave 2 (FFI): `Point { x: 1.0, y: 2.0 }`.
            inner = ", ".join(f"{k}: {self.fmt_expr(v)}"
                              for k, v in e.fields)
            return f"{e.name} {{ {inner} }}"
        if isinstance(e, ast.MapLit):
            inner = ", ".join(f'{self.quote(k)}: {self.fmt_expr(v)}'
                              for k, v in e.entries)
            return "{ " + inner + " }"
        if isinstance(e, ast.Lambda):
            return self.fmt_lambda(e)
        if isinstance(e, ast.BinOp):
            p = PREC[e.op]
            s = (f"{self.fmt_expr(e.left, p)} {e.op} "
                 f"{self.fmt_expr(e.right, p + 1)}")
            return f"({s})" if p < parent_prec else s
        if isinstance(e, ast.UnOp):
            op = "not " if e.op == "not" else "-"
            return f"{op}{self.fmt_expr(e.operand, 7)}"
        if isinstance(e, ast.Await):
            s = f"await {self.fmt_expr(e.operand, 7)}"
            # Phase 7 (concurrency): `await t timeout <ms>`.
            if e.timeout is not None:
                s += f" timeout {self.fmt_expr(e.timeout, 7)}"
            return s
        if isinstance(e, ast.Spawn):
            # Phase 7 (concurrency): block lambda style.
            inline = _Inline()
            stmts = "; ".join(inline.format_stmt_inline(s)
                              for s in e.body)
            return f"spawn {{ {stmts} }}"
        if isinstance(e, ast.Match):
            # Phase 5b: `match` expression.
            arms = []
            for a in e.arms:
                if a.wildcard:
                    pat = "_"
                else:
                    bs = f"({', '.join(a.binds)})" if a.binds else ""
                    pat = f"{a.type_name}.{a.variant}{bs}"
                arms.append(f"{pat} => {self.fmt_expr(a.body)}")
            return (f"match {self.fmt_expr(e.scrutinee)} {{ "
                    + ", ".join(arms) + " }")
        if isinstance(e, ast.StreamDecl):
            return f"stream {e.name} = {self.fmt_expr(e.call)}"
        raise ValueError(f"cannot format {type(e).__name__}")

    def fmt_str(self, e: ast.StrLit) -> str:
        if e.raw and len(e.parts) == 1 and isinstance(e.parts[0], str):
            # W-6: preserve rawness — a raw string formats back to r"...".
            # Content can't contain a newline (single-line) or '"' + the
            # closing delimiter; fall back to r"""...""" when needed.
            body = e.parts[0]
            if "\n" in body or '"' in body:
                return 'r"""\n' + body + '"""'
            return 'r"' + body + '"'
        if len(e.parts) == 1 and isinstance(e.parts[0], str):
            return self.quote(e.parts[0])
        # multiline or interpolated -> triple-quoted
        body = ""
        for p in e.parts:
            if isinstance(p, str):
                body += p.replace("{", "{{")
            else:
                body += "{" + self.fmt_expr(p) + "}"
        if "\n" in body or any(not isinstance(p, str) for p in e.parts):
            return '"""\n' + body + '"""'
        return self.quote(body)

    def quote(self, s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') \
                       .replace("\n", "\\n").replace("\t", "\\t") \
                       .replace("\r", "\\r") + '"'

    def fmt_lambda(self, e: ast.Lambda) -> str:
        def _p(pn_pte):
            pn, pte = pn_pte
            return f"{pn}: {self.fmt_type(pte)}" if pte else pn
        params = ", ".join(_p(pp) for pp in e.params)
        if e.expr_body is not None:
            return f"|{params}| {self.fmt_expr(e.expr_body)}"
        inline = _Inline()
        stmts = "; ".join(inline.format_stmt_inline(s) for s in e.body)
        return f"|{params}| {{ {stmts} }}"


class _Inline(Formatter):
    """Formats a single statement without indentation or comments."""
    def format_stmt_inline(self, s: ast.Node) -> str:
        self.out = []
        self.ind = 0
        self.format_stmt(s)
        return " ".join(line.strip() for line in self.out)


def format_source(src: str, filename: str = "<input>") -> str:
    from .parser import parse_source
    prog = parse_source(src, filename)
    return Formatter().format_program(prog)
