#!/usr/bin/env python3
"""Differential test: Misa self-hosted lexer vs Python lexer.

Runs the Python lexer (impl/misa/lexer.py) and the Misa lexer
(selfhost/lexer.misa) on every .misa file in examples/ and tests/,
plus edge cases. Compares token streams exactly (kind, line, col)
with value-aware text comparison (Python stores parsed numbers,
Misa stores raw lexemes).

Usage: python3 selfhost/test_lexer.py (from repo root)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa.lexer import Lexer, LexError

MISA_BIN = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
LEXER_MISA = os.path.join(ROOT, "selfhost", "lexer.misa")

PASS = 0
FAIL = 0
FAILURES = []


def esc(s):
    """Escape text for the pipe-delimited driver output."""
    return s.replace("\\", "\\\\").replace("|", "\\p").replace("\n", "\\n").replace("\r", "\\r")


def unesc(s):
    """Reverse esc()."""
    out = ""
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "\\":
                out += "\\"
            elif n == "p":
                out += "|"
            elif n == "n":
                out += "\n"
            elif n == "r":
                out += "\r"
            else:
                out += n
            i += 2
        else:
            out += s[i]
            i += 1
    return out


def python_lex(src):
    """Returns (ok, tokens_or_error). Tokens are (kind, value, line, col)."""
    try:
        toks = Lexer(src).tokens()
        return (True, [(t.kind, t.value, t.line, t.col) for t in toks])
    except LexError as e:
        return (False, (str(e), e.line, e.col))


def misa_lex(src_path):
    """Run the Misa lexer on a file. Returns (ok, tokens_or_error)."""
    with open(LEXER_MISA) as f:
        lexer_src = f.read()
    driver = lexer_src + f'''
let src = fs.read("{src_path}")
try {{
    let toks = lex(src)
    for t in toks {{
        print("TOK")
        print(t.kind)
        print(esc_text(t.text))
        print(str(t.line))
        print(str(t.col))
    }}
}} catch e {{
    print("ERR")
    print(esc_text(e.message))
}}

fn esc_text(s: Text) -> Text {{
    var out = ""
    for c in s {{
        if c == "\\\\" {{
            out = out + "\\\\\\\\"
        }} else {{
            if c == "\\n" {{
                out = out + "\\\\n"
            }} else {{
                if c == "\\r" {{
                    out = out + "\\\\r"
                }} else {{
                    out = out + c
                }}
            }}
        }}
    }}
    return out
}}
'''
    # Write driver to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".misa", delete=False) as f:
        f.write(driver)
        driver_path = f.name
    try:
        r = subprocess.run(MISA_BIN + ["run", driver_path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return (False, (f"misa run failed: {r.stderr}", 0, 0))
        lines = r.stdout.strip().split("\n")
        toks = []
        i = 0
        while i < len(lines):
            if lines[i] == "ERR":
                return (False, (unesc(lines[i + 1]), 0, 0))
            assert lines[i] == "TOK", f"expected TOK, got {lines[i]!r}"
            kind = lines[i + 1]
            text = unesc(lines[i + 2])
            line = int(lines[i + 3])
            col = int(lines[i + 4])
            toks.append((kind, text, line, col))
            i += 5
        return (True, toks)
    finally:
        os.unlink(driver_path)


def parse_duration(s):
    """Parse Misa raw duration text to ms, mirroring Python lexer."""
    if s.endswith("ms"):
        return int(float(s[:-2]))
    elif s.endswith("s"):
        return int(float(s[:-1]) * 1000)
    raise ValueError(f"not a duration: {s}")


def compare(py_result, misa_result, name):
    """Compare Python and Misa lex results. Returns True if match."""
    global PASS, FAIL
    py_ok, py_data = py_result
    misa_ok, misa_data = misa_result

    if py_ok != misa_ok:
        FAIL += 1
        FAILURES.append((name, f"ok mismatch: python ok={py_ok}, misa ok={misa_ok}"))
        if not py_ok:
            print(f"  FAIL {name}: python errored: {py_data[0]}")
        if not misa_ok:
            print(f"  FAIL {name}: misa errored: {misa_data[0]}")
        return False

    if not py_ok:
        # Both errored: compare messages
        py_msg = py_data[0]
        misa_msg = misa_data[0]
        if py_msg == misa_msg:
            PASS += 1
            print(f"  ok  {name} (both error: {py_msg[:50]})")
            return True
        else:
            FAIL += 1
            FAILURES.append((name, f"error msg mismatch: py={py_msg!r} misa={misa_msg!r}"))
            print(f"  FAIL {name}: error msg mismatch")
            print(f"    python: {py_msg!r}")
            print(f"    misa:   {misa_msg!r}")
            return False

    # Both succeeded: compare token streams
    if len(py_data) != len(misa_data):
        FAIL += 1
        FAILURES.append((name, f"token count: py={len(py_data)} misa={len(misa_data)}"))
        print(f"  FAIL {name}: token count mismatch (py={len(py_data)}, misa={len(misa_data)})")
        return False

    for idx, (pt, mt) in enumerate(zip(py_data, misa_data)):
        pkind, pval, pline, pcol = pt
        mkind, mtext, mline, mcol = mt
        if pkind != mkind or pline != mline or pcol != mcol:
            FAIL += 1
            FAILURES.append((name, f"token {idx}: py=({pkind},{pline},{pcol}) misa=({mkind},{mline},{mcol})"))
            print(f"  FAIL {name}: token {idx} kind/pos mismatch")
            print(f"    python: {pkind} at {pline}:{pcol}")
            print(f"    misa:   {mkind} at {mline}:{mcol}")
            return False
        # Value/text comparison (kind-aware)
        match = False
        if pkind == "INT":
            match = int(mtext) == pval
        elif pkind == "FLOAT":
            match = float(mtext) == pval
        elif pkind == "DURATION":
            match = parse_duration(mtext) == pval
        elif pkind == "MONEY":
            # Misa stores "$5.99", Python stores 5.99
            match = float(mtext[1:]) == pval if mtext.startswith("$") else False
        elif pkind == "STRING":
            # Both decoded
            match = mtext == pval
        elif pkind == "EOF":
            match = True  # text is "" vs None, ignore
        else:
            match = mtext == pval
        if not match:
            FAIL += 1
            FAILURES.append((name, f"token {idx} ({pkind}): py={pval!r} misa={mtext!r}"))
            print(f"  FAIL {name}: token {idx} ({pkind}) value mismatch")
            print(f"    python: {pval!r}")
            print(f"    misa:   {mtext!r}")
            return False

    PASS += 1
    print(f"  ok  {name} ({len(py_data)} tokens)")
    return True


def collect_misa_files():
    files = []
    # Phase 5b: files using sum types / match are out of scope for the
    # v0.2-targeted self-hosted port; they gain coverage when 5b is ported.
    SKIP_5B = {"a14_sums.misa"}
    for base in ["examples", "tests"]:
        basedir = os.path.join(ROOT, base)
        for root, dirs, filenames in os.walk(basedir):
            # Skip __pycache__
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".misa") and fn not in SKIP_5B:
                    files.append(os.path.join(root, fn))
    return sorted(files)


def test_edge_cases():
    cases = [
        ("unterminated string", '"hello'),
        ("unterminated block comment", "/* hello"),
        ("bang alone", "!"),
        ("bang in expr", "x = !y"),
        ("dollar not followed by digit", "$x"),
        ("dollar at end", "$"),
        ("triple-quoted", '"""hello"""'),
        ("triple-quoted multiline", '"""\nhello\nworld\n"""'),
        ("triple-quoted escapes", '"""a\\nb"""'),
        ("triple-quoted unknown escape", '"""a\\qb"""'),
        ("duration s", "5s"),
        ("duration ms", "100ms"),
        ("duration not ident", "5second"),
        ("duration float", "2.5s"),
        ("money int", "$5"),
        ("money float", "$5.99"),
        ("money trailing dot", "$5."),
        ("int leading zeros", "007"),
        ("float", "3.14"),
        ("float trailing dot", "5."),
        ("dot then int", ".5"),
        ("string escapes", '"a\\nb\\tc\\"d"'),
        ("string unknown escape", '"a\\qb"'),
        ("string newline", '"a\nb"'),
        ("not equal", "!="),
        ("equal equal", "=="),
        ("arrow", "->"),
        ("less equal", "<="),
        ("semicolon insertion", "let x = 1\nlet y = 2\n"),
        ("no semi after lparen", "(\n"),
        ("comment line", "// hello\nlet x = 1"),
        ("comment block", "/* hi */ let x = 1"),
        ("empty", ""),
        ("only whitespace", "   \n  "),
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, src in cases:
            path = os.path.join(tmpdir, "case.misa")
            with open(path, "w") as f:
                f.write(src)
            py_result = python_lex(src)
            misa_result = misa_lex(path)
            compare(py_result, misa_result, f"edge: {name}")


def main():
    print("Collecting .misa files...")
    files = collect_misa_files()
    print(f"Found {len(files)} files\n")

    print("Testing example/test files:")
    for path in files:
        rel = os.path.relpath(path, ROOT)
        with open(path) as f:
            src = f.read()
        py_result = python_lex(src)
        misa_result = misa_lex(path)
        compare(py_result, misa_result, rel)

    print("\nTesting edge cases:")
    test_edge_cases()

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for name, msg in FAILURES:
            print(f"  {name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
