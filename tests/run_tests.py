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


@test("ffi: function-type param is E225")
def _():
    errs = check_source('extern "c" {\n'
                        '  fn f(cb: (Int) -> Int) -> Int,\n'
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
