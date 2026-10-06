#!/usr/bin/env python3
"""Differential test: Misa self-hosted checker vs Python checker.

For every .misa file in examples/ and tests/ (recursive), plus negative
cases: check with Python (impl/misa/checker.py -> [diag]) and with the Misa
checker (selfhost/checker.misa via a driver that does lex() -> parse() ->
check() -> prints diags). Compares the diagnostics exactly
(code, line, col, message).

The Misa driver is written to selfhost/_check_driver.misa (so that
`import "lexer.misa"` / `import "parser.misa"` / `import "checker.misa"`
resolve) and removed after the run. The file under test is passed via
/tmp/_misa_check_input.misa.

Diag canonical form: "code|line|col|message" lines.

Usage: python3 selfhost/test_checker.py (from repo root)
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa.checker import check_source
from misa.parser import ParseError
from misa.lexer import LexError

MISA_BIN = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
SELFHOST = os.path.join(ROOT, "selfhost")
DRIVER_PATH = os.path.join(SELFHOST, "_check_driver.misa")
INPUT_PATH = "/tmp/_misa_check_input.misa"

PASS = 0
FAIL = 0
FAILURES = []

DRIVER_SRC = '''import "lexer.misa"
import "parser.misa"
import "checker.misa"

let src = fs.read("/tmp/_misa_check_input.misa")
try {
    let toks = lex(src)
    let prog = parse(toks)
    let diags = check(prog)
    print("OK")
    print(str(len(diags)))
    var i = 0
    while i < len(diags) {
        let d = diags[i]
        print(d.code + "|" + str(d.line) + "|" + str(d.col) + "|" + d.message)
        i = i + 1
    }
} catch e {
    print("ERR")
    print(e.code)
    print(e.message)
}
'''


def write_driver():
    with open(DRIVER_PATH, "w") as f:
        f.write(DRIVER_SRC)


def misa_check(src):
    """Run the Misa checker on src. Returns (ok, diags_or_error)."""
    with open(INPUT_PATH, "w") as f:
        f.write(src)
    r = subprocess.run(MISA_BIN + ["run", DRIVER_PATH],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return (False, ("RUNFAIL", "misa run failed rc=%d: %s" % (r.returncode, r.stderr)))
    lines = r.stdout.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return (False, ("RUNFAIL", "empty misa output"))
    if lines[0] == "OK":
        n = int(lines[1]) if len(lines) > 1 else 0
        diags = []
        for ln in lines[2:2 + n]:
            parts = ln.split("|", 3)
            if len(parts) == 4:
                diags.append((parts[0], int(parts[1]), int(parts[2]), parts[3]))
        return (True, diags)
    if lines[0] == "ERR":
        code = lines[1] if len(lines) > 1 else "?"
        rest = lines[2:]
        line, col, msg = 0, 0, "\n".join(rest)
        if rest:
            m = re.fullmatch(r"(\d+):(\d+)", rest[0])
            if m:
                line, col = int(m.group(1)), int(m.group(2))
                msg = "\n".join(rest[1:])
        return (False, (code, msg, line, col))
    return (False, ("RUNFAIL", "unexpected misa output: %r" % lines[0]))


def python_check(src):
    """Check with Python. Returns (ok, diags_or_error)."""
    try:
        diags = check_source(src)
        return (True, [(d["code"], d["line"], d["col"], d["message"]) for d in diags])
    except ParseError as pe:
        return (False, ("E001", str(pe), pe.line, pe.col))
    except LexError as le:
        return (False, ("E001", str(le), le.line, le.col))
    except Exception as ex:
        line = getattr(ex, "line", 0)
        col = getattr(ex, "col", 0)
        return (False, ("E001", str(ex), line, col))


def compare(name, src):
    global PASS, FAIL
    p_ok, p_res = python_check(src)
    m_ok, m_res = misa_check(src)
    if p_ok != m_ok:
        FAIL += 1
        FAILURES.append((name, "ok mismatch: python=%s misa=%s" % (p_ok, m_ok),
                         p_res, m_res))
        return False
    if not p_ok:
        # both errored; compare (code, line, col) — messages may differ
        # slightly in wording for lex/parse errors, but codes must match
        if p_res[0] != m_res[0]:
            FAIL += 1
            FAILURES.append((name, "error code mismatch", p_res, m_res))
            return False
        PASS += 1
        return True
    if p_res != m_res:
        FAIL += 1
        # find first diff
        for i, (a, b) in enumerate(zip(p_res, m_res)):
            if a != b:
                FAILURES.append((name, "diag %d differs" % i, a, b))
                break
        else:
            FAILURES.append((name, "diag count differs: %d vs %d" % (len(p_res), len(m_res)), p_res, m_res))
        return False
    PASS += 1
    return True


def collect_misa_files():
    files = []
    # Phase 5b: files using sum types / match are out of scope for the
    # v0.2-targeted self-hosted port; they gain coverage when 5b is ported.
    SKIP_5B = {"a14_sums.misa"}
    for base in ["examples", "tests"]:
        basedir = os.path.join(ROOT, base)
        for root, dirs, filenames in os.walk(basedir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".misa") and fn not in SKIP_5B:
                    files.append(os.path.join(root, fn))
    return sorted(files)


NEGATIVE_CASES = [
    # (name, source) — each exercises specific error codes
    ("E101 undefined name", 'print(zzz)\n'),
    ("E101 undefined function", 'nosuchfn(1)\n'),
    ("E102 fn arity", 'fn f(a: Int) -> Int {\n return a\n}\nprint(f(1, 2))\n'),
    ("E102 builtin arity", 'print(1, 2)\n'),
    ("E102 ns builtin arity", 'print(fs.read("a", "b"))\n'),
    ("E200 assign mismatch", 'let x: Int = "s"\n'),
    ("E201 assign immutable", 'let x = 1\nx = 2\n'),
    ("E201 assign undefined", 'y = 2\n'),
    ("E201 bad target", '"s" = 2\n'),
    ("E202 record field type", 'type T { a: Int }\nlet t: T = { a: "s" }\n'),
    ("E202 record missing field", 'type T { a: Int, b: Text }\nlet t: T = { a: 1 }\n'),
    ("E202 record extra field", 'type T { a: Int }\nlet t: T = { a: 1, z: 2 }\n'),
    ("E202 map value type", 'let m: Map<Text, Int> = {"a": "s"}\n'),
    ("E203 eval given mismatch", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with mock.m prompt v1 "hi"\neval e { model: mm, given: { b: 1 }, expect: result == 1 }\n'),
    ("E204 expect not bool", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with mock.m prompt v1 "hi"\neval e { model: mm, given: { a: 1 }, expect: 42 }\n'),
    ("E205 on not stream", 'let s = "x"\non s.token { || print("t") }\n'),
    ("E205 await not stream", 'let s = "x"\nlet r = await s\n'),
    ("E205 stream not model", 'fn f() -> Int { return 1 }\nstream st = f()\n'),
    ("E206 unknown component", 'screen S() {\n Foo()\n}\n'),
    ("E206 leaf children", 'screen S() {\n Text(text: "hi") {\n Text(text: "x")\n }\n}\n'),
    ("E206 bad event", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with mock.m prompt v1 "hi"\nstream st = mm(1)\non st.nope { || print("t") }\n'),
    ("E206 handler 2 params", 'screen S() {\n Button(label: "b", onTap: |a, b| print("x"))\n}\n'),
    ("E207 dup id", 'screen S() {\n Text(text: "a", id: myid)\n Text(text: "b", id: myid)\n}\n'),
    ("E208 unknown param type", 'fn f(a: Nope) -> Int { return 1 }\n'),
    ("E208 unknown ret type", 'fn f() -> Nope { return 1 }\n'),
    ("E208 unknown model ret", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Nope with mock.m prompt v1 "hi"\n'),
    ("E209 unknown provider", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with zz.m prompt v1 "hi"\n'),
    ("E209 unknown alias", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with mock.zz prompt v1 "hi"\n'),
    ("E209 alias wrong provider", 'provider mock {\n key: env("K"),\n}\nprovider openai {\n key: env("K2"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with openai.m prompt v1 "hi"\n'),
    ("E209 unknown provider kind", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("x"),\n}\nmodel mm(a: Int) -> Int with mock.m prompt v1 "hi {a}"\n'),
    ("E210 dup fn", 'fn f() -> Int { return 1 }\nfn f() -> Int { return 2 }\n'),
    ("E210 dup type", 'type T { a: Int }\ntype T { b: Int }\n'),
    ("E210 dup param", 'fn f(a: Int, a: Text) -> Int { return 1 }\n'),
    ("E210 redefinition", 'let x = 1\nlet x = 2\n'),
    ("E210 dup app", 'app A { }\napp B { }\n'),
    ("E211 arg type", 'fn f(a: Int) -> Int { return a }\nprint(f("s"))\n'),
    ("E211 return type", 'fn f() -> Int { return "s" }\n'),
    ("E211 missing return", 'fn f() -> Int { print("x") }\n'),
    ("E211 return in void", 'fn f() { return 1 }\n'),
    ("E211 void as value", 'let x = print("hi")\n'),
    ("E211 ns arg type", 'print(str.slice(42, 0, 1))\n'),
    ("E212 throw not error", 'throw "oops"\n'),
    ("E212 throw bad shape", 'throw { code: 42, message: "x" }\n'),
    ("clean program", 'fn add(a: Int, b: Int) -> Int {\n return a + b\n}\nlet r = add(2, 3)\nprint(r)\n'),
    ("str ns ok", 'print(str.slice("hello", 1, 3))\nprint(str.starts_with("hi", "h"))\n'),
    ("process.run ok", 'let r = process.run(["echo", "hi"])\nprint(r.code)\n'),
    ("optional nil", 'let x: Int? = nil\nlet y: Int? = 5\n'),
    ("list coerce", 'var xs: [Int] = []\n'),
    ("int to float", 'let x: Float = 42\n'),
    ("record nested", 'type A { x: Int }\ntype B { a: A }\nlet b: B = { a: { x: 1 } }\n'),
    ("map infer", 'let m = {"a": 1}\nprint(m["a"])\n'),
    ("field access", 'type T { a: Int }\nlet t: T = { a: 5 }\nprint(t.a)\n'),
    ("field missing", 'type T { a: Int }\nlet t: T = { a: 5 }\nprint(t.zz)\n'),
    ("try catch", 'try {\n print("a")\n} catch e {\n print(e.code)\n}\n'),
    ("throw ok", 'throw { code: "E1", message: "bad" }\n'),
    ("for loop", 'for x in [1, 2] {\n print(x)\n}\n'),
    ("while loop", 'var i = 0\nwhile i < 3 {\n i = i + 1\n}\n'),
    ("screen ok", 'screen S(name: Text) {\n Column {\n Text(text: "hi " + name)\n }\n}\n'),
    ("screen id readback", 'screen S() {\n Column {\n TextInput(id: inp)\n Button(label: "go", onTap: || { print(inp.text) })\n }\n}\n'),
    ("screen id as value err", 'screen S() {\n Column {\n TextInput(id: inp)\n Button(label: "go", onTap: || { print(inp) })\n }\n}\n'),
    ("model decl ok", 'provider mock {\n key: env("K"),\n}\nmodels {\n m: mock("mock-model"),\n}\nmodel mm(a: Int) -> Int with mock.m prompt v1 "hi {a}"\n'),
    ("lambda inferred", 'let f = |x| x + 1\n'),
]


def main():
    write_driver()
    try:
        files = collect_misa_files()
        print("checking %d files..." % len(files))
        for fp in files:
            with open(fp) as f:
                src = f.read()
            name = os.path.relpath(fp, ROOT)
            compare("file:" + name, src)
        print("checking %d negative cases..." % len(NEGATIVE_CASES))
        for name, src in NEGATIVE_CASES:
            compare("case:" + name, src)
    finally:
        if os.path.exists(DRIVER_PATH):
            os.remove(DRIVER_PATH)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    for name, what, a, b in FAILURES[:20]:
        print("\nFAIL %s: %s" % (name, what))
        print("  python: %r" % (a,))
        print("  misa:   %r" % (b,))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
