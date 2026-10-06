#!/usr/bin/env python3
"""Differential test: Misa self-hosted parser vs Python parser.

For every .misa file in examples/ and tests/ (recursive), plus edge
cases: parse with Python (impl/misa/parser.py -> AST) and with the Misa
parser (selfhost/parser.misa via a driver that does lex() -> parse() ->
prints a canonical S-expression). Compares the S-expressions exactly,
including source positions.

The Misa driver is written to selfhost/_parse_driver.misa (so that
`import "lexer.misa"` / `import "parser.misa"` resolve) and removed
after the run. The file under test is passed via /tmp/_misa_parse_input.misa.

Error canonical form: ("ERR", code, message, line, col). The Misa parser
encodes positions into the thrown message as "line:col\\n"; the driver
prints it raw and this script splits it back. Lexer errors (no position
prefix on the Misa side) are compared by (code, message) only.

Usage: python3 selfhost/test_parser.py (from repo root)
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa.parser import parse_source, ParseError
from misa.lexer import LexError
from misa import ast as mast

MISA_BIN = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
SELFHOST = os.path.join(ROOT, "selfhost")
DRIVER_PATH = os.path.join(SELFHOST, "_parse_driver.misa")
INPUT_PATH = "/tmp/_misa_parse_input.misa"

PASS = 0
FAIL = 0
FAILURES = []

# ----------------------------------------------------------------------
# Python S-expression serializer (must match parser.misa's sexpr exactly)
# ----------------------------------------------------------------------

def qstr(s):
    out = '"'
    for c in s:
        if c == "\\":
            out += "\\\\"
        elif c == '"':
            out += '\\"'
        elif c == "\n":
            out += "\\n"
        elif c == "\r":
            out += "\\r"
        elif c == "\t":
            out += "\\t"
        else:
            out += c
    return out + '"'


def type_text(t):
    if t.is_map:
        s = "Map<%s,%s>" % (type_text(t.map_key), type_text(t.map_val))
    elif t.is_list:
        s = "[%s]" % t.name
    else:
        s = t.name
    if t.optional:
        s += "?"
    return s


def sx_type(t):
    return "(Type %s %d %d)" % (qstr(type_text(t)), t.line, t.col)


def sx_type_opt(t):
    return "nil" if t is None else sx_type(t)


def sx_list(ns):
    return "".join(" " + sx(x) for x in ns)


def sx_opt(x):
    return "nil" if x is None else sx(x)


def sx_fields(tag, fs):
    return "".join(" (%s %s %s)" % (tag, qstr(k), sx(v)) for k, v in fs)


def sx_params(ps):
    return "".join(" (param %s %s)" % (qstr(k), sx_type(v)) for k, v in ps)


def sx_tfields(fs):
    return "".join(" (field %s %s)" % (qstr(k), sx_type(v)) for k, v in fs)


def sx_part(p):
    if isinstance(p, str):
        return "(StrPart %s)" % qstr(p)
    return sx(p)


def sx_parts(ps):
    return "".join(" " + sx_part(p) for p in ps)


def sx(node):
    tag = type(node).__name__
    lc = " %d %d" % (node.line, node.col)
    if tag == "Program":
        return "(Program (items%s)%s)" % (sx_list(node.items), lc)
    if tag == "AppDecl":
        return "(AppDecl %s (fields%s)%s)" % (
            qstr(node.name), sx_fields("field", node.fields), lc)
    if tag == "ProviderDecl":
        return "(ProviderDecl %s %s %s%s)" % (
            qstr(node.name), qstr(node.key_env), qstr(node.model_file), lc)
    if tag == "ModelsDecl":
        e = "".join(" (entry %s %s %s)" % (qstr(a), qstr(p), qstr(m))
                    for a, p, m in node.entries)
        return "(ModelsDecl (entries%s)%s)" % (e, lc)
    if tag == "TypeDecl":
        return "(TypeDecl %s (fields%s)%s)" % (
            qstr(node.name), sx_tfields(node.fields), lc)
    if tag == "Budget":
        b = ""
        if node.tokens is not None:
            b += " (tokens %d)" % node.tokens
        if node.latency_ms is not None:
            b += " (latency %d)" % node.latency_ms
        if node.cost_usd is not None:
            b += " (cost %s)" % repr(node.cost_usd)
        return "(Budget%s%s)" % (b, lc)
    if tag == "ModelDecl":
        return "(ModelDecl %s (params%s) (ret %s) %s %s (budget %s) %d (prompt%s)%s)" % (
            qstr(node.name), sx_params(node.params), sx_type_opt(node.ret),
            qstr(node.provider), qstr(node.alias), sx_opt(node.budget),
            node.prompt_version, sx_parts(node.prompt), lc)
    if tag == "Component":
        return "(Component %s (args%s) (children%s)%s)" % (
            qstr(node.name), sx_fields("arg", node.args),
            sx_list(node.children), lc)
    if tag == "ScreenDecl":
        return "(ScreenDecl %s (params%s) (body%s)%s)" % (
            qstr(node.name), sx_params(node.params), sx_list(node.body), lc)
    if tag == "ScreenIf":
        return "(ScreenIf %s (then%s) (else%s)%s)" % (
            sx(node.cond), sx_list(node.then), sx_list(node.els), lc)
    if tag == "ScreenFor":
        return "(ScreenFor %s %s (body%s)%s)" % (
            qstr(node.var), sx(node.iter), sx_list(node.body), lc)
    if tag == "EvalDecl":
        return "(EvalDecl %s %s (given%s) (expect %s) %d%s)" % (
            qstr(node.name), qstr(node.model_name),
            sx_fields("field", node.given), sx_opt(node.expect),
            node.runs, lc)
    if tag == "FnDecl":
        return "(FnDecl %s (params%s) (ret %s) (body%s)%s)" % (
            qstr(node.name), sx_params(node.params), sx_type_opt(node.ret),
            sx_list(node.body), lc)
    if tag == "ImportDecl":
        return "(ImportDecl %s%s)" % (qstr(node.path), lc)
    if tag == "StreamDecl":
        return "(StreamDecl %s %s%s)" % (
            qstr(node.name), sx(node.call), lc)
    if tag == "LetStmt":
        return "(Let %s %d (ann %s) %s%s)" % (
            qstr(node.name), 1 if node.mutable else 0,
            sx_type_opt(node.ann), sx(node.value), lc)
    if tag == "AssignStmt":
        return "(Assign %s %s%s)" % (sx(node.target), sx(node.value), lc)
    if tag == "IfStmt":
        return "(If %s (then%s) (else%s)%s)" % (
            sx(node.cond), sx_list(node.then), sx_list(node.els), lc)
    if tag == "ForStmt":
        return "(For %s %s (body%s)%s)" % (
            qstr(node.var), sx(node.iter), sx_list(node.body), lc)
    if tag == "WhileStmt":
        return "(While %s (body%s)%s)" % (
            sx(node.cond), sx_list(node.body), lc)
    if tag == "ReturnStmt":
        return "(Return (value %s)%s)" % (sx_opt(node.value), lc)
    if tag == "TryStmt":
        return "(Try (body%s) (catch %s (body%s))%s)" % (
            sx_list(node.body), qstr(node.catch_var),
            sx_list(node.handler), lc)
    if tag == "ThrowStmt":
        return "(Throw %s%s)" % (sx(node.value), lc)
    if tag == "OnStmt":
        return "(On %s %s %s%s)" % (
            sx(node.stream), qstr(node.event), sx(node.handler), lc)
    if tag == "ExprStmt":
        return "(Expr %s%s)" % (sx(node.expr), lc)
    if tag == "IntLit":
        return "(Int %d%s)" % (node.value, lc)
    if tag == "FloatLit":
        return "(Float %s%s)" % (repr(node.value), lc)
    if tag == "StrLit":
        return "(Str%s%s)" % (sx_parts(node.parts), lc)
    if tag == "BoolLit":
        return "(Bool %s%s)" % ("true" if node.value else "false", lc)
    if tag == "NilLit":
        return "(Nil%s)" % lc
    if tag == "DurationLit":
        return "(Duration %d%s)" % (node.ms, lc)
    if tag == "MoneyLit":
        return "(Money %s%s)" % (repr(node.usd), lc)
    if tag == "Ident":
        return "(Ident %s%s)" % (qstr(node.name), lc)
    if tag == "Call":
        return "(Call %s (args%s)%s)" % (
            qstr(node.name), sx_list(node.args), lc)
    if tag == "Field":
        return "(Field %s %s%s)" % (sx(node.obj), qstr(node.field), lc)
    if tag == "Index":
        return "(Index %s %s%s)" % (sx(node.obj), sx(node.idx), lc)
    if tag == "ListLit":
        return "(List (items%s)%s)" % (sx_list(node.items), lc)
    if tag == "RecordLit":
        return "(Record (fields%s)%s)" % (
            sx_fields("field", node.fields), lc)
    if tag == "MapLit":
        return "(Map (entries%s)%s)" % (
            sx_fields("entry", node.entries), lc)
    if tag == "Lambda":
        h = " (params%s)" % "".join(" " + qstr(p) for p in node.params)
        if node.expr_body is not None:
            h += " (expr %s)" % sx(node.expr_body)
        else:
            h += " (body%s)" % sx_list(node.body)
        return "(Lambda%s%s)" % (h, lc)
    if tag == "BinOp":
        return "(BinOp %s %s %s%s)" % (
            qstr(node.op), sx(node.left), sx(node.right), lc)
    if tag == "UnOp":
        return "(UnOp %s %s%s)" % (qstr(node.op), sx(node.operand), lc)
    if tag == "Await":
        return "(Await %s%s)" % (sx(node.operand), lc)
    raise ValueError("unhandled AST node: %s" % tag)


# ----------------------------------------------------------------------
# Misa side
# ----------------------------------------------------------------------

DRIVER_SRC = '''import "lexer.misa"
import "parser.misa"

let src = fs.read("/tmp/_misa_parse_input.misa")
try {
    let toks = lex(src)
    let prog = parse(toks)
    print("OK")
    print(sexpr(prog))
} catch e {
    print("ERR")
    print(e.code)
    print(e.message)
}
'''


def write_driver():
    with open(DRIVER_PATH, "w") as f:
        f.write(DRIVER_SRC)


def misa_parse(src):
    """Run the Misa parser on src. Returns (ok, payload)."""
    with open(INPUT_PATH, "w") as f:
        f.write(src)
    r = subprocess.run(MISA_BIN + ["run", DRIVER_PATH],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return (False, ("RUNFAIL", "misa run failed rc=%d: %s" % (r.returncode, r.stderr), 0, 0))
    lines = r.stdout.split("\n")
    # drop trailing empty from final newline
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return (False, ("RUNFAIL", "empty misa output", 0, 0))
    if lines[0] == "OK":
        if len(lines) < 2:
            return (False, ("RUNFAIL", "OK without sexpr", 0, 0))
        return (True, lines[1])
    if lines[0] == "ERR":
        code = lines[1] if len(lines) > 1 else "?"
        rest = lines[2:]
        # Misa parser errors carry "line:col\\n" prefix from err_msg();
        # lexer errors have no prefix.
        line, col, msg = 0, 0, "\n".join(rest)
        if rest:
            m = re.fullmatch(r"(\d+):(\d+)", rest[0])
            if m:
                line, col = int(m.group(1)), int(m.group(2))
                msg = "\n".join(rest[1:])
        return (False, (code, msg, line, col))
    return (False, ("RUNFAIL", "unexpected misa output: %r" % lines[0], 0, 0))


def python_parse(src):
    """Parse with Python. Returns (ok, payload)."""
    try:
        prog = parse_source(src)
        return (True, sx(prog))
    except ParseError as e:
        return (False, ("E001", str(e), e.line, e.col))
    except LexError as e:
        # Misa lexer errors carry no position; compare (code, message) only
        return (False, ("E001", str(e), -1, -1))


def compare(py_result, misa_result, name):
    """Compare Python and Misa parse results. Returns True if match."""
    global PASS, FAIL
    py_ok, py_data = py_result
    misa_ok, misa_data = misa_result

    if py_ok and misa_ok:
        if py_data == misa_data:
            PASS += 1
            print("  ok  %s (%d chars)" % (name, len(py_data)))
            return True
        FAIL += 1
        FAILURES.append((name, "sexpr mismatch"))
        print("  FAIL %s: sexpr mismatch" % name)
        # show first difference
        a, b = py_data, misa_data
        i = 0
        while i < min(len(a), len(b)) and a[i] == b[i]:
            i += 1
        print("    first diff at char %d" % i)
        print("    python: ...%s..." % a[max(0, i - 60):i + 120])
        print("    misa:   ...%s..." % b[max(0, i - 60):i + 120])
        return False

    if py_ok != misa_ok:
        FAIL += 1
        FAILURES.append((name, "ok mismatch py=%s misa=%s" % (py_ok, misa_ok)))
        print("  FAIL %s: ok mismatch (python ok=%s, misa ok=%s)" % (name, py_ok, misa_ok))
        print("    python: %r" % (py_data,))
        print("    misa:   %r" % (misa_data,))
        return False

    # both errored: (code, msg, line, col)
    py_code, py_msg, py_line, py_col = py_data
    m_code, m_msg, m_line, m_col = misa_data
    if py_code == m_code and py_msg == m_msg and (
            (py_line, py_col) == (m_line, m_col) or py_line == -1):
        PASS += 1
        print("  ok  %s (both error: %s)" % (name, py_msg[:60]))
        return True
    FAIL += 1
    FAILURES.append((name, "error mismatch"))
    print("  FAIL %s: error mismatch" % name)
    print("    python: %r @%s:%s" % (py_msg, py_line, py_col))
    print("    misa:   %r @%s:%s" % (m_msg, m_line, m_col))
    return False


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


def test_edge_cases():
    cases = [
        ("empty program", ""),
        ("only whitespace/comments", "  \n // hi\n /* x */\n"),
        ("precedence arith", "let x = 1 + 2 * 3 - 4 / 2 % 3\n"),
        ("precedence bool", "let b = a and b or c and not d\n"),
        ("precedence cmp", "let c = a < b == c != d >= e <= f > g\n"),
        ("unary chains", "let u = - - x\nlet v = not not y\n"),
        ("await", "let w = await fetch(url)\n"),
        ("nested calls", "let r = f(g(1), h(2, 3))\n"),
        ("dotted call", "let s = fs.read(\"x\")\nlet t = str.slice(a, 0, 1)\n"),
        ("field index chains", "let q = a.b[0].c\n"),
        ("interp simple", "let s = \"hello {name}!\"\n"),
        ("interp nested braces", "let s = \"a {f({x: 1})} b\"\n"),
        ("interp empty", "let s = \"{}\"\n"),
        ("interp escaped brace", "let s = \"a {{ b\"\n"),
        ("interp multiline", "let s = \"\"\"\nline1 {x +\n  y} line2\n\"\"\"\n"),
        ("interp unbalanced", "let s = \"a {b\"\n"),
        ("interp trailing junk", "let s = \"{a} {b c}\"\n"),
        ("strings escapes", "let e = \"a\\nb\\t\\\\\\\"q\"\n"),
        ("empty string", "let e = \"\"\n"),
        ("numbers", "let i = 42\nlet f = 3.14\nlet d = 5s\nlet m = 100ms\nlet c = $5.99\n"),
        ("list trailing comma", "let l = [1, 2,]\n"),
        ("empty list/record/map", "let a = []\nlet b = {}\nlet c = {\"k\": 1}\n"),
        ("record nested", "let r = {a: 1, b: {c: [1, 2]}}\n"),
        ("map multi", "let mp = {\"a\": 1, \"b\": 2}\n"),
        ("lambda expr", "let f = |x| x + 1\n"),
        ("lambda block", "let f = |x| {\nreturn x\n}\n"),
        ("lambda empty block", "let f = || {}\n"),
        ("if/else", "if a {\nlet x = 1\n} else {\nlet y = 2\n}\n"),
        ("if semi else", "if a {\n} \nelse {\n}\n"),
        ("for loop", "for i in items {\nprint(i)\n}\n"),
        ("while loop", "while ok {\nok = next()\n}\n"),
        ("return bare", "fn f() {\nreturn\n}\n"),
        ("return expr", "fn f() -> Int {\nreturn 1 + 2\n}\n"),
        ("try/catch/throw", "try {\nrisky()\n} catch e {\nthrow e\n}\n"),
        ("assign field/index", "a.b = 1\na[0] = 2\nx = 3\n"),
        ("stream decl", "stream s = tick(1s)\n"),
        ("on stmt", "on s.data {\n|x| {\nhandle(x)\n}\n}\n"),
        ("type decl", "type Point {\nx: Int,\ny: Float\n}\n"),
        ("type nested", "type T {\na: [Int],\nb: Text?,\nc: Map<Text, [Int?]>\n}\n"),
        ("type ge-split", "fn f(m: Map<Text,Int>={}) {\n}\n"),
        ("fn full", "fn add(a: Int, b: Int) -> Int {\nreturn a + b\n}\n"),
        ("app decl", "app demo {\ntitle: \"Hi\",\nversion: 3\n}\n"),
        ("provider decl", "provider openai {\nkey: env(\"OPENAI_KEY\"),\nmodel: file(\"m.onnx\")\n}\n"),
        ("models decl", "models {\nfast: openai(\"gpt-4o-mini\"),\nslow: openai(\"gpt-4o\")\n}\n"),
        ("model decl", "model classify(img: Text) -> Text with openai.fast\nbudget {\ntokens: 100,\nlatency: 5s,\ncost: $0.50\n}\nprompt v2 \"\"\"\nlabel {img}\n\"\"\"\n"),
        ("model no budget", "model m(x: Int) -> Int with p.q\nprompt v1 \"hi\"\n"),
        ("screen dsl", "screen Home(user: Text) {\nColumn {\nText(text: \"hi {user}\")\nButton(id: go, label: \"Go\")\n}\n}\n"),
        ("screen if/for", "screen L(items: [Text]) {\nif show {\nText(text: \"y\")\n}\nfor i in items {\nText(text: i)\n}\n}\n"),
        ("eval decl", "eval e1 {\nmodel: classify,\ngiven: {img: \"x\"},\nexpect: \"cat\",\nruns: 3\n}\n"),
        ("import", "import \"lib.misa\"\n"),
        ("navigate call", "navigate(\"home\")\n"),
        ("navigate bare err", "navigate\n"),
        ("env call", "let k = env(\"K\")\n"),
        ("neg duration", "let d = -5s\n"),
        ("bad version", "model m(x: Int) -> Int with p.q\nprompt v9x \"hi\"\n"),
        ("missing else brace", "if a {\n"),
        ("assign to call err", "f(1) = 2\n"),
        ("map type opt", "let m: Map<Text,Int>? = {}\n"),
        ("list type opt", "let l: [Text]? = []\n"),
        ("deep nesting", "let x = (((((1)))))\n"),
        ("semicolons", "let a = 1; let b = 2;\n"),
    ]
    for name, src in cases:
        py_result = python_parse(src)
        misa_result = misa_parse(src)
        compare(py_result, misa_result, "edge: " + name)


def main():
    write_driver()
    try:
        print("Collecting .misa files...")
        files = collect_misa_files()
        print("Found %d files\n" % len(files))

        print("Testing example/test files:")
        for path in files:
            rel = os.path.relpath(path, ROOT)
            with open(path) as f:
                src = f.read()
            py_result = python_parse(src)
            misa_result = misa_parse(src)
            compare(py_result, misa_result, rel)

        print("\nTesting edge cases:")
        test_edge_cases()

        print("\n%d passed, %d failed" % (PASS, FAIL))
        if FAILURES:
            print("\nFailures:")
            for name, msg in FAILURES:
                print("  %s: %s" % (name, msg))
            sys.exit(1)
    finally:
        if os.path.exists(DRIVER_PATH):
            os.unlink(DRIVER_PATH)
        if os.path.exists(INPUT_PATH):
            os.unlink(INPUT_PATH)


if __name__ == "__main__":
    main()
