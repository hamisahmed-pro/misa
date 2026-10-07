"""Phase 5 DB2: a real SQL parser for Misa's static query analysis.

Tokenizer + recursive-descent parser producing a small AST. Covers the
SELECT/INSERT/UPDATE/DELETE shapes Misa statically checks. Anything
outside the grammar raises _ParseFail and parse() returns None — the
checker then treats the statement as dynamic (unchecked), exactly like
v1's regex fallback. Never raise anything else.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

# ---------------------------------------------------------------- tokens

KEYWORDS = {
    "select", "from", "where", "join", "inner", "left", "right", "full",
    "outer", "cross", "on", "using", "as", "and", "or", "not", "null",
    "like", "glob", "in", "is", "between", "distinct", "all", "group",
    "by", "having", "order", "limit", "offset", "asc", "desc",
    "insert", "into", "values", "update", "set", "delete",
    "case", "when", "then", "else", "end", "exists", "isnull",
    "notnull", "escape", "collate",
}


@dataclass
class Tok:
    kind: str   # KW | IDENT | STRING | NUMBER | PH | OP | PUNCT | EOF
    val: str
    pos: int


def tokenize(sql: str) -> List[Tok]:
    toks: List[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            if j < 0:
                raise _ParseFail("unterminated comment")
            i = j + 2
            continue
        if c == "'":
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1:j + 2] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            if j >= n:
                raise _ParseFail("unterminated string")
            toks.append(Tok("STRING", "".join(buf), i))
            i = j + 1
            continue
        if c in "\"`":
            q = c
            j = sql.find(q, i + 1)
            if j < 0:
                raise _ParseFail("unterminated identifier")
            toks.append(Tok("IDENT", sql[i + 1:j], i))
            i = j + 1
            continue
        if c == "[":
            j = sql.find("]", i + 1)
            if j < 0:
                raise _ParseFail("unterminated identifier")
            toks.append(Tok("IDENT", sql[i + 1:j], i))
            i = j + 1
            continue
        if c == "?":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            toks.append(Tok("PH", sql[i:j], i))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isdigit() or sql[j] in ".eE+-"):
                # keep it simple: consume a plausible numeric run, then
                # validate with float(); exponent sign handled loosely
                if sql[j] in "eE" and j + 1 < n and sql[j + 1] in "+-":
                    j += 2
                    continue
                j += 1
            word = sql[i:j]
            try:
                float(word)
            except ValueError:
                raise _ParseFail(f"bad number {word!r}")
            toks.append(Tok("NUMBER", word, i))
            i = j
            continue
        if c.isalpha() or c == "_" or ord(c) > 127:
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"
                             or ord(sql[j]) > 127):
                j += 1
            word = sql[i:j]
            toks.append(Tok("KW" if word.lower() in KEYWORDS else "IDENT",
                            word, i))
            i = j
            continue
        two = sql[i:i + 2]
        if two in ("<>", "!=", "<=", ">=", "||"):
            toks.append(Tok("OP", two, i))
            i += 2
            continue
        if c in "=<>+-*/%":
            toks.append(Tok("OP", c, i))
            i += 1
            continue
        if c in "(),.;":
            toks.append(Tok("PUNCT", c, i))
            i += 1
            continue
        raise _ParseFail(f"unexpected character {c!r}")
    toks.append(Tok("EOF", "", n))
    return toks


class _ParseFail(Exception):
    pass


# ---------------------------------------------------------------- AST

@dataclass
class ColRef:
    qual: Optional[str]
    name: str


@dataclass
class Lit:
    v: object
    t: str  # Int | Float | Text | Bool | Nil


@dataclass
class PH:
    n: int  # 1-based placeholder index


@dataclass
class BinOp:
    op: str
    l: "Expr"
    r: "Expr"


@dataclass
class UnOp:
    op: str
    e: "Expr"


@dataclass
class Call:
    name: str
    args: List["Expr"]
    star: bool = False


@dataclass
class InOp:
    e: "Expr"
    vals: List["Expr"]
    neg: bool = False


@dataclass
class BetweenOp:
    e: "Expr"
    lo: "Expr"
    hi: "Expr"
    neg: bool = False


@dataclass
class IsNullOp:
    e: "Expr"
    neg: bool = False


Expr = Union[ColRef, Lit, PH, BinOp, UnOp, Call, InOp, BetweenOp, IsNullOp]


@dataclass
class SelectItem:
    expr: Optional[Expr]      # None for star
    alias: Optional[str]
    star: Optional[str] = None  # None | "*" | qualifier for "t.*"


@dataclass
class TableRef:
    name: str
    alias: Optional[str] = None


@dataclass
class SubqueryRef:
    query: "Select"
    alias: str


@dataclass
class Join:
    kind: str  # inner|left|right|full|cross
    source: Union[TableRef, SubqueryRef]
    on: Optional[Expr] = None


@dataclass
class OrderTerm:
    e: Expr
    desc: bool = False


@dataclass
class Select:
    items: List[SelectItem]
    from_: List[Union[TableRef, SubqueryRef, Join]]
    where: Optional[Expr] = None
    distinct: bool = False
    group_by: List[Expr] = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: List[OrderTerm] = field(default_factory=list)
    limit: Optional[Expr] = None


@dataclass
class Insert:
    table: str
    cols: List[str]            # [] = all columns
    rows: List[List[Expr]]     # VALUES rows; [] when from SELECT
    select: Optional[Select] = None


@dataclass
class Update:
    table: str
    alias: Optional[str]
    sets: List[Tuple[str, Expr]]
    where: Optional[Expr] = None


@dataclass
class Delete:
    table: str
    where: Optional[Expr] = None


# ---------------------------------------------------------------- parser

class _P:
    def __init__(self, toks: List[Tok]):
        self.toks = toks
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def kw(self, *words: str) -> bool:
        t = self.peek()
        if t.kind == "KW" and t.val.lower() in words:
            self.i += 1
            return True
        return False

    def expect_kw(self, *words: str) -> Tok:
        t = self.peek()
        if t.kind == "KW" and t.val.lower() in words:
            return self.next()
        raise _ParseFail(f"expected {'/'.join(words)}, got {t.val!r}")

    def ident(self) -> str:
        t = self.peek()
        if t.kind in ("IDENT", "KW") and t.kind != "EOF":
            # allow keywords as identifiers where unambiguous (aliases)
            if t.kind == "KW" and t.val.lower() in _RESERVED_CTX:
                raise _ParseFail(f"expected identifier, got {t.val!r}")
            return self.next().val
        raise _ParseFail(f"expected identifier, got {t.val!r}")

    def expect_punct(self, p: str):
        t = self.next()
        if t.kind != "PUNCT" or t.val != p:
            raise _ParseFail(f"expected {p!r}, got {t.val!r}")


# Keywords that can never serve as a bare alias/table name.
_RESERVED_CTX = {
    "select", "from", "where", "join", "inner", "left", "right", "full",
    "outer", "cross", "on", "using", "and", "or", "not", "group", "order",
    "limit", "having", "distinct", "values", "set", "into",
}


def _parse_expr(p: _P) -> Expr:
    return _parse_or(p)


def _parse_or(p: _P) -> Expr:
    e = _parse_and(p)
    while p.kw("or"):
        e = BinOp("or", e, _parse_and(p))
    return e


def _parse_and(p: _P) -> Expr:
    e = _parse_not(p)
    while p.kw("and"):
        e = BinOp("and", e, _parse_not(p))
    return e


def _parse_not(p: _P) -> Expr:
    if p.kw("not"):
        return UnOp("not", _parse_not(p))
    return _parse_cmp(p)


def _parse_cmp(p: _P) -> Expr:
    e = _parse_add(p)
    t = p.peek()
    if t.kind == "OP" and t.val in ("=", "<>", "!=", "<", "<=", ">", ">="):
        p.next()
        return BinOp(t.val, e, _parse_add(p))
    if t.kind == "KW":
        w = t.val.lower()
        neg = False
        if w == "not":
            # `x NOT LIKE / NOT IN / NOT BETWEEN / NOT NULL-input`
            # — peek ahead; otherwise leave NOT for the unary level.
            t2 = p.toks[p.i + 1] if p.i + 1 < len(p.toks) else None
            if t2 is not None and t2.kind == "KW" and t2.val.lower() in (
                    "like", "glob", "in", "between"):
                p.next()
                neg = True
                t = p.peek()
                w = t.val.lower()
            else:
                return e
        if w == "is":
            p.next()
            not2 = p.kw("not")
            p.expect_kw("null")
            return IsNullOp(e, neg or not2)
        if w in ("like", "glob"):
            p.next()
            return BinOp(w, e, _parse_add(p)) if not neg else \
                UnOp("not", BinOp(w, e, _parse_add(p)))
        if w == "in":
            p.next()
            p.expect_punct("(")
            # subquery IN is dynamic — bail to keep checking sound
            if p.peek().kind == "KW" and p.peek().val.lower() == "select":
                raise _ParseFail("subquery IN is dynamic")
            vals = [_parse_expr(p)]
            while p.peek().kind == "PUNCT" and p.peek().val == ",":
                p.next()
                vals.append(_parse_expr(p))
            p.expect_punct(")")
            return InOp(e, vals, neg)
        if w == "between":
            p.next()
            lo = _parse_add(p)
            p.expect_kw("and")
            hi = _parse_add(p)
            return BetweenOp(e, lo, hi, neg)
    # [NOT] prefix handled by caller simplicity: re-check NOT forms
    return e


def _parse_add(p: _P) -> Expr:
    e = _parse_mul(p)
    while True:
        t = p.peek()
        if t.kind == "OP" and t.val in ("+", "-", "||"):
            p.next()
            e = BinOp(t.val, e, _parse_mul(p))
        else:
            return e


def _parse_mul(p: _P) -> Expr:
    e = _parse_unary(p)
    while True:
        t = p.peek()
        if t.kind == "OP" and t.val in ("*", "/", "%"):
            p.next()
            e = BinOp(t.val, e, _parse_unary(p))
        else:
            return e


def _parse_unary(p: _P) -> Expr:
    t = p.peek()
    if t.kind == "OP" and t.val in ("+", "-"):
        p.next()
        return UnOp(t.val, _parse_unary(p))
    return _parse_primary(p)


def _parse_primary(p: _P) -> Expr:
    t = p.peek()
    if t.kind == "PUNCT" and t.val == "(":
        p.next()
        if p.peek().kind == "KW" and p.peek().val.lower() == "select":
            raise _ParseFail("scalar subquery is dynamic")
        e = _parse_expr(p)
        p.expect_punct(")")
        return e
    if t.kind == "STRING":
        p.next()
        return Lit(t.val, "Text")
    if t.kind == "NUMBER":
        p.next()
        v = t.val
        if any(c in v for c in ".eE"):
            return Lit(float(v), "Float")
        return Lit(int(v), "Int")
    if t.kind == "PH":
        p.next()
        digits = t.val[1:]
        # bare "?" placeholders are numbered sequentially at check time;
        # "?NNN" carries its own index.
        n = int(digits) if digits else 0
        return PH(n)
    if t.kind == "KW":
        w = t.val.lower()
        if w in ("true", "false"):
            p.next()
            return Lit(w == "true", "Bool")
        if w == "null":
            p.next()
            return Lit(None, "Nil")
        if w == "case":
            raise _ParseFail("CASE is dynamic")
        if w == "exists":
            raise _ParseFail("EXISTS is dynamic")
    if t.kind in ("IDENT", "KW"):
        name = p.next().val
        if p.peek().kind == "PUNCT" and p.peek().val == "(":
            p.next()
            args: List[Expr] = []
            star = False
            if p.peek().kind == "OP" and p.peek().val == "*":
                p.next()
                star = True
            elif not (p.peek().kind == "PUNCT" and p.peek().val == ")"):
                args.append(_parse_expr(p))
                while p.peek().kind == "PUNCT" and p.peek().val == ",":
                    p.next()
                    if p.peek().kind == "KW" and \
                            p.peek().val.lower() == "distinct":
                        p.next()
                    args.append(_parse_expr(p))
            p.expect_punct(")")
            return Call(name.lower(), args, star)
        if p.peek().kind == "PUNCT" and p.peek().val == ".":
            p.next()
            col = p.ident()
            return ColRef(name, col)
        return ColRef(None, name)
    raise _ParseFail(f"unexpected {t.val!r} in expression")


def _parse_table_factor(p: _P) -> Union[TableRef, SubqueryRef]:
    t = p.peek()
    if t.kind == "PUNCT" and t.val == "(":
        p.next()
        q = _parse_select(p)
        p.expect_punct(")")
        p.kw("as")
        alias = p.ident()
        return SubqueryRef(q, alias)
    name = p.ident()
    # allow db.table — qualifier ignored for now (single database)
    if p.peek().kind == "PUNCT" and p.peek().val == ".":
        p.next()
        name = p.ident()
    alias = None
    if p.kw("as"):
        alias = p.ident()
    elif p.peek().kind in ("IDENT", "KW") and \
            p.peek().val.lower() not in _RESERVED_CTX:
        # bare alias
        alias = p.next().val
    return TableRef(name, alias)


def _parse_from(p: _P) -> List[Union[TableRef, SubqueryRef, Join]]:
    out: List[Union[TableRef, SubqueryRef, Join]] = [ _parse_table_factor(p)]
    while True:
        # comma join
        if p.peek().kind == "PUNCT" and p.peek().val == ",":
            p.next()
            out.append(_parse_table_factor(p))
            continue
        t = p.peek()
        if t.kind != "KW" or t.val.lower() not in (
                "join", "inner", "left", "right", "full", "cross"):
            break
        kind = "inner"
        if p.kw("left"):
            kind = "left"
            p.kw("outer")
        elif p.kw("right"):
            kind = "right"
            p.kw("outer")
        elif p.kw("full"):
            kind = "full"
            p.kw("outer")
        elif p.kw("inner"):
            kind = "inner"
        elif p.kw("cross"):
            kind = "cross"
        p.expect_kw("join")
        src = _parse_table_factor(p)
        on = None
        if p.kw("on"):
            on = _parse_expr(p)
        elif p.kw("using"):
            p.expect_punct("(")
            cols = [p.ident()]
            while p.peek().kind == "PUNCT" and p.peek().val == ",":
                p.next()
                cols.append(p.ident())
            p.expect_punct(")")
            # USING(a,b) == left.a=right.a AND ... — desugar later;
            # keep the column names for scope checking.
            on = None
            src_us = getattr(src, "_using", None)
            src._using = cols  # type: ignore[attr-defined]
        out.append(Join(kind, src, on))
    return out


def _parse_select_list(p: _P) -> List[SelectItem]:
    items: List[SelectItem] = []
    while True:
        t = p.peek()
        if t.kind == "OP" and t.val == "*":
            p.next()
            items.append(SelectItem(None, None, star="*"))
        elif t.kind in ("IDENT", "KW") and \
                p.toks[p.i + 1].kind == "PUNCT" and \
                p.toks[p.i + 1].val == "." and \
                p.toks[p.i + 2].kind == "OP" and \
                p.toks[p.i + 2].val == "*":
            qual = p.next().val
            p.next()  # .
            p.next()  # *
            items.append(SelectItem(None, None, star=qual))
        else:
            e = _parse_expr(p)
            alias = None
            if p.kw("as"):
                alias = p.ident()
            elif p.peek().kind == "IDENT":
                alias = p.next().val
            items.append(SelectItem(e, alias))
        if p.peek().kind == "PUNCT" and p.peek().val == ",":
            p.next()
            continue
        return items


def _parse_order_by(p: _P) -> List[OrderTerm]:
    terms = []
    while True:
        e = _parse_expr(p)
        desc = False
        if p.kw("desc"):
            desc = True
        elif p.kw("asc"):
            pass
        # ORDER BY position numbers / aliases resolve dynamically
        terms.append(OrderTerm(e, desc))
        if p.peek().kind == "PUNCT" and p.peek().val == ",":
            p.next()
            continue
        return terms


def _parse_select(p: _P) -> Select:
    p.expect_kw("select")
    distinct = bool(p.kw("distinct"))
    p.kw("all")
    items = _parse_select_list(p)
    p.expect_kw("from")
    from_ = _parse_from(p)
    where = None
    if p.kw("where"):
        where = _parse_expr(p)
    group_by: List[Expr] = []
    if p.kw("group"):
        p.expect_kw("by")
        group_by.append(_parse_expr(p))
        while p.peek().kind == "PUNCT" and p.peek().val == ",":
            p.next()
            group_by.append(_parse_expr(p))
    having = None
    if p.kw("having"):
        having = _parse_expr(p)
    order_by: List[OrderTerm] = []
    if p.kw("order"):
        p.expect_kw("by")
        order_by = _parse_order_by(p)
    limit = None
    if p.kw("limit"):
        limit = _parse_expr(p)
        if p.kw("offset"):
            _parse_expr(p)
    return Select(items, from_, where, distinct, group_by, having,
                  order_by, limit)


def _parse_insert(p: _P) -> Insert:
    p.expect_kw("insert")
    p.kw("into")
    table = p.ident()
    cols: List[str] = []
    if p.peek().kind == "PUNCT" and p.peek().val == "(":
        # could be a SELECT — only treat as col list if it parses as
        # idents; the grammar is unambiguous enough here.
        save = p.i
        try:
            p.next()
            cols.append(p.ident())
            while p.peek().kind == "PUNCT" and p.peek().val == ",":
                p.next()
                cols.append(p.ident())
            p.expect_punct(")")
        except _ParseFail:
            p.i = save
            cols = []
    sel = None
    rows: List[List[Expr]] = []
    if p.kw("values"):
        while True:
            p.expect_punct("(")
            row = [_parse_expr(p)]
            while p.peek().kind == "PUNCT" and p.peek().val == ",":
                p.next()
                row.append(_parse_expr(p))
            p.expect_punct(")")
            rows.append(row)
            if p.peek().kind == "PUNCT" and p.peek().val == ",":
                p.next()
                continue
            break
    elif p.peek().kind == "KW" and p.peek().val.lower() == "select":
        sel = _parse_select(p)
    else:
        raise _ParseFail("expected VALUES or SELECT")
    return Insert(table, cols, rows, sel)


def _parse_update(p: _P) -> Update:
    p.expect_kw("update")
    table = p.ident()
    alias = None
    if p.kw("as"):
        alias = p.ident()
    elif p.peek().kind == "IDENT":
        alias = p.next().val
    p.expect_kw("set")
    sets = []
    while True:
        col = p.ident()
        t = p.next()
        if not (t.kind == "OP" and t.val == "="):
            raise _ParseFail("expected = in SET")
        sets.append((col, _parse_expr(p)))
        if p.peek().kind == "PUNCT" and p.peek().val == ",":
            p.next()
            continue
        break
    where = None
    if p.kw("where"):
        where = _parse_expr(p)
    return Update(table, alias, sets, where)


def _parse_delete(p: _P) -> Delete:
    p.expect_kw("delete")
    p.expect_kw("from")
    table = p.ident()
    where = None
    if p.kw("where"):
        where = _parse_expr(p)
    return Delete(table, where)


def parse(sql: str):
    """Parse one SQL statement. Returns Select|Insert|Update|Delete,
    or None when the SQL is outside the statically-checked grammar."""
    try:
        toks = tokenize(sql)
        p = _P(toks)
        t = p.peek()
        if t.kind != "KW":
            return None
        w = t.val.lower()
        if w == "select":
            node = _parse_select(p)
        elif w == "insert":
            node = _parse_insert(p)
        elif w == "update":
            node = _parse_update(p)
        elif w == "delete":
            node = _parse_delete(p)
        else:
            return None  # DDL etc: dynamic
        # trailing semicolons are fine; anything else bails
        while p.peek().kind == "PUNCT" and p.peek().val == ";":
            p.next()
        if p.peek().kind != "EOF":
            return None
        return node
    except _ParseFail:
        return None


def count_placeholders(node) -> int:
    """Count ? placeholders (bare ? numbered sequentially)."""
    n = [0]

    def visit(e):
        if isinstance(e, PH):
            n[0] += 1
        elif isinstance(e, BinOp):
            visit(e.l)
            visit(e.r)
        elif isinstance(e, UnOp):
            visit(e.e)
        elif isinstance(e, Call):
            for a in e.args:
                visit(a)
        elif isinstance(e, InOp):
            visit(e.e)
            for v in e.vals:
                visit(v)
        elif isinstance(e, BetweenOp):
            visit(e.e)
            visit(e.lo)
            visit(e.hi)
        elif isinstance(e, IsNullOp):
            visit(e.e)

    def visit_select(s: Select):
        for it in s.items:
            if it.expr is not None:
                visit(it.expr)
        for f in s.from_:
            if isinstance(f, Join) and f.on is not None:
                visit(f.on)
            if isinstance(f, SubqueryRef):
                visit_select(f.query)
        if s.where is not None:
            visit(s.where)
        for g in s.group_by:
            visit(g)
        if s.having is not None:
            visit(s.having)
        for o in s.order_by:
            visit(o.e)
        if s.limit is not None:
            visit(s.limit)

    if isinstance(node, Select):
        visit_select(node)
    elif isinstance(node, Insert):
        for row in node.rows:
            for e in row:
                visit(e)
        if node.select is not None:
            visit_select(node.select)
    elif isinstance(node, Update):
        for _, e in node.sets:
            visit(e)
        if node.where is not None:
            visit(node.where)
    elif isinstance(node, Delete):
        if node.where is not None:
            visit(node.where)
    return n[0]


def iter_colrefs(e: Expr):
    """Yield every ColRef in an expression tree."""
    if isinstance(e, ColRef):
        yield e
    elif isinstance(e, BinOp):
        yield from iter_colrefs(e.l)
        yield from iter_colrefs(e.r)
    elif isinstance(e, UnOp):
        yield from iter_colrefs(e.e)
    elif isinstance(e, Call):
        for a in e.args:
            yield from iter_colrefs(a)
    elif isinstance(e, InOp):
        yield from iter_colrefs(e.e)
        for v in e.vals:
            yield from iter_colrefs(v)
    elif isinstance(e, BetweenOp):
        yield from iter_colrefs(e.e)
        yield from iter_colrefs(e.lo)
        yield from iter_colrefs(e.hi)
    elif isinstance(e, IsNullOp):
        yield from iter_colrefs(e.e)
