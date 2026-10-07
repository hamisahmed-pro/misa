#!/usr/bin/env python3
"""Misa v0.2 test suite. Zero dependencies: `python3 tests/run_tests.py`.

Covers lexer, parser, checker, formatter, interpreter, providers
(against local stubs speaking the exact API formats), and the CLI.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa.lexer import Lexer
from misa.parser import parse_source, ParseError
from misa.checker import check_source, check_file, Checker
from misa.fmt import format_source
from misa.interp import Interpreter
from misa import ast as last
from misa import errors as Lerr

PASS = 0
FAIL = 0
FAILURES = []


def test(name):
    def deco(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print(f"  ok  {name}")
        except AssertionError as e:
            FAIL += 1
            FAILURES.append((name, str(e)[:300]))
            print(f" FAIL {name}: {e}")
        except Exception as e:
            FAIL += 1
            FAILURES.append((name, f"{type(e).__name__}: {e}"[:300]))
            print(f" FAIL {name}: {type(e).__name__}: {e}")
    return deco


def eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r}, got {a!r}"


def raises_code(fn, code):
    try:
        fn()
    except Lerr.MisaError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e}"
        return e
    raise AssertionError(f"expected MisaError {code}, nothing raised")


# ---------------------------------------------------------------- lexer

@test("lexer: basic tokens")
def _():
    toks = Lexer('let x = 42\n').tokens()
    kinds = [t.kind for t in toks]
    eq(kinds, ["let", "IDENT", "=", "INT", "SEMI", "EOF"])


@test("lexer: implicit semicolons")
def _():
    toks = Lexer('let a = 1\nlet b = 2\n').tokens()
    semis = [t for t in toks if t.kind == "SEMI"]
    eq(len(semis), 2)


@test("lexer: no semi after open brace")
def _():
    toks = Lexer('if x {\nprint(1)\n}\n').tokens()
    kinds = [t.kind for t in toks]
    assert "SEMI" not in kinds[1:4], kinds


@test("lexer: duration and money")
def _():
    toks = Lexer('5s 500ms $0.01').tokens()
    eq(toks[0].value, 5000)
    eq(toks[1].value, 500)
    eq(toks[2].value, 0.01)


@test("lexer: arrow token")
def _():
    toks = Lexer('->').tokens()
    eq(toks[0].kind, "->")


# ---------------------------------------------------------------- parser

@test("parser: precedence")
def _():
    prog = parse_source("let x = 1 + 2 * 3\n")
    stmt = prog.items[0]
    # (1 + (2*3))
    eq(stmt.value.op, "+")
    eq(stmt.value.right.op, "*")


@test("parser: string interpolation")
def _():
    prog = parse_source('let s = "hi {name}, you are {1 + 1}"\n')
    parts = prog.items[0].value.parts
    eq(len(parts), 4)
    eq(parts[0], "hi ")
    eq(parts[1].name, "name")


@test("parser: model decl")
def _():
    src = ('model f(a: Text) -> Text with mock.m\n'
           '    budget { tokens: 10 }\n'
           '    prompt v2 """hi {a}"""\n')
    m = parse_source(src).items[0]
    eq(m.name, "f")
    eq(m.prompt_version, 2)
    eq(m.budget.tokens, 10)
    eq(m.provider, "mock")


@test("parser: screen if/for")
def _():
    src = ('screen S(d: Text) {\n'
           '    if d { Text(text: "y") }\n'
           '    for x in [1] { Text(text: "z") }\n'
           '}\n')
    s = parse_source(src).items[0]
    assert isinstance(s.body[0], last.ScreenIf)
    assert isinstance(s.body[1], last.ScreenFor)


@test("parser: rejects bad syntax with E001")
def _():
    errs = check_source("let = 3\n")
    eq(errs[0]["code"], "E001")


# ---------------------------------------------------------------- checker

@test("checker: undefined name E101")
def _():
    errs = check_source("print(nope)\n")
    assert any(e["code"] == "E101" for e in errs), errs


@test("checker: arity E102")
def _():
    errs = check_source("len([1], [2])\n")
    assert any(e["code"] == "E102" for e in errs), errs


@test("checker: annotation mismatch E200")
def _():
    errs = check_source('let x: Text = 1\n')
    assert any(e["code"] == "E200" for e in errs), errs


@test("checker: immutable assign E201")
def _():
    errs = check_source("let x = 1\nx = 2\n")
    assert any(e["code"] == "E201" for e in errs), errs


@test("checker: record field mismatch E202")
def _():
    src = "type T { a: Text }\nlet x: T = { a: 1 }\n"
    errs = check_source(src)
    assert any(e["code"] == "E202" for e in errs), errs


@test("checker: nested record literal checks contextually (T12)")
def _():
    # agent trial T12 (2026-10-04): nested record literal against a
    # declared field type must type-check contextually
    src = ("type Address { city: Text, zip: Text }\n"
           "type Person { name: Text, address: Address, nickname: Text? }\n"
           "let p: Person = { name: \"Ada\", "
           "address: { city: \"Lagos\", zip: \"10001\" }, nickname: nil }\n")
    errs = check_source(src)
    assert errs == [], errs


@test("checker: nested record list checks contextually")
def _():
    src = ("type Item { name: Text }\n"
           "type Cart { items: [Item] }\n"
           'let c: Cart = { items: [{ name: "x" }, { name: "y" }] }\n')
    errs = check_source(src)
    assert errs == [], errs


@test("checker: nested record bad field E202")
def _():
    src = ("type Address { city: Text }\n"
           "type Person { address: Address }\n"
           'let p: Person = { address: { town: "Lagos" } }\n')
    errs = check_source(src)
    assert any(e["code"] == "E202" for e in errs), errs


@test("parser: multi-line list literal of records (ext T02)")
def _():
    # external cold trial T02 (2026-10-05): a newline before `]` must not
    # break list parsing (ASI semicolon inside brackets)
    src = ("type Item { name: Text, price: Float, qty: Int }\n"
           "let items: [Item] = [\n"
           '    { name: "Apple", price: 1.5, qty: 4 },\n'
           '    { name: "Bread", price: 2.0, qty: 3 }\n'
           "]\n"
           "print(len(items))\n")
    errs = check_source(src)
    assert errs == [], errs


@test("checker: unknown component E206")
def _():
    errs = check_source("screen S { Frobnicate }\n")
    assert any(e["code"] == "E206" for e in errs), errs


@test("checker: duplicate id E207")
def _():
    src = ("screen S { Column { TextInput(id: a, hint: \"x\")\n"
           "TextInput(id: a, hint: \"y\") } }\n")
    errs = check_source(src)
    assert any(e["code"] == "E207" for e in errs), errs


@test("checker: state() outside screen E211")
def _():
    errs = check_source("fn f() -> Int {\n  let x = state(0)\n  return x\n}\n")
    assert any(e["code"] == "E211" and "screen body" in e["message"]
               for e in errs), errs


@test("checker: state() must be bound E211")
def _():
    errs = check_source("screen S { Text(text: \"{state(0)}\") }\n")
    assert any(e["code"] == "E211" and "bound with let" in e["message"]
               for e in errs), errs


@test("checker: state() in handler E211")
def _():
    src = ("screen S { let c = state(0)\n"
           "  Button(label: \"x\", on_press: || { let y = state(1) }) }\n")
    errs = check_source(src)
    assert any(e["code"] == "E211" and "handler" in e["message"]
               for e in errs), errs


@test("checker: state() wrong type on write E211")
def _():
    src = ("screen S { let c = state(0)\n"
           "  Button(label: \"x\", on_press: || { c = \"no\" }) }\n")
    errs = check_source(src)
    assert any(e["code"] == "E211" and "cannot assign Text" in e["message"]
               for e in errs), errs


@test("checker: state() annotation mismatch E200")
def _():
    errs = check_source("screen S { let c: Text = state(0) }\n")
    assert any(e["code"] == "E200" for e in errs), errs


@test("checker: state() arity E211")
def _():
    errs = check_source("screen S { let c = state() }\n")
    assert any(e["code"] == "E211" and "exactly 1 argument" in e["message"]
               for e in errs), errs


@test("checker: unknown provider kind E209")
def _():
    src = ("provider wat { key: env(\"K\") }\nmodels { m: wat(\"x\") }\n"
           "model f() -> Text with wat.m prompt v1 \"\"\"hi\"\"\"\n")
    errs = check_source(src)
    assert any(e["code"] == "E209" for e in errs), errs


@test("checker: eval given keys E203")
def _():
    src = ("provider mock { key: env(\"K\") }\nmodels { m: mock(\"s\") }\n"
           "model f(a: Text) -> Text with mock.m prompt v1 \"\"\"{a}\"\"\"\n"
           "eval e { model: f, given: { b: \"x\" }, "
           "expect: true, runs: 1 }\n")
    errs = check_source(src)
    assert any(e["code"] == "E203" for e in errs), errs


@test("checker: json shape")
def _():
    errs = check_source("print(nope)\n", "f.misa")
    e = errs[0]
    assert set(("code", "message", "line", "col",
                "hint")) <= set(e.keys()), e


@test("checker: json shape")
def _():
    errs = check_source("print(nope)\n", "f.misa")
    e = errs[0]
    assert set(("code", "message", "line", "col",
                "hint")) <= set(e.keys()), e


# ---------------------------------------------------------------- checked throws (Phase 8 overtake)

@test("throws: parser accepts clause, throws as ident still works")
def _():
    p = parse_source(
        "fn fetch(u: Text) -> Text throws E304, E303 {\n"
        "  return http.get(u)\n}\n", "f.misa")
    fn = p.items[0]
    eq(fn.throws, ["E304", "E303"])
    # `throws` as an identifier is unaffected (contextual keyword)
    p2 = parse_source("fn f() {\n  let throws = 5\n  return throws\n}\n",
                      "f.misa")
    eq(p2.items[0].body[0].name, "throws")


@test("throws: formatter round-trips clause")
def _():
    src = ("fn fetch(u: Text) -> Text throws E304 {\n"
           "    return http.get(u)\n}\n")
    eq(format_source(src, "f.misa"), src)


@test("throws: declared and satisfied")
def _():
    src = ("fn fetch(u: Text) -> Text throws E304 {\n"
           "  return http.get(u)\n}\n")
    errs = check_source(src)
    assert errs == [], errs


@test("throws: undeclared call is E227")
def _():
    src = ("fn load(u: Text) -> Text {\n"
           "  return fetch(u)\n"
           "}\n"
           "fn fetch(u: Text) -> Text throws E304 {\n"
           "  return http.get(u)\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E304" in e["message"]
               and "'load'" in e["message"] for e in errs), errs


@test("throws: try/catch discharges")
def _():
    src = ("fn fetch(u: Text) -> Text throws E304 {\n"
           "  return http.get(u)\n"
           "}\n"
           "fn safe(u: Text) -> Text {\n"
           "  try {\n"
           "    return fetch(u)\n"
           "  } catch e {\n"
           "    return \"\"\n"
           "  }\n"
           "  return \"\"\n"
           "}\n")
    errs = [e for e in check_source(src) if e["code"] == "E227"]
    assert errs == [], errs


@test("throws: transitive inference across undeclared fns")
def _():
    src = ("fn a() -> Text {\n"
           "  return b()\n"
           "}\n"
           "fn b() -> Text {\n"
           "  return http.get(\"x\")\n"
           "}\n")
    errs = check_source(src)
    codes = [e["code"] for e in errs]
    # both a (via b) and b (via http.get) are unhandled
    assert codes.count("E227") == 2, errs


@test("throws: builtin sets (fs E303, env E302, json E300)")
def _():
    src = ("fn r(p: Text) -> Text {\n"
           "  return fs.read(p)\n"
           "}\n"
           "fn g(k: Text) -> Text {\n"
           "  return env(k)\n"
           "}\n"
           "fn j(s: Text) {\n"
           "  let v = json.parse(s)\n"
           "}\n")
    errs = check_source(src)
    msgs = " ".join(e["message"] for e in errs if e["code"] == "E227")
    assert "E303" in msgs and "E302" in msgs and "E300" in msgs, errs


@test("throws: over-broad declaration is allowed (upper bound)")
def _():
    # Declared ⊃ inferred is permitted: the clause is a contract
    # (upper bound), not an exact set. Only under-declaration errors.
    src = ("fn fetch(u: Text) -> Text throws E304, E303 {\n"
           "  return http.get(u)\n"
           "}\n")
    errs = [e for e in check_source(src) if e["code"] == "E227"]
    assert errs == [], errs


@test("throws: malformed code in clause is E227")
def _():
    src = ("fn f() throws OOPS {\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "OOPS" in e["message"]
               for e in errs), errs


@test("throws: throw with literal code is tracked")
def _():
    src = ("fn f() {\n"
           "  throw { code: \"E303\", message: \"bad\" }\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E303" in e["message"]
               for e in errs), errs
    src2 = ("fn f() throws E303 {\n"
            "  throw { code: \"E303\", message: \"bad\" }\n"
            "}\n")
    assert [e for e in check_source(src2)
            if e["code"] == "E227"] == []


@test("throws: rethrow of caught error carries the try set")
def _():
    src = ("fn fetch(u: Text) -> Text throws E304 {\n"
           "  return http.get(u)\n"
           "}\n"
           "fn wrap(u: Text) -> Text throws E304 {\n"
           "  try {\n"
           "    return fetch(u)\n"
           "  } catch e {\n"
           "    throw e\n"
           "  }\n"
           "}\n")
    errs = [e for e in check_source(src) if e["code"] == "E227"]
    assert errs == [], errs


@test("throws: lambda infers, call site checks")
def _():
    src = ("fn run(u: Text) -> Text {\n"
           "  let f = |u| http.get(u)\n"
           "  return f(u)\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E304" in e["message"]
               for e in errs), errs


@test("throws: spawn/await propagates task set")
def _():
    src = ("fn run(u: Text) -> Text {\n"
           "  let t = spawn { return http.get(u) }\n"
           "  return await t\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E304" in e["message"]
               for e in errs), errs
    src2 = ("fn run(u: Text) -> Text throws E304, E306 {\n"
            "  let t = spawn { return http.get(u) }\n"
            "  return await t\n"
            "}\n")
    assert [e for e in check_source(src2)
            if e["code"] == "E227"] == []


@test("throws: top level is exempt (program boundary)")
def _():
    errs = check_source("print(http.get(\"http://x\"))\n")
    assert [e for e in errs if e["code"] == "E227"] == [], errs


@test("throws: extern calls raise E226")
def _():
    src = ("extern \"c\" {\n"
           "  fn strlen(s: Text) -> Int\n"
           "}\n"
           "fn n(s: Text) -> Int {\n"
           "  return strlen(s)\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E226" in e["message"]
               for e in errs), errs


@test("throws: model calls raise E400")
def _():
    src = ("provider mock { key: env(\"K\") }\n"
           "models { m: mock(\"s\") }\n"
           "model f(a: Text) -> Text with mock.m prompt v1 \"\"\"{a}\"\"\"\n"
           "fn g(a: Text) -> Text {\n"
           "  return f(a)\n"
           "}\n")
    errs = check_source(src)
    assert any(e["code"] == "E227" and "E400" in e["message"]
               for e in errs), errs


@test("throws: throws_graph in Checker")
def _():
    src = ("fn fetch(u: Text) -> Text throws E304 {\n"
           "  return http.get(u)\n"
           "}\n"
           "fn load(u: Text) -> Text {\n"
           "  return fetch(u)\n"
           "}\n")
    c = Checker()
    c.check(parse_source(src, "f.misa"))
    eq(c.throws_graph["fetch"],
       {"declared": ["E304"], "inferred": ["E304"]})
    eq(c.throws_graph["load"],
       {"declared": [], "inferred": ["E304"]})


@test("throws: operators are unchecked (panic-like)")
def _():
    # `xs[0]` and `a / b` may raise E300 at runtime, but operators are
    # unchecked by design — no throws clause required.
    src = ("fn first(xs: [Int]) -> Int {\n"
           "  return xs[0]\n"
           "}\n"
           "fn half(a: Int) -> Int {\n"
           "  return a / 2\n"
           "}\n")
    errs = [e for e in check_source(src) if e["code"] == "E227"]
    assert errs == [], errs


@test("throws: screens are exempt in v1")
def _():
    # Screen handlers are inline lambdas; the v1 "inline lambdas
    # unchecked" limitation applies. (Requiring try/catch in every UI
    # handler would be the verbosity trap in the most handler-dense
    # layer.)
    src = ("screen Main() {\n"
           "  button(id: b, text: \"go\", on_click: |_| {\n"
           "    let x = http.get(\"http://x\")\n"
           "  })\n"
           "}\n")
    assert [e for e in check_source(src)
            if e["code"] == "E227"] == []


# ---------------------------------------------------------------- formatter

@test("formatter: idempotent on examples")
def _():
    for name in ("hello.misa", "plantcare.misa", "budgets.misa"):
        src = open(os.path.join(ROOT, "examples", name)).read()
        once = format_source(src, name)
        twice = format_source(once, name)
        eq(once, twice, name)


@test("formatter: canonical spacing")
def _():
    out = format_source("let x=1+2\n")
    eq(out, "let x = 1 + 2\n")


# ---------------------------------------------------------------- interpreter

def run_src(src, mock_scripts=None):
    prog = parse_source(src)
    errs = check_source(src)
    assert not errs, errs
    buf = io.StringIO()
    Interpreter(mock_scripts=mock_scripts, out=buf).run(prog)
    return buf.getvalue()


@test("interp: hello example output")
def _():
    src = open(os.path.join(ROOT, "examples", "hello.misa")).read()
    out = run_src(src)
    assert "Hello from Misa!" in out
    assert "spider has 8 legs" in out
    assert "tour complete" in out


def run_src_checked(src):
    """Parse + check + run on the SAME ast (the checker stamps screen
    state slots onto it)."""
    prog = parse_source(src)
    errs = Checker().check(prog)
    assert not errs, errs
    buf = io.StringIO()
    ip = Interpreter(out=buf)
    ip.run(prog)
    return ip, buf


@test("interp: screen state init + handler re-render")
def _():
    src = ("screen Counter() {\n"
           "  let count = state(0)\n"
           "  Text(text: \"count: {count}\")\n"
           "  Button(label: \"inc\", on_press: || { count = count + 1 })\n"
           "}\n"
           "navigate(Counter())\n")
    ip, buf = run_src_checked(src)
    assert "count: 0" in buf.getvalue(), buf.getvalue()
    assert len(ip._screen_frame["handlers"]) == 1
    buf.truncate(0)
    buf.seek(0)
    ip.test_fire_handler(0)
    assert "count: 1" in buf.getvalue(), buf.getvalue()
    buf.truncate(0)
    buf.seek(0)
    ip.test_fire_handler(0)
    assert "count: 2" in buf.getvalue(), buf.getvalue()


@test("interp: screen state multiple cells are independent")
def _():
    src = ("screen M() {\n"
           "  let a = state(0)\n"
           "  let b = state(\"x\")\n"
           "  Text(text: \"{a} {b}\")\n"
           "  Button(label: \"a\", on_press: || { a = a + 1 })\n"
           "  Button(label: \"b\", on_press: || { b = \"y\" })\n"
           "}\n"
           "navigate(M())\n")
    ip, buf = run_src_checked(src)
    assert "0 x" in buf.getvalue()
    buf.truncate(0)
    buf.seek(0)
    ip.test_fire_handler(1)  # only b changes
    out = buf.getvalue()
    assert "0 y" in out, out


@test("interp: screen state resets on navigate")
def _():
    src = ("screen C() {\n"
           "  let n = state(0)\n"
           "  Text(text: \"{n}\")\n"
           "  Button(label: \"i\", on_press: || { n = n + 1 })\n"
           "}\n"
           "navigate(C())\n")
    prog = parse_source(src)
    assert not Checker().check(prog)
    buf = io.StringIO()
    ip = Interpreter(out=buf)
    ip.run(prog)
    ip.test_fire_handler(0)
    assert "Text(text=1)" in buf.getvalue(), buf.getvalue()
    buf.truncate(0)
    buf.seek(0)
    ip.run(prog)  # navigate again: state resets
    assert "Text(text=0)" in buf.getvalue(), buf.getvalue()


@test("interp: model call returns typed record")
def _():
    src = open(os.path.join(ROOT, "examples", "plantcare.misa")).read()
    mocks = json.load(open(os.path.join(ROOT, "examples",
                                        "mocks.json")))
    out = run_src(src, mocks)
    assert "condition: nitrogen deficiency (0.91)" in out
    assert "--- screen ResultScreen ---" in out
    assert "Banner(text=All clear, style=info)" in out
    assert "Text(text=Add nitrogen-rich fertilizer)" in out


@test("interp: budget exceeded E500")
def _():
    src = open(os.path.join(ROOT, "examples", "budgets.misa")).read()
    mocks = json.load(open(os.path.join(ROOT, "examples",
                                        "mocks.json")))
    raises_code(lambda: run_src(src, mocks), "E500")


@test("interp: schema repair gives up with E400")
def _():
    src = ("provider mock { key: env(\"K\") }\n"
           "models { m: mock(\"bad_shape\") }\n"
           "type T { condition: Text }\n"
           "model f() -> T with mock.m prompt v1 \"\"\"hi\"\"\"\n"
           "let x = f()\n")
    raises_code(lambda: run_src(src, {"m": {"condition": 1}}),
                "E400")


@test("interp: immutable assign caught statically E201")
def _():
    errs = check_source("let x = 1\nx = 2\n")
    assert any(e["code"] == "E201" for e in errs), errs


@test("interp: assert failure E301")
def _():
    raises_code(lambda: run_src('assert(false, "nope")\n'), "E301")


@test("interp: string interpolation with fields")
def _():
    out = run_src('type T { a: Text }\nlet x: T = { a: "hi" }\n'
                  'print("v={x.a}!")\n')
    eq(out, "v=hi!\n")


# ---------------------------------------------------------------- providers

class _Stub(BaseHTTPRequestHandler):
    mode = "openai"
    last_request = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Stub.last_request = (self.path, body)
        if _Stub.mode == "openai":
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for tok in ["{\"con", "dition\": \"ok\"}"]:
                    chunk = {"choices": [
                        {"delta": {"content": tok}}]}
                    self.wfile.write(
                        f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            payload = {
                "choices": [{"message": {"content":
                                         json.dumps({"ok": True})}}],
                "usage": {"prompt_tokens": 11,
                          "completion_tokens": 22},
            }
        else:  # gemini
            payload = {
                "candidates": [{"content": {"parts": [{"text":
                    json.dumps({"ok": True})}]}}],
                "usageMetadata": {"promptTokenCount": 5,
                                  "candidatesTokenCount": 7},
            }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


def _with_stub(mode, fn):
    _Stub.mode = mode
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        fn(port)
    finally:
        srv.shutdown()


@test("provider: openai-compatible complete")
def _():
    from misa import providers as P
    def go(port):
        os.environ["MISA_OPENAI_BASE_URL"] = \
            f"http://127.0.0.1:{port}/v1"
        pr = P.OpenAICompatProvider("k")
        c = pr.complete(model_id="m", prompt="hi", images=[],
                        timeout_s=10)
        eq(json.loads(c.text), {"ok": True})
        eq((c.usage.input_tokens, c.usage.output_tokens), (11, 22))
        path, body = _Stub.last_request
        eq(body["response_format"], {"type": "json_object"})
    _with_stub("openai", go)


@test("provider: openai-compatible stream")
def _():
    from misa import providers as P
    def go(port):
        os.environ["MISA_OPENAI_BASE_URL"] = \
            f"http://127.0.0.1:{port}/v1"
        pr = P.OpenAICompatProvider("k")
        toks = list(pr.stream(model_id="m", prompt="hi", images=[],
                              timeout_s=10))
        eq("".join(toks), '{"condition": "ok"}')
    _with_stub("openai", go)


@test("provider: gemini complete")
def _():
    from misa import providers as P
    def go(port):
        os.environ["MISA_GEMINI_BASE_URL"] = \
            f"http://127.0.0.1:{port}"
        pr = P.GeminiProvider("k")
        c = pr.complete(model_id="gemini-x", prompt="hi", images=[],
                        timeout_s=10)
        eq(json.loads(c.text), {"ok": True})
        eq((c.usage.input_tokens, c.usage.output_tokens), (5, 7))
        path, body = _Stub.last_request
        assert ":generateContent?" in path, path
        eq(body["generationConfig"]["responseMimeType"],
           "application/json")
    _with_stub("gemini", go)


@test("provider: mock missing script E401")
def _():
    from misa import providers as P
    pr = P.MockProvider({})
    pr.alias = "nope"
    raises_code(lambda: pr.complete(model_id="m", prompt="x",
                                    images=[], timeout_s=5), "E401")


@test("interp: streaming end-to-end via stub")
def _():
    from misa import providers as P  # noqa
    def go(port):
        os.environ["MISA_OPENAI_BASE_URL"] = \
            f"http://127.0.0.1:{port}/v1"
        os.environ["LM_TEST_KEY"] = "k"
        src = ("provider openai { key: env(\"LM_TEST_KEY\") }\n"
               "models { m: openai(\"mm\") }\n"
               "type R { condition: Text }\n"
               "model f() -> R with openai.m prompt v1 \"\"\"hi\"\"\"\n"
               "stream s = f()\n"
               "on s.token { |t| print(\"tok:\" + t) }\n"
               "let r = await s\n"
               "print(\"done:{r.condition}\")\n")
        out = run_src(src)
        assert "tok:" in out, out
        assert "done:ok" in out, out
    _with_stub("openai", go)


# ---------------------------------------------------------------- CLI

def _cli(*args):
    exe = os.path.join(ROOT, "impl", "bin", "misa")
    return subprocess.run([exe] + list(args), capture_output=True,
                          text=True, timeout=60)


@test("cli: check/run/test exit codes")
def _():
    ex = os.path.join(ROOT, "examples")
    r = _cli("check", f"{ex}/hello.misa")
    eq(r.returncode, 0, r.stderr)
    r = _cli("run", f"{ex}/hello.misa")
    eq(r.returncode, 0, r.stderr)
    assert "tour complete" in r.stdout
    r = _cli("test", f"{ex}/plantcare.misa", "--mock",
             f"{ex}/mocks.json")
    eq(r.returncode, 0, r.stdout + r.stderr)
    assert "1/1 evals passed" in r.stdout
    r = _cli("check", f"{ex}/hello.misa", "--json")
    eq(r.returncode, 0)


@test("cli: check fails cleanly on bad code")
def _():
    p = "/tmp/misa_bad.misa"
    open(p, "w").write("print(nope)\n")
    r = _cli("check", p)
    eq(r.returncode, 1)
    assert "E101" in r.stderr


@test("cli: run enforces budget, exit 1")
def _():
    ex = os.path.join(ROOT, "examples")
    r = _cli("run", f"{ex}/budgets.misa", "--mock", f"{ex}/mocks.json")
    eq(r.returncode, 1)
    assert "E500" in r.stderr


# ---------------------------------------------------------------- v0.2: functions

@test("v0.2 parser: fn decl")
def _():
    p = parse_source('fn add(a: Int, b: Int) -> Int {\n    return a + b\n}\n',
                     "f.misa")
    fn = p.items[0]
    assert isinstance(fn, last.FnDecl) and fn.name == "add"
    assert fn.ret.name == "Int" and len(fn.params) == 2


@test("v0.2 parser: fn without return type")
def _():
    p = parse_source('fn greet(name: Text) {\n    print(name)\n}\n', "f.misa")
    assert isinstance(p.items[0], last.FnDecl)
    assert p.items[0].ret is None


@test("v0.2 checker: fn call ok")
def _():
    src = 'fn add(a: Int, b: Int) -> Int {\n    return a + b\n}\nlet x = add(1, 2)\n'
    assert check_source(src) == [], check_source(src)


@test("v0.2 checker: fn arity E102")
def _():
    src = 'fn add(a: Int, b: Int) -> Int {\n    return a + b\n}\nlet x = add(1)\n'
    errs = check_source(src)
    assert any(e["code"] == "E102" for e in errs), errs


@test("v0.2 checker: fn arg type E211")
def _():
    src = 'fn add(a: Int, b: Int) -> Int {\n    return a + b\n}\nlet x = add(1, "s")\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("v0.2 checker: fn return type E211")
def _():
    src = 'fn f() -> Int {\n    return "s"\n}\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("v0.2 checker: missing return E211")
def _():
    src = 'fn f(x: Int) -> Int {\n    if x > 0 {\n        return 1\n    }\n}\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("v0.2 checker: return value from Void fn E211")
def _():
    src = 'fn f() {\n    return 1\n}\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("v0.2 checker: Void call as value E211")
def _():
    src = 'fn f() {\n    print("x")\n}\nlet y = f()\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("v0.2 interp: fn call and recursion")
def _():
    src = ('fn fib(n: Int) -> Int {\n    if n < 2 {\n        return n\n    }\n'
           '    return fib(n - 1) + fib(n - 2)\n}\nprint(fib(10))\n')
    eq(run_src(src).strip(), "55")


@test("v0.2 interp: Void fn as statement")
def _():
    src = 'fn g() {\n    print("hi")\n}\ng()\n'
    eq(run_src(src).strip(), "hi")


# ---------------------------------------------------------------- v0.2: modules

def _write_files(files):
    d = tempfile.mkdtemp(prefix="misa-v02-")
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
    return d


@test("v0.2 loader: import resolves and runs")
def _():
    d = _write_files({
        "utils.misa": 'fn double(n: Int) -> Int {\n    return n * 2\n}\n',
        "main.misa": 'import "utils.misa"\nprint(double(21))\n',
    })
    errs = check_file(os.path.join(d, "main.misa"))
    assert errs == [], errs


@test("v0.2 loader: missing import E112")
def _():
    d = _write_files({"main.misa": 'import "nope.misa"\n'})
    errs = check_file(os.path.join(d, "main.misa"))
    assert any(e["code"] == "E112" for e in errs), errs


@test("v0.2 loader: circular import E111")
def _():
    d = _write_files({
        "a.misa": 'import "b.misa"\n',
        "b.misa": 'import "a.misa"\n',
    })
    errs = check_file(os.path.join(d, "a.misa"))
    assert any(e["code"] == "E111" for e in errs), errs


@test("v0.2 loader: duplicate across imports E110")
def _():
    d = _write_files({
        "u1.misa": 'fn f() -> Int {\n    return 1\n}\n',
        "u2.misa": 'fn f() -> Int {\n    return 2\n}\n',
        "main.misa": 'import "u1.misa"\nimport "u2.misa"\n',
    })
    errs = check_file(os.path.join(d, "main.misa"))
    assert any(e["code"] == "E110" for e in errs), errs


@test("v0.2 interp: imported statements run first")
def _():
    d = _write_files({
        "u.misa": 'print("from-import")\n',
        "main.misa": 'import "u.misa"\nprint("from-main")\n',
    })
    main = os.path.join(d, "main.misa")
    assert check_file(main) == []
    r = _cli("run", main)
    eq(r.returncode, 0, r.stderr)
    eq(r.stdout.strip().splitlines(), ["from-import", "from-main"])


# ---------------------------------------------------------------- v0.2: try/catch/throw

@test("v0.2 checker: throw non-Error E212")
def _():
    errs = check_source('throw 42\n')
    assert any(e["code"] == "E212" for e in errs), errs


@test("v0.2 checker: throw Error literal ok")
def _():
    src = 'throw { code: "E1", message: "m" }\n'
    assert check_source(src) == [], check_source(src)


@test("v0.2 interp: try/catch catches runtime error")
def _():
    src = ('try {\n    let x = int("nope")\n} catch e {\n'
           '    print("got {e.code}")\n}\n')
    eq(run_src(src).strip(), "got E300")


@test("v0.2 interp: try/catch catches throw")
def _():
    src = ('try {\n    throw { code: "E999", message: "boom" }\n'
           '} catch e {\n    print("{e.code}:{e.message}")\n}\n')
    eq(run_src(src).strip(), "E999:boom")


@test("v0.2 interp: catch skipped when no error")
def _():
    src = ('try {\n    print("ok")\n} catch e {\n    print("bad")\n}\n')
    eq(run_src(src).strip(), "ok")


@test("v0.2 interp: uncaught throw reports code")
def _():
    prog = 'throw { code: "E404", message: "gone" }\n'
    with tempfile.NamedTemporaryFile("w", suffix=".misa",
                                     delete=False) as f:
        f.write(prog)
        path = f.name
    r = _cli("run", path)
    eq(r.returncode, 1)
    assert "E404" in r.stderr, r.stderr


# ---------------------------------------------------------------- v0.2: Map

@test("v0.2 parser: map vs record disambiguation")
def _():
    p = parse_source('let m = {"a": 1}\nlet r = {a: 1}\n', "f.misa")
    assert isinstance(p.items[0].value, last.MapLit)
    assert isinstance(p.items[1].value, last.RecordLit)


@test("v0.2 checker: map annotation ok")
def _():
    src = 'let m: Map<Text, Int> = {"a": 1}\n'
    assert check_source(src) == [], check_source(src)


@test("v0.2 checker: map value mismatch E202")
def _():
    src = 'let m: Map<Text, Int> = {"a": "x"}\n'
    errs = check_source(src)
    assert any(e["code"] == "E202" for e in errs), errs


@test("v0.2 interp: map get/set/len")
def _():
    src = ('var m: Map<Text, Int> = {"a": 1}\n'
           'm["b"] = 2\n'
           'print(m["a"])\nprint(m["b"])\nprint(len(m))\n')
    eq(run_src(src).strip().splitlines(), ["1", "2", "2"])


@test("v0.2 interp: missing map key E305 catchable")
def _():
    src = ('var m: Map<Text, Int> = {"a": 1}\n'
           'try {\n    print(m["zzz"])\n} catch e {\n'
           '    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E305")


# ---------------------------------------------------------------- v0.2: fs/http

@test("v0.2 interp: fs.write/read roundtrip")
def _():
    d = tempfile.mkdtemp(prefix="misa-fs-")
    target = os.path.join(d, "note.txt").replace("\\", "/")
    src = (f'fs.write("{target}", "hello v0.2")\n'
           f'print(fs.read("{target}"))\n')
    eq(run_src(src).strip(), "hello v0.2")


@test("v0.2 interp: fs.read missing throws E303")
def _():
    src = ('try {\n    fs.read("/definitely/not/here-xyz.txt")\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E303")


@test("v0.2 checker: fs.write arity E102")
def _():
    errs = check_source('fs.write("a")\n')
    assert any(e["code"] == "E102" for e in errs), errs


@test("v0.2 interp: http.get")
def _():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"misa-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        src = f'print(http.get("http://127.0.0.1:{port}/"))\n'
        eq(run_src(src).strip(), "misa-ok")
    finally:
        srv.shutdown()


@test("v0.2 interp: http.get failure E304")
def _():
    src = ('try {\n    http.get("http://127.0.0.1:1/")\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E304")


# ---------------------------------------------------------------- v0.2 stdlib2: JSON

@test("stdlib2: json.parse basics")
def _():
    src = ('let v = json.parse("{{\\"a\\": 1, \\"b\\": true, \\"c\\": null}")\n'
           'print(v.a)\nprint(v.b)\nprint(v.c)\n')
    eq(run_src(src).strip().splitlines(), ["1", "true", "nil"])


@test("stdlib2: json.parse number kinds")
def _():
    src = ('let v = json.parse("[42, 4.0, 1e3, -7, 0]")\n'
           'print(v[0])\nprint(v[1])\nprint(v[2])\nprint(v[3])\n')
    eq(run_src(src).strip().splitlines(), ["42", "4.0", "1000.0", "-7"])


@test("stdlib2: json.parse nested + unicode")
def _():
    # Misa string: {{ -> { . JSON text uses a literal é (Misa has no \u).
    src = ('let v = json.parse('
           '"{{\\"u\\": \\"h\u00e9llo\\", '
           '\\"n\\": {{\\"x\\": [1, {{\\"y\\": 2}]}}")\n'
           'print(v.u)\nprint(v.n.x[1].y)\n')
    eq(run_src(src).strip().splitlines(), ["héllo", "2"])


@test("stdlib2: json.parse empty object/array")
def _():
    src = ('print(json.stringify(json.parse("{{}")))\n'
           'print(json.stringify(json.parse("[]")))\n')
    eq(run_src(src).strip().splitlines(), ["{}", "[]"])


@test("stdlib2: json.parse errors E300")
def _():
    for bad in ['"{{"', '"[1,]"', '"{{\\"a\\":}}"', '"tru"', '""']:
        src = (f'try {{\n    json.parse({bad})\n'
               '} catch e {\n    print(e.code)\n}\n')
        eq(run_src(src).strip(), "E300", bad)


@test("stdlib2: json.stringify canonical")
def _():
    src = ('print(json.stringify({"b": [1, "x"], "a": true}))\n'
           'print(json.stringify(nil))\n'
           'print(json.stringify("hi"))\n')
    eq(run_src(src).strip().splitlines(),
       ['{"b":[1,"x"],"a":true}', 'null', '"hi"'])


@test("stdlib2: json.stringify escapes")
def _():
    # Misa strings support \" \\ \n etc.; str.chr(1) for \u0001.
    src = ('print(json.stringify("a\\"b\\\\c\\nd" + str.chr(1)))\n')
    eq(run_src(src).strip(), '"a\\"b\\\\c\\nd\\u0001"')


@test("stdlib2: json.stringify rejects non-JSON E300")
def _():
    src = ('fn f() -> Int { return 1 }\n'
           'try {\n    print(json.stringify(f))\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E300")


@test("stdlib2: json round-trip")
def _():
    src = ('let v = {"n": 42, "f": 2.5, "s": "x", "l": [1, nil],\n'
           '         "o": {"k": false}}\n'
           'let s = json.stringify(v)\n'
           'let w = json.parse(s)\n'
           'print(w.n)\nprint(w.o.k)\nprint(json.stringify(w) == s)\n')
    eq(run_src(src).strip().splitlines(), ["42", "false", "true"])


# ---------------------------------------------------------------- v0.2 stdlib2: HTTP POST

@test("stdlib2: http.post + post_json")
def _():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            resp = (b'{"got":' + json.dumps(body.decode()).encode()
                    + b',"ct":' + json.dumps(ctype).encode() + b'}')
            self.send_response(200)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        src = (f'print(http.post("http://127.0.0.1:{port}/", "hi"))\n'
               f'print(http.post_json("http://127.0.0.1:{port}/", '
               '{"a": 1}))\n')
        lines = run_src(src).strip().splitlines()
        eq(json.loads(lines[0]),
           {"got": "hi", "ct": "text/plain"})
        eq(json.loads(lines[1]),
           {"got": '{"a":1}', "ct": "application/json"})
    finally:
        srv.shutdown()


@test("stdlib2: http.post failure E304")
def _():
    src = ('try {\n    http.post("http://127.0.0.1:1/", "x")\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E304")


# ---------------------------------------------------------------- v0.2 stdlib2: time

@test("stdlib2: time.now / now_ms")
def _():
    src = 'print(time.now() > 1700000000)\nprint(time.now_ms() > 1700000000000)\n'
    eq(run_src(src).strip().splitlines(), ["true", "true"])


@test("stdlib2: time.format / parse round-trip")
def _():
    src = ('print(time.format(0, "%Y-%m-%d"))\n'
           'print(time.parse("2026-10-06", "%Y-%m-%d"))\n'
           'let e = time.parse("2026-10-06 12:30:45", "%Y-%m-%d %H:%M:%S")\n'
           'print(time.format(e, "%H:%M"))\n')
    eq(run_src(src).strip().splitlines(),
       ["1970-01-01", "1791244800", "12:30"])


@test("stdlib2: time.parse failure E300")
def _():
    src = ('try {\n    time.parse("not-a-date", "%Y-%m-%d")\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E300")


# ---------------------------------------------------------------- v0.2 stdlib2: math

@test("stdlib2: math basics")
def _():
    src = ('print(math.sqrt(2.0))\n'
           'print(math.pow(2.0, 10.0))\n'
           'print(math.abs(0 - 5))\n'
           'print(math.abs(2.5))\n'
           'print(math.floor(2.7))\n'
           'print(math.ceil(2.2))\n'
           'print(math.round(2.5))\n'
           'print(math.round(0 - 2.5))\n'
           'print(math.floor(0 - 2.2))\n')
    eq(run_src(src).strip().splitlines(),
       ["1.4142135623730951", "1024.0", "5", "2.5",
        "2", "3", "3", "-3", "-3"])


@test("stdlib2: math.sqrt negative E300")
def _():
    src = ('try {\n    math.sqrt(0 - 1.0)\n'
           '} catch e {\n    print(e.code)\n}\n')
    eq(run_src(src).strip(), "E300")


# ---------------------------------------------------------------- v0.2: formatter

@test("v0.2 fmt: new constructs canonical")
def _():
    src = ('import "u.misa"\nfn f(a:Int)->Int{return a}\n'
           'try{print("x")}catch e{print(e.code)}\n'
           'let m:Map<Text,Int>={"a":1}\n')
    out = format_source(src, "f.misa")
    assert 'import "u.misa"' in out, out
    assert "fn f(a: Int) -> Int {" in out, out
    assert "} catch e {" in out, out
    assert 'let m: Map<Text, Int> = { "a": 1 }' in out, out
    eq(format_source(out, "f.misa"), out)


# ---------------------------------------------------------------- Phase 5a: first-class functions

@test("5a parser: function type")
def _():
    p = parse_source('let f: (Int, Text) -> Bool = g\n', "f.misa")
    te = p.items[0].ann
    assert te.is_func, te
    assert [t.name for t in te.func_params] == ["Int", "Text"]
    assert te.func_ret.name == "Bool"


@test("5a parser: thunk type and nested function type")
def _():
    p = parse_source('let f: () -> Void = g\n', "f.misa")
    te = p.items[0].ann
    assert te.is_func and te.func_params == []
    assert te.func_ret.name == "Void"
    p2 = parse_source('let f: ((Int) -> Int) -> Bool = g\n', "f.misa")
    te2 = p2.items[0].ann
    assert te2.is_func and te2.func_params[0].is_func


@test("5a parser: typed lambda params")
def _():
    p = parse_source('let f = |x: Int, y| x\n', "f.misa")
    lam = p.items[0].value
    assert isinstance(lam, last.Lambda)
    assert lam.params[0][0] == "x" and lam.params[0][1].name == "Int"
    assert lam.params[1][0] == "y" and lam.params[1][1] is None


@test("5a checker: fn as value has function type")
def _():
    src = ('fn double(x: Int) -> Int {\n    return x * 2\n}\n'
           'let g = double\n'
           'let h: (Int) -> Int = g\n')
    assert check_source(src) == [], check_source(src)


@test("5a checker: fn value type mismatch E200")
def _():
    src = ('fn foo() -> Int {\n    return 1\n}\n'
           'let g: (Text) -> Int = foo\n')
    errs = check_source(src)
    assert any(e["code"] == "E200" for e in errs), errs


@test("5a checker: untyped lambda without expected type E213")
def _():
    errs = check_source('let f = |x| x + 1\n')
    assert any(e["code"] == "E213" for e in errs), errs


@test("5a checker: builtin as value E214")
def _():
    for b in ("print", "str", "len", "fs.read", "str.slice"):
        errs = check_source(f'let f = {b}\n')
        assert any(e["code"] == "E214" for e in errs), (b, errs)


@test("5a checker: calling non-function E215")
def _():
    errs = check_source('let x = 5\nx(1)\n')
    assert any(e["code"] == "E215" for e in errs), errs


@test("5a checker: arity through variable E102")
def _():
    src = 'let f: (Int) -> Int = |x: Int| x\nf(1, 2)\n'
    errs = check_source(src)
    assert any(e["code"] == "E102" for e in errs), errs


@test("5a checker: arg type through variable E211")
def _():
    src = 'let f: (Int) -> Int = |x: Int| x\nf("s")\n'
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("5a checker: lambda arg infers from fn param")
def _():
    src = ('fn apply(f: (Int) -> Int, v: Int) -> Int {\n'
           '    return f(v)\n}\n'
           'print(apply(|x| x + 1, 10))\n')
    assert check_source(src) == [], check_source(src)


@test("5a checker: function subtyping is structural")
def _():
    # (Float) -> Int is assignable to (Int) -> Float:
    # params contravariant (Int arg usable where Float expected),
    # return covariant (Int usable where Float expected).
    src = ('let g: (Float) -> Int = |x: Float| 1\n'
           'let h: (Int) -> Float = g\n')
    assert check_source(src) == [], check_source(src)
    # ...but not the other way: (Int) -> Float cannot take a Float.
    src2 = ('let g: (Int) -> Float = |x: Int| 1.5\n'
            'let h: (Float) -> Float = g\n')
    errs = check_source(src2)
    assert any(e["code"] == "E200" for e in errs), errs
    # arity mismatch is not assignable
    src3 = ('fn foo() -> Int {\n    return 1\n}\n'
            'let g: (Int) -> Int = foo\n')
    errs = check_source(src3)
    assert any(e["code"] == "E200" for e in errs), errs


@test("5a interp: fn value call and higher-order")
def _():
    src = ('fn double(x: Int) -> Int {\n    return x * 2\n}\n'
           'fn apply(f: (Int) -> Int, v: Int) -> Int {\n'
           '    return f(v)\n}\n'
           'let g = double\n'
           'print(g(21))\n'
           'print(apply(double, 5))\n'
           'print(apply(|x: Int| x + 1, 10))\n')
    eq(run_src(src), "42\n10\n11\n")


@test("5a interp: closure captures defining scope")
def _():
    src = ('fn make_adder(n: Int) -> (Int) -> Int {\n'
           '    return |x: Int| x + n\n}\n'
           'let add5 = make_adder(5)\n'
           'print(add5(10))\n'
           'print(add5(100))\n')
    eq(run_src(src), "15\n105\n")


@test("5a interp: closure shares mutable state")
def _():
    src = ('var count = 0\n'
           'let inc = || {\n'
           '    count = count + 1\n'
           '    return count\n}\n'
           'print(inc())\n'
           'print(inc())\n'
           'count = 10\n'
           'print(inc())\n'
           'print(count)\n')
    eq(run_src(src), "1\n2\n11\n11\n")


@test("5a interp: nested closures and compose")
def _():
    src = ('fn compose(f: (Int) -> Int, g: (Int) -> Int) -> (Int) -> Int {\n'
           '    return |x: Int| f(g(x))\n}\n'
           'let f = compose(|x: Int| x + 1, |x: Int| x * 10)\n'
           'print(f(5))\n'
           'let mk = |a: Int| {\n'
           '    let inner = |b: Int| a + b\n'
           '    return inner\n}\n'
           'let add9 = mk(9)\n'
           'print(add9(1))\n')
    eq(run_src(src), "51\n10\n")


@test("5a interp: call result is callable")
def _():
    src = ('fn mk(a: Int) -> (Int) -> Int {\n'
           '    return |b: Int| a + b\n}\n'
           'print(mk(9)(1))\n')
    eq(run_src(src), "10\n")


@test("5a interp: untyped lambda infers from annotation")
def _():
    src = 'let h: (Int) -> Int = |x| x * 3\nprint(h(7))\n'
    eq(run_src(src), "21\n")


@test("5a interp: function renders as <function name>")
def _():
    src = 'fn foo() {\n}\nprint(foo)\n'
    eq(run_src(src), "<function foo>\n")


@test("5a formatter: function types and typed lambdas round-trip")
def _():
    src = ('let f: (Int, Text) -> Bool = g\n'
           'let h = |x: Int| x + 1\n')
    out = format_source(src, "f.misa")
    assert "(Int, Text) -> Bool" in out, out
    assert "|x: Int|" in out, out
    eq(format_source(out, "f.misa"), out)


# ---------------------------------------------------------------- Phase 5b: sum types + match

@test("5b parser: sum type decl")
def _():
    p = parse_source('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n', "f.misa")
    d = p.items[0]
    assert isinstance(d, last.TypeDecl) and d.is_sum, d
    assert [v.name for v in d.variants] == ["Circle", "Rect"]
    assert d.variants[0].fields[0][0] == "radius"
    assert d.variants[1].fields[1][0] == "h"


@test("5b parser: positional variant fields")
def _():
    p = parse_source('type Opt { None, Some(Int) }\n', "f.misa")
    d = p.items[0]
    assert d.is_sum
    assert d.variants[0].fields == []
    assert len(d.variants[1].fields) == 1
    assert d.variants[1].fields[0][1].name == "Int"


@test("5b parser: record type still parses")
def _():
    p = parse_source('type Point { x: Float, y: Float }\n', "f.misa")
    d = p.items[0]
    assert not d.is_sum and [f for f, _ in d.fields] == ["x", "y"]


@test("5b parser: match expression")
def _():
    src = ('let a = match s {\n'
           '  Shape.Circle(r) => r * 2.0,\n'
           '  _ => 0.0,\n}\n')
    p = parse_source(src, "f.misa")
    m = p.items[0].value
    assert isinstance(m, last.Match), m
    assert len(m.arms) == 2
    a0, a1 = m.arms
    assert (a0.type_name, a0.variant, a0.binds) == ("Shape", "Circle", ["r"])
    assert a1.wildcard


@test("5b checker: construction types as the sum")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let s: Shape = Shape.Circle(1.0)\n'
           'let t = Shape.Rect(2.0, 3.0)\n')
    assert check_source(src) == [], check_source(src)


@test("5b checker: bare none variant")
def _():
    src = 'type Opt { None, Some(Int) }\nlet n: Opt = Opt.None\n'
    assert check_source(src) == [], check_source(src)


@test("5b checker: payload type mismatch E211")
def _():
    src = ('type Opt { None, Some(Int) }\n'
           'let x = Opt.Some("s")\n')
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("5b checker: arity mismatch E102")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let x = Shape.Rect(1.0)\n')
    errs = check_source(src)
    assert any(e["code"] == "E102" for e in errs), errs


@test("5b checker: unknown variant E218")
def _():
    src = 'type Opt { None, Some(Int) }\nlet x = Opt.Bogus\n'
    errs = check_source(src)
    assert any(e["code"] == "E218" for e in errs), errs


@test("5b checker: bare payload variant E218")
def _():
    src = 'type Opt { None, Some(Int) }\nlet x = Opt.Some\n'
    errs = check_source(src)
    assert any(e["code"] == "E218" for e in errs), errs


@test("5b checker: non-exhaustive match E216")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let s = Shape.Circle(1.0)\n'
           'let a = match s {\n  Shape.Circle(r) => r,\n}\n')
    errs = check_source(src)
    assert any(e["code"] == "E216" for e in errs), errs


@test("5b checker: wildcard satisfies exhaustiveness")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let s = Shape.Circle(1.0)\n'
           'let a = match s {\n  Shape.Circle(r) => r,\n  _ => 0.0,\n}\n')
    assert check_source(src) == [], check_source(src)


@test("5b checker: arm type mismatch E217")
def _():
    src = ('type Opt { None, Some(Int) }\n'
           'let n = Opt.None\n'
           'let a = match n {\n  Opt.Some(x) => "s",\n  Opt.None => 0,\n}\n')
    errs = check_source(src)
    assert any(e["code"] == "E217" for e in errs), errs


@test("5b checker: match on non-sum E218")
def _():
    src = 'let x = 5\nlet a = match x { _ => 1 }\n'
    errs = check_source(src)
    assert any(e["code"] == "E218" for e in errs), errs


@test("5b checker: wrong pattern type E218")
def _():
    src = ('type A { X }\ntype B { Y }\n'
           'let a = A.X\n'
           'let r = match a { B.Y => 1, _ => 0 }\n')
    errs = check_source(src)
    assert any(e["code"] == "E218" for e in errs), errs


@test("5b checker: bind count mismatch E218")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let s = Shape.Rect(1.0, 2.0)\n'
           'let a = match s {\n  Shape.Rect(w) => w,\n  _ => 0.0,\n}\n')
    errs = check_source(src)
    assert any(e["code"] == "E218" for e in errs), errs


@test("5b checker: match binds payload types")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'fn area(s: Shape) -> Float {\n'
           '  return match s {\n'
           '    Shape.Circle(r) => r * 3.14,\n'
           '    Shape.Rect(w, h) => w * h,\n'
           '  }\n'
           '}\n')
    assert check_source(src) == [], check_source(src)


@test("5b interp: construction and match")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let s = Shape.Circle(1.0)\n'
           'print(match s {\n'
           '  Shape.Circle(r) => r * 2.0,\n'
           '  Shape.Rect(w, h) => w * h,\n})\n'
           'print(match Shape.Rect(2.0, 3.0) {\n'
           '  Shape.Circle(r) => r,\n'
           '  Shape.Rect(w, h) => w + h,\n})\n')
    eq(run_src(src), "2.0\n5.0\n")


@test("5b interp: none/some idiom with wildcard")
def _():
    src = ('type Opt { None, Some(Int) }\n'
           'fn or_zero(o: Opt) -> Int {\n'
           '  return match o {\n'
           '    Opt.Some(x) => x,\n'
           '    _ => 0,\n'
           '  }\n'
           '}\n'
           'print(or_zero(Opt.Some(41)))\n'
           'print(or_zero(Opt.None))\n')
    eq(run_src(src), "41\n0\n")


@test("5b interp: nested match")
def _():
    src = ('type AB { A, B }\n'
           'type XY { X, Y }\n'
           'let r = match AB.A {\n'
           '  AB.A => match XY.Y {\n'
           '    XY.X => "x",\n'
           '    XY.Y => "y",\n'
           '  },\n'
           '  AB.B => "b",\n'
           '}\n'
           'print(r)\n')
    eq(run_src(src), "y\n")


@test("5b interp: match as expression value")
def _():
    src = ('type Opt { None, Some(Int) }\n'
           'let o = Opt.Some(7)\n'
           'let doubled = match o { Opt.Some(x) => x * 2, _ => 0 }\n'
           'print(doubled)\n')
    eq(run_src(src), "14\n")


@test("5b interp: recursive sum type")
def _():
    src = ('type Tree { Leaf(v: Int), Node(l: Tree, r: Tree) }\n'
           'fn count(t: Tree) -> Int {\n'
           '  return match t {\n'
           '    Tree.Leaf(v) => 1,\n'
           '    Tree.Node(l, r) => count(l) + count(r),\n'
           '  }\n'
           '}\n'
           'let t = Tree.Node(Tree.Leaf(1), Tree.Leaf(2))\n'
           'print(count(t))\n')
    eq(run_src(src), "2\n")


@test("5b interp: sums in lists and records")
def _():
    src = ('type Opt { None, Some(Int) }\n'
           'type Wrap { fst: Opt }\n'
           'let xs = [Opt.Some(1), Opt.None, Opt.Some(3)]\n'
           'var total = 0\n'
           'for o in xs {\n'
           '  total = total + match o { Opt.Some(x) => x, _ => 0 }\n'
           '}\n'
           'print(total)\n'
           'let r: Wrap = { fst: Opt.Some(9) }\n'
           'print(match r.fst { Opt.Some(x) => x, _ => 0 })\n')
    eq(run_src(src), "4\n9\n")


@test("5b interp: variant payload holds a closure")
def _():
    src = ('type H { Fn(f: (Int) -> Int), Val(n: Int) }\n'
           'fn run(h: H) -> Int {\n'
           '  return match h {\n'
           '    H.Fn(f) => f(10),\n'
           '    H.Val(n) => n,\n'
           '  }\n'
           '}\n'
           'print(run(H.Fn(|x: Int| x * 3)))\n'
           'print(run(H.Val(5)))\n')
    eq(run_src(src), "30\n5\n")


@test("5b interp: sum value rendering")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'type Opt { None, Some(Int) }\n'
           'print(Shape.Circle(1.5))\n'
           'print(Opt.None)\n'
           'print(Opt.Some(2))\n')
    eq(run_src(src), "Shape.Circle(1.5)\nOpt.None\nOpt.Some(2)\n")


@test("5b formatter: sum types and match round-trip")
def _():
    src = ('type Shape {\n  Circle(radius: Float),\n  Rect(w: Float, h: Float),\n}\n'
           'let a = match s {\n  Shape.Circle(r) => r,\n  _ => 0.0,\n}\n')
    out = format_source(src, "f.misa")
    assert "Circle(radius: Float)" in out, out
    assert "Shape.Circle(r) => r" in out, out
    eq(format_source(out, "f.misa"), out)


# ---------------------------------------------------------------- Phase 5c: generics

@test("5c parser: generic fn decl")
def _():
    p = parse_source('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n',
                     "f.misa")
    d = p.items[0]
    assert isinstance(d, last.FnDecl), d
    eq(d.type_params, ["T"])
    eq(d.params[0][1].name, "T")
    assert d.params[0][1].is_list


@test("5c parser: multiple type params")
def _():
    p = parse_source('fn f<K, V>(k: K, v: V) -> Int {\n  return 1\n}\n',
                     "f.misa")
    eq(p.items[0].type_params, ["K", "V"])


@test("5c parser: generic type decl")
def _():
    p = parse_source('type Box<T> {\n  value: T,\n}\n', "f.misa")
    d = p.items[0]
    assert isinstance(d, last.TypeDecl), d
    eq(d.type_params, ["T"])
    eq(d.fields[0][1].name, "T")


@test("5c parser: type application in annotation")
def _():
    p = parse_source('let b: Box<Int> = Box<Int>(42)\n', "f.misa")
    ann = p.items[0].ann
    eq(ann.name, "Box")
    eq([a.name for a in ann.type_args], ["Int"])


@test("5c parser: explicit type args on call")
def _():
    p = parse_source('let b = first<Text>(["x"])\n', "f.misa")
    c = p.items[0].value
    assert isinstance(c, last.Call), c
    eq(c.name, "first")
    eq([a.name for a in c.type_args], ["Text"])


@test("5c parser: generic variant construction")
def _():
    p = parse_source('let e = Result<Int>.Ok(42)\n', "f.misa")
    c = p.items[0].value
    assert isinstance(c, last.Call), c
    eq(c.name, "Result.Ok")
    eq([a.name for a in c.type_args], ["Int"])


@test("5c parser: comparison not misparsed as generics")
def _():
    p = parse_source('let x = a < b\n', "f.misa")
    assert isinstance(p.items[0].value, last.BinOp)


@test("5c checker: inference binds T")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let a = first([1, 2, 3])\n'
           'let b: Int = a\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: explicit pinning")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let b = first<Text>(["x"])\n'
           'let c: Text = b\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: nested generics [[T]]")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let a = first([[1, 2], [3]])\n'
           'let b: [Int] = a\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: multiple type params")
def _():
    src = ('fn pair<K, V>(k: K, v: V) -> K {\n  return k\n}\n'
           'let a = pair(1, "x")\n'
           'let b: Int = a\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: generic type construction")
def _():
    src = ('type Box<T> {\n  value: T,\n}\n'
           'let b: Box<Int> = Box<Int>(42)\n'
           'let v: Int = b.value\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: generic record literal")
def _():
    src = ('type Box<T> {\n  value: T,\n}\n'
           'let b: Box<Int> = { value: 42 }\n'
           'let v: Int = b.value\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: Result<T> with match")
def _():
    src = ('type Result<T> { Ok(T), Err(Text) }\n'
           'let e: Result<Int> = Result.Ok(42)\n'
           'let x = match e {\n'
           '  Result.Ok(v) => v + 1,\n'
           '  Result.Err(_) => 0,\n}\n'
           'let y: Int = x\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: generic fn calling generic fn")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'fn second<T>(xs: [T]) -> T {\n  return first<T>(xs)\n}\n'
           'let a = second([1, 2])\n'
           'let b: Int = a\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: generic fn as value")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let g = first\n'
           'let a = g([1, 2])\n'
           'let b: Int = a\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: higher-order generic with lambda")
def _():
    src = ('fn map<T, K>(xs: [T], f: (T) -> K) -> [K] {\n'
           '  var out: [K] = []\n'
           '  for x in xs {\n    out = out + [f(x)]\n  }\n'
           '  return out\n}\n'
           'let r = map([1, 2], |x: Int| x * 2)\n'
           'let s: [Int] = r\n')
    assert check_source(src) == [], check_source(src)


@test("5c checker: E219 ambiguity")
def _():
    src = ('fn f<T, K>(x: T) -> K {\n  return x\n}\n'
           'let a = f(1)\n')
    errs = check_source(src)
    assert any(e["code"] == "E219" for e in errs), errs


@test("5c checker: E220 conflict")
def _():
    src = ('fn f<T>(x: T, y: T) -> T {\n  return x\n}\n'
           'let a = f(1, "s")\n')
    errs = check_source(src)
    assert any(e["code"] == "E220" for e in errs), errs


@test("5c checker: E221 type arity mismatch")
def _():
    src = ('type Box<T> {\n  value: T,\n}\n'
           'let b: Box<Int, Text> = Box<Int, Text>(1)\n')
    errs = check_source(src)
    assert any(e["code"] == "E221" for e in errs), errs


@test("5c checker: E221 fn type-arg arity")
def _():
    src = ('fn f<T>(x: T) -> T {\n  return x\n}\n'
           'let a = f<Int, Text>(1)\n')
    errs = check_source(src)
    assert any(e["code"] == "E221" for e in errs), errs


@test("5c checker: E222 unbound type variable")
def _():
    src = 'fn f<T>(x: T) -> K {\n  return 1\n}\n'
    errs = check_source(src)
    assert any(e["code"] == "E222" for e in errs), errs


@test("5c checker: E222 unbound in type decl")
def _():
    src = 'type Box<T> {\n  value: T,\n  other: K,\n}\n'
    errs = check_source(src)
    assert any(e["code"] == "E222" for e in errs), errs


@test("5c checker: E211 on substituted mismatch")
def _():
    # pinned T=Text, but the arg is [Int]
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let a = first<Text>([1, 2])\n')
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


@test("5c interp: generics end to end")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'type Box<T> {\n  value: T,\n}\n'
           'type Result<T> { Ok(T), Err(Text) }\n'
           'print(first([1, 2, 3]))\n'
           'print(first(["a", "b"]))\n'
           'let b = Box<Int>(41)\n'
           'print(b.value + 1)\n'
           'let e: Result<Int> = Result.Ok(41)\n'
           'print(match e { Result.Ok(v) => v, Result.Err(_) => 0 })\n')
    eq(run_src(src), "1\na\n42\n41\n")


@test("5c interp: generic fn as value")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'let g = first\n'
           'print(g([7, 8]))\n')
    eq(run_src(src), "7\n")





@test("5c formatter: generics round-trip")
def _():
    src = ('fn first<T>(xs: [T]) -> T {\n  return xs[0]\n}\n'
           'type Box<T> {\n  value: T,\n}\n'
           'let b = first<Text>(["x"])\n')
    out = format_source(src, "f.misa")
    assert "fn first<T>" in out, out
    assert "type Box<T>" in out, out
    assert 'first<Text>(["x"])' in out, out
    eq(format_source(out, "f.misa"), out)




@test("5c interp: recursive generic sum")
def _():
    src = ('type L<T> { Nil, Cons(T, L<T>) }\n'
           'fn len<T>(l: L<T>) -> Int {\n'
           '  return match l {\n'
           '    L.Nil => 0,\n'
           '    L.Cons(_, rest) => 1 + len<T>(rest),\n  }\n}\n'
           'let l: L<Int> = L.Cons(1, L.Cons(2, L.Nil))\n'
           'print(len(l))\n'
           'print(len<Text>(L.Cons("a", L.Nil)))\n')
    eq(run_src(src), "2\n1\n")


# ---------------------------------------------------------------- Phase 5: database (SQLite)

@test("db: open/exec/query/close roundtrip")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")\n'
           'print(db.exec(db, "INSERT INTO t (name) VALUES (?)", ["x"]))\n'
           'let rows = db.query(db, "SELECT id, name FROM t")\n'
           'print(len(rows))\n'
           'print(rows[0].name)\n'
           'db.close(db)\n'
           'print("closed")\n')
    eq(run_src(src), "1\n1\nx\nclosed\n")


@test("db: type mapping int/float/text/null")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE m (i INTEGER, f REAL, t TEXT, n TEXT)")\n'
           'db.exec(db, "INSERT INTO m VALUES (1, 2.5, \'hi\', NULL)")\n'
           'let r = db.query(db, "SELECT i, f, t, n FROM m")[0]\n'
           'print(r.i)\n'
           'print(r.f)\n'
           'print(r.t)\n'
           'print(r.n)\n'
           'db.close(db)\n')
    eq(run_src(src), "1\n2.5\nhi\nnil\n")


@test("db: exec returns rows changed; update/delete")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE u (name TEXT)")\n'
           'db.exec(db, "INSERT INTO u VALUES (?), (?), (?)", ["a", "b", "c"])\n'
           'print(db.exec(db, "UPDATE u SET name = ? WHERE name = ?", ["z", "a"]))\n'
           'print(db.exec(db, "DELETE FROM u WHERE name = ?", ["b"]))\n'
           'print(len(db.query(db, "SELECT name FROM u")))\n'
           'db.close(db)\n')
    eq(run_src(src), "1\n1\n2\n")


@test("db: sql error throws E303")
def _():
    src = ('let db = db.open(":memory:")\n'
           'try {\n    db.exec(db, "CREATE TABL broken")\n'
           '} catch e {\n    print(e.code)\n}\n'
           'db.close(db)\n')
    eq(run_src(src), "E303\n")


@test("db: query on missing table throws E303")
def _():
    src = ('let db = db.open(":memory:")\n'
           'try {\n    db.query(db, "SELECT * FROM nope")\n'
           '} catch e {\n    print(e.code)\n}\n'
           'db.close(db)\n')
    eq(run_src(src), "E303\n")


@test("db: checker table decl + typed query")
def _():
    src = ('table users {\n'
           '  id: Int @primary,\n'
           '  name: Text,\n'
           '}\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id, name FROM users")\n'
           'for r in rows {\n'
           '  print(r.name)\n'
           '}\n')
    eq(check_source(src), [])


@test("db: checker E224 unknown column")
def _():
    src = ('table users {\n'
           '  id: Int @primary,\n'
           '  name: Text,\n'
           '}\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id, nam FROM users")\n')
    errs = check_source(src)
    assert any(e["code"] == "E224" and "nam" in e["message"]
               for e in errs), errs


@test("db: checker E224 unknown column in WHERE")
def _():
    src = ('table users {\n'
           '  id: Int @primary,\n'
           '  name: Text,\n'
           '}\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id FROM users WHERE age > 3")\n')
    errs = check_source(src)
    assert any(e["code"] == "E224" and "age" in e["message"]
               for e in errs), errs


@test("db2: JOIN with aliases type-checks")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'table orders { id: Int @primary, user_id: Int, total: Float }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT u.name, o.total FROM users u '
           'JOIN orders o ON o.user_id = u.id WHERE o.total > 1.5")\n')
    eq(check_source(src), [])


@test("db2: E232 unknown table alias")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT x.name FROM users u")\n')
    errs = check_source(src)
    assert any(e["code"] == "E232" and "x" in e["message"]
               for e in errs), errs


@test("db2: E232 ambiguous column across JOIN")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'table orders { id: Int @primary, user_id: Int }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id FROM users u '
           'JOIN orders o ON o.user_id = u.id")\n')
    errs = check_source(src)
    assert any(e["code"] == "E232" and "ambiguous" in e["message"]
               for e in errs), errs


@test("db2: E231 WHERE type mismatch")
def _():
    src = ('table users { id: Int @primary, age: Int }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id FROM users '
           'WHERE age = \'old\'")\n')
    errs = check_source(src)
    assert any(e["code"] == "E231" for e in errs), errs


@test("db2: E231 non-Bool WHERE")
def _():
    src = ('table users { id: Int @primary, age: Int }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id FROM users WHERE age")\n')
    errs = check_source(src)
    assert any(e["code"] == "E231" and "WHERE" in e["message"]
               for e in errs), errs


@test("db2: E231 AND on non-Bool")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT id FROM users '
           'WHERE name AND id > 1")\n')
    errs = check_source(src)
    assert any(e["code"] == "E231" for e in errs), errs


@test("db2: E233 placeholder/arg count mismatch")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT name FROM users WHERE id = ?", '
           '[1, 2])\n')
    errs = check_source(src)
    assert any(e["code"] == "E233" and "1 placeholder" in e["message"]
               for e in errs), errs


@test("db2: E233 missing bind args")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT name FROM users WHERE id = ?")\n')
    errs = check_source(src)
    assert any(e["code"] == "E233" for e in errs), errs


@test("db2: E224 INSERT unknown column")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'db.exec(db, "INSERT INTO users (nam) VALUES (?)", ["x"])\n')
    errs = check_source(src)
    assert any(e["code"] == "E224" and "nam" in e["message"]
               for e in errs), errs


@test("db2: E233 INSERT arity mismatch")
def _():
    src = ('table users { id: Int @primary, name: Text, age: Int }\n'
           'let db = db.open(":memory:")\n'
           'db.exec(db, "INSERT INTO users (name, age) VALUES (?)", ["x"])\n')
    errs = check_source(src)
    assert any(e["code"] == "E233" for e in errs), errs


@test("db2: E224 UPDATE unknown column")
def _():
    src = ('table users { id: Int @primary, name: Text }\n'
           'let db = db.open(":memory:")\n'
           'db.exec(db, "UPDATE users SET nam = ? WHERE id = ?", ["x", 1])\n')
    errs = check_source(src)
    assert any(e["code"] == "E224" and "nam" in e["message"]
               for e in errs), errs


@test("db2: E233 exec_many row arity")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec_many(db, "INSERT INTO t VALUES (?, ?)", '
           '[[1, "a"], [2]])\n')
    errs = check_source(src)
    assert any(e["code"] == "E233" and "row 1" in e["message"]
               for e in errs), errs


@test("db2: aggregates infer Int/Float")
def _():
    src = ('table orders { id: Int @primary, total: Float }\n'
           'let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT COUNT(*) AS n, AVG(total) AS a '
           'FROM orders")\n'
           'for r in rows { print(str(r.n) + "/" + str(r.a)) }\n')
    eq(check_source(src), [])


@test("db2: nested transactions via savepoints")
def _():
    src = ('fn m() -> Int throws E303 {\n'
           ' let db = db.open(":memory:")\n'
           ' db.exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")\n'
           ' db.begin(db)\n'
           ' db.exec(db, "INSERT INTO t (v) VALUES (?)", ["outer"])\n'
           ' db.begin(db)\n'
           ' db.exec(db, "INSERT INTO t (v) VALUES (?)", ["inner"])\n'
           ' db.rollback(db)\n'
           ' db.commit(db)\n'
           ' let rows = db.query(db, "SELECT v FROM t")\n'
           ' print(str(len(rows)))\n'
           ' db.close(db)\n'
           ' return 0\n}\n'
           'm()\n')
    eq(run_src(src), "1\n")


@test("db2: db.migrate applies pending only")
def _():
    src = ('fn m() -> Int throws E303 {\n'
           ' let db = db.open(":memory:")\n'
           ' let a = db.migrate(db, [\n'
           '   {version: 1, sql: "CREATE TABLE t (id INTEGER)"},\n'
           '   {version: 2, sql: "ALTER TABLE t ADD COLUMN v TEXT"},\n'
           ' ])\n'
           ' print(str(a))\n'
           ' let b = db.migrate(db, [\n'
           '   {version: 1, sql: "CREATE TABLE t (id INTEGER)"},\n'
           '   {version: 2, sql: "ALTER TABLE t ADD COLUMN v TEXT"},\n'
           '   {version: 3, sql: "CREATE INDEX i ON t(id)"},\n'
           ' ])\n'
           ' print(str(b))\n'
           ' db.close(db)\n'
           ' return 0\n}\n'
           'm()\n')
    eq(run_src(src), "2\n1\n")


@test("db2: db.explain returns plan rows")
def _():
    src = ('fn m() -> Int throws E303 {\n'
           ' let db = db.open(":memory:")\n'
           ' db.exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")\n'
           ' let plan = db.explain(db, "SELECT v FROM t WHERE id = ?", [1])\n'
           ' print(str(len(plan)))\n'
           ' db.close(db)\n'
           ' return 0\n}\n'
           'm()\n')
    eq(run_src(src), "1\n")


@test("db: exec_many bulk insert")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE t (id INT, name TEXT)")\n'
           'let rows = [[1, "a"], [2, "b"], [3, "c"]]\n'
           'print(db.exec_many(db, "INSERT INTO t VALUES (?, ?)", rows))\n'
           'print(len(db.query(db, "SELECT * FROM t")))\n'
           'db.close(db)\n')
    eq(run_src(src), "3\n3\n")


@test("db: begin/commit/rollback")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE t (id INT)")\n'
           'db.begin(db)\n'
           'db.exec(db, "INSERT INTO t VALUES (1)")\n'
           'db.rollback(db)\n'
           'print(len(db.query(db, "SELECT * FROM t")))\n'
           'db.begin(db)\n'
           'db.exec(db, "INSERT INTO t VALUES (2)")\n'
           'db.commit(db)\n'
           'print(len(db.query(db, "SELECT * FROM t")))\n'
           'db.close(db)\n')
    eq(run_src(src), "0\n1\n")


@test("builder: basic append/build")
def _():
    src = ('let b = builder()\n'
           'b.append("hello")\n'
           'b.append(" ")\n'
           'b.append("world")\n'
           'print(b.build())\n')
    _, buf = run_src_checked(src)
    eq(buf.getvalue(), "hello world\n")


@test("builder: append non-text (int)")
def _():
    src = ('let b = builder()\n'
           'b.append(42)\n'
           'b.append("!")\n'
           'print(b.build())\n')
    _, buf = run_src_checked(src)
    eq(buf.getvalue(), "42!\n")


@test("builder: empty build")
def _():
    src = ('let b = builder()\n'
           'print(b.build())\n'
           'print(len(b.build()))\n')
    _, buf = run_src_checked(src)
    eq(buf.getvalue(), "\n0\n")


@test("unbox: int arithmetic wrapping (P-2)")
def _():
    # 64-bit two's-complement wrapping must be preserved by unboxing.
    src = ('let max = 9223372036854775807\n'
           'print(max + 1)\n'
           'print(-max - 2)\n'
           'let x: Int = 100\n'
           'let y: Int = 200\n'
           'print(x * y + 5)\n'
           'print((x < y) == true)\n')
    eq(run_src(src), "-9223372036854775808\n9223372036854775807\n20005\ntrue\n")


@test("unbox: fib via int specialization (P-2)")
def _():
    src = ('fn fib(n: Int) -> Int {\n'
           '  if n < 2 { return n }\n'
           '  return fib(n - 1) + fib(n - 2)\n'
           '}\n'
           'print(fib(20))\n')
    eq(run_src(src), "6765\n")


@test("semantics: Int/Int is truncating division, Int%Int is srem")
def _():
    # 2026-10-06: the interpreter did float `/` and floored `%` while the
    # checker typed Int/Int->Int and AOT emitted sdiv/srem -- a three-way
    # divergence the debugger caught. All backends now agree: truncation
    # toward zero (C99), remainder takes the sign of the dividend.
    src = ('print(7 / 2)\n'        # 3, not 3.5
           'print(-7 / 2)\n'       # -3 (trunc, not floor)
           'print(7 / -2)\n'       # -3
           'print(-7 / -2)\n'      # 3
           'print(7 % 3)\n'        # 1
           'print(-7 % 3)\n'       # -1 (sign of dividend, not divisor)
           'print(7 % -3)\n'       # 1
           'print(7.0 / 2)\n'      # 3.5 (float stays float)
           'print(7 / 2.0)\n')     # 3.5
    eq(run_src(src),
       "3\n-3\n-3\n3\n1\n-1\n1\n3.5\n3.5\n")
    raises_code(lambda: run_src("print(1 / 0)\n"), "E300")
    raises_code(lambda: run_src("print(1 % 0)\n"), "E300")


@test("db: checker query on undeclared table is dynamic (no error)")
def _():
    src = ('let db = db.open(":memory:")\n'
           'let rows = db.query(db, "SELECT a FROM whatever")\n')
    eq(check_source(src), [])


@test("db: checker db.exec arity E102")
def _():
    errs = check_source('let db = db.open(":memory:")\n'
                        'db.exec(db)\n')
    assert any(e["code"] == "E102" for e in errs), errs


@test("db: checker db.query arg type E211")
def _():
    errs = check_source('let db = db.open(":memory:")\n'
                        'db.query(db, 42)\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("db: checker duplicate table E210")
def _():
    errs = check_source('table t {\n  a: Int,\n}\n'
                        'table t {\n  b: Int,\n}\n')
    assert any(e["code"] == "E210" for e in errs), errs


@test("db: parser rejects unknown table attribute")
def _():
    from misa.parser import ParseError
    try:
        parse_source('table t {\n  a: Int @bogus,\n}\n')
    except ParseError as ex:
        assert "bogus" in str(ex), ex
        return
    raise AssertionError("expected ParseError for @bogus")


# ----------------------------------------------------------------------
# Phase 7: FFI — extern "lib" { fn ... }
# ----------------------------------------------------------------------

@test("ffi: extern libc strlen + sqrt via interpreter")
def _():
    src = ('extern "c" {\n'
           '  fn strlen(s: Text) -> Int,\n'
           '  fn sqrt(x: Float) -> Float,\n'
           '}\n'
           'print(strlen("hello"))\n'
           'print(sqrt(2.0))\n')
    out = run_src(src)
    assert out.strip().splitlines() == ["5", "1.4142135623730951"], out


@test("ffi: extern libm proves second-library linking")
def _():
    src = ('extern "m" {\n'
           '  fn cos(x: Float) -> Float,\n'
           '  fn pow(x: Float, y: Float) -> Float,\n'
           '}\n'
           'print(cos(0.0))\n'
           'print(pow(2.0, 10.0))\n')
    out = run_src(src)
    assert out.strip().splitlines() == ["1.0", "1024.0"], out


@test("ffi: Ptr round-trips through sqlite3 (opaque handle)")
def _():
    src = ('extern "sqlite3" {\n'
           '  fn sqlite3_open(name: Text, db: Ptr) -> Int,\n'
           '  fn sqlite3_close(db: Ptr) -> Int,\n'
           '}\n'
           'print("loaded")\n')
    # sqlite3_open needs a Ptr* out-param (unsupported in v1), so we
    # only prove the library loads and Ptr-typed symbols bind. A real
    # open/close round-trip would need pointer-to-pointer marshalling.
    out = run_src(src)
    assert out.strip() == "loaded", out


@test("ffi: Ptr values are opaque (<ptr>) and compare to nil")
def _():
    src = ('extern "c" {\n'
           '  fn malloc(n: Int) -> Ptr,\n'
           '  fn free(p: Ptr) -> Void,\n'
           '}\n'
           'let p = malloc(16)\n'
           'print(p)\n'
           'print(p == nil)\n'
           'free(p)\n'
           'let q: Ptr = nil\n'
           'print(q == nil)\n')
    out = run_src(src)
    lines = out.strip().splitlines()
    assert lines[0] == "<ptr>", lines
    assert lines[1] == "false", lines
    assert lines[2] == "true", lines


@test("ffi: missing library is E226, not a traceback")
def _():
    raises_code(lambda: run_src('extern "no-such-lib-xyz" {\n'
                                '  fn f() -> Int,\n'
                                '}\n'
                                'print(f())\n'), "E226")


@test("ffi: missing symbol is E226")
def _():
    raises_code(lambda: run_src('extern "c" {\n'
                                '  fn no_such_symbol_xyz() -> Int,\n'
                                '}\n'
                                'print(no_such_symbol_xyz())\n'), "E226")


@test("ffi: arity mismatch on extern call is E102")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn strlen(s: Text) -> Int,\n'
                        '}\n'
                        'print(strlen("a", "b"))\n')
    assert any(e["code"] == "E102" for e in errs), errs


@test("ffi: arg type mismatch on extern call is E211")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn strlen(s: Text) -> Int,\n'
                        '}\n'
                        'print(strlen(42))\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("ffi: non-FFI param type is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn f(xs: [Int]) -> Int,\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi: callback param type is now accepted")
def _():
    # Overtake Wave 2: `(Int) -> Int` params are C callbacks now.
    errs = check_source('extern "c" {\n'
                        '  fn f(cb: (Int) -> Int) -> Int,\n'
                        '}\n')
    assert errs == [], errs


@test("ffi: callback as return type is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn f(x: Int) -> (Int) -> Int,\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi: generic extern fn is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn f<T>(x: T) -> T,\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi: extern name colliding with Misa fn is E210")
def _():
    errs = check_source('fn strlen(s: Text) -> Int {\n'
                        '  return 0\n'
                        '}\n'
                        'extern "c" {\n'
                        '  fn strlen(s: Text) -> Int,\n'
                        '}\n')
    assert any(e["code"] == "E210" for e in errs), errs


# ----------------------------------------------------------------------
# Overtake Wave 2: FFI depth — structs by value, callbacks, varargs
# ----------------------------------------------------------------------

def _ffi_helpers_so():
    """Compile tests/ffi_helpers.c to /tmp/misa_ffi_helpers.so (once)."""
    import subprocess
    so = "/tmp/misa_ffi_helpers.so"
    src = os.path.join(ROOT, "tests", "ffi_helpers.c")
    if not os.path.exists(so) or \
            os.path.getmtime(src) > os.path.getmtime(so):
        r = subprocess.run(["cc", "-shared", "-fPIC", "-O2",
                            "-o", so, src],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    return so


@test("ffi2: struct decl + literal + field read (interp)")
def _():
    src = ('extern "c" {\n'
           '  struct Point { x: Float, y: Float },\n'
           '  fn sqrt(x: Float) -> Float,\n'
           '}\n'
           'let p = Point { x: 3.0, y: 4.0 }\n'
           'print(p.x)\n'
           'print(p.y)\n'
           'print(sqrt(p.x * p.x + p.y * p.y))\n')
    out = run_src(src)
    assert out.strip().splitlines() == ["3.0", "4.0", "5.0"], out


@test("ffi2: struct by value round-trip through C helper")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  struct Point { x: Int, y: Int },\n'
           '  fn ffi_scale_point(p: Point, s: Float) -> Point,\n'
           '}\n'
           'let p = Point { x: 3, y: 4 }\n'
           'let q = ffi_scale_point(p, 2.5)\n'
           'print("{q.x} {q.y}")\n')
    out = run_src(src)
    assert out.strip() == "7 10", out


@test("ffi2: struct with mixed field types")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  struct Mixed { id: Int, score: Float, flags: Int, tag: Ptr },\n'
           '  fn ffi_make_mixed(id: Int, score: Float, flags: Int, tag: Ptr) -> Mixed,\n'
           '  fn ffi_mixed_id(m: Mixed) -> Int,\n'
           '}\n'
           'let m = ffi_make_mixed(7, 2.5, 1, malloc(8))\n'
           'print(m.id)\n'
           'print(m.score)\n'
           'print(m.flags)\n'
           'print(ffi_mixed_id(m))\n')
    # malloc is not declared here; use a null-ish Ptr via helper-free path:
    src = (f'extern "{so}" {{\n'
           '  struct Mixed { id: Int, score: Float, flags: Int, tag: Ptr },\n'
           '  fn ffi_make_mixed(id: Int, score: Float, flags: Int, tag: Ptr) -> Mixed,\n'
           '  fn ffi_mixed_id(m: Mixed) -> Int,\n'
           '}\n'
           'extern "c" {\n'
           '  fn malloc(size: Int) -> Ptr,\n'
           '}\n'
           'let m = ffi_make_mixed(7, 2.5, 3, malloc(8))\n'
           'print(m.id)\n'
           'print(m.score)\n'
           'print(m.flags)\n'
           'print(ffi_mixed_id(m))\n')
    out = run_src(src)
    assert out.strip().splitlines() == ["7", "2.5", "3", "7"], out


@test("ffi2: qsort with a Misa comparator (interp)")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  fn ffi_i64_set(base: Ptr, i: Int, v: Int) -> Void,\n'
           '  fn ffi_i64_get(base: Ptr, i: Int) -> Int,\n'
           '}\n'
           'extern "c" {\n'
           '  fn qsort(base: Ptr, n: Int, size: Int, cmp: (Ptr, Ptr) -> Int) -> Void,\n'
           '  fn malloc(size: Int) -> Ptr,\n'
           '  fn free(p: Ptr) -> Void,\n'
           '}\n'
           'let cmp = |a: Ptr, b: Ptr| {\n'
           '  let x = ffi_i64_get(a, 0)\n'
           '  let y = ffi_i64_get(b, 0)\n'
           '  if x < y { return -1 }\n'
           '  if x > y { return 1 }\n'
           '  return 0\n'
           '}\n'
           'let base = malloc(5 * 8)\n'
           'ffi_i64_set(base, 0, 30)\n'
           'ffi_i64_set(base, 1, 10)\n'
           'ffi_i64_set(base, 2, 50)\n'
           'ffi_i64_set(base, 3, 20)\n'
           'ffi_i64_set(base, 4, 40)\n'
           'qsort(base, 5, 8, cmp)\n'
           'var out = ""\n'
           'var i = 0\n'
           'while i < 5 {\n'
           '  out = out + "{ffi_i64_get(base, i)} "\n'
           '  i = i + 1\n'
           '}\n'
           'print(out)\n'
           'free(base)\n')
    out = run_src(src)
    assert out.strip() == "10 20 30 40 50", out


@test("ffi2: varargs printf (interp)")
def _():
    # Note: C printf writes to fd 1 directly, bypassing run_src's
    # capture — assert the return value (chars written) instead.
    # "42 2.500000 hi\n" is 15 chars: proves the format string and
    # the Int/Float/Text varargs all crossed correctly.
    src = ('extern "c" {\n'
           '  fn printf(fmt: Text, ...) -> Int,\n'
           '}\n'
           'let n = printf("%d %f %s\\n", 42, 2.5, "hi")\n'
           'print("ret={n}")\n')
    out = run_src(src)
    assert out.strip() == "ret=15", out


@test("ffi2: struct field type Text is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct Bad { s: Text },\n'
                        '  fn f(b: Bad) -> Int,\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi2: nested struct fields accepted (final wave)")
def _():
    # Final wave: nested aggregates are a feature (were E225 in Wave 2).
    errs = check_source('extern "c" {\n'
                        '  struct Inner { x: Int },\n'
                        '  struct Outer { inner: Inner, y: Float },\n'
                        '}\n')
    assert errs == [], errs
    # Cyclic nesting is still E225 (C requires complete by-value types).
    errs = check_source('extern "c" {\n'
                        '  struct A { b: B },\n'
                        '  struct B { a: A },\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi2: unions and bitfields accepted (final wave)")
def _():
    errs = check_source('extern "c" {\n'
                        '  union U { i: Int, f: Float },\n'
                        '  struct B { a: Int:3, b: Int },\n'
                        '}\n')
    assert errs == [], errs
    # Bad bitfield width is E225.
    errs = check_source('extern "c" {\n'
                        '  struct B { a: Int:0 },\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs
    # Bitfield on non-Int is E225.
    errs = check_source('extern "c" {\n'
                        '  struct B { a: Float:3 },\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


# ----------------------------------------------------------------------
# Overtake final wave: nested structs, unions, bitfields (runtime)
# ----------------------------------------------------------------------

@test("ffi3: nested struct literal + chained field access (interp)")
def _():
    src = ('extern "c" {\n'
           '  struct Point { x: Int, y: Int },\n'
           '  struct Outer { p: Point, n: Int },\n'
           '}\n'
           'let o = Outer { p: Point { x: 3, y: 4 }, n: 5 }\n'
           'print(o.p.x)\n'
           'print(o.p.y)\n'
           'print(o.n)\n')
    eq(run_src(src).strip().splitlines(), ["3", "4", "5"])


@test("ffi3: nested struct by value through C (interp)")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  struct Point { x: Int, y: Int },\n'
           '  struct Outer { tag: Bool, p: Point, n: Int },\n'
           '  fn ffi_outer_add(o: Outer, k: Int) -> Outer,\n'
           '  fn ffi_outer_tag(o: Outer) -> Int,\n'
           '}\n'
           'let o = Outer { tag: true, p: Point { x: 3, y: 4 }, n: 5 }\n'
           'let q = ffi_outer_add(o, 10)\n'
           'print("{q.p.x} {q.p.y} {q.n}")\n'
           'print(ffi_outer_tag(q))\n')
    # tag is a 4-byte Bool: the nested Point must start at offset 8
    # (padding); C adding 10 to every field proves the layout matches.
    eq(run_src(src).strip().splitlines(), ["13 14 15", "11"])


@test("ffi3: union write/read reinterpretation (interp)")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  union Value { i: Int, f: Float },\n'
           '  fn ffi_value_as_f(v: Value) -> Float,\n'
           '  fn ffi_value_from_i(i: Int) -> Value,\n'
           '}\n'
           'let v = Value { i: 42 }\n'
           'print(v.i)\n'
           # int64 bits of 1.5 reinterpreted as double:
           'let w = ffi_value_from_i(4609434218613702656)\n'
           'print(w.f)\n'
           'print(ffi_value_as_f(Value { f: 2.5 }))\n')
    eq(run_src(src).strip().splitlines(), ["42", "1.5", "2.5"])


@test("ffi3: union literal takes any field subset (interp)")
def _():
    src = ('extern "c" {\n'
           '  union Value { i: Int, f: Float },\n'
           '}\n'
           'let a = Value { i: 7 }\n'
           'let b = Value { f: 1.25 }\n'
           'print(a.i)\n'
           'print(b.f)\n')
    eq(run_src(src).strip().splitlines(), ["7", "1.25"])


@test("ffi3: bitfields pack per C rules, cross-checked via C (interp)")
def _():
    so = _ffi_helpers_so()
    src = (f'extern "{so}" {{\n'
           '  struct Bits { a: Int:3, b: Int:5, c: Int:20, d: Int, e: Int:9 },\n'
           '  fn ffi_bits_sum(b: Bits) -> Int,\n'
           '  fn ffi_bits_d(b: Bits) -> Int,\n'
           '  fn ffi_bits_e(b: Bits) -> Int,\n'
           '  fn ffi_bits_word0(b: Bits) -> Int,\n'
           '}\n'
           'let b = Bits { a: 5, b: 17, c: 703710, d: -7, e: 511 }\n'
           'print(b.a)\n'
           'print(b.b)\n'
           'print(b.c)\n'
           'print(b.d)\n'
           'print(b.e)\n'
           'print(ffi_bits_sum(b))\n'
           'print(ffi_bits_d(b))\n'
           'print(ffi_bits_e(b))\n'
           'print(ffi_bits_word0(b))\n')
    out = run_src(src).strip().splitlines()
    # C reads the Misa-built bitfields: packing must be identical.
    # word0 = 5 | (17<<3) | (703710<<8) = 180149901.
    eq(out, ["5", "17", "703710", "-7", "511",
             "703732", "-7", "511", "180149901"])


@test("ffi3: bitfield value out of range is E300 (interp)")
def _():
    src = ('extern "c" {\n'
           '  struct Bits { a: Int:3, b: Int:5 },\n'
           '}\n'
           'try {\n'
           '  let b = Bits { a: 8, b: 1 }\n'
           '} catch e {\n'
           '  print("got {e.code}")\n'
           '}\n'
           'try {\n'
           '  let b = Bits { a: -1, b: 1 }\n'
           '} catch e {\n'
           '  print("got {e.code}")\n'
           '}\n')
    eq(run_src(src).strip().splitlines(), ["got E300", "got E300"])


@test("ffi3: byte-exact C layout vs GCC")
def _():
    """Compile a C reference with offsetof/sizeof prints; Misa's
    ffi_layout must agree on every offset, size, and alignment.
    Layout bugs are silent killers — this is the differential check."""
    import subprocess, tempfile
    c_src = r'''
#include <stdio.h>
#include <stddef.h>
#include <stdint.h>
struct Point { int64_t x; int64_t y; };
struct Outer { int tag; struct Point p; int64_t n; };
struct Mixed { int64_t id; double score; int64_t flags; void *tag; };
union Value { int64_t i; double f; };
struct UWrap { int t; union Value v; int64_t n; };
struct Bits { unsigned long a:3; unsigned long b:5; unsigned long c:20;
              int64_t d; unsigned long e:9; };
union UBits { unsigned long a:3; int64_t i; };
struct NestBits { int t; struct Bits b; };
#define P(T, m) printf("%s.%s %zu\n", #T, #m, offsetof(struct T, m))
#define PU(T, m) printf("%s.%s %zu\n", #T, #m, offsetof(union T, m))
#define S(T) printf("%s.size %zu\n", #T, sizeof(struct T))
#define SU(T) printf("%s.size %zu\n", #T, sizeof(union T))
#define A(T) printf("%s.align %zu\n", #T, _Alignof(struct T))
#define AU(T) printf("%s.align %zu\n", #T, _Alignof(union T))
int main(void) {
    P(Point, x); P(Point, y); S(Point); A(Point);
    P(Outer, tag); P(Outer, p); P(Outer, n); S(Outer); A(Outer);
    P(Mixed, id); P(Mixed, score); P(Mixed, flags); P(Mixed, tag);
    S(Mixed); A(Mixed);
    PU(Value, i); PU(Value, f); SU(Value); AU(Value);
    P(UWrap, t); P(UWrap, v); P(UWrap, n); S(UWrap); A(UWrap);
    P(Bits, d); S(Bits); A(Bits);
    /* offsetof cannot apply to a bit-field; UBits.a is at 0 by union rule */
    PU(UBits, i); SU(UBits); AU(UBits);
    P(NestBits, t); P(NestBits, b); S(NestBits); A(NestBits);
    /* bit positions: find which bit index each field starts at */
    struct Bits bs = {0}; bs.a = 1;
    for (int i = 0; i < 64; i++)
        if (((unsigned long *)&bs)[0] & (1UL << i)) {
            printf("Bits.a.bit %d\n", i); break; }
    bs.a = 0; bs.b = 1;
    for (int i = 0; i < 64; i++)
        if (((unsigned long *)&bs)[0] & (1UL << i)) {
            printf("Bits.b.bit %d\n", i); break; }
    bs.b = 0; bs.c = 1;
    for (int i = 0; i < 64; i++)
        if (((unsigned long *)&bs)[0] & (1UL << i)) {
            printf("Bits.c.bit %d\n", i); break; }
    /* e starts a new unit after d (offset 16): bit 128 overall */
    struct Bits be = {0}; be.e = 1;
    unsigned long *w = (unsigned long *)&be;
    for (int k = 0; k < 4; k++)
        for (int i = 0; i < 64; i++)
            if (w[k] & (1UL << i)) { printf("Bits.e.bit %d\n", k*64+i); k = 9; break; }
    return 0;
}
'''
    with tempfile.TemporaryDirectory() as td:
        c_path = os.path.join(td, "ref.c")
        exe = os.path.join(td, "ref")
        open(c_path, "w").write(c_src)
        r = subprocess.run(["cc", "-O2", "-o", exe, c_path],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([exe], capture_output=True, text=True)
        assert r.returncode == 0
        gcc = {}
        for line in r.stdout.strip().splitlines():
            k, v = line.split()
            gcc[k] = int(v)

    from misa import ffi_layout as L
    from misa.ast import StructDecl, TypeExpr
    def te(n, w=None):
        return TypeExpr(name=n, bitfield_width=w)
    decls = {
        "Point": StructDecl(name="Point", kind="struct",
                            fields=[("x", te("Int")), ("y", te("Int"))]),
        "Outer": StructDecl(name="Outer", kind="struct",
                            fields=[("tag", te("Bool")),
                                    ("p", te("Point")), ("n", te("Int"))]),
        "Mixed": StructDecl(name="Mixed", kind="struct",
                            fields=[("id", te("Int")), ("score", te("Float")),
                                    ("flags", te("Int")), ("tag", te("Ptr"))]),
        "Value": StructDecl(name="Value", kind="union",
                            fields=[("i", te("Int")), ("f", te("Float"))]),
        "UWrap": StructDecl(name="UWrap", kind="struct",
                            fields=[("t", te("Bool")), ("v", te("Value")),
                                    ("n", te("Int"))]),
        "Bits": StructDecl(name="Bits", kind="struct",
                           fields=[("a", te("Int", 3)), ("b", te("Int", 5)),
                                   ("c", te("Int", 20)), ("d", te("Int")),
                                   ("e", te("Int", 9))]),
        "UBits": StructDecl(name="UBits", kind="union",
                            fields=[("a", te("Int", 3)), ("i", te("Int"))]),
        "NestBits": StructDecl(name="NestBits", kind="struct",
                               fields=[("t", te("Bool")),
                                       ("b", te("Bits"))]),
    }
    # sizes + alignments
    for name in decls:
        size = L.aggregate_size(name, decls)
        gk = f"{name}.size"
        assert size == gcc[gk], (name, size, gcc[gk])
    # field offsets (bit-fields have no offsetof; their unit offsets
    # are verified via bit positions below)
    for name in decls:
        for fname, (desc, off, _extra) in L.field_map(name,
                                                      decls).items():
            if desc == "bits":
                continue
            gk = f"{name}.{fname}"
            assert off == gcc[gk], (name, fname, off, gcc[gk])
    # UBits.a (union bitfield) is at offset 0 by the union rule
    assert L.field_map("UBits", decls)["a"][1] == 0
    # bit positions (little-endian: a at 0, b at 3, c at 8, e at 128)
    assert gcc["Bits.a.bit"] == 0, gcc
    assert gcc["Bits.b.bit"] == 3, gcc
    assert gcc["Bits.c.bit"] == 8, gcc
    assert gcc["Bits.e.bit"] == 128, gcc


@test("ffi2: duplicate struct name is E210")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int },\n'
                        '  struct P { y: Int },\n'
                        '}\n')
    assert any(e["code"] == "E210" for e in errs), errs


@test("ffi2: struct literal missing field is E211")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int, y: Int },\n'
                        '}\n'
                        'let p = P { x: 1 }\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("ffi2: struct literal unknown field is E101")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int },\n'
                        '}\n'
                        'let p = P { x: 1, z: 2 }\n')
    assert any(e["code"] == "E101" for e in errs), errs


@test("ffi2: struct literal wrong field type is E211")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int },\n'
                        '}\n'
                        'let p = P { x: "nope" }\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("ffi2: unknown struct literal is E101")
def _():
    errs = check_source('let p = Nope { x: 1 }\n')
    assert any(e["code"] == "E101" for e in errs), errs


@test("ffi2: unknown struct field read is E101")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int },\n'
                        '}\n'
                        'let p = P { x: 1 }\n'
                        'print(p.z)\n')
    assert any(e["code"] == "E101" for e in errs), errs


@test("ffi2: varargs too few args is E102")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn printf(fmt: Text, ...) -> Int,\n'
                        '}\n'
                        'printf()\n')
    assert any(e["code"] == "E102" for e in errs), errs


@test("ffi2: varargs bad tail type is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn printf(fmt: Text, ...) -> Int,\n'
                        '}\n'
                        'printf("%d\\n", [1, 2])\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi2: callback arg with wrong signature is E211")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn qsort(base: Ptr, n: Int, size: Int, cmp: (Ptr, Ptr) -> Int) -> Void,\n'
                        '}\n'
                        'let bad = |a: Ptr| { return 0 }\n'
                        'qsort(bad, 1, 8, bad)\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("ffi2: struct arg of wrong struct is E211")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct A { x: Int },\n'
                        '  struct B { x: Int },\n'
                        '  fn f(a: A) -> Int,\n'
                        '}\n'
                        'let b = B { x: 1 }\n'
                        'f(b)\n')
    assert any(e["code"] == "E211" for e in errs), errs


@test("ffi2: callback with struct param is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  struct P { x: Int },\n'
                        '  fn f(cb: (P) -> Int) -> Int,\n'
                        '}\n')
    assert any(e["code"] == "E225" for e in errs), errs


@test("ffi2: ... on a Misa fn is a parse error")
def _():
    from misa.parser import ParseError
    try:
        parse_source('fn f(fmt: Text, ...) -> Int {\n'
                     '  return 0\n'
                     '}\n')
    except ParseError as ex:
        assert "varargs" in str(ex), ex
        return
    raise AssertionError("expected ParseError for ... on Misa fn")


@test("ffi2: formatter round-trips structs/varargs/callbacks")
def _():
    from misa.fmt import format_source
    src = ('extern "c" {\n'
           '  struct Point { x: Float, y: Float },\n'
           '  fn qsort(base: Ptr, n: Int, size: Int, cmp: (Ptr, Ptr) -> Int) -> Void,\n'
           '  fn printf(fmt: Text, ...) -> Int,\n'
           '}\n'
           'let p = Point { x: 1.0, y: 2.0 }\n')
    out = format_source(src)
    assert "struct Point { x: Float, y: Float }" in out, out
    assert "cmp: (Ptr, Ptr) -> Int" in out, out
    assert "fn printf(fmt: Text, ...)" in out, out
    assert "Point { x: 1.0, y: 2.0 }" in out, out
    # idempotent
    assert format_source(out) == out


@test("ffi3: formatter round-trips unions/nested/bitfields")
def _():
    from misa.fmt import format_source
    src = ('extern "c" {\n'
           '  struct Point { x: Int, y: Int },\n'
           '  struct Outer { p: Point, n: Int },\n'
           '  union Value { i: Int, f: Float },\n'
           '  struct Bits { a: Int:3, b: Int:5 },\n'
           '}\n'
           'let o = Outer { p: Point { x: 1, y: 2 }, n: 3 }\n')
    out = format_source(src)
    assert "union Value { i: Int, f: Float }" in out, out
    assert "struct Bits { a: Int:3, b: Int:5 }" in out, out
    assert "struct Outer { p: Point, n: Int }" in out, out
    assert "Outer { p: Point { x: 1, y: 2 }, n: 3 }" in out, out
    assert format_source(out) == out


@test("str: slice/starts_with/split/trim edge cases incl. unicode")
def _():
    out = run_src(
        'print(str.slice("hello world", 0, 5))\n'
        'print(str.slice("hello", 2, 100))\n'
        'print(str.slice("hello", -5, 3))\n'
        'print("[" + str.slice("hello", 3, 2) + "]")\n'
        'let e = str.chr(233)\n'
        'let sm = str.chr(128512)\n'
        'print(str.slice("h" + e + "llo" + sm, 1, 4))\n'
        'print(str.starts_with("hello", "he"))\n'
        'print(str.starts_with("hello", ""))\n'
        'print(str.split("a,,b", ","))\n'
        'print("[" + str.trim(" \\t\\n hi \\r\\n") + "]")\n'
        'print("[" + str.trim(str.chr(160) + "hi" + str.chr(160)) + "]")\n'
    )
    eq(out,
       "hello\nllo\nhel\n[]\n\u00e9ll\n"
       "true\ntrue\n[a, , b]\n[hi]\n[hi]\n")


@test("str: ord/chr incl. non-BMP round-trip and error cases")
def _():
    out = run_src(
        'print(str.ord("A"))\n'
        'print(str.ord(str.chr(128512)))\n'
        'print(str.chr(65))\n'
        'print(str.chr(128512))\n'
        'print(str.chr(str.ord(str.chr(233))))\n'
    )
    eq(out, "65\n128512\nA\n\U0001F600\n\u00e9\n")
    raises_code(lambda: run_src('print(str.split("a", ""))\n'), "E300")
    raises_code(lambda: run_src('print(str.ord(""))\n'), "E300")
    raises_code(lambda: run_src('print(str.chr(-1))\n'), "E300")
    raises_code(lambda: run_src('print(str.chr(1114112))\n'), "E300")
    # lone surrogates are rejected cleanly (no traceback)
    raises_code(lambda: run_src('print(str.chr(55296))\n'), "E300")


@test("ffi: formatter round-trips extern blocks")
def _():
    from misa.fmt import format_source
    src = ('extern "c" {\n'
           '  fn strlen(s: Text) -> Int,\n'
           '  fn sqrt(x: Float) -> Float,\n'
           '}\n')
    out = format_source(src)
    assert 'extern "c"' in out and "fn strlen(s: Text) -> Int," in out, out
    # idempotent
    assert format_source(out) == out


# ---------------------------------------------------------------- C-1..C-8
# Critical gap fixes (audit 2026-10-06). Each test pins the fixed behavior.

@test("c1: int arithmetic wraps to 64-bit two's complement")
def _():
    out = run_src('''
print(9223372036854775807 + 1)
print(-9223372036854775808 - 1)
print(9223372036854775807 * 2)
print(-(-9223372036854775808))
''')
    eq(out, "-9223372036854775808\n9223372036854775807\n-2\n"
            "-9223372036854775808\n")


@test("c1: int() parsing wraps instead of clamping")
def _():
    out = run_src('''
print(int("99999999999999999999999"))
print(int("-9223372036854775809"))
print(int("  42  "))
''')
    # 99999999999999999999999 mod 2^64, as signed
    eq(out, "200376420520689663\n9223372036854775807\n42\n")


@test("c1: int() on out-of-range float is E300")
def _():
    raises_code(lambda: run_src('print(int(math.pow(10.0, 30.0)))'), "E300")


@test("c5: division by zero is E300 and catchable")
def _():
    out = run_src('''
try {
  print(1 / 0)
} catch e {
  print("caught " + e.code)
}
try {
  print(1 % 0)
} catch e {
  print("caught " + e.code)
}
try {
  print(1.0 / 0.0)
} catch e {
  print("caught " + e.code)
}
''')
    eq(out, "caught E300\ncaught E300\ncaught E300\n")


@test("c6: binop type errors fail misa check with E211")
def _():
    errs = check_source('print(1 - "a")')
    assert errs and errs[0]["code"] == "E211", errs
    errs = check_source('print("a" * 2)')
    assert errs and errs[0]["code"] == "E211", errs
    errs = check_source('print([1] - [2])')
    assert errs and errs[0]["code"] == "E211", errs


@test("c6: valid binops still check clean")
def _():
    assert not check_source('print(1 + 2 * 3)')
    assert not check_source('print("a" + "b")')
    assert not check_source('print([1] + [2])')
    assert not check_source('print(1.5 + 2)')
    assert not check_source('print(1 < 2)')
    assert not check_source('print("a" < "b")')
    assert not check_source('print(1 == "a")')


@test("c6: ordering-comparison type errors fail misa check with E211")
def _():
    for src in ('print(1 < "a")', 'print("a" >= 2)',
                'print(true < false)', 'print([1] <= [2])'):
        errs = check_source(src)
        assert errs and errs[0]["code"] == "E211", (src, errs)


@test("c6: dynamic comparison mismatch is E200 at runtime, not a traceback")
def _():
    src = ('let x = json.parse("1")\n'
           'try {\n'
           '  print(x < "a")\n'
           '} catch e {\n'
           '  print("caught " + e.code)\n'
           '}\n')
    out = run_src(src)
    eq(out, "caught E200\n")


@test("c7: json.parse rejects lone surrogates with E300")
def _():
    src = ('let j = str.chr(34) + str.chr(92) + "ud800" + str.chr(34)\n'
           'let x = json.parse(j)\n'
           'print(x)\n')
    raises_code(lambda: run_src(src), "E300")


@test("c8: missing onnxruntime raises E503 not ModuleNotFoundError")
def _():
    import builtins
    from misa import providers
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("No module named 'onnxruntime'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        try:
            providers._ort_session("/tmp/fake.onnx")
            raise AssertionError("expected E503")
        except Lerr.MisaError as e:
            assert e.code == "E503", f"expected E503, got {e.code}"
    finally:
        builtins.__import__ = real_import


@test("c1: out-of-range integer literals wrap (interp matches AOT)")
def _():
    out = run_src('''
print(18446744073709551615)
print(99999999999999999999999999999)
print(-9223372036854775809)
''')
    eq(out, "-1\n7886392056514347007\n9223372036854775807\n")


@test("c2: web-target toolchain failure carries E600, not a traceback")
def _():
    # Simulate a broken/missing emsdk: the emcc step raises RuntimeError.
    # build() must convert it to an E600 error dict (the contract for
    # unsupported/failing targets), never let it escape as a traceback.
    from misa.aot import build as bmod
    real_ensure = bmod.ensure_runtime

    def boom(target="", **kw):
        raise RuntimeError("emcc not found: simulated missing SDK")

    bmod.ensure_runtime = boom
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "t.misa")
            with open(src, "w") as f:
                f.write('print("hi")\n')
            ok, errors = bmod.build(src, os.path.join(td, "t.js"),
                                    target="wasm32-unknown-emscripten")
    finally:
        bmod.ensure_runtime = real_ensure
    assert not ok, "expected the web build to fail"
    assert errors and errors[0]["code"] == "E600", errors


@test("device: aarch64-apple-ios emits a Mach-O arm64 object")
def _():
    # The iOS device target (unsigned IPA pipeline) must lower through
    # llvmlite to a real Mach-O arm64 relocatable object.
    from misa.aot import build as bmod
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "t.misa")
        with open(src, "w") as f:
            f.write('print("hi")\n')
        out = os.path.join(td, "t.o")
        ok, errors = bmod.build(src, out, target="aarch64-apple-ios")
        assert ok, errors
        with open(out, "rb") as f:
            magic = f.read(4)
    # Mach-O 64-bit magic, little-endian (0xFEEDFACF -> CF FA ED FE)
    assert magic == b"\xcf\xfa\xed\xfe", magic.hex()


@test("bonus: process.run is E600 (not E101) for AOT targets")
def _():
    from misa.aot import build as bmod
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "t.misa")
        with open(src, "w") as f:
            f.write('let r = process.run(["echo", "hi"])\nprint(r.code)\n')
        ok, errors = bmod.build(src, os.path.join(td, "t.bin"))
    assert not ok, "expected the build to fail"
    assert errors and errors[0]["code"] == "E600", errors
    assert "process.run" in errors[0]["message"], errors


# ---------------------------------------------------------------- audit H/M fixes

@test("H-1: float rendering matches Python str() (interp side)")
def _():
    # The AOT side is covered by a21 (byte-identical via misa_fmt_float,
    # verified against 4015 values incl. denormals). Here: shortest
    # round-trip + ".0" for integral floats.
    out = run_src('print(0.1)\nprint(1.0)\nprint(1.0/3.0)\n'
                  'print(1000000.0)\nprint(10000000000000000.0)\n'
                  'print(0.1+0.2)\n')
    eq(out, "0.1\n1.0\n0.3333333333333333\n1000000.0\n1e+16\n"
            "0.30000000000000004\n")


@test("H-3: db bind args accept heterogeneous scalars (interp)")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE t (name TEXT, n INTEGER, f REAL)")\n'
           'db.exec(db, "INSERT INTO t VALUES (?, ?, ?)", ["x", 11, 2.5])\n'
           'let rows = db.query(db, "SELECT * FROM t")\n'
           'print(rows[0].name)\nprint(rows[0].n)\nprint(rows[0].f)\n'
           'db.close(db)\n')
    out = run_src(src)
    eq(out, "x\n11\n2.5\n")


@test("H-3: db.exec rejects non-scalar bind arg with E200")
def _():
    src = ('let db = db.open(":memory:")\n'
           'db.exec(db, "CREATE TABLE t (x TEXT)")\n'
           'db.exec(db, "INSERT INTO t VALUES (?)", [[1]])\n')
    raises_code(lambda: run_src(src), "E200")


@test("H-4: json.stringify rejects sum values with E300 (interp)")
def _():
    src = ('type Shape { Circle(radius: Float), Rect(w: Float, h: Float) }\n'
           'let s: Shape = Shape.Circle(1.0)\n'
           'print(json.stringify({"shape": s}))\n')
    raises_code(lambda: run_src(src), "E300")


@test("H-5: deep recursion is a clean E300, not a traceback")
def _():
    src = ('fn boom(n: Int) -> Int {\n'
           '  if (n <= 0) { return 0 }\n'
           '  return boom(n - 1) + 1\n'
           '}\n'
           'print(boom(100000))\n')
    e = raises_code(lambda: run_src(src), "E300")
    assert "recursion" in e.message.lower(), e.message
    assert "Traceback" not in e.message


@test("H-5: deep recursion E300 is catchable")
def _():
    src = ('fn boom(n: Int) -> Int {\n'
           '  if (n <= 0) { return 0 }\n'
           '  return boom(n - 1) + 1\n'
           '}\n'
           'try {\n  boom(100000)\n} catch e {\n'
           '  print("caught " + e.code)\n'
           '}\n')
    eq(run_src(src), "caught E300\n")


@test("H-6: misa build surfaces E600 (not bare text) on toolchain failure")
def _():
    # Exercise via the CLI: build a program for a bogus target triple.
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "t.misa")
        with open(src, "w") as f:
            f.write('print("hi")\n')
        p = subprocess.run([sys.executable,
                            os.path.join(ROOT, "impl", "bin", "misa"),
                            "build", src, "--target", "bogus-unknown-xyz"],
                           capture_output=True, text=True)
    assert p.returncode != 0
    assert "E600" in p.stderr, p.stderr
    assert "Traceback" not in p.stderr


@test("H-6: misa serve rejects a bad --port with E600, not a traceback")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "t.misa")
        with open(src, "w") as f:
            f.write('print("hi")\n')
        p = subprocess.run([sys.executable,
                            os.path.join(ROOT, "impl", "bin", "misa"),
                            "serve", src, "--port", "abc"],
                           capture_output=True, text=True, timeout=30)
    assert p.returncode == 2, p.returncode
    assert "E600" in p.stderr, p.stderr
    assert "Traceback" not in p.stderr


@test("M-2: unknown type in let annotation is E208 (not E200)")
def _():
    errs = check_source("let x: Wat = 1\n")
    eq(len(errs), 1)
    eq(errs[0]["code"], "E208")


@test("M-2: unknown type in plain type-decl field is E208")
def _():
    errs = check_source("type T { x: Wat }\n")
    eq(len(errs), 1)
    eq(errs[0]["code"], "E208")


@test("M-2: unbound type variable in generic decl stays E222")
def _():
    errs = check_source("type Box<T> { value: Wat }\n")
    eq(len(errs), 1)
    eq(errs[0]["code"], "E222")


@test("M-4: db handle may use any variable name")
def _():
    src = ('let mydb = db.open(":memory:")\n'
           'mydb.exec(mydb, "CREATE TABLE t (x INT)")\n'
           'mydb.exec(mydb, "INSERT INTO t VALUES (?)", [7])\n'
           'let rows = mydb.query(mydb, "SELECT x FROM t")\n'
           'print(rows[0].x)\n'
           'mydb.close(mydb)\n')
    eq(run_src(src), "7\n")


@test("M-5: nested quote in interpolation names the real problem")
def _():
    errs = check_source('let x = "a{"b"}c"\n')
    eq(len(errs), 1)
    eq(errs[0]["code"], "E001")
    assert "interpolation" in errs[0]["message"], errs[0]["message"]


@test("M-6: json.parse maps out-of-int64 integers to Float")
def _():
    out = run_src('let v = json.parse("99999999999999999999999")\n'
                  'print(v)\n'
                  'let w = json.parse("42")\n'
                  'print(w)\n')
    eq(out, "1e+23\n42\n")


@test("M-7: extern function used as a value is E214 (not E101)")
def _():
    src = ('extern "c" {\n'
           '  fn strlen(s: Text) -> Int\n'
           '}\n'
           'let f = strlen\n')
    errs = check_source(src)
    eq(len(errs), 1)
    eq(errs[0]["code"], "E214")
    assert "extern" in errs[0]["message"], errs[0]["message"]


@test("M-8: env() on unset variable is E302")
def _():
    e = raises_code(lambda: run_src('print(env("MISA_NO_SUCH_VAR_XYZ"))\n'),
                    "E302")
    assert "not set" in e.message


@test("M-8: env() E302 is catchable")
def _():
    out = run_src('try {\n'
                  '  env("MISA_NO_SUCH_VAR_XYZ")\n'
                  '} catch e {\n'
                  '  print("caught " + e.code)\n'
                  '}\n')
    eq(out, "caught E302\n")


# ---------------------------------------------------------------- LOW audit
# fixes (2026-10-06)

@test("L-1: E300 int-parse carries the call-site position")
def _():
    e = raises_code(lambda: run_src('print(int("abc"))\n'), "E300")
    eq((e.line, e.col), (1, 7), f"got {e.line}:{e.col}")


@test("L-1: E301 assert carries the assert position")
def _():
    e = raises_code(lambda: run_src('assert(false)\n'), "E301")
    eq((e.line, e.col), (1, 1), f"got {e.line}:{e.col}")


@test("L-1+L-5: E303 fs.read position and AOT-identical message text")
def _():
    e = raises_code(lambda: run_src('print(fs.read("nope-xyz.txt"))\n'),
                    "E303")
    eq((e.line, e.col), (1, 9), f"got {e.line}:{e.col}")
    # L-5b: identical to the AOT runtime's wording.
    eq(e.message, "fs.read failed: cannot open 'nope-xyz.txt'")


@test("L-1: E305 missing map key carries the index position")
def _():
    src = ('let m = {"a": 1}\n'
           'print(m["zz"])\n')
    e = raises_code(lambda: run_src(src), "E305")
    eq((e.line, e.col), (2, 8), f"got {e.line}:{e.col}")


@test("L-1: E304 http.get bad URL carries the call-site position")
def _():
    e = raises_code(lambda: run_src('print(http.get("notaurl"))\n'), "E304")
    eq((e.line, e.col), (1, 11), f"got {e.line}:{e.col}")


@test("L-1: E112 missing import points at the import line")
def _():
    d = _write_files({"main.misa": 'import "nope.misa"\n'})
    errs = check_file(os.path.join(d, "main.misa"))
    e112 = [e for e in errs if e["code"] == "E112"]
    assert e112, errs
    eq((e112[0]["line"], e112[0]["col"]), (1, 1))


@test("L-1: E111 circular import points at the import line")
def _():
    d = _write_files({
        "a.misa": 'import "b.misa"\n',
        "b.misa": 'import "a.misa"\n',
    })
    errs = check_file(os.path.join(d, "a.misa"))
    e111 = [e for e in errs if e["code"] == "E111"]
    assert e111, errs
    assert e111[0]["line"] == 1, e111[0]


@test("L-4: E212 message no longer promises Error-shape assignment")
def _():
    src = 'throw 42\n'
    errs = check_source(src)
    assert any(e["code"] == "E212" for e in errs), errs
    msg = [e for e in errs if e["code"] == "E212"][0]["message"]
    assert "record literal" in msg, msg


@test("L-6: E219 fires alone, no leading E211")
def _():
    src = ('fn f<T, K>(x: T) -> K {\n'
           '  return x\n'
           '}\n'
           'let a = f(1)\n')
    errs = check_source(src)
    codes = [e["code"] for e in errs]
    assert "E219" in codes, errs
    assert "E211" not in codes, errs


@test("L-6: genuine return mismatch in generic fn still E211s")
def _():
    src = ('fn f<T>(x: T) -> Int {\n'
           '  return x\n'
           '}\n')
    errs = check_source(src)
    assert any(e["code"] == "E211" for e in errs), errs


# ---------------------------------------------------------------- solutions sprint (benchmark W-1..W-6)

@test("W-5: else-if chains")
def _():
    src = ('fn c(n: Int) -> Text {\n'
           '  if n < 0 {\n    return "neg"\n'
           '  } else if n == 0 {\n    return "zero"\n'
           '  } else {\n    return "pos"\n'
           '  }\n}\n'
           'print(c(-1))\nprint(c(0))\nprint(c(5))\n')
    eq(run_src(src), "neg\nzero\npos\n")


@test("W-5: else-if without trailing else")
def _():
    src = ('let n = 5\n'
           'if n < 0 {\n  print("neg")\n'
           '} else if n > 10 {\n  print("big")\n'
           '}\n'
           'print("done")\n')
    eq(run_src(src), "done\n")


@test("W-6: raw strings disable interpolation and escapes")
def _():
    toks = Lexer('r"a{b}\\n"\n').tokens()
    eq([t.kind for t in toks], ["RAWSTRING", "SEMI", "EOF"])
    eq(toks[0].value, "a{b}\\n")
    src = 'let who = "Ada"\nprint(r"hi {who} \\n")\n'
    eq(run_src(src), "hi {who} \\n\n")


@test("W-6: raw triple strings span lines literally")
def _():
    src = 'print(r"""\n{"a": 1}\n""")\n'
    # content is '{"a": 1}\n' (leading newline stripped); print adds one
    eq(run_src(src), '{"a": 1}\n\n')


@test("W-6: 'r' still lexes as identifier when not followed by quote")
def _():
    eq(run_src('let r = 42\nprint(r)\n'), "42\n")


@test("W-4: str.chars returns code points")
def _():
    eq(run_src('print(str.chars("héllo"))\n'), "[h, é, l, l, o]\n")
    eq(run_src('print(len(str.chars("😀")))\n'), "1\n")


@test("W-1a: E208 suggests omitting the annotation for 'any'")
def _():
    errs = check_source('let x: any = 5\n')
    assert any(e["code"] == "E208" for e in errs), errs
    msg = [e for e in errs if e["code"] == "E208"][0]["message"]
    assert "omit the type" in msg, msg


@test("W-6: formatter preserves raw strings")
def _():
    src = 'print(r"a{b}\\n")\n'
    once = format_source(src, "f.misa")
    assert ('r"a{b}\\n"' in once), once
    eq(format_source(once, "f.misa"), once)
    eq(run_src(src), run_src(once))


@test("W-1c: E211 points at the leaking branch, not the fn header")
def _():
    src = ('fn f(n: Int) -> Int {\n'
           '  if n > 0 {\n'
           '    return 1\n'
           '  } else {\n'
           '    print("leak")\n'
           '  }\n'
           '}\n')
    errs = check_source(src)
    e211 = [e for e in errs if e["code"] == "E211"]
    assert e211, errs
    # 5:5 is the print inside the leaking else branch (1-based)
    assert (e211[0]["line"], e211[0]["col"]) == (5, 5), e211[0]


# ---------------------------------------------------------------- Phase 7: concurrency

@test("conc: spawn lexes as a keyword")
def _():
    toks = Lexer('spawn { }\n').tokens()
    kinds = [t.kind for t in toks]
    eq(kinds, ["spawn", "{", "}", "SEMI", "EOF"])


@test("conc: spawn parses to ast.Spawn")
def _():
    prog = parse_source('let t = spawn { return 1 }\n')
    stmt = prog.items[0]
    assert isinstance(stmt.value, last.Spawn), type(stmt.value)
    eq(len(stmt.value.body), 1)


@test("conc: await timeout parses; timeout is contextual")
def _():
    prog = parse_source('let x = await t timeout 5000\n')
    aw = prog.items[0].value
    assert isinstance(aw, last.Await)
    assert aw.timeout is not None
    # a `timeout(...)` call on the next line is unaffected (SEMI intervenes)
    prog2 = parse_source('let x = await t\ntimeout(5)\n')
    aw2 = prog2.items[0].value
    assert isinstance(aw2, last.Await) and aw2.timeout is None


@test("conc: fmt round-trips spawn and await timeout")
def _():
    src = 'let t = spawn { return 1 }\nlet x = await t timeout 5000\n'
    out = format_source(src)
    assert "spawn { return 1 }" in out, out
    assert "await t timeout 5000" in out, out
    prog = parse_source(out)
    assert isinstance(prog.items[0].value, last.Spawn)
    assert prog.items[1].value.timeout is not None


@test("conc: checker accepts a valid concurrency program")
def _():
    src = ('let t: Task<Text> = spawn { return "hi" }\n'
           'let b: Text = await t\n'
           'sleep(10)\n'
           'let ch = chan<Int>()\n'
           'ch.send(1)\n'
           'let v: Int = ch.recv()\n'
           'close(ch)\n'
           'let ps: [Text] = fetch_all(["http://x"])\n')
    eq(check_source(src), [])


@test("conc: E205 await on non-task")
def _():
    errs = check_source('let x = await 42\n')
    eq([e["code"] for e in errs], ["E205"])


@test("conc: E211 await timeout must be Int")
def _():
    src = 'let t = spawn { return 1 }\nlet x = await t timeout "soon"\n'
    eq([e["code"] for e in check_source(src)], ["E211"])


@test("conc: E221 bare Task/Chan and bad chan type args")
def _():
    eq([e["code"] for e in check_source('let t: Task = spawn { return 1 }\n')],
       ["E221"])
    eq([e["code"] for e in check_source('let c = chan()\n')], ["E221"])
    eq([e["code"] for e in check_source('let c = chan<Int, Text>()\n')],
       ["E221"])


@test("conc: E211 builtin argument types")
def _():
    eq([e["code"] for e in check_source('sleep("x")\n')], ["E211"])
    eq([e["code"] for e in check_source('fetch_all("x")\n')], ["E211"])
    eq([e["code"] for e in check_source('close(42)\n')], ["E211"])
    eq([e["code"] for e in check_source('let ch = chan<Int>()\nch.send("x")\n')],
       ["E211"])


@test("conc: E200 spawn return type mismatch")
def _():
    src = 'let t: Task<Int> = spawn { return "oops" }\n'
    eq([e["code"] for e in check_source(src)], ["E200"])


@test("conc: E225 Task across the FFI boundary")
def _():
    src = ('extern "c" { fn strlen(s: Text) -> Int }\n'
           'let t = spawn { return 1 }\n'
           'strlen(t)\n')
    eq([e["code"] for e in check_source(src)], ["E225"])


@test("conc: interp spawn and await")
def _():
    src = 'let t = spawn { return 40 + 2 }\nprint(await t)\n'
    eq(run_src(src).strip(), "42")


@test("conc: interp sleep does not block other tasks")
def _():
    src = ('let ch = chan<Text>()\n'
           'spawn { sleep(300)\nch.send("slow") }\n'
           'spawn { ch.send("fast") }\n'
           'print(ch.recv())\n'
           'print(ch.recv())\n')
    eq(run_src(src).strip().splitlines(), ["fast", "slow"])


@test("conc: interp await timeout raises catchable E306")
def _():
    src = ('let t = spawn { sleep(500)\nreturn 1 }\n'
           'try {\n  let x = await t timeout 100\n  print("no-timeout")\n'
           '} catch e {\n  print(e.code)\n}\n'
           'print(await t)\n')
    eq(run_src(src).strip().splitlines(), ["E306", "1"])


@test("conc: interp channel close drains then E306")
def _():
    src = ('let ch = chan<Int>()\n'
           'ch.send(7)\n'
           'close(ch)\n'
           'print(ch.recv())\n'
           'try {\n  ch.recv()\n} catch e {\n  print(e.code)\n}\n')
    eq(run_src(src).strip().splitlines(), ["7", "E306"])


@test("conc: interp scope cancellation raises E306 in the child")
def _():
    # NB: Misa functions cannot see globals — the channel goes in as a param.
    src = ('let ch = chan<Text>()\n'
           'fn worker(ch: Chan<Text>) {\n'
           '  spawn {\n'
           '    try {\n'
           '      sleep(5000)\n'
           '    } catch e {\n'
           '      ch.send(e.code)\n'
           '    }\n'
           '  }\n'
           '}\n'
           'worker(ch)\n'
           'print(ch.recv())\n')
    eq(run_src(src).strip(), "E306")


@test("conc: interp never-awaited task error goes to stderr")
def _():
    import contextlib
    src = ('spawn { throw { code: "E999", message: "boom" } }\n'
           'print("done")\n')
    prog = parse_source(src)
    assert not check_source(src)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):
        Interpreter(out=out).run(prog)
    eq(out.getvalue().strip(), "done")
    assert "E999" in err.getvalue() and "boom" in err.getvalue(), \
        err.getvalue()


@test("conc: interp fetch_all is parallel and order-preserving")
def _():
    import time as _time
    from http.server import ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            _time.sleep(0.4)
            body = self.path.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    # NB: ThreadingHTTPServer — the plain HTTPServer serializes requests,
    # which would make even parallel clients look sequential.
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        urls = ", ".join(
            f'"http://127.0.0.1:{port}/{c}"' for c in "abcd")
        src = (f'let ps = fetch_all([{urls}])\n'
               'print(ps[0])\nprint(ps[1])\nprint(ps[2])\nprint(ps[3])\n')
        start = _time.time()
        out = run_src(src).strip().splitlines()
        wall = _time.time() - start
        eq(out, ["/a", "/b", "/c", "/d"])
        # sequential would take >= 1.6s; parallel should be well under
        assert wall < 1.2, f"fetch_all not parallel: {wall:.2f}s"
    finally:
        srv.shutdown()


@test("conc: interp fetch_all first error is catchable")
def _():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/bad":
                self.send_response(500)
                self.end_headers()
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        src = (f'try {{\n  fetch_all(["http://127.0.0.1:{port}/bad"])\n'
               '} catch e {\n  print(e.code)\n}\n')
        eq(run_src(src).strip(), "E304")
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------
# `test` blocks + `misa test` runner, assert_eq/assert_ne, argv()


def run_src_argv(src, argv):
    prog = parse_source(src)
    errs = check_source(src)
    assert not errs, errs
    buf = io.StringIO()
    Interpreter(out=buf, argv=argv).run(prog)
    return buf.getvalue()


@test("parser: test block")
def _():
    prog = parse_source("test addition {\n  assert_eq(1 + 1, 2)\n}\n")
    assert len(prog.items) == 1
    t = prog.items[0]
    assert type(t).__name__ == "TestDecl", type(t).__name__
    eq(t.name, "addition")
    assert len(t.body) == 1


@test("parser: `test` stays usable as an identifier (contextual keyword)")
def _():
    # `test` alone / called / followed by a block is NOT a test block.
    prog = parse_source("test\n")
    assert type(prog.items[0]).__name__ != "TestDecl"
    prog = parse_source("print(test)\n")
    assert type(prog.items[0]).__name__ != "TestDecl"
    prog = parse_source("test { }\n")
    assert all(type(i).__name__ != "TestDecl" for i in prog.items)
    # but `test <name> {` is
    prog = parse_source("test foo { }\n")
    assert type(prog.items[0]).__name__ == "TestDecl"


@test("checker: duplicate test name E210")
def _():
    errs = check_source("test a { }\ntest a { }\n")
    assert any(e["code"] == "E210" for e in errs), errs


@test("checker: assert_eq/assert_ne/argv signatures")
def _():
    assert not check_source("assert_eq(1, 1)\nassert_ne(1, 2)\n"
                            "let a = argv()\n")
    errs = check_source("assert_eq(1)\n")
    assert any(e["code"] == "E102" for e in errs), errs
    errs = check_source("argv(1)\n")
    assert any(e["code"] == "E102" for e in errs), errs


@test("checker: test body sees top-level bindings")
def _():
    assert not check_source("let x = 1\ntest t { assert_eq(x, 1) }\n"), \
        check_source("let x = 1\ntest t { assert_eq(x, 1) }\n")


@test("checker: test body rejects bad types")
def _():
    errs = check_source("test t { assert_eq(1, \"a\") }\n")
    # assert_eq takes any/any — no error; but unknown names still E101
    assert not errs, errs
    errs = check_source("test t { assert_eq(nope, 1) }\n")
    assert any(e["code"] == "E101" for e in errs), errs


@test("interp: assert_eq/assert_ne pass")
def _():
    out = run_src('assert_eq([1, 2], [1, 2])\n'
                  'assert_eq({a: 1}, {a: 1})\n'
                  'assert_ne(1, 2)\nprint("ok")\n')
    eq(out.strip(), "ok")


@test("interp: assert_eq failure renders values (E301)")
def _():
    e = raises_code(
        lambda: run_src('assert_eq(1 + 1, 3)\n'), "E301")
    assert "2 != 3" in e.message, e.message


@test("interp: assert_ne failure renders value (E301)")
def _():
    e = raises_code(lambda: run_src('assert_ne("x", "x")\n'), "E301")
    assert "both are x" in e.message, e.message


@test("interp: argv() returns program args")
def _():
    out = run_src_argv('print(str(argv()))\n', ["a", "b c"])
    eq(out.strip(), "[a, b c]")
    out = run_src_argv('print(str(len(argv())))\n', [])
    eq(out.strip(), "0")


@test("fmt: test block round-trips")
def _():
    src = "test addition {\n    assert_eq(1 + 1, 2)\n}\n"
    eq(format_source(src), src)


@test("cli: misa test discovers, isolates, and reports")
def _():
    d = tempfile.mkdtemp(prefix="misa-test-")
    try:
        with open(os.path.join(d, "math_test.misa"), "w") as f:
            f.write("test ok1 { assert_eq(1, 1) }\n"
                    "test bad1 { assert_eq(1, 2) }\n")
        with open(os.path.join(d, "test_str.misa"), "w") as f:
            f.write("test ok2 { assert_ne(\"a\", \"b\") }\n"
                    "test boom { throw {code: \"E1\", message: \"x\"} }\n")
        with open(os.path.join(d, "not_a_test.txt"), "w") as f:
            f.write("test nope { }\n")
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "test", d],
            capture_output=True, text=True)
        assert p.returncode == 1, p.stdout  # failures -> non-zero
        assert "PASS  ok1" in p.stdout, p.stdout
        assert "FAIL  bad1" in p.stdout, p.stdout
        assert "E301: assert_eq failed: 1 != 2" in p.stdout, p.stdout
        assert "PASS  ok2" in p.stdout, p.stdout
        assert "FAIL  boom" in p.stdout, p.stdout  # throw doesn't kill run
        assert "nope" not in p.stdout  # .txt not discovered
        assert "2/4 tests passed" in p.stdout, p.stdout
        # all-pass run exits 0
        with open(os.path.join(d, "math_test.misa"), "w") as f:
            f.write("test ok1 { assert_eq(1, 1) }\n")
        os.remove(os.path.join(d, "test_str.misa"))
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "test", d],
            capture_output=True, text=True)
        assert p.returncode == 0, p.stdout
        assert "1/1 tests passed" in p.stdout, p.stdout
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


@test("cli: misa test --json is structured")
def _():
    d = tempfile.mkdtemp(prefix="misa-test-")
    try:
        with open(os.path.join(d, "a_test.misa"), "w") as f:
            f.write("test ok { assert_eq(1, 1) }\n"
                    "test bad { assert_eq(1, 2) }\n")
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "test", d, "--json"],
            capture_output=True, text=True)
        assert p.returncode == 1
        data = json.loads(p.stdout)
        assert len(data) == 2, data
        by_name = {r["name"]: r for r in data}
        assert by_name["ok"]["pass"] is True
        assert by_name["ok"]["kind"] == "test"
        assert by_name["bad"]["pass"] is False
        assert "E301" in by_name["bad"]["message"]
        assert by_name["bad"]["line"] == 2
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


@test("cli: misa run -- forwards argv to the program")
def _():
    d = tempfile.mkdtemp(prefix="misa-test-")
    try:
        prog = os.path.join(d, "args.misa")
        with open(prog, "w") as f:
            f.write('for a in argv() { print("arg:" + a) }\n')
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "run", prog, "--", "x", "y z"],
            capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        eq(p.stdout, "arg:x\narg:y z\n")
        # no `--`: empty argv
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "run", prog],
            capture_output=True, text=True)
        eq(p.stdout, "")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _dbg_run(prog_src, commands):
    """Drive `misa debug --agent` through a pipe. commands is a list of
    dicts (break file paths may use $F for the temp program path).
    Returns (returncode, events, stderr, prog_path)."""
    import shutil
    d = tempfile.mkdtemp(prefix="misa-dbg-")
    try:
        prog = os.path.join(d, "prog.misa")
        with open(prog, "w") as f:
            f.write(prog_src)
        lines = []
        for c in commands:
            if isinstance(c, dict) and c.get("cmd") == "break":
                c = dict(c)
                c["file"] = c["file"].replace("$F", prog)
            lines.append(c if isinstance(c, str) else json.dumps(c))
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "debug", "--agent", prog],
            input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=60)
        events = [json.loads(ln) for ln in p.stdout.splitlines()
                  if ln.strip()]
        return p.returncode, events, p.stderr, prog
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("debug: agent protocol break/run/locals/eval/continue/exit")
def _():
    src = ("fn total(prices: [Int]) -> Int {\n"
           "  var sum = 0\n"
           "  for p in prices {\n"
           "    sum = sum + p\n"
           "  }\n"
           "  return sum\n"
           "}\n"
           "let t = total([10, 20])\n"
           'print(t)\n')
    rc, ev, err, prog = _dbg_run(src, [
        {"cmd": "break", "file": "$F", "line": 4},
        {"cmd": "run"},
        {"cmd": "locals"},
        {"cmd": "eval", "expr": "sum + 100"},
        {"cmd": "continue"},
    ])
    assert rc == 0, err
    kinds = [e.get("event") for e in ev]
    eq(kinds[0], "started")
    assert ev[1] == {"ok": True, "breakpoint": 1}, ev[1]
    brk = ev[2]
    eq(brk["event"], "break")
    eq(brk["breakpoint"], 1)
    eq(brk["line"], 4)
    eq(brk["function"], "total")
    loc = ev[3]
    assert loc["ok"], loc
    by_name = {v["name"]: v for v in loc["locals"]}
    eq(by_name["sum"]["type"], "Int")
    eq(by_name["sum"]["value"], 0)
    eq(by_name["prices"]["type"], "List")
    eq(by_name["prices"]["value"][0], {"type": "Int", "value": 10})
    eq(ev[4], {"ok": True, "type": "Int", "value": 100})
    # loop has two iterations, hitting the breakpoint each time
    breaks = [e for e in ev if e.get("event") == "break"]
    eq(len(breaks), 2)
    assert ev[-1] == {"event": "exit", "code": 0}, ev[-1]


@test("debug: step/next/out and stack")
def _():
    src = ("fn inner(x: Int) -> Int {\n"
           "  let y = x * 2\n"
           "  return y\n"
           "}\n"
           "fn outer() -> Int {\n"
           "  let a = inner(21)\n"
           "  return a\n"
           "}\n"
           "let r = outer()\n"
           "let s = r + 1\n"
           "print(s)\n")
    rc, ev, err, prog = _dbg_run(src, [
        {"cmd": "break", "file": "$F", "line": 3},
        {"cmd": "run"},
        {"cmd": "out"},
        {"cmd": "stack"},
        {"cmd": "clear", "id": 1},
        {"cmd": "continue"},
    ])
    assert rc == 0, err
    assert ev[2]["event"] == "break" and ev[2]["line"] == 3, ev[2]
    # `out` from inner's last line: next pause is outer's line 7
    assert ev[3]["event"] == "break" and ev[3]["line"] == 7, ev[3]
    eq(ev[3]["function"], "outer")
    st = ev[4]
    assert st["ok"], st
    names = [(f["depth"], f["name"]) for f in st["frames"]]
    eq(names, [(1, "outer"), (0, "<main>")])
    eq(ev[5], {"ok": True})
    outs = [e for e in ev if e.get("event") == "output"]
    eq([o["text"] for o in outs], ["43"])
    assert ev[-1] == {"event": "exit", "code": 0}, ev[-1]


@test("debug: runtime error produces a structured error event")
def _():
    rc, ev, err, prog = _dbg_run(
        "fn f() -> Int {\n  return 1 / 0\n}\nprint(f())\n",
        [{"cmd": "run"}])
    eq(rc, 1)  # debugger exits nonzero when the program fails
    assert ev[0]["event"] == "started", ev
    last = ev[-1]
    eq(last["event"], "error")
    eq(last["code"], "E300")
    assert "division by zero" in last["message"], last
    eq(last["line"], 2)


@test("debug: breakpoints listing and bad commands")
def _():
    rc, ev, err, prog = _dbg_run(
        'print("hi")\n',
        [{"cmd": "break", "file": "$F", "line": 1},
         {"cmd": "breakpoints"},
         {"cmd": "clear", "id": 99},
         {"cmd": "bogus"},
         {"cmd": "clear", "id": 1},
         {"cmd": "run"}])
    assert rc == 0, err
    eq(ev[1], {"ok": True, "breakpoint": 1})
    bps = ev[2]
    assert bps["ok"] and len(bps["breakpoints"]) == 1, bps
    eq(bps["breakpoints"][0]["line"], 1)
    assert not ev[3]["ok"], ev[3]
    assert not ev[4]["ok"], ev[4]
    assert ev[-1] == {"event": "exit", "code": 0}, ev[-1]


@test("debug: human text mode session")
def _():
    import shutil
    d = tempfile.mkdtemp(prefix="misa-dbg-")
    try:
        prog = os.path.join(d, "prog.misa")
        with open(prog, "w") as f:
            f.write("var x = 41\nx = x + 1\nprint(x)\n")
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "debug", prog],
            input=f"b {prog}:2\nr\np x\nc\n",
            capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        assert "breakpoint 1" in p.stdout, p.stdout
        # breakpoint pauses BEFORE `x = x + 1` runs, so x is still 41
        assert "Int: 41" in p.stdout, p.stdout
        assert "program exited (code 0)" in p.stdout, p.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------
# Phase 8 (overtake): interfaces

IFACE_PREAMBLE = '''
interface Drawable {
  fn draw(self) -> Text
}
type Circle { x: Int, y: Int, r: Int }
type Rect { w: Int, h: Int }
impl Drawable for Circle {
  fn draw(self) -> Text { return "circle({self.r})" }
}
impl Drawable for Rect {
  fn draw(self) -> Text { return "rect({self.w}x{self.h})" }
}
'''


@test("iface: declare + impl check clean")
def _():
    errs = check_source(IFACE_PREAMBLE)
    eq(errs, [])


@test("iface: static dispatch on concrete receiver")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  print(c.draw())
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "circle(5)")


@test("iface: dynamic dispatch on interface value")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  var d: Drawable = c
  print(d.draw())
  let r: Rect = { w: 3, h: 4 }
  d = r
  print(d.draw())
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "circle(5)\nrect(3x4)")


@test("iface: heterogeneous list + UFCS map")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn map<T, K>(xs: [T], f: (T) -> K) -> [K] {
  var out: [K] = []
  for x in xs { out = out + [f(x)] }
  return out
}
fn render_all(items: [Drawable]) -> [Text] {
  return map(items, |d| d.draw())
}
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let r: Rect = { w: 3, h: 4 }
  print(render_all([c, r]))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "[circle(5), rect(3x4)]")


@test("iface: bounded generic dispatches dynamically")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn describe<T: Drawable>(x: T) -> Text {
  return "got: " + x.draw()
}
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let r: Rect = { w: 3, h: 4 }
  print(describe(c))
  print(describe(r))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "got: circle(5)\ngot: rect(3x4)")


@test("iface: complex receiver via index")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn first<T: Drawable>(xs: [T]) -> Text { return xs[0].draw() }
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  print(first([c, c]))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "circle(5)")


@test("iface: interface value renders as payload")
def _():
    _, buf = run_src_checked(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let d: Drawable = c
  print(d)
  return 0
}
main()
''')
    assert "r: 5" in buf.getvalue(), buf.getvalue()


@test("iface: E200 unknown method on interface value")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let d: Drawable = c
  print(d.bogus())
  return 0
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: E200 impl missing method")
def _():
    errs = check_source('''
interface I { fn a(self) -> Int
              fn b(self) -> Int }
type T { x: Int }
impl I for T {
  fn a(self) -> Int { return 1 }
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: E200 impl signature mismatch")
def _():
    errs = check_source('''
interface I { fn a(self) -> Int }
type T { x: Int }
impl I for T {
  fn a(self) -> Text { return "x" }
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: E210 duplicate impl")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
impl Drawable for Circle {
  fn draw(self) -> Text { return "dup" }
}
''')
    assert any(e["code"] == "E210" for e in errs), errs


@test("iface: E200 bound violation on generic call")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
fn describe<T: Drawable>(x: T) -> Text { return x.draw() }
fn main() -> Int {
  print(describe(42))
  return 0
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: E200 match on interface value")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let d: Drawable = c
  match d { _ => print("x") }
  return 0
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: E200 field access on interface value")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
fn main() -> Int {
  let c: Circle = { x: 0, y: 0, r: 5 }
  let d: Drawable = c
  print(d.r)
  return 0
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: throws — impl subset ok, superset E200")
def _():
    errs = check_source('''
interface Repo {
  fn get(self, id: Int) -> Text throws E404, E408
}
type Db { url: Text }
impl Repo for Db {
  fn get(self, id: Int) -> Text throws E404 { return "row" }
}
fn main() -> Int {
  let db: Db = { url: "x" }
  let r: Repo = db
  try { print(r.get(1)) } catch e { print("caught") }
  return 0
}
''')
    eq(errs, [])
    errs = check_source('''
interface Repo {
  fn get(self, id: Int) -> Text throws E404
}
type Db { url: Text }
impl Repo for Db {
  fn get(self, id: Int) -> Text throws E404, E408 { return "row" }
}
''')
    assert any(e["code"] == "E200" for e in errs), errs


@test("iface: throws — dynamic call enforces interface set (E227)")
def _():
    errs = check_source(IFACE_PREAMBLE + '''
interface Loud {
  fn boom(self) -> Int throws E500
}
type T { x: Int }
impl Loud for T {
  fn boom(self) -> Int throws E500 { return 1 }
}
fn main() -> Int {
  let t: T = { x: 1 }
  let l: Loud = t
  print(l.boom())
  return 0
}
''')
    assert any(e["code"] == "E227" for e in errs), errs


@test("iface: formatter round-trips interface + impl")
def _():
    src = IFACE_PREAMBLE.strip() + "\n"
    out = format_source(src)
    assert "interface Drawable" in out, out
    assert "impl Drawable for Circle" in out, out
    # formatted output re-parses and re-checks clean
    errs = check_source(out)
    eq(errs, [])


@test("iface: fmt bounded type param")
def _():
    src = "fn f<T: Drawable>(x: T) -> T { return x }\n"
    out = format_source(src)
    assert "<T: Drawable>" in out, out


# --------------------------------------------------------------------------
# Wave 3: profiler (`misa profile`) and REPL (`misa repl`)
# --------------------------------------------------------------------------

def _repl_run(lines):
    """Drive `misa repl` through a pipe. Returns (returncode, stdout,
    stderr)."""
    import shutil
    d = tempfile.mkdtemp(prefix="misa-repl-")
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "repl"],
            input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=60, cwd=d)
        return p.returncode, p.stdout, p.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("repl: evaluates expressions and persists bindings")
def _():
    rc, out, err = _repl_run(["let x = 5", "x + 1", ":quit"])
    assert rc == 0, err
    assert "misa> 6" in out, out


@test("repl: print does not echo nil")
def _():
    rc, out, err = _repl_run(['print("hi")', ":quit"])
    assert rc == 0, err
    assert "hi" in out, out
    assert "nil" not in out, out


@test("repl: multi-line fn definition")
def _():
    rc, out, err = _repl_run([
        "fn double(n: Int) -> Int {",
        "return n * 2",
        "}",
        "double(21)",
        ":quit",
    ])
    assert rc == 0, err
    assert "misa> 42" in out, out


@test("repl: :type shows the checker's inferred type")
def _():
    rc, out, err = _repl_run([":type 1 + 2", ":type [1, 2]", ":quit"])
    assert rc == 0, err
    assert "1 + 2: Int" in out, out
    assert "[1, 2]: [Int]" in out, out


@test("repl: errors are clean E-codes, never tracebacks")
def _():
    rc, out, err = _repl_run([
        'let x: Int = "hi"',
        "no_such_name + 1",
        ":quit",
    ])
    assert rc == 0, err
    assert "E200" in out, out
    assert "E101" in out, out
    assert "Traceback" not in out + err, out + err


@test("repl: :reset forgets bindings")
def _():
    rc, out, err = _repl_run(["let x = 5", ":reset", "x", ":quit"])
    assert rc == 0, err
    assert "reset" in out, out
    assert "E101" in out, out  # x is gone after :reset


@test("repl: spawn/await works across lines")
def _():
    rc, out, err = _repl_run([
        "let t = spawn { sleep(50) return 7 }",
        "await t",
        ":quit",
    ])
    assert rc == 0, err
    assert "misa> 7" in out, out


def _profile_run(prog_src, extra_args=()):
    """Write prog_src to a temp file and run `misa profile` on it.
    Returns (returncode, stdout, stderr)."""
    import shutil
    d = tempfile.mkdtemp(prefix="misa-prof-")
    try:
        prog = os.path.join(d, "prog.misa")
        with open(prog, "w") as f:
            f.write(prog_src)
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "impl", "bin", "misa"),
             "profile", *extra_args, prog],
            capture_output=True, text=True, timeout=120, cwd=d)
        return p.returncode, p.stdout, p.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


_FIB_SRC = ("fn fib(n: Int) -> Int {\n"
            "  if n < 2 { return n }\n"
            "  return fib(n - 1) + fib(n - 2)\n"
            "}\n"
            "fib(25)\n")


@test("profile: human table attributes fib correctly")
def _():
    rc, out, err = _profile_run(_FIB_SRC)
    assert rc == 0, err
    assert "samples" in out and "fib" in out, out


@test("profile: --json attributes >80% to fib")
def _():
    rc, out, err = _profile_run(_FIB_SRC, ("--json",))
    assert rc == 0, err
    # The program's own output precedes the JSON document.
    doc = json.loads(out[out.index("{"):])
    assert doc["samples"] > 50, doc["samples"]
    fib_pct = sum(r["pct"] for r in doc["profile"]
                  if r["function"] == "fib")
    assert fib_pct > 80, (fib_pct, doc["profile"])
    row = doc["profile"][0]
    assert set(row) >= {"function", "file", "line", "samples",
                        "pct"}, row


@test("profile: check errors fail cleanly")
def _():
    rc, out, err = _profile_run('let x: Int = "hi"\n')
    assert rc == 1, (rc, out, err)
    assert "E200" in err, err




# -- Wave 3 (networking): TCP sockets + HTTP server --------------------

@test("net: echo server round-trip via spawn (interp)")
def _():
    src = ('let lis = net.listen(0)\n'
           'let port = lis.port()\n'
           'let srv = spawn {\n'
           '  let conn = lis.accept()\n'
           '  let msg = conn.recv(1024)\n'
           '  conn.send("echo:" + msg)\n'
           '  conn.close()\n'
           '}\n'
           'sleep(200)\n'
           'let c = net.connect("127.0.0.1", port)\n'
           'c.send("hello")\n'
           'print(c.recv(1024))\n'
           'c.close()\n'
           'await srv\n'
           'print("done")\n')
    eq(run_src(src), "echo:hello\ndone\n")


@test("net: method-style socket calls")
def _():
    src = ('let lis = net.listen(0)\n'
           'let port = lis.port()\n'
           'let srv = spawn {\n'
           '  let conn = lis.accept()\n'
           '  conn.send(conn.recv(64))\n'
           '  conn.close()\n'
           '}\n'
           'sleep(200)\n'
           'let c = net.connect("127.0.0.1", port)\n'
           'let n = c.send("ping")\n'
           'print(n)\n'
           'print(c.recv(64))\n'
           'c.close()\n'
           'lis.close()\n'
           'await srv\n')
    eq(run_src(src), "4\nping\n")


@test("net: recv at EOF is clean empty text, not an error")
def _():
    src = ('let lis = net.listen(0)\n'
           'let port = lis.port()\n'
           'let srv = spawn {\n'
           '  let conn = lis.accept()\n'
           '  conn.close()\n'  # close immediately -> client sees EOF
           '}\n'
           'sleep(200)\n'
           'let c = net.connect("127.0.0.1", port)\n'
           'let chunk = c.recv(1024)\n'
           'print("eof:" + chunk + ":end")\n'
           'c.close()\n'
           'await srv\n')
    eq(run_src(src), "eof::end\n")


@test("net: connect to a closed port raises catchable E304")
def _():
    src = ('let tmp = net.listen(0)\n'
           'let port = tmp.port()\n'
           'tmp.close()\n'
           'try {\n'
           '  net.connect("127.0.0.1", port)\n'
           '  print("NOPE")\n'
           '} catch e {\n'
           '  print("caught:" + e.code)\n'
           '}\n')
    out = run_src(src)
    assert out.startswith("caught:E304"), out


@test("net: set_timeout makes recv raise E304")
def _():
    src = ('let lis = net.listen(0)\n'
           'let port = lis.port()\n'
           'let srv = spawn {\n'
           '  let conn = lis.accept()\n'
           '  sleep(2000)\n'  # hold the connection open, send nothing
           '  conn.close()\n'
           '}\n'
           'sleep(200)\n'
           'let c = net.connect("127.0.0.1", port)\n'
           'c.set_timeout(300)\n'
           'try {\n'
           '  c.recv(1024)\n'
           '  print("NOPE")\n'
           '} catch e {\n'
           '  print("caught:" + e.code)\n'
           '}\n'
           'c.close()\n'
           'await srv\n')
    out = run_src(src)
    assert out.startswith("caught:E304"), out


@test("net: socket ops are in checked throws (E304)")
def _():
    # Calling a throwing builtin from a fn without `throws` is E227.
    errs = check_source('fn f() { net.connect("h", 80) }\n')
    assert any(e["code"] == "E227" for e in errs), errs
    errs = check_source(
        'fn f() throws E304 { net.connect("h", 80) }\n')
    assert not errs, errs


@test("net: bad args are clean E200, not tracebacks")
def _():
    errs = check_source('net.connect("h", "eighty")\n')
    assert any(e["code"] == "E211" for e in errs), errs
    # Wrong arity
    errs = check_source('net.listen()\n')
    assert errs, "expected arity error"


def _net_http_src(handler_body):
    # http.serve never returns; run it in a fn so scope exit cancels
    # the server promptly (no 10s linger).
    return ('fn serve_once() throws E304 {\n'
            '  let tmp = net.listen(0)\n'
            '  let port = tmp.port()\n'
            '  tmp.close()\n'
            '  let srv = spawn {\n'
            '    http.serve(port, |req: Request| {\n'
            + handler_body +
            '    })\n'
            '  }\n'
            '  sleep(400)\n'
            '  PORT.put("port", port)\n'
            '}\n')


@test("net: http.serve GET with query parsing")
def _():
    src = ('fn t() throws E304, E306 {\n'
           '  let tmp = net.listen(0)\n'
           '  let p = tmp.port()\n'
           '  tmp.close()\n'
           '  let srv = spawn {\n'
           '    http.serve(p, |req: Request| {\n'
           '      return {status: 200,\n'
           '              body: req.method + " " + req.path + " " +\n'
           '                    req.query["name"]}\n'
           '    })\n'
           '  }\n'
           '  sleep(400)\n'
           '  print(http.get("http://127.0.0.1:" + str(p) +\n'
           '               "/hello?name=world"))\n'
           '}\n'
           't()\n')
    eq(run_src(src), "GET /hello world\n")


@test("net: http.serve POST with body + custom status")
def _():
    src = ('fn t() throws E304, E306 {\n'
           '  let tmp = net.listen(0)\n'
           '  let p = tmp.port()\n'
           '  tmp.close()\n'
           '  let srv = spawn {\n'
           '    http.serve(p, |req: Request| {\n'
           '      if req.path == "/echo" {\n'
           '        return {status: 201, body: "got:" + req.body}\n'
           '      }\n'
           '      return {status: 404, body: "nope"}\n'
           '    })\n'
           '  }\n'
           '  sleep(400)\n'
           '  print(http.post("http://127.0.0.1:" + str(p) + "/echo",\n'
           '                "ping"))\n'
           '}\n'
           't()\n')
    eq(run_src(src), "got:ping\n")


@test("net: http.serve custom response headers")
def _():
    src = ('fn t() throws E304, E306 {\n'
           '  let tmp = net.listen(0)\n'
           '  let p = tmp.port()\n'
           '  tmp.close()\n'
           '  let srv = spawn {\n'
           '    http.serve(p, |req: Request| {\n'
           '      return {status: 200,\n'
           '              headers: {"x-echo": "yes"},\n'
           '              body: "ok"}\n'
           '    })\n'
           '  }\n'
           '  sleep(400)\n'
           '  print(http.get("http://127.0.0.1:" + str(p) + "/"))\n'
           '}\n'
           't()\n')
    eq(run_src(src), "ok\n")


@test("net: http.serve malformed request gets 400")
def _():
    # Pure Misa: speak raw HTTP with a bad request line, expect 400.
    src = ('fn t() throws E300, E304, E306 {\n'
           '  let tmp = net.listen(0)\n'
           '  let p = tmp.port()\n'
           '  tmp.close()\n'
           '  let srv = spawn {\n'
           '    http.serve(p, |req: Request| {\n'
           '      return {status: 200, body: "unreachable"}\n'
           '    })\n'
           '  }\n'
           '  sleep(400)\n'
           '  let c = net.connect("127.0.0.1", p)\n'
           '  c.send("GARBAGE\\r\\n\\r\\n")\n'
           '  let resp = c.recv(1024)\n'
           '  c.close()\n'
           '  print(str.split(resp, "\\r\\n")[0])\n'
           '}\n'
           't()\n')
    eq(run_src(src), "HTTP/1.1 400 Bad Request\n")


# ------------------------------------------------------------ Wave 3: stdlib

@test("stdlib: regex basic API")
def _():
    src = ('let re = regex("([0-9]+)-([0-9]+)")\n'
           'print(regex.matches(re, "123-456"))\n'
           'print(regex.matches(re, "abc"))\n'
           'print(regex.find(re, "tel: 123-456!"))\n'
           'print(regex.find_all(regex("[0-9]+"), "a1 b22 c333"))\n'
           'print(regex.replace(regex("[0-9]+"), "a1 b22", "#"))\n'
           'print(regex.captures(re, "123-456"))\n')
    eq(run_src(src),
       "true\nfalse\n123-456\n[1, 22, 333]\na# b#\n[123, 456]\n")


@test("stdlib: regex check-time rejection E200")
def _():
    for bad in ['regex("([0-9]+")', 'regex("[a-")', 'regex(r"a{2,1}")',
                'regex("\\\\d+")', 'regex("(a)(?b)")', 'regex("*a")',
                'regex("[]")']:
        errs = check_source(f'let r = {bad}\n')
        assert any(e["code"] == "E200" for e in errs), (bad, errs)
    # And the position info names the index.
    errs = check_source('let r = regex(r"a{2,1}")\n')
    e = [e for e in errs if e["code"] == "E200"][0]
    assert "index" in e["message"], e


@test("stdlib: regex runtime rejection E300 (non-literal pattern)")
def _():
    src = ('fn bad(p: Text) {\n'
           '  let r = regex(p)\n'
           '}\n'
           'bad("[oops")\n')
    def go():
        prog = parse_source(src)
        errs = check_source(src)
        assert not errs, errs
        Interpreter(out=io.StringIO()).run(prog)
    raises_code(go, "E300")


@test("stdlib: regex backtracking semantics")
def _():
    src = ('print(regex.matches(regex("a|ab"), "ab"))\n'
           'print(regex.matches(regex("(a|ab)*"), "abab"))\n'
           'print(regex.find(regex("(a|ab)c"), "abc"))\n'
           'print(regex.matches(regex("(a|b)*abb"), "aababb"))\n'
           'print(regex.matches(regex("^a+$"), "aaab"))\n'
           'print(regex.find_all(regex("a*"), "baa"))\n')
    eq(run_src(src), "true\ntrue\nabc\ntrue\nfalse\n[, aa, ]\n")


@test("stdlib: regex unicode + anchors")
def _():
    src = ('print(regex.matches(regex("h.llo"), "h\u00e9llo"))\n'
           'print(regex.find(regex("."), "a\\nb"))\n'
           'print(regex.replace(regex("([a-z]+)@([a-z]+)"), "x@yy", "$2@$1"))\n'
           'print(regex.replace(regex("a+"), "aaa", "$$"))\n')
    eq(run_src(src), "true\na\nyy@x\n$\n")


@test("stdlib: crypto digests")
def _():
    src = ('print(crypto.sha256("hello"))\n'
           'print(crypto.sha1("hello"))\n'
           'print(crypto.md5("hello"))\n'
           'print(crypto.hmac_sha256("key", '
           '"The quick brown fox jumps over the lazy dog"))\n')
    eq(run_src(src),
       "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824\n"
       "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d\n"
       "5d41402abc4b2a76b9719d911017c592\n"
       "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8\n")


@test("stdlib: random determinism with seed")
def _():
    src = ('random.seed(42)\n'
           'let a = random.int(1, 100)\n'
           'let b = random.float()\n'
           'let c = random.shuffle([1, 2, 3, 4, 5])\n'
           'random.seed(42)\n'
           'print(random.int(1, 100) == a)\n'
           'print(random.float() == b)\n'
           'print(random.shuffle([1, 2, 3, 4, 5]) == c)\n'
           'print(random.choice([7]))\n'
           'print(len(random.shuffle([1, 2, 3])) == 3)\n')
    eq(run_src(src), "true\ntrue\ntrue\n7\ntrue\n")


@test("stdlib: random errors")
def _():
    def go1():
        run_src('print(random.int(10, 1))\n')
    raises_code(go1, "E300")
    def go2():
        run_src('print(random.choice([]))\n')
    raises_code(go2, "E300")


@test("stdlib: time")
def _():
    src = ('let t = time.from_parts(2026, 10, 7, 12, 0, 0)\n'
           'print(time.weekday(t))\n'
           'print(time.to_utc(t) == t)\n'
           'print(time.add(t, 3600) - t)\n'
           'print(time.add_hours(t, 2) - t)\n'
           'print(time.in_tz(t, 1) - t)\n'
           'print(time.is_leap(2024))\n'
           'print(time.is_leap(2025))\n'
           'print(time.is_leap(2000))\n'
           'print(time.is_leap(1900))\n'
           'print(time.now_utc() > 0)\n')
    eq(run_src(src),
       "Wednesday\ntrue\n3600\n7200\n3600\n"
       "true\nfalse\ntrue\nfalse\ntrue\n")


@test("stdlib: time.from_parts rejects invalid dates")
def _():
    def go():
        run_src('print(time.from_parts(2026, 2, 30, 0, 0, 0))\n')
    raises_code(go, "E300")


@test("stdlib: fs temp files")
def _():
    src = ('let f = fs.temp_file()\n'
           'let d = fs.temp_dir()\n'
           'print(len(f) > 0)\n'
           'print(len(d) > 0)\n'
           'print(f != d)\n')
    eq(run_src(src), "true\ntrue\ntrue\n")


@test("stdlib: map/filter/fold/sort_by")
def _():
    src = ('print(map([1, 2, 3], |x| x * 2))\n'
           'print(filter([1, 2, 3, 4], |x| x % 2 == 0))\n'
           'print(fold([1, 2, 3, 4], 0, |a, b| a + b))\n'
           'print(fold(["a", "b"], "", |a, b| a + b))\n'
           'print(sort_by([3, 1, 2], |a, b| a < b))\n'
           'print(sort_by(["bb", "a", "ccc"], |a, b| len(a) < len(b)))\n')
    eq(run_src(src),
       "[2, 4, 6]\n[2, 4]\n10\nab\n[1, 2, 3]\n[a, bb, ccc]\n")


@test("stdlib: sort_by is stable")
def _():
    src = ('let xs = [{k: 1, v: "a"}, {k: 1, v: "b"}, {k: 0, v: "c"}]\n'
           'let s = sort_by(xs, |a, b| a.k < b.k)\n'
           'print(map(s, |x| x.v))\n')
    eq(run_src(src), "[c, a, b]\n")


@test("stdlib: list.flat/zip/unique")
def _():
    src = ('print(list.flat([[1, 2], [3], [4, 5]]))\n'
           'print(list.flat([]))\n'
           'print(list.zip([1, 2, 3], ["a", "b"]))\n'
           'print(list.unique([3, 1, 3, 2, 1]))\n')
    eq(run_src(src),
       "[1, 2, 3, 4, 5]\n[]\n[[1, a], [2, b]]\n[3, 1, 2]\n")


@test("stdlib: set semantics")
def _():
    src = ('let s = set()\n'
           's.add(1)\n'
           's.add(2)\n'
           's.add(1)\n'
           'print(set.len(s))\n'
           'print(len(s))\n'
           'print(set.contains(s, 2))\n'
           'print(set.contains(s, 9))\n'
           's.remove(1)\n'
           'print(set.to_list(s))\n'
           's.remove(99)\n'
           'print(set.len(s))\n'
           'let a = set()\n'
           'a.add(1)\n'
           'a.add(2)\n'
           'let b = set()\n'
           'b.add(2)\n'
           'b.add(3)\n'
           'print(set.to_list(set.union(a, b)))\n'
           'print(set.to_list(set.intersect(a, b)))\n'
           'print(set.to_list(set.diff(a, b)))\n')
    eq(run_src(src),
       "2\n2\ntrue\nfalse\n[2]\n1\n[1, 2, 3]\n[2]\n[1]\n")


@test("stdlib: set of text + typed set")
def _():
    src = ('let s: Set<Text> = set()\n'
           's.add("x")\n'
           's.add("y")\n'
           'print(set.contains(s, "x"))\n'
           'print(set.to_list(s))\n')
    eq(run_src(src), "true\n[x, y]\n")


@test("stdlib: str extras")
def _():
    src = ('print(str.join(["a", "b", "c"], ", "))\n'
           'print(str.join([], ", "))\n'
           'print(str.replace("aaa", "a", "bb"))\n'
           'print(str.replace("hello", "l", ""))\n'
           'print(str.to_upper("Hello, World!"))\n'
           'print(str.to_lower("Hello, World!"))\n'
           'print(str.ends_with("hello", "lo"))\n'
           'print(str.ends_with("hello", "la"))\n'
           'print(str.pad_left("42", 5, "0"))\n'
           'print(str.pad_right("42", 5, "0"))\n'
           'print(str.pad_left("abcdef", 3, "0"))\n')
    eq(run_src(src),
       "a, b, c\n\nbbbbbb\nheo\nHELLO, WORLD!\nhello, world!\n"
       "true\nfalse\n00042\n42000\nabcdef\n")


@test("stdlib: str errors")
def _():
    def go1():
        run_src('print(str.replace("a", "", "b"))\n')
    raises_code(go1, "E300")
    def go2():
        run_src('print(str.pad_left("a", 3, ""))\n')
    raises_code(go2, "E300")


# ------------------------------------------------------------ Final wave: compression

@test("compress: gzip round-trip + Bytes rendering + len + equality")
def _():
    src = ('let b = compress.gzip("hello world")\n'
           'print(len(b))\n'
           'print(b)\n'
           'print(compress.gunzip(b))\n'
           'print(compress.gzip("hello world") == b)\n'
           'print(compress.gzip("other") == b)\n'
           'print(compress.gzip("other") != b)\n')
    eq(run_src(src),
       "31\n<bytes: 31>\nhello world\ntrue\nfalse\ntrue\n")


@test("compress: known vector is byte-exact (file API)")
def _():
    import gzip as _gz
    import shutil
    # Reference: level 9, mtime=0. Both backends must emit exactly
    # these bytes (the AOT parity program asserts the same).
    expected = _gz.compress(b"hello world", compresslevel=9, mtime=0)
    assert len(expected) == 31, len(expected)
    assert expected[:10] == bytes(
        [0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x03])
    d = tempfile.mkdtemp(prefix="misa-gz-")
    try:
        inp = os.path.join(d, "in.txt")
        outp = os.path.join(d, "out.gz")
        with open(inp, "wb") as f:
            f.write(b"hello world")
        eq(run_src(f'let n = compress.gzip_file("{inp}", "{outp}")\n'
                   f'print(n)\n'), "31\n")
        assert open(outp, "rb").read() == expected, "not byte-identical"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("compress: gunzip_bytes round-trip")
def _():
    src = ('let b = compress.gzip("abcabcabc")\n'
           'let r = compress.gunzip_bytes(b)\n'
           'print(len(r))\n'
           'print(r == compress.gunzip_bytes(compress.gzip("abcabcabc")))\n')
    eq(run_src(src), "9\ntrue\n")


@test("compress: unicode round-trip")
def _():
    src = ('let t = "h\xc3\xa9llo w\xc3\xb6rld"\n'
           'print(compress.gunzip(compress.gzip(t)) == t)\n')
    eq(run_src(src), "true\n")


@test("compress: corrupt in-memory input is E300")
def _():
    # gunzip_bytes(gzip("hi")) is raw b"hi" -- not a gzip stream.
    def go():
        run_src('let raw = compress.gunzip_bytes(compress.gzip("hi"))\n'
                'print(compress.gunzip(raw))\n')
    raises_code(go, "E300")


@test("compress: corrupt file is E300, missing file is E303")
def _():
    import shutil
    d = tempfile.mkdtemp(prefix="misa-gz-")
    try:
        bad = os.path.join(d, "bad.gz")
        out = os.path.join(d, "out.txt")
        with open(bad, "wb") as f:
            f.write(b"not gzip at all........")
        def go_bad():
            run_src(f'print(compress.gunzip_file("{bad}", "{out}"))\n')
        raises_code(go_bad, "E300")

        def go_trunc():
            # truncated: header only, no deflate stream
            tpath = os.path.join(d, "trunc.gz")
            with open(tpath, "wb") as f:
                f.write(bytes([0x1f, 0x8b, 0x08, 0x00] * 3))
            run_src(f'print(compress.gunzip_file("{tpath}", "{out}"))\n')
        raises_code(go_trunc, "E300")

        def go_missing():
            run_src(f'print(compress.gzip_file("{d}/nope.txt", "{out}"))\n')
        raises_code(go_missing, "E303")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("compress: file round-trip (1MB)")
def _():
    import shutil
    d = tempfile.mkdtemp(prefix="misa-gz-")
    try:
        inp = os.path.join(d, "big.txt")
        gz = os.path.join(d, "big.gz")
        back = os.path.join(d, "big.back")
        data = ("The quick brown fox jumps over the lazy dog. " * 23000)
        assert len(data.encode("utf-8")) > 1000000
        with open(inp, "w", encoding="utf-8") as f:
            f.write(data)
        src = (f'let n = compress.gzip_file("{inp}", "{gz}")\n'
               f'print(n > 0)\n'
               f'print(n < 1000000)\n'
               f'let m = compress.gunzip_file("{gz}", "{back}")\n'
               f'print(m > 1000000)\n')
        eq(run_src(src), "true\ntrue\ntrue\n")
        with open(back, encoding="utf-8") as f:
            assert f.read() == data, "1MB round-trip mismatch"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("compress: gunzip needs throws E300 (E227)")
def _():
    errs = check_source('fn f() -> Text {\n'
                        '  return compress.gunzip(compress.gzip("x"))\n'
                        '}\n')
    assert any(e["code"] == "E227" for e in errs), errs
    errs = check_source('fn f() -> Text throws E300 {\n'
                        '  return compress.gunzip(compress.gzip("x"))\n'
                        '}\n')
    assert not errs, errs


# ------------------------------------------------------------ cli: declarative args parsing

_CLI_PROG = (
    'let p = cli.parser("tasks", "A tiny task manager")\n'
    'p.flag("db", "PATH", "Database path", "/tmp/tasks.db")\n'
    'p.bool_flag("verbose", "Verbose output")\n'
    'p.short("db", "d")\n'
    'p.command("add", "Add a task")\n'
    'p.command("list", "List tasks")\n'
)


@test("cli: long flags, = form, bool flags, defaults")
def _():
    src = _CLI_PROG + (
        'let args = p.parse(argv())\n'
        'print(args.get("db"))\n'
        'print(args.get_bool("verbose"))\n'
        'print(args.command())\n'
        'print(args.positionals())\n'
    )
    out = run_src_argv(src, ["--db", "/tmp/x.db", "--verbose",
                             "add", "Buy milk"])
    eq(out, "/tmp/x.db\ntrue\nadd\n[Buy milk]\n")
    out = run_src_argv(src, ["--db=/tmp/y.db", "list"])
    eq(out, "/tmp/y.db\nfalse\nlist\n[]\n")
    # defaults when flags absent
    out = run_src_argv(src, ["list"])
    eq(out, "/tmp/tasks.db\nfalse\nlist\n[]\n")


@test("cli: short flags")
def _():
    src = _CLI_PROG + (
        'let args = p.parse(argv())\n'
        'print(args.get("db"))\n'
    )
    eq(run_src_argv(src, ["-d", "/tmp/s.db", "list"]), "/tmp/s.db\n")
    eq(run_src_argv(src, ["-d=/tmp/e.db", "list"]), "/tmp/e.db\n")


@test("cli: -- terminator and lone -")
def _():
    src = _CLI_PROG + (
        'let args = p.parse(argv())\n'
        'print(args.command())\n'
        'print(args.positionals())\n'
    )
    out = run_src_argv(src, ["--", "--db", "add"])
    eq(out, "nil\n[--db, add]\n")
    # lone - is a positional (here: after --, so not a command word)
    out = run_src_argv(src, ["list", "--", "-"])
    eq(out, "list\n[-]\n")
    # ...but as the first word with commands defined it is an
    # unknown command, like any other invalid command word
    e = raises_code(lambda: run_src_argv(src, ["-", "list"]), "E300")
    assert "unknown command '-'" in e.message, e.message


@test("cli: --help prints and returns nil")
def _():
    src = _CLI_PROG + (
        'let args = p.parse(argv())\n'
        'if args == nil {\n'
        '  print("got nil")\n'
        '} else {\n'
        '  print("got args")\n'
        '}\n'
    )
    out = run_src_argv(src, ["--help"])
    assert "got nil" in out, out
    help_text = out.split("got nil")[0]
    eq(help_text,
       "tasks - A tiny task manager\n"
       "\n"
       "Usage: tasks [flags] <command> [args]\n"
       "\n"
       "Flags:\n"
       "  -d, --db PATH  Database path (default: /tmp/tasks.db)\n"
       "  --verbose      Verbose output\n"
       "  -h, --help     Show this help and exit\n"
       "\n"
       "Commands:\n"
       "  add   Add a task\n"
       "  list  List tasks\n")
    # -h is the short form
    out = run_src_argv(src, ["-h"])
    assert "Show this help and exit" in out, out
    assert "got nil" in out, out


@test("cli: no commands -> all positionals")
def _():
    src = ('let p = cli.parser("tool", "does things")\n'
           'p.flag("out", "FILE", "output file")\n'
           'let args = p.parse(argv())\n'
           'print(args.command())\n'
           'print(args.positionals())\n'
           'print(args.get("out"))\n')
    out = run_src_argv(src, ["a", "b", "--out", "f.txt"])
    eq(out, "nil\n[a, b]\nf.txt\n")


@test("cli: E300 parse errors")
def _():
    prog = _CLI_PROG + 'let args = p.parse(argv())\n'
    cases = [
        (["--bogus"], "unknown flag '--bogus'"),
        (["--db"], "flag '--db' needs a value"),
        (["--db", "--verbose"], "flag '--db' needs a value"),
        (["--verbose=x"], "takes no value"),
        (["-abc"], "combined short flags"),
        (["-z"], "unknown flag '-z'"),
        (["frobnicate"], "unknown command 'frobnicate'"),
    ]
    for argv, msg in cases:
        e = raises_code(lambda: run_src_argv(prog, argv), "E300")
        assert msg in e.message, (argv, e.message)


@test("cli: E300 on get/get_bool misuse")
def _():
    prog = _CLI_PROG + 'let args = p.parse(argv())\n'
    e = raises_code(
        lambda: run_src_argv(prog + 'print(args.get("nope"))\n', ["list"]),
        "E300")
    assert "unknown flag 'nope'" in e.message, e.message
    e = raises_code(
        lambda: run_src_argv(prog + 'print(args.get("verbose"))\n', ["list"]),
        "E300")
    assert "use get_bool" in e.message, e.message
    e = raises_code(
        lambda: run_src_argv(prog + 'print(args.get_bool("db"))\n', ["list"]),
        "E300")
    assert "use get, not get_bool" in e.message, e.message
    # required flag (no default) not provided
    src = ('let p = cli.parser("t", "d")\n'
           'p.flag("req", "V", "required")\n'
           'let args = p.parse(argv())\n'
           'print(args.get("req"))\n')
    e = raises_code(lambda: run_src_argv(src, []), "E300")
    assert "was not provided and has no default" in e.message, e.message


@test("cli: get on nil args is E300")
def _():
    # NOTE: canonical cli.get form — the test harness runs the raw
    # AST (not the checker-desugared one), and a nil receiver has no
    # runtime type for the interpreter's method fallback. The real
    # CLI desugars `args.get(..)` via the checker, same E300.
    src = _CLI_PROG + ('let args = p.parse(["--help"])\n'
                       'print(cli.get(args, "db"))\n')
    e = raises_code(lambda: run_src_argv(src, []), "E300")
    assert "no parsed arguments" in e.message, e.message
    # ...but get_command/positionals degrade gracefully on nil
    src = _CLI_PROG + ('let args = p.parse(["--help"])\n'
                       'print(cli.get_command(args))\n'
                       'print(cli.positionals(args))\n')
    out = run_src_argv(src, [])
    assert "nil\n[]\n" in out, out


@test("cli: duplicate definitions are E300")
def _():
    e = raises_code(
        lambda: run_src_argv(
            'let p = cli.parser("t", "d")\n'
            'p.flag("db", "P", "x")\n'
            'p.flag("db", "P", "y")\n', []),
        "E300")
    assert "duplicate flag 'db'" in e.message, e.message
    e = raises_code(
        lambda: run_src_argv(
            'let p = cli.parser("t", "d")\n'
            'p.flag("db", "P", "x")\n'
            'p.short("nope", "d")\n', []),
        "E300")
    assert "no such flag 'nope'" in e.message, e.message


@test("cli: parse/get need throws E300 (E227)")
def _():
    errs = check_source('fn f(a: [Text]) -> Void {\n'
                        '  let p = cli.parser("t", "d")\n'
                        '  let args = p.parse(a)\n'
                        '}\n')
    assert any(e["code"] == "E227" for e in errs), errs
    errs = check_source('fn f(a: [Text]) -> Void throws E300 {\n'
                        '  let p = cli.parser("t", "d")\n'
                        '  let args = p.parse(a)\n'
                        '  print(args.get("x"))\n'
                        '}\n')
    assert not errs, errs


@test("cli: check-time E200 for bad literals")
def _():
    errs = check_source('let p = cli.parser("t", "d")\n'
                        'p.flag("", "V", "h")\n')
    assert any(e["code"] == "E200" for e in errs), errs
    errs = check_source('let p = cli.parser("t", "d")\n'
                        'p.flag("db", "V", "h")\n'
                        'p.short("db", "dd")\n')
    assert any(e["code"] == "E200" for e in errs), errs


@test("cli: method desugar on CliArgs? receiver")
def _():
    # args is CliArgs?; method calls must still rewrite to cli.*.
    errs = check_source(_CLI_PROG +
                        'let args = p.parse(argv())\n'
                        'print(args.get("db"))\n'
                        'print(args.get_bool("verbose"))\n'
                        'print(args.command())\n'
                        'print(args.positionals())\n')
    assert not errs, errs


# ---------------------------------------------------------------- Phase 5d:
# advanced types — where clauses, match guards, W301, TCO

WHERE_PREAMBLE = '''
interface Comparable {
  fn cmp(self, other: Box) -> Int
}
interface Printable {
  fn show(self) -> Text
}
type Box { v: Int }
impl Comparable for Box {
  fn cmp(self, other: Box) -> Int { return self.v - other.v }
}
impl Printable for Box {
  fn show(self) -> Text { return "box({self.v})" }
}
'''

SUM5D = '''
type Shape {
  Circle(radius: Float),
  Rect(w: Float, h: Float),
}
type Opt { None, Some(Int) }
'''


@test("5d parser: where clause single bound")
def _():
    prog = parse_source("fn sort<T>(xs: [T]) -> [T] where T: Comparable "
                        "{ return xs }\n")
    fn = prog.items[0]
    eq(fn.where_bounds, {"T": ["Comparable"]})


@test("5d parser: where clause multiple bounds and params")
def _():
    prog = parse_source("fn f<T, K>(x: T, y: K) -> Int "
                        "where T: Comparable + Printable, K: Printable "
                        "{ return 0 }\n")
    fn = prog.items[0]
    eq(fn.where_bounds, {"T": ["Comparable", "Printable"],
                         "K": ["Printable"]})


@test("5d parser: where clause on type decl")
def _():
    prog = parse_source("type Cache<K, V> where K: Comparable "
                        "{ k: K }\n")
    td = prog.items[0]
    eq(td.where_bounds, {"K": ["Comparable"]})


@test("5d parser: where on unknown type param is ParseError")
def _():
    try:
        parse_source("fn f<T>(x: T) -> T where K: Comparable "
                     "{ return x }\n")
    except ParseError as e:
        assert "not a type parameter" in str(e), e
        return
    raise AssertionError("expected ParseError")


@test("5d parser: where without type params is ParseError")
def _():
    try:
        parse_source("fn f(x: Int) -> Int where T: Comparable "
                     "{ return x }\n")
    except ParseError:
        return
    raise AssertionError("expected ParseError")


@test("5d checker: where bound dispatches in body")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
fn describe<T>(x: T) -> Text where T: Printable {
  return x.show()
}
fn main() -> Int {
  let b: Box = { v: 3 }
  print(describe(b))
  return 0
}
main()
''')
    eq(errs, [])


@test("5d checker: where bound multiple — method from each")
def _():
    errs = check_source('''
interface A { fn am(self) -> Text }
interface B { fn bm(self) -> Text }
type T2 { v: Int }
impl A for T2 { fn am(self) -> Text { return "a" } }
impl B for T2 { fn bm(self) -> Text { return "b" } }
fn both<T>(x: T) -> Text where T: A + B {
  return x.am() + x.bm()
}
fn main() -> Int {
  let t: T2 = { v: 1 }
  print(both(t))
  return 0
}
main()
''')
    eq(errs, [])


@test("5d checker: E229 unknown interface in where")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
fn f<T>(x: T) -> T where T: Nope { return x }
''')
    assert any(e["code"] == "E229" for e in errs), errs


@test("5d checker: E228 where-bound violation at call")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
fn show_it<T>(x: T) -> Text where T: Printable { return x.show() }
fn main() -> Int {
  print(show_it(42))
  return 0
}
main()
''')
    assert any(e["code"] == "E228" for e in errs), errs


@test("5d checker: where-bound satisfied passes")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
fn show_it<T>(x: T) -> Text where T: Printable { return x.show() }
fn main() -> Int {
  let b: Box = { v: 1 }
  print(show_it(b))
  return 0
}
main()
''')
    eq(errs, [])


@test("5d checker: inline bound and where bound merge")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
fn f<T: Printable>(x: T) -> Text where T: Printable {
  return x.show()
}
''')
    eq(errs, [])


@test("5d checker: where on type decl validates bounds")
def _():
    errs = check_source(WHERE_PREAMBLE + '''
type Cache<K, V> where K: Comparable {
  k: K,
  v: V,
}
''')
    eq(errs, [])


@test("5d checker: E229 unknown interface in type where")
def _():
    errs = check_source('type C<K> where K: Nope { k: K }\n')
    assert any(e["code"] == "E229" for e in errs), errs


@test("5d interp: where-bounded generic end to end")
def _():
    _, buf = run_src_checked(WHERE_PREAMBLE + '''
fn show_it<T>(x: T) -> Text where T: Printable { return x.show() }
fn main() -> Int {
  let b: Box = { v: 7 }
  print(show_it(b))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "box(7)")


@test("5d formatter: where clause round-trips")
def _():
    src = ("fn sort<T>(xs: [T]) -> [T] where T: Comparable + Printable "
           "{ return xs }\n")
    out = format_source(src, "f.misa")
    assert "where T: Comparable + Printable" in out, out
    # idempotent
    eq(format_source(out, "f.misa"), out)


@test("5d parser: match guard on variant, bare, wildcard")
def _():
    prog = parse_source('fn f(s: Shape) -> Int {\n'
                        '  return match s {\n'
                        '    Shape.Circle(r) if r > 0 => 1,\n'
                        '    Opt.None if ok => 2,\n'
                        '    _ if ok => 3,\n'
                        '    _ => 4,\n'
                        '  }\n'
                        '}\n')
    arms = prog.items[0].body[0].value.arms
    eq([a.guard is not None for a in arms], [True, True, True, False])


@test("5d checker: E230 guard must be Bool")
def _():
    errs = check_source(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) if r => r,
    Shape.Circle(r) => 0.0,
    Shape.Rect(w, h) => 0.0,
  }
}
''')
    assert any(e["code"] == "E230" for e in errs), errs


@test("5d checker: guard sees arm bindings with payload types")
def _():
    # r is Float; r > 0 is Bool — clean.
    errs = check_source(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) if r > 0.0 => r,
    Shape.Circle(r) => 0.0,
    Shape.Rect(w, h) => w,
  }
}
''')
    eq(errs, [])


@test("5d checker: guarded arm does not satisfy exhaustiveness")
def _():
    errs = check_source(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) if r > 0.0 => r,
    Shape.Rect(w, h) => w,
  }
}
''')
    assert any(e["code"] == "E216" for e in errs), errs


@test("5d checker: guardless arm after guarded covers")
def _():
    errs = check_source(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) if r > 0.0 => r,
    Shape.Circle(r) => 0.0,
    Shape.Rect(w, h) => w,
  }
}
''')
    eq(errs, [])


@test("5d interp: guard fallthrough in source order")
def _():
    _, buf = run_src_checked(SUM5D + '''
fn classify(s: Shape) -> Text {
  return match s {
    Shape.Circle(r) if r > 10.0 => "big",
    Shape.Circle(r) if r > 0.0 => "small",
    Shape.Circle(r) => "degenerate",
    Shape.Rect(w, h) => "rect",
  }
}
fn main() -> Int {
  print(classify(Shape.Circle(20.0)))
  print(classify(Shape.Circle(5.0)))
  print(classify(Shape.Circle(0.0)))
  print(classify(Shape.Circle(-1.0)))
  print(classify(Shape.Rect(1.0, 2.0)))
  return 0
}
main()
''')
    eq(buf.getvalue().strip().splitlines(),
       ["big", "small", "degenerate", "degenerate", "rect"])


@test("5d interp: guarded wildcard")
def _():
    _, buf = run_src_checked(SUM5D + '''
fn f(o: Opt) -> Text {
  return match o {
    Opt.Some(x) if x > 0 => "pos",
    _ if true => "other",
    _ => "unreachable",
  }
}
fn main() -> Int {
  print(f(Opt.Some(3)))
  print(f(Opt.Some(-3)))
  print(f(Opt.None))
  return 0
}
main()
''')
    eq(buf.getvalue().strip().splitlines(),
       ["pos", "other", "other"])


@test("5d formatter: match guard round-trips")
def _():
    src = ("fn f(s: Shape) -> Int {\n"
           "  return match s { Shape.Circle(r) if r > 0 => 1, _ => 2 }\n"
           "}\n")
    out = format_source(src, "f.misa")
    assert "if r > 0 =>" in out, out
    eq(format_source(out, "f.misa"), out)


def _warnings(src):
    c = Checker()
    prog = parse_source(src)
    errs = c.check(prog)
    assert not errs, errs
    return c.warnings


@test("5d checker: W301 arm after wildcard")
def _():
    ws = _warnings(SUM5D + '''
fn f(s: Shape) -> Int {
  return match s {
    _ => 1,
    Shape.Circle(r) => 2,
  }
}
''')
    assert any(w["code"] == "W301" for w in ws), ws


@test("5d checker: W301 duplicate variant arm")
def _():
    ws = _warnings(SUM5D + '''
fn f(s: Shape) -> Int {
  return match s {
    Shape.Circle(r) => 1,
    Shape.Circle(r) => 2,
    Shape.Rect(w, h) => 3,
  }
}
''')
    assert any(w["code"] == "W301" for w in ws), ws


@test("5d checker: W301 dead wildcard after full coverage")
def _():
    ws = _warnings(SUM5D + '''
fn f(s: Shape) -> Int {
  return match s {
    Shape.Circle(r) => 1,
    Shape.Rect(w, h) => 2,
    _ => 3,
  }
}
''')
    assert any(w["code"] == "W301" for w in ws), ws


@test("5d checker: no W301 when earlier arm has guard")
def _():
    ws = _warnings(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) if r > 0.0 => r,
    Shape.Circle(r) => 0.0,
    Shape.Rect(w, h) => w,
  }
}
''')
    assert not any(w["code"] == "W301" for w in ws), ws


@test("5d checker: no W301 on clean match")
def _():
    ws = _warnings(SUM5D + '''
fn f(s: Shape) -> Float {
  return match s {
    Shape.Circle(r) => r,
    Shape.Rect(w, h) => w,
  }
}
''')
    eq(ws, [])


@test("5d cli: --deny-warnings promotes W301 to error")
def _():
    import subprocess, tempfile, os
    src = SUM5D + '''
fn f(s: Shape) -> Int {
  return match s {
    _ => 1,
    Shape.Circle(r) => 2,
  }
}
'''
    with tempfile.NamedTemporaryFile("w", suffix=".misa",
                                     delete=False) as tf:
        tf.write(src)
        path = tf.name
    try:
        misa = os.path.join(ROOT, "impl", "bin", "misa")
        r1 = subprocess.run([misa, "check", path], capture_output=True,
                            text=True, timeout=60)
        eq(r1.returncode, 0, r1.stderr)
        assert "W301" in r1.stderr, r1.stderr
        r2 = subprocess.run([misa, "check", path, "--deny-warnings"],
                            capture_output=True, text=True, timeout=60)
        assert r2.returncode != 0, r2.stdout
        assert "W301" in r2.stderr, r2.stderr
    finally:
        os.unlink(path)


@test("5d interp: TCO deep tail recursion")
def _():
    _, buf = run_src_checked('''
fn sum_to(n: Int, acc: Int) -> Int {
  if n <= 0 {
    return acc
  }
  return sum_to(n - 1, acc + n)
}
fn main() -> Int {
  print(sum_to(100000, 0))
  return 0
}
main()
''')
    # 100000 deep would overflow Python's own stack without TCO.
    eq(buf.getvalue().strip(), "5000050000")


@test("5d interp: TCO preserves semantics (countdown)")
def _():
    _, buf = run_src_checked('''
fn count(n: Int) -> Int {
  if n <= 0 {
    return 0
  }
  return count(n - 1)
}
fn main() -> Int {
  print(count(50000))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "0")


@test("5d interp: no TCO inside nested lambda (correctness)")
def _():
    _, buf = run_src_checked('''
fn apply(f: (Int) -> Int, x: Int) -> Int {
  return f(x)
}
fn fact(n: Int) -> Int {
  if n <= 1 {
    return 1
  }
  let rec = |k: Int| k * fact(k - 1)
  return apply(rec, n)
}
fn main() -> Int {
  print(fact(10))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "3628800")


@test("5d interp: TCO through try/catch is not swallowed")
def _():
    _, buf = run_src_checked('''
fn down(n: Int) -> Int {
  if n <= 0 {
    return 0
  }
  try {
    return down(n - 1)
  } catch e {
    return -1
  }
  return -2
}
fn main() -> Int {
  print(down(20000))
  return 0
}
main()
''')
    eq(buf.getvalue().strip(), "0")


if __name__ == "__main__":
    # run all @test functions in definition order
    print(f"Misa v0.2 test suite — {len([k for k in list(globals())])} "
          f"checks starting")
    # tests self-register by running at import of decorators above
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for name, msg in FAILURES:
            print(f"  - {name}: {msg}")
        sys.exit(1)
