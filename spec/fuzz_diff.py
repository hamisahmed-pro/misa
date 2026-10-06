#!/usr/bin/env python3
"""fuzz_diff.py — adversarial differential + robustness fuzzing for the Misa toolchain.

Subcommands:
  diff    Generate N random Misa programs; run each under the interpreter AND the
          AOT backend; compare stdout/exit code exactly. Divergences are saved
          to /tmp/fuzz_bugs/.
  parser  Generate N random token soups; run `misa check` on each. Any Python
          traceback (or crash) is a bug; the input is saved.
  robust  Hand-written robustness probes (deep recursion, 1MB strings, 10k db
          rows, deep JSON, unicode edges, int64/float boundaries) on both backends.

Determinism: the GENERATOR is seeded (randomness lives here, not in programs).
Generated programs never use time.now(), map iteration, network, model calls,
or screens, so their output must be identical on both backends.

Usage:
  python3 fuzz_diff.py diff   [--count 600] [--seed 1] [--jobs 4]
  python3 fuzz_diff.py parser [--count 250] [--seed 7]
  python3 fuzz_diff.py robust
"""

import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.expanduser("~/workspace/misa")
MISA = [sys.executable, os.path.join(REPO, "impl", "bin", "misa")]
BUGDIR = "/tmp/fuzz_bugs"
ECODE_RE = re.compile(r"\bE(\d{3})\b")

os.makedirs(BUGDIR, exist_ok=True)

# --------------------------------------------------------------------------
# Program generator
# --------------------------------------------------------------------------

SCALARS = ["Int", "Float", "Bool", "Text"]
LISTS = ["[Int]", "[Float]", "[Text]", "[Bool]"]

TEXT_POOL = [
    "hello", "world", "ada", "foo bar", "a", "xyz",
    "caf\u00e9", "\u65e5\u672c\u8a9e", "emoji \U0001f600 test",
    "line1\\nline2", "tab\\there", "quote\\\"q\\\"", "back\\\\slash",
    "  padded  ", "a,b,c", "MiSa 123 !@#",
]
SAFE_CHR = [65, 66, 90, 97, 122, 48, 57, 945, 937, 128512, 127829, 33]
FLOAT_LITS = ["0.5", "-1.25", "2.0", "3.75", "100.25", "0.1", "-0.5", "7.0"]


