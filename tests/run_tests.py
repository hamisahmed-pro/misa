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
from misa.checker import check_source, check_file
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