def q(s):
    """Quote a Python string as a Misa string literal (escape backslash/quote)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Ctx:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.scopes = [{}]
        self.n = 0

    def fresh(self, p="v"):
        self.n += 1
        return f"{p}{self.n}"

    def push(self):
        self.scopes.append({})

    def pop(self):
        self.scopes.pop()

    def decl(self, name, t, mut=False):
        self.scopes[-1][name] = (t, mut)

    def vars_of(self, t, mut=None):
        out = []
        for s in self.scopes:
            for name, (tt, m) in s.items():
                if tt == t and (mut is None or m == mut):
                    out.append(name)
        return out

    def any_var_of(self, types):
        out = []
        for t in types:
            out += [(t, n) for n in self.vars_of(t)]
        return out


LOOP_BOUND = 12  # bound for generated while loops
PRINT_BUDGET = 4000  # max estimated prints per program (bound**loop_depth each)


class Gen:
    def __init__(self, seed, ffi=False):
        self.c = Ctx(seed)
        self.r = self.c.rng
        self.ffi = ffi
        self.loop_depth = 0
        self.print_cost = 0

    def try_print(self, t):
        cost = LOOP_BOUND ** self.loop_depth
        if self.print_cost + cost > PRINT_BUDGET:
            return None
        self.print_cost += cost
        return f"print({self.expr(t, 3)})"

    # -- expressions -----------------------------------------------------
    def expr(self, t, d):
        r = self.r
        if d <= 0 or r.random() < 0.38:
            return self.terminal(t)
        if t == "Int":
            return self.int_expr(d)
        if t == "Float":
            return self.float_expr(d)
        if t == "Bool":
            return self.bool_expr(d)
        if t == "Text":
            return self.text_expr(d)
        if t in LISTS:
            return self.list_expr(t, d)
        return self.terminal(t)

    def terminal(self, t):
        r = self.r
        vs = self.c.vars_of(t)
        if vs and r.random() < 0.55:
            return r.choice(vs)
        if t == "Int":
            return str(r.choice([0, 1, -1, 2, 3, 5, 7, 10, 42, 99,
                                 r.randint(-50, 200)]))
        if t == "Float":
            return r.choice(FLOAT_LITS)
        if t == "Bool":
            return r.choice(["true", "false"])
        if t == "Text":
            return q(r.choice(TEXT_POOL))
        if t in LISTS:
            el = t[1:-1]
            n = r.randint(0, 3)
            return "[" + ", ".join(self.terminal(el) for _ in range(n)) + "]"
        return "0"

    def num(self, d):
        """Int or Float expression."""
        t = self.r.choice(["Int", "Float"])
        return self.expr(t, d)

    def int_expr(self, d):
        r = self.r
        k = r.random()
        a = lambda: self.expr("Int", d - 1)
        if k < 0.30:
            return f"({a()} {r.choice(['+', '-', '*'])} {a()})"
        if k < 0.40:
            return f"({a()} % {r.randint(1, 9)})"
        if k < 0.46:
            return f"(-{a()})"
        if k < 0.54:
            xs = self.c.any_var_of(LISTS)
            if xs:
                t, name = r.choice(xs)
                return f"len({name})"
            return f"({a()} + {a()})"
        if k < 0.60:
            return f"int({q('42')})" if r.random() < 0.5 else f"int(str({a()}))"
        if k < 0.66:
            ch = r.choice(["A", "z", "0", "!", "\u03b1"])
            return f"str.ord({q(ch)})"
        if k < 0.76:
            f = self.expr("Float", d - 1)
            return f"math.{r.choice(['floor', 'ceil', 'round'])}({f})"
        if k < 0.82 and self.ffi:
            return f"strlen({self.expr('Text', d - 1)})"
        if k < 0.88:
            return f"({a()} * {a()} + {a()})"
        if k < 0.93:
            return self.list_index("Int", d)
        return f"({a()} - {a()})"

    def safe_divisor(self, d):
        r = self.r
        if r.random() < 0.5:
            return str(r.choice([1, 2, 3, 5, 10, -2, 4]))
        f = self.expr("Float", d - 1)
        return f"(({f}) * ({f}) + 1.0)"

    def float_expr(self, d):
        r = self.r
        k = r.random()
        n = lambda: self.num(d - 1)
        if k < 0.28:
            return f"({n()} {r.choice(['+', '-', '*'])} {n()})"
        if k < 0.40:
            return f"({n()} / {self.safe_divisor(d)})"
        if k < 0.46:
            return f"(-{n()})"
        if k < 0.54:
            f = self.expr("Float", d - 1)
            return f"math.sqrt(({f}) * ({f}))"
        if k < 0.62:
            f = self.expr("Float", d - 1)
            g = r.choice(["0.5", "1.0", "2.0", "3.0"])
            return f"math.pow(({f}) * ({f}) + 1.0, {g})"
        if k < 0.70:
            return f"math.abs({n()})"
        if k < 0.76:
            return f"float({q('2.5')})" if r.random() < 0.5 \
                else f"float(str({self.expr('Int', d - 1)}))"
        if k < 0.82:
            return f"({self.expr('Int', d - 1)} + 0.5)"
        if k < 0.86:
            return self.list_index("Float", d)
        return f"({n()} * {n()} - {n()})"

    def bool_expr(self, d):
        r = self.r
        k = r.random()
        if k < 0.45:
            t = r.choice(["Int", "Float", "Text"])
            a = self.expr(t, d - 1)
            b = self.expr(t, d - 1)
            return f"({a} {r.choice(['==', '!=', '<', '<=', '>', '>='])} {b})"
        if k < 0.70:
            a = self.expr("Bool", d - 1)
            b = self.expr("Bool", d - 1)
            return f"({a} {r.choice(['and', 'or'])} {b})"
        if k < 0.80:
            return f"(not {self.expr('Bool', d - 1)})"
        return self.terminal("Bool")

    def text_expr(self, d):
        r = self.r
        k = r.random()
        s = lambda: self.expr("Text", d - 1)
        if k < 0.22:
            return f"({s()} + {s()})"
        if k < 0.34:
            sc = self.expr(r.choice(["Int", "Float", "Bool"]), d - 1)
            return f"str({sc})"
        if k < 0.46:
            pre = r.choice(["A", "x=", "val:", ""])
            post = r.choice(["B", "!", ".", ""])
            # non-Text payloads only: Text payloads could inject quotes.
            # ALSO: nested plain-quote strings inside interpolation confuse
            # the lexer (E001 "unbalanced '{'"), so sanitize the payload.
            e = self.expr(r.choice(["Int", "Float", "Bool"]), d - 1)
            if '"' in e or "{" in e or "}" in e:
                e = str(r.randint(0, 99))
            return f'"{pre}{{{e}}}{post}"'
        if k < 0.56:
            i = self.expr("Int", d - 1)
            j = self.expr("Int", d - 1)
            return f"str.slice({s()}, {i}, {j})"
        if k < 0.62:
            return f"str.trim({s()})"
        if k < 0.68:
            return f"str.chr({r.choice(SAFE_CHR)})"
        if k < 0.76:
            lit = r.choice([t for t in TEXT_POOL if t and "\\" not in t])
            idx = r.randrange(len(lit))
            return f"({q(lit)})[{idx}]"
        if k < 0.82:
            return f"(str.split({s()}, {q(',')})[0])"
        if k < 0.88:
            return f"json.stringify({self.json_val(d - 1)})"
        if k < 0.93:
            return f"({self.expr('Int', d - 1)} + {s()})"
        if k < 0.96:
            return self.list_index("Text", d)
        return self.terminal("Text")

    def json_val(self, d):
        r = self.r
        k = r.random()
        if k < 0.4 or d <= 0:
            return self.expr(r.choice(["Int", "Float", "Bool", "Text"]), 0)
        if k < 0.7:
            n = r.randint(1, 3)
            fs = []
            for i in range(n):
                fs.append(f"{q(r.choice(['a', 'b', 'name', 'x', 'ok']))}: "
                          f"{self.json_val(d - 1)}")
            return "{" + ", ".join(fs) + "}"
        n = r.randint(1, 3)
        return "[" + ", ".join(self.json_val(d - 1) for _ in range(n)) + "]"

    def list_expr(self, t, d):
        r = self.r
        el = t[1:-1]
        k = r.random()
        e = lambda: self.expr(el, d - 1)
        if k < 0.4:
            n = r.randint(0, 3)
            return "[" + ", ".join(e() for _ in range(n)) + "]"
        xs = self.c.vars_of(t)
        if k < 0.65 and xs:
            name = r.choice(xs)
            ys = self.c.vars_of(t)
            other = r.choice(ys)
            return f"({name} + {other})"
        n = r.randint(1, 3)
        return "[" + ", ".join(e() for _ in range(n)) + "]"

    def list_index(self, el, d):
        """Scalar element read: (xs)[0] for a [el] variable, else literal."""
        r = self.r
        xs = self.c.vars_of(f"[{el}]")
        if xs and r.random() < 0.7:
            return f"({r.choice(xs)})[0]"
        return self.terminal(el)

    # -- statements ------------------------------------------------------
    def stmts(self, n, d=2, allow_decl=True):
        out = []
        for _ in range(n):
            out += self.stmt(d, allow_decl)
        return out

    def stmt(self, d, allow_decl=True):
        r = self.r
        k = r.random()
        if k < 0.34:
            t = r.choice(SCALARS)
            pl = self.try_print(t)
            return [pl] if pl else [f"let {self.c.fresh('np')} = {self.expr(t, 2)}"]
        if k < 0.52 and allow_decl:
            t = r.choice(SCALARS + ["[Int]", "[Text]"])
            name = self.c.fresh("v")
            mut = r.random() < 0.4
            kw = "var" if mut else "let"
            ann = f": {t}" if r.random() < 0.35 else ""
            e = self.expr(t, 3)  # generate BEFORE decl: no self-reference
            self.c.decl(name, t, mut)
            return [f"{kw} {name}{ann} = {e}"]
        if k < 0.60:
            cands = [(name, t) for t in SCALARS + LISTS
                     for name in self.c.vars_of(t, mut=True)]
            if cands:
                name, t = r.choice(cands)
                return [f"{name} = {self.expr(t, 3)}"]
            pl = self.try_print("Int")
            return [pl] if pl else []
        if k < 0.74:
            return self.if_stmt(d)
        if k < 0.84:
            return self.while_stmt(d)
        if k < 0.94:
            return self.for_stmt(d)
        t = r.choice(SCALARS)
        pl = self.try_print(t)
        if pl:
            return [pl]
        name = self.c.fresh("np")
        e = self.expr(t, 2)
        self.c.decl(name, t, False)
        return [f"let {name} = {e}"]

    def block(self, n, d):
        self.c.push()
        lines = self.stmts(n, d)
        self.c.pop()
        return lines

    def if_stmt(self, d):
        cond = self.expr("Bool", 2)
        then_b = self.block(self.r.randint(1, 3), max(d - 1, 1))
        lines = [f"if {cond} {{"]
        lines += ["  " + l for l in then_b]
        if self.r.random() < 0.6:
            else_b = self.block(self.r.randint(1, 3), max(d - 1, 1))
            lines += ["} else {"]
            lines += ["  " + l for l in else_b]
        lines += ["}"]
        return lines

    def while_stmt(self, d):
        r = self.r
        done = self.c.fresh("done")
        k = self.c.fresh("k")
        # Control vars are read-only to the generator: the reassign branch
        # must never touch them, or `k = k - 5` / `done = false` in the body
        # creates an infinite loop (both backends hang identically).
        # The increment and flag-set are emitted as literal lines below.
        self.c.decl(done, "Bool", False)
        self.c.decl(k, "Int", False)
        cond = self.expr("Bool", 2)
        self.loop_depth += 1
        body = self.block(r.randint(1, 3), max(d - 1, 1))
        self.loop_depth -= 1
        lines = [f"var {done} = false",
                 f"var {k} = 0",
                 f"while (not {done}) and ({k} < {LOOP_BOUND}) {{"]
        lines += ["  " + l for l in [f"{k} = {k} + 1"] + body]
        lines += ["  " + f"if {cond} {{", f"    {done} = true", "  }", "}"]
        return lines

    def for_stmt(self, d):
        r = self.r
        x = self.c.fresh("x")
        if r.random() < 0.6:
            t = r.choice(["[Int]", "[Text]"])
            el = t[1:-1]
            src = self.expr(t, 2)
        else:
            el = "Text"
            src = q(r.choice(["abc", "hello", "xyz"]))
        self.c.push()
        self.c.decl(x, el, False)
        self.loop_depth += 1
        body = self.stmts(r.randint(1, 2), max(d - 1, 1), allow_decl=True)
        self.loop_depth -= 1
        self.c.pop()
        lines = [f"for {x} in {src} {{"]
        lines += ["  " + l for l in body]
        lines += ["}"]
        return lines

    # -- top-level function ----------------------------------------------
    def top_fn(self, recursive=False):
        r = self.r
        name = self.c.fresh("f")
        nparams = r.randint(0, 2)
        ptypes = [r.choice(["Int", "Float", "Bool", "Text"]) for _ in range(nparams)]
        params = [(self.c.fresh("p"), t) for t in ptypes]
        ret = r.choice(["Int", "Float", "Bool", "Text"])
        self.c.push()
        for pn, pt in params:
            self.c.decl(pn, pt, False)
        lines = [f"fn {name}({', '.join(f'{pn}: {pt}' for pn, pt in params)})"
                 f" -> {ret} {{"]
        if recursive and params and params[0][1] == "Int":
            pn = params[0][0]
            body = self.block(r.randint(1, 2), 1)
            lines += ["  " + l for l in body]
            base = self.expr(ret, 1)
            other_args = ", ".join(qn for qn, qt in params[1:])
            step_args = f"{pn} - 1" + (", " + other_args if other_args else "")
            step = f"{name}({step_args})"
            if ret == "Int":
                comb = f"({pn} + {step})"
            elif ret == "Text":
                comb = f"({step} + {q('!')})"
            elif ret == "Bool":
                comb = f"(not {step})"
            else:
                comb = f"({step} + 0.5)"
            lines += [f"  var rr = {base}",
                      f"  if {pn} > 0 {{",
                      f"    rr = {comb}",
                      "  }",
                      "  return rr"]
        else:
            body = self.block(r.randint(1, 3), 1)
            lines += ["  " + l for l in body]
            lines += [f"  return {self.expr(ret, 2)}"]
        lines += ["}", ""]
        self.c.pop()
        # call it a few times
        calls = []
        for _ in range(r.randint(1, 3)):
            args = ", ".join(self.expr(t, 1) for _, t in params)
            calls.append(f"print({name}({args}))")
        return lines + calls + [""]

    def lambda_block(self):
        r = self.r
        t = r.choice(["Int", "Float", "Text"])
        p = self.c.fresh("p")
        l = self.c.fresh("l")
        self.c.push()
        self.c.decl(p, t, False)
        if r.random() < 0.5:
            body_expr = self.expr(t, 2)
            self.c.pop()
            lines = [f"let {l} = |{p}: {t}| {body_expr}"]
        else:
            body = self.stmts(r.randint(1, 2), 1)
            ret_e = self.expr(t, 2)
            self.c.pop()
            lines = [f"let {l} = |{p}: {t}| {{"]
            lines += ["  " + s for s in body]
            lines += ["  " + f"return {ret_e}", "}"]
        arg = self.expr(t, 1)
        lines.append(f"print({l}({arg}))")
        return lines + [""]

    def closure_mut(self):
        r = self.r
        c = self.c.fresh("c")
        inc = self.c.fresh("inc")
        self.c.decl(c, "Int", True)
        lines = [f"var {c} = {r.randint(0, 5)}",
                 f"let {inc} = || {{",
                 f"  {c} = {c} + 1",
                 f"  return {c}",
                 "}",
                 f"print({inc}())",
                 f"print({inc}())",
                 f"print({c})",
                 ""]
        return lines


# --------------------------------------------------------------------------
# Program families
# --------------------------------------------------------------------------

def fam_arith(g):
    lines = g.stmts(g.r.randint(6, 12), d=3)
    return "\n".join(lines)


def fam_str(g):
    g.c.decl("s1", "Text", False)
    g.c.decl("s2", "Text", True)
    head = [f'let s1 = {q(g.r.choice(TEXT_POOL))}',
            f'var s2 = {q(g.r.choice(TEXT_POOL))}']
    return "\n".join(head + g.stmts(g.r.randint(6, 10), d=3))


def fam_ctrl(g):
    return "\n".join(g.stmts(g.r.randint(5, 9), d=3))


def fam_fn(g):
    lines = []
    if g.r.random() < 0.5:
        lines += g.top_fn(recursive=True)
    lines += g.top_fn(recursive=False)
    if g.r.random() < 0.5:
        lines += g.top_fn(recursive=False)
    lines += g.lambda_block()
    if g.r.random() < 0.6:
        lines += g.lambda_block()
    lines += g.closure_mut()
    # higher-order: pass a lambda to a plain fn
    if g.r.random() < 0.5:
        lines += ["fn appx(f: (Int) -> Int, v: Int) -> Int {",
                  "  return f(v)", "}", "",
                  "print(appx(|a: Int| a * 3, 7))", ""]
    # fn used as a value
    lines += ["fn dblx(x: Int) -> Int {", "  return x * 2", "}", "",
              "let gx = dblx", "print(gx(21))", ""]
    lines += g.stmts(g.r.randint(2, 4), d=2)
    return "\n".join(lines)


def fam_coll(g):
    r = g.r
    lines = []
    xs = g.c.fresh("xs")
    t = r.choice(["[Int]", "[Float]", "[Text]"])
    el = t[1:-1]
    lit = "[" + ", ".join(g.expr(el, 1) for _ in range(r.randint(1, 4))) + "]"
    lines.append(f"var {xs}: {t} = {lit}")
    g.c.decl(xs, t, True)
    m = g.c.fresh("m")
    lines.append(f'var {m} = {{{q("a")}: {g.expr("Int", 1)}, '
                 f'{q("b")}: {g.expr("Int", 1)}}}')
    g.c.decl(m, "Map", True)
    rec = g.c.fresh("rec")
    lines.append(f"let {rec} = {{name: {q('ada')}, n: {g.expr('Int', 1)}}}")
    g.c.decl(rec, "Rec", False)
    lines.append(f"print({xs}[0])")
    lines.append(f"print(len({xs}))")
    lines.append(f"{xs} = {xs} + [{g.expr(el, 1)}]")
    lines.append(f"print({xs})")
    lines.append(f"print({m}[{q('a')}])")
    lines.append(f"{m}[{q('c')}] = {g.expr('Int', 1)}")
    lines.append(f"print(len({m}))")
    lines.append(f"print({rec}.name)")
    lines.append(f"print({rec}.n)")
    lines += g.stmts(r.randint(2, 5), d=2)
    return "\n".join(lines)


def fam_shadow(g):
    # NOTE: Misa forbids shadowing entirely (E210), even in nested blocks.
    # This family covers: reassignment, same name reused across SIBLING
    # blocks, and reads of outer bindings from inside blocks.
    r = g.r
    lines = ["var shw = 1",
             "shw = 2",
             "print(shw)",
             "shw = shw * 10 + 1",
             "print(shw)",
             "let sho = 100",
             "if true {",
             "  print(sho + 1)",
             "}",
             "if true {",
             "  let blk = 1",
             "  print(blk)",
             "}",
             "if true {",
             "  let blk = 2",
             "  print(blk)",
             "}",
             "for shc in [7, 8] {",
             "  print(sho + shc)",
             "}"]
    g.c.decl("shw", "Int", True)
    g.c.decl("sho", "Int", False)
    lines += g.stmts(r.randint(3, 6), d=2)
    return "\n".join(lines)


def fam_sum(g):
    r = g.r
    s = g.c.fresh("S")
    variants = ["A(Int)"]
    if r.random() < 0.8:
        variants.append("B")
    if r.random() < 0.7:
        variants.append("C(Text)")
    if r.random() < 0.4:
        variants.append("D(Bool)")
    lines = [f"type {s} {{ {', '.join(variants)} }}", ""]
    varname = g.c.fresh("sv")
    vkind = r.choice(variants)
    if vkind.startswith("A"):
        init = f"{s}.A({g.expr('Int', 1)})"
    elif vkind.startswith("C"):
        init = f"{s}.C({g.expr('Text', 1)})"
    elif vkind.startswith("D"):
        init = f"{s}.D({g.expr('Bool', 1)})"
    else:
        init = f"{s}.B"
    lines.append(f"let {varname} = {init}")
    arms = []
    for v in variants:
        vn = v.split("(")[0]
        if vn == "A":
            arms.append(f"{s}.A(x) => x * 2")
        elif vn == "C":
            arms.append(f"{s}.C(t) => len(t)")
        elif vn == "D":
            arms.append(f"{s}.D(b) => 9")
        else:
            arms.append(f"{s}.B => 0")
    if r.random() < 0.5:
        arms.append("_ => -1")
    match_e = (f"match {varname} {{\n" +
               ",\n".join("    " + a for a in arms) + ",\n}")
    lines.append(f"print({match_e})")
    # nested match inside a fn
    fn = g.c.fresh("cls")
    lines += ["", f"fn {fn}(v: {s}) -> Text {{",
              f"  return match v {{"]
    for v in variants:
        vn = v.split("(")[0]
        if vn == "A":
            lines.append(f"    {s}.A(n) => {q('A')} + str(n),")
        elif vn == "C":
            lines.append(f"    {s}.C(t) => t,")
        elif vn == "D":
            lines.append(f"    {s}.D(b) => str(b),")
        else:
            lines.append(f"    {s}.B => {q('B')},")
    lines += ["  }", "}", "", f"print({fn}({varname}))", ""]
    lines += g.stmts(r.randint(1, 3), d=1)
    return "\n".join(lines)


def fam_generic(g):
    r = g.r
    gid = g.c.fresh("gid")
    lines = [f"fn {gid}<T>(x: T) -> T {{",
             "  return x", "}", "",
             f"print({gid}(5))",
             f"print({gid}({q('hi')}))",
             f"print({gid}(2.5))", ""]
    if r.random() < 0.6:
        gf = g.c.fresh("gfirst")
        lines += [f"fn {gf}<T>(xs: [T]) -> T {{",
                  "  return xs[0]", "}", "",
                  f"print({gf}([10, 20]))",
                  f"print({gf}([{q('a')}, {q('b')}]))", ""]
    if r.random() < 0.6:
        gb = g.c.fresh("Box")
        bv = g.c.fresh("bv")
        lines += [f"type {gb}<T> {{", "  value: T,", "}", "",
                  f"let {bv}: {gb}<Int> = {gb}<Int>(42)",
                  f"print({bv}.value * 2)", ""]
    if r.random() < 0.6:
        gr = g.c.fresh("Res")
        gu = g.c.fresh("gunwrap")
        lines += [f"type {gr}<T> {{ Ok(T), Err(Text) }}", "",
                  f"fn {gu}<T>(r: {gr}<T>, d: T) -> T {{",
                  f"  return match r {{",
                  f"    {gr}.Ok(v) => v,",
                  f"    {gr}.Err(_) => d,",
                  "  }", "}", "",
                  f"print({gu}({gr}.Ok(7), 0))",
                  f"print({gu}({gr}<Int>.Err({q('bad')}), 0))", ""]
    if r.random() < 0.4:
        lines.append(f"print({gid}<Float>([1.5][0]))")
        lines.append("")
    return "\n".join(lines)


def fam_json(g):
    r = g.r
    lines = []
    for _ in range(r.randint(2, 3)):
        o = g.c.fresh("o")
        j = g.c.fresh("j")
        b = g.c.fresh("b")
        lines.append(f"let {o} = {g.json_val(3)}")
        lines.append(f"let {j} = json.stringify({o})")
        lines.append(f"print({j})")
        lines.append(f"let {b} = json.parse({j})")
        lines.append(f"print(json.stringify({b}) == {j})")
        lines.append("")
    # parse literal with escapes / nesting ({{ }} = literal braces in Misa)
    lines.append(r'let e1 = json.parse("{{\"a\": [1, {{\"b\": true}}]}}")')
    lines.append("print(e1.a[1].b)")
    lines.append("")
    return "\n".join(lines)


def fam_db(g, n):
    r = g.r
    tname = "tusers"
    # NOTE: the db handle variable MUST be named `db`: `db.exec(...)` is
    # namespace-style resolution, not a method call on an arbitrary name.
    dbv = "db"
    rowv = g.c.fresh("r")
    nrows = r.randint(1, 3)
    names = ["ada", "bisi", "chidi", "dayo"][:nrows]
    lines = [f"table {tname} {{",
             "  id: Int @primary,",
             "  name: Text,",
             "  score: Int,",
             "}",
             "",
             f'let {dbv} = db.open("/tmp/fuzz_{n}.db")',
             f'{dbv}.exec({dbv}, "CREATE TABLE {tname} '
             '(id INTEGER PRIMARY KEY, name TEXT, score INTEGER)")']
    for nm in names:
        sc = r.randint(0, 100)
        lines.append(f'{dbv}.exec({dbv}, "INSERT INTO {tname} '
                     f'(name, score) VALUES (?, ?)", [{q(nm)}, {q(str(sc))}])')
    lines.append(f'let {rowv} = {dbv}.query({dbv}, '
                 f'"SELECT id, name, score FROM {tname} ORDER BY id")')
    lines.append(f"print(len({rowv}))")
    lines.append(f"for {rowv}x in {rowv} {{")
    lines.append(f'  print(str({rowv}x.id) + {q(":")} + {rowv}x.name '
                 f'+ {q(":")} + str({rowv}x.score))')
    lines.append("}")
    lines.append(f'{dbv}.close({dbv})')
    lines.append("")
    return "\n".join(lines)


def fam_ffi(g):
    r = g.r
    lines = ['extern "c" {',
             "  fn strlen(s: Text) -> Int,",
             "  fn strcmp(a: Text, b: Text) -> Int,",
             "}",
             "",
             f"print(strlen({q('hello')}))",
             f"print(strlen({q(g.r.choice(TEXT_POOL))} + {q('!')}))",
             f"print(strcmp({q('abc')}, {q('abd')}))",
             f"print(strcmp({q('same')}, {q('same')}))",
             ""]
    return "\n".join(lines)


def fam_mixed(g):
    r = g.r
    lines = []
    if r.random() < 0.6:
        lines += g.top_fn(recursive=r.random() < 0.5)
    lines += g.lambda_block()
    lines += g.stmts(r.randint(6, 12), d=3)
    return "\n".join(lines)


FAM_INVALID = [
    'let x: Int = "s"',
    'print(nosuchvar)',
    'fn f1(a: Int) -> Int {\n  return a\n}\nprint(f1(1, 2))',
    'y = 5',
    'let z1 = 5\nz1 = "s"',
    'type SI { A(Int), B }\nlet sv = SI.A(1)\nprint(match sv {\n  SI.A(x) => x,\n})',
    'print(5(3))',
    'let gl = |x| x + 1',
    'let pv = print',
    'let = 5',
    'fn ( {',
    'print("unclosed)',
    'let a1 = [1, 2\nprint(a1)',
    'if true {\nprint(1)',
    'type SB { A(Int) }\nprint(SB.B)',
    'print(1 + )',
    '}}}} {{{{',
    'fn dup1() -> Int {\n  return 1\n}\nfn dup1() -> Int {\n  return 2\n}',
    'let m1: Map<Text, Int> = {}\nprint(m1["k"])',
]


def fam_invalid(g):
    return g.r.choice(FAM_INVALID)


FAMILIES = [
    ("arith", 90, fam_arith, {}),
    ("str", 70, fam_str, {}),
    ("ctrl", 60, fam_ctrl, {}),
    ("fn", 70, fam_fn, {}),
    ("coll", 60, fam_coll, {}),
    ("shadow", 30, fam_shadow, {}),
    ("sum", 50, fam_sum, {}),
    ("generic", 40, fam_generic, {}),
    ("json", 40, fam_json, {}),
    ("db", 30, fam_db, {"needs_n": True}),
    ("ffi", 20, fam_ffi, {"ffi": True}),
    ("mixed", 40, fam_mixed, {}),
    ("invalid", 30, fam_invalid, {}),
]

# targeted sign/overflow families (hand-written, deterministic)
MODSIGN_PROGS = [
    'print(-7 % 3)\nprint(7 % -3)\nprint(-7 % -3)\nprint(7 % 3)\nprint(-8 % 3)',
    'print(-7 / 2)\nprint(7 / -2)\nprint(-7.5 / 2.0)',
    'print(math.round(2.5))\nprint(math.round(-2.5))\nprint(math.round(3.5))\nprint(math.round(-3.5))',
    'print(math.floor(-2.1))\nprint(math.ceil(-2.9))\nprint(math.floor(2.9))\nprint(math.ceil(2.1))',
]
INTBOUND_PROGS = [
    'print(9223372036854775807)',
    'print(9223372036854775807 + 1)',
    'print(-9223372036854775808)',
    'print(-(-9223372036854775808))',
    'print(9223372036854775807 * 2)',
    'print(9223372036854775807 - -1)',
    'print(1000000 * 1000000 * 1000000)',
]


def gen_program(family, seed, n):
    name, _, fn, opts = family
    g = Gen(seed, ffi=opts.get("ffi", False))
    if opts.get("needs_n"):
        src = fn(g, n)
    else:
        src = fn(g)
    return f"// fuzz {name} seed={seed} n={n}\n" + src + "\n"


# --------------------------------------------------------------------------
# Backend runners
# --------------------------------------------------------------------------

def ecode_of(stderr):
    m = ECODE_RE.search(stderr or "")
    return "E" + m.group(1) if m else None


def run_interp(path):
    try:
        p = subprocess.run(MISA + ["run", path], capture_output=True, text=True,
                           timeout=30, cwd="/tmp")
        crashed = "Traceback (most recent call last)" in (p.stderr or "")
        return {"stdout": p.stdout, "stderr": p.stderr, "exit": p.returncode,
                "crashed": crashed, "timeout": False}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "TIMEOUT", "exit": -99,
                "crashed": False, "timeout": True}


def run_aot(path, n):
    out = f"/tmp/fuzz_{n}"
    for suf in ("", ".db", ".db-wal", ".db-journal"):
        try:
            os.remove(out + suf)
        except OSError:
            pass
    try:
        b = subprocess.run(MISA + ["build", path, "-o", out],
                           capture_output=True, text=True, timeout=120,
                           cwd="/tmp")
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "BUILD TIMEOUT", "exit": -99,
                "crashed": False, "timeout": True, "build_failed": True}
    if b.returncode != 0 or not os.path.exists(out):
        crashed = "Traceback (most recent call last)" in (b.stderr or "")
        return {"stdout": b.stdout, "stderr": b.stderr, "exit": b.returncode,
                "crashed": crashed, "timeout": False, "build_failed": True}
    try:
        p = subprocess.run([out], capture_output=True, text=True,
                           timeout=30, cwd="/tmp")
        crashed = "Traceback (most recent call last)" in (p.stderr or "")
        res = {"stdout": p.stdout, "stderr": p.stderr, "exit": p.returncode,
               "crashed": crashed, "timeout": False, "build_failed": False}
    except subprocess.TimeoutExpired:
        res = {"stdout": "", "stderr": "RUN TIMEOUT", "exit": -99,
               "crashed": False, "timeout": True, "build_failed": False}
    finally:
        # keep /tmp (512MB tmpfs) from filling: binaries are ~1MB each
        for suf in ("", ".db", ".db-wal", ".db-journal"):
            try:
                os.remove(out + suf)
            except OSError:
                pass
    return res


def compare(n, family, src, ri, ra):
    """Return None if equivalent, else a divergence dict."""
    if ri["stdout"] == ra["stdout"] and ri["exit"] == ra["exit"]:
        # stderr compared loosely: only the E-code must agree
        ei, ea = ecode_of(ri["stderr"]), ecode_of(ra["stderr"])
        if ei != ea and (ei or ea):
            return {"kind": "stderr-ecode", "ei": ei, "ea": ea}
        return None
    return {"kind": "stdout-or-exit"}


def save_bug(n, family, src, ri, ra, cmp):
    base = os.path.join(BUGDIR, f"fuzz_{n}")
    with open(base + ".misa", "w") as f:
        f.write(src)
    with open(base + ".report.txt", "w") as f:
        f.write(f"family: {family}\ndivergence: {cmp['kind']}\n")
        f.write(f"--- interp: exit={ri['exit']} crashed={ri['crashed']} "
                f"timeout={ri['timeout']}\n{ri['stdout']}\n"
                f"[stderr]\n{ri['stderr']}\n")
        f.write(f"--- aot: exit={ra['exit']} crashed={ra['crashed']} "
                f"timeout={ra['timeout']} build_failed={ra.get('build_failed')}\n"
                f"{ra['stdout']}\n[stderr]\n{ra['stderr']}\n")


def cmd_diff(args):
    plan = []
    n = 0
    for fam in FAMILIES:
        name, count, _, _ = fam
        for i in range(count):
            plan.append((n, fam, args.seed * 100003 + n))
            n += 1
    for i, src in enumerate(MODSIGN_PROGS):
        plan.append((n, ("modsign", 1, None, {}), src))
        n += 1
    for i, src in enumerate(INTBOUND_PROGS):
        plan.append((n, ("intbound", 1, None, {}), src))
        n += 1
    total = len(plan)
    print(f"[diff] {total} programs planned", flush=True)

    results = {"total": total, "pass": 0, "diverge": 0, "interp_crash": 0,
               "aot_crash": 0, "timeout": 0, "by_family": {},
               "divergences": []}

    def one(job):
        n, fam, seed_or_src = job
        name = fam[0]
        path = f"/tmp/fuzz_{n}.misa"
        if isinstance(seed_or_src, str):
            src = "// fuzz " + name + f" n={n}\n" + seed_or_src + "\n"
        else:
            src = gen_program(fam, seed_or_src, n)
        with open(path, "w") as f:
            f.write(src)
        # fresh db for each backend run
        for suf in (".db", ".db-wal", ".db-journal"):
            try:
                os.remove(f"/tmp/fuzz_{n}{suf}")
            except OSError:
                pass
        ri = run_interp(path)
        for suf in (".db", ".db-wal", ".db-journal"):
            try:
                os.remove(f"/tmp/fuzz_{n}{suf}")
            except OSError:
                pass
        ra = run_aot(path, n)
        cmp = compare(n, name, src, ri, ra)
        if cmp:
            try:
                save_bug(n, name, src, ri, ra, cmp)
            except OSError as e:
                cmp = {"kind": cmp["kind"] + f" (save_failed: {e})"}
        try:
            os.remove(path)
        except OSError:
            pass
        return (n, name, cmp, ri, ra)

    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(one, job): job for job in plan}
        for fut in cf.as_completed(futs):
            n, name, cmp, ri, ra = fut.result()
            done += 1
            fam_stats = results["by_family"].setdefault(
                name, {"total": 0, "diverge": 0})
            fam_stats["total"] += 1
            if ri["crashed"]:
                results["interp_crash"] += 1
            if ra["crashed"]:
                results["aot_crash"] += 1
            if ri["timeout"] or ra["timeout"]:
                results["timeout"] += 1
            if cmp:
                results["diverge"] += 1
                fam_stats["diverge"] += 1
                results["divergences"].append(
                    {"n": n, "family": name, "kind": cmp["kind"],
                     "interp_exit": ri["exit"], "aot_exit": ra["exit"],
                     "interp_ecode": ecode_of(ri["stderr"]),
                     "aot_ecode": ecode_of(ra["stderr"])})
                print(f"  DIVERGE #{n} [{name}] {cmp['kind']} "
                      f"i_exit={ri['exit']} a_exit={ra['exit']} "
                      f"i_ec={ecode_of(ri['stderr'])} a_ec={ecode_of(ra['stderr'])}",
                      flush=True)
            else:
                results["pass"] += 1
            if done % 50 == 0:
                el = time.time() - t0
                print(f"[diff] {done}/{total} done, "
                      f"{results['diverge']} divergences, {el:.0f}s",
                      flush=True)
    el = time.time() - t0
    results["seconds"] = round(el, 1)
    with open("/tmp/fuzz_diff_summary.json", "w") as f:
        json.dump(results, f, indent=1)
    print(json.dumps({k: v for k, v in results.items()
                      if k != "divergences"}, indent=1))
    print(f"[diff] full summary: /tmp/fuzz_diff_summary.json  "
          f"bugs in {BUGDIR}/fuzz_N.{{misa,report.txt}}")


# --------------------------------------------------------------------------
# Parser fuzzing: token soup
# --------------------------------------------------------------------------

TOKENS = [
    "fn", "let", "var", "if", "else", "while", "for", "in", "return",
    "type", "match", "extern", "table", "true", "false", "nil", "and",
    "or", "not", "print", "str", "len", "int", "float", "assert",
    "{", "}", "(", ")", "[", "]", ",", ":", ";", ".", "=>", "->",
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "foo", "bar", "x1", "_q", "Qux", "s",
    '"str"', '"a\\"b"', '"\\n"', '""', '"unclosed',
    "123", "1.5", "-7", "0x10", "9999999999999999999999",
    "|", "||", "\\", "@primary", "#", "?", "!", "&", "^", "~", "`",
]


def token_soup(rng):
    n = rng.randint(1, 60)
    parts = []
    depth = 0
    for _ in range(n):
        t = rng.choice(TOKENS)
        parts.append(t)
        if t in ("{", "(", "["):
            depth += 1
        elif t in ("}", ")", "]"):
            depth -= 1
    # sometimes leave unbalanced, sometimes sprinkle newlines
    s = " ".join(parts)
    if rng.random() < 0.5:
        s = s.replace(" ", "\n", rng.randint(0, 4))
    return s


def cmd_parser(args):
    rng = random.Random(args.seed)
    crashes = 0
    ecodes = {}
    t0 = time.time()
    for i in range(args.count):
        src = token_soup(rng)
        path = f"/tmp/psoup_{i}.misa"
        with open(path, "w") as f:
            f.write(src)
        try:
            p = subprocess.run(MISA + ["check", path], capture_output=True,
                               text=True, timeout=10, cwd="/tmp")
            err = p.stderr or ""
            if "Traceback (most recent call last)" in err:
                crashes += 1
                with open(os.path.join(BUGDIR, f"psoup_{i}.misa"), "w") as f:
                    f.write(src)
                with open(os.path.join(BUGDIR, f"psoup_{i}.stderr.txt"),
                          "w") as f:
                    f.write(err)
                print(f"  PARSER CRASH #{i}", flush=True)
            else:
                m = ECODE_RE.search(err)
                code = "E" + m.group(1) if m else ("OK" if p.returncode == 0
                                                  else "NOECODE")
                ecodes[code] = ecodes.get(code, 0) + 1
        except subprocess.TimeoutExpired:
            crashes += 1
            print(f"  PARSER TIMEOUT #{i}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"[parser] {i + 1}/{args.count}, crashes={crashes}",
                  flush=True)
    print(json.dumps({"soups": args.count, "crashes": crashes,
                      "ecode_histogram": ecodes,
                      "seconds": round(time.time() - t0, 1)}, indent=1))


# --------------------------------------------------------------------------
# Robustness probes
# --------------------------------------------------------------------------

def _clean_robust_dbs(name):
    for suf in (".db", ".db-wal", ".db-journal"):
        try:
            os.remove(f"/tmp/robust_{name}{suf}")
        except OSError:
            pass


def probe_both(name, src, timeout_i=30, timeout_b=120, timeout_r=60):
    path = f"/tmp/robust_{name}.misa"
    with open(path, "w") as f:
        f.write(src)
    _clean_robust_dbs(name)
    try:
        pi = subprocess.run(MISA + ["run", path], capture_output=True,
                            text=True, timeout=timeout_i, cwd="/tmp")
        ri = (pi.returncode, pi.stdout[-600:], (pi.stderr or "")[-600:])
    except subprocess.TimeoutExpired:
        ri = ("TIMEOUT", "", "")
    _clean_robust_dbs(name)  # interp's db must not leak into the AOT run
    out = f"/tmp/robust_{name}"
    try:
        pb = subprocess.run(MISA + ["build", path, "-o", out],
                            capture_output=True, text=True, timeout=timeout_b,
                            cwd="/tmp")
        if pb.returncode != 0:
            ra = (f"BUILDFAIL:{pb.returncode}", pb.stdout[-300:],
                  (pb.stderr or "")[-600:])
        else:
            try:
                pr = subprocess.run([out], capture_output=True, text=True,
                                    timeout=timeout_r, cwd="/tmp")
                ra = (pr.returncode, pr.stdout[-600:], (pr.stderr or "")[-600:])
            except subprocess.TimeoutExpired:
                ra = ("TIMEOUT", "", "")
    except subprocess.TimeoutExpired:
        ra = ("BUILDTIMEOUT", "", "")
    return {"interp": ri, "aot": ra}


def cmd_robust(args):
    out = {}
    fib = ('fn fibd(n: Int) -> Int {\n'
           '  var r = 1\n'
           '  if n > 1 {\n'
           '    r = fibd(n - 1) + fibd(n - 2)\n'
           '  }\n'
           '  return r\n'
           '}\n')
    # R1: deep recursion — linear (not exponential) to isolate depth
    lin = ('fn countdown(n: Int) -> Int {\n'
           '  if n <= 0 {\n'
           '    return 0\n'
           '  }\n'
           '  return countdown(n - 1) + 1\n'
           '}\n')
    for depth in (100, 1000, 10000):
        src = lin + f"print(countdown({depth}))\n"
        out[f"recursion_{depth}"] = probe_both(f"rec{depth}", src,
                                              timeout_i=60, timeout_r=60)
        print(f"[robust] recursion depth {depth}: "
              f"i={out[f'recursion_{depth}']['interp'][0]} "
              f"a={out[f'recursion_{depth}']['aot'][0]}", flush=True)
    # fib(20) sanity on both
    out["fib20"] = probe_both("fib20", fib + "print(fibd(20))\n")
    print(f"[robust] fib20: i={out['fib20']['interp'][0]} "
          f"a={out['fib20']['aot'][0]}", flush=True)

    # R2: 1MB string
    big = ('var s = ""\n'
           'var i = 0\n'
           'while i < 1000 {\n'
           '  s = s + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"\n'
           '  i = i + 1\n'
           '}\n'
           'print(len(s))\n'
           'print(str.slice(s, 0, 10))\n'
           'print(str.slice(s, len(s) - 10, len(s)))\n'
           'let j = json.stringify({"big": s})\n'
           'print(len(j))\n'
           'let back = json.parse(j)\n'
           'print(len(back.big) == len(s))\n')
    out["bigstring_1MB"] = probe_both("bigstr", big, timeout_i=120,
                                      timeout_r=120)
    print(f"[robust] 1MB string: i={out['bigstring_1MB']['interp'][0]} "
          f"a={out['bigstring_1MB']['aot'][0]}", flush=True)

    # R3: 10k db rows
    db10k = ('let db = db.open("/tmp/robust_10k.db")\n'
             'db.exec(db, "CREATE TABLE nums (id INTEGER PRIMARY KEY, v INTEGER)")\n'
             'var i = 0\n'
             'while i < 10000 {\n'
             '  db.exec(db, "INSERT INTO nums (v) VALUES (?)", [str(i)])\n'
             '  i = i + 1\n'
             '}\n'
             'let rows = db.query(db, "SELECT COUNT(*) AS c FROM nums")\n'
             'print(rows[0].c)\n'
             'let rows2 = db.query(db, "SELECT SUM(v) AS s FROM nums")\n'
             'print(rows2[0].s)\n'
             'db.close(db)\n')
    try:
        os.remove("/tmp/robust_10k.db")
    except OSError:
        pass
    out["db_10k"] = probe_both("db10k", db10k, timeout_i=180, timeout_r=180)
    print(f"[robust] db 10k: i={out['db_10k']['interp'][0]} "
          f"a={out['db_10k']['aot'][0]}", flush=True)

    # R4: 200-deep nested JSON
    nest = ""
    for _ in range(200):
        nest = "[" + nest
    nest = nest + "1" + "]" * 200
    r4src = (f'let d = json.parse({q(nest)})\n'
             'print("parsed")\n'
             'let s = json.stringify(d)\n'
             'print(len(s))\n'
             'print(json.parse(s) == d)\n')
    out["json_200deep"] = probe_both("jsondeep", r4src)
    print(f"[robust] json 200-deep: i={out['json_200deep']['interp'][0]} "
          f"a={out['json_200deep']['aot'][0]}", flush=True)

    # R5: unicode edges. NOTE: Misa string literals support only
    # \n \t \\ \" — NO \u/\U0001F600 escapes, NO 0x hex ints. Use literal UTF-8.
    r5 = ('print("caf\u00e9 \U0001F600")\n'
          'print(len("\U0001F600\U0001F601"))\n'
          'print(str.ord("\U0001F600"))\n'
          'print(str.chr(128512))\n'
          'print(str.chr(233))\n'
          'print(json.stringify({"e": "\U0001F600"}))\n')
    out["unicode"] = probe_both("unicode", r5)
    print(f"[robust] unicode: i={out['unicode']['interp'][0]} "
          f"a={out['unicode']['aot'][0]}", flush=True)
    # lone surrogate via json parse: build the JSON text "\ud800"
    # programmatically (Misa string literals have no \u escapes).
    # FINDING 2026-10-06: interp ACCEPTS then traceback-crashes on print
    # (UnicodeEncodeError); AOT cleanly E300-rejects. See
    # /tmp/fuzz_bugs/min_json_lone_surrogate.{misa,report.txt}.
    r5b = ('let q = str.chr(34)\n'
           'let bs = str.chr(92)\n'
           'let j = q + bs + "ud800" + q\n'
           'let x = json.parse(j)\n'
           'print(x)\n'
           'print(len(x))\n')
    out["unicode_lone_surrogate"] = probe_both("unisur", r5b)
    # astral chars through db + json
    r5c = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE u (t TEXT)")\n'
           'db.exec(db, "INSERT INTO u (t) VALUES (?)", ["\U0001F600\u00e9"])\n'
           'let rows = db.query(db, "SELECT t FROM u")\n'
           'print(rows[0].t)\n'
           'print(len(rows[0].t))\n'
           'print(json.stringify(rows[0]))\n'
           'db.close(db)\n')
    out["unicode_db_json"] = probe_both("unidb", r5c)

    # R6: int64 boundaries
    r6 = ('print(9223372036854775807)\n'
          'print(9223372036854775807 + 1)\n'
          'print(-9223372036854775808)\n'
          'print(-(-9223372036854775808))\n'
          'print(9223372036854775807 * 2)\n'
          'print(0 - 9223372036854775807 - 1)\n')
    out["int64"] = probe_both("int64", r6)
    print(f"[robust] int64: i={out['int64']['interp'][0]} "
          f"a={out['int64']['aot'][0]}", flush=True)

    # R7: float edges (NOTE: Misa float literals have NO scientific notation;
    # `1e308` is E001. Use math.pow(10.0, 308.0) to reach inf.)
    r7 = ('let big = math.pow(10.0, 308.0)\n'
          'print(big)\n'
          'print(big * 10.0)\n'
          'print(math.sqrt(-1.0))\n'
          'print(json.stringify(big * 10.0))\n')
    out["float_edges"] = probe_both("floatedge", r7)
    print(f"[robust] float edges: i={out['float_edges']['interp'][0]} "
          f"a={out['float_edges']['aot'][0]}", flush=True)
    # inf/nan through json.stringify specifically
    r7b = ('let inf = math.pow(10.0, 308.0) * 10.0\n'
           'print(inf)\n'
           'print(json.stringify({"x": inf}))\n')
    out["float_inf_json"] = probe_both("infjson", r7b)

    with open("/tmp/fuzz_robust_summary.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    print("[robust] summary: /tmp/fuzz_robust_summary.json")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diff")
    d.add_argument("--count", type=int, default=0)
    d.add_argument("--seed", type=int, default=1)
    d.add_argument("--jobs", type=int, default=4)
    p = sub.add_parser("parser")
    p.add_argument("--count", type=int, default=250)
    p.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("robust")
    args = ap.parse_args()
    if args.cmd == "diff":
        # --count scales family quotas proportionally
        if args.count:
            total0 = sum(c for _, c, _, _ in FAMILIES)
            FAMILIES[:] = [(n, max(1, round(c * args.count / total0)), f, o)
                           for n, c, f, o in FAMILIES]
        cmd_diff(args)
    elif args.cmd == "parser":
        cmd_parser(args)
    elif args.cmd == "robust":
        cmd_robust(args)


if __name__ == "__main__":
    main()
