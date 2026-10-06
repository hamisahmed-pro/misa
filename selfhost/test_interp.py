#!/usr/bin/env python3
"""Differential test: Python `misa run` vs self-hosted Misa interpreter.

For each corpus file: run with the Python interpreter and with the
self-hosted interpreter (selfhost/interp.misa via a temp driver), then
compare (exit code, stdout, stderr) exactly.

Usage: python3 selfhost/test_interp.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELFHOST = os.path.join(ROOT, "selfhost")
MISA = os.path.join(ROOT, "impl", "bin", "misa")

EXIT_MARK = "@@MISA-SELFHOST-EXIT@@"
ERR_MARK = "@@MISA-SELFHOST-ERR@@"
DIAG_MARK = "@@MISA-SELFHOST-ERR@@"  # diags use the same channel

# Corpus files exercised through both interpreters.
CORPUS = [
    "tests/aot/prog/a01_arith.misa",
    "tests/aot/prog/a02_strings.misa",
    "tests/aot/prog/a03_control.misa",
    "tests/aot/prog/a04_collections.misa",
    "tests/aot/prog/a05_errors.misa",
    "tests/aot/prog/a06_builtins.misa",
    "tests/aot/prog/a07_fs.misa",
    "tests/aot/prog/a10_screen.misa",
    "examples/hello.misa",
    "examples/android.misa",
    "examples/tourlib.misa",
]

# Deliberately excluded (documented):
# - tests/aot/prog/a08_model.misa, a09_budget.misa, a12_stream_reject.misa:
#   live model calls / streams (E503 stub in self-hosted v1).
# - tests/aot/prog/a11_import.misa + a11_util.misa: `import` not resolved
#   by the single-file self-hosted pipeline.
# - tests/web/webfetch.misa: needs a localhost HTTP server.
# - examples/v02tour.misa: imports tourlib (`import` not resolved by the
#   single-file self-hosted pipeline).
# - examples/{plantcare,plantdoc,plantdoc_edge,plantdoc_edge_tflite,
#   plantdoc_live,webslice,webmodel,webmodel_live,budgets}.misa:
#   declare/call AI models.

PASS = 0
FAIL = 0
FAILURES = []


def run_python(path, timeout=60):
    p = subprocess.run(
        [sys.executable, MISA, "run", path],
        capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def run_selfhosted(path, timeout=240):
    drv = os.path.join(SELFHOST, "_tmp_interp_driver.misa")
    with open(drv, "w", encoding="utf-8") as f:
        f.write('import "interp.misa"\n\nrun_file("%s")\n'
                % os.path.abspath(path))
    # Bump the host recursion limit: the self-hosted interpreter layers
    # Misa calls on host-Python frames, so deep target recursion (e.g.
    # fib(20)) needs more headroom than the default 1000.
    wrapper = (
        "import sys, runpy; sys.setrecursionlimit(30000); "
        "sys.argv = ['misa', 'run', %r]; runpy.run_path(%r, run_name='__main__')"
        % (drv, MISA))
    try:
        p = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    finally:
        if os.path.exists(drv):
            os.unlink(drv)
    # Split driver framing from program output.
    stdout_lines = []
    err_lines = []
    exit_code = None
    harness_err = None
    for line in p.stdout.splitlines():
        if line.startswith(EXIT_MARK):
            try:
                exit_code = int(line[len(EXIT_MARK):])
            except ValueError:
                harness_err = f"bad exit marker: {line!r}"
        elif line.startswith(ERR_MARK):
            err_lines.append(line[len(ERR_MARK):])
        else:
            stdout_lines.append(line)
    if p.returncode != 0 and exit_code is None:
        harness_err = (f"driver crashed rc={p.returncode} "
                       f"stderr={p.stderr[:500]!r}")
    if harness_err:
        return None, None, None, harness_err
    stdout = "\n".join(stdout_lines)
    if stdout_lines:
        stdout += "\n"
    stderr = "\n".join(err_lines)
    if err_lines:
        stderr += "\n"
    return exit_code, stdout, stderr, None


def check_one(label, path, timeout_py=60, timeout_sh=240):
    global PASS, FAIL
    try:
        py = run_python(path, timeout_py)
    except subprocess.TimeoutExpired:
        FAIL += 1
        FAILURES.append((label, "python timeout"))
        print(f"  FAIL {label}: python timeout")
        return
    try:
        sh = run_selfhosted(path, timeout_sh)
    except subprocess.TimeoutExpired:
        FAIL += 1
        FAILURES.append((label, "selfhosted timeout"))
        print(f"  FAIL {label}: selfhosted timeout")
        return
    sh_code, sh_out, sh_err, harness_err = sh
    if harness_err:
        FAIL += 1
        FAILURES.append((label, f"harness: {harness_err}"))
        print(f"  FAIL {label}: harness: {harness_err}")
        return
    py_code, py_out, py_err = py
    if (py_code, py_out, py_err) == (sh_code, sh_out, sh_err):
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        detail = []
        if py_code != sh_code:
            detail.append(f"exit py={py_code} sh={sh_code}")
        if py_out != sh_out:
            detail.append(f"stdout diff:\n--- py ---\n{py_out[:800]}\n--- sh ---\n{sh_out[:800]}")
        if py_err != sh_err:
            detail.append(f"stderr diff:\n--- py ---\n{py_err[:800]}\n--- sh ---\n{sh_err[:800]}")
        FAILURES.append((label, " | ".join(detail)[:1200]))
        print(f"  FAIL {label}: {' | '.join(detail)[:400]}")


def check_inline(label, src):
    fd, path = tempfile.mkstemp(suffix=".misa", dir="/tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        check_one(label, path)
    finally:
        os.unlink(path)


def inline_imports(path):
    """Inline `import "std/X"` and relative imports; returns merged source.

    Deduplicates by absolute path (import DAG). Used for the stdlib tests,
    which the single-file self-hosted pipeline cannot resolve natively.
    """
    seen = set()
    chunks = []

    def visit(p):
        ap = os.path.abspath(p)
        if ap in seen:
            return
        seen.add(ap)
        with open(ap, encoding="utf-8") as f:
            src = f.read()
        base = os.path.dirname(ap)
        for line in src.splitlines():
            s = line.strip()
            if s.startswith("import "):
                # import "std/X" or import "./X.misa" or import "X"
                q = s[len("import "):].strip().strip('"')
                if q.startswith("std/"):
                    mod = q[len("std/"):]
                    ipath = os.path.join(ROOT, "stdlib", "std", "src",
                                         mod + ".misa")
                elif q.startswith("./"):
                    ipath = os.path.join(base, q[2:])
                else:
                    ipath = os.path.join(base, q)
                    if not ipath.endswith(".misa"):
                        ipath += ".misa"
                visit(ipath)
            else:
                chunks.append(line)

    visit(path)
    return "\n".join(chunks) + "\n"


STDLIB_TESTS = [
    "stdlib/std/tests/test_strings.misa",
    "stdlib/std/tests/test_lists.misa",
    "stdlib/std/tests/test_result.misa",
    "stdlib/std/tests/test_json.misa",
    "stdlib/std/tests/test_http.misa",
]


def check_stdlib(label, rel):
    # Compare Python (native import resolution) vs self-hosted (inlined).
    global PASS, FAIL
    src_path = os.path.join(ROOT, rel)
    try:
        py = run_python(src_path)
    except subprocess.TimeoutExpired:
        FAIL += 1
        FAILURES.append((label, "python timeout"))
        print(f"  FAIL {label}: python timeout")
        return
    merged = inline_imports(src_path)
    fd, tpath = tempfile.mkstemp(suffix=".misa", dir="/tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(merged)
    try:
        sh = run_selfhosted(tpath)
    except subprocess.TimeoutExpired:
        FAIL += 1
        FAILURES.append((label, "selfhosted timeout"))
        print(f"  FAIL {label}: selfhosted timeout")
        return
    finally:
        os.unlink(tpath)
    sh_code, sh_out, sh_err, harness_err = sh
    if harness_err:
        FAIL += 1
        FAILURES.append((label, f"harness: {harness_err}"))
        print(f"  FAIL {label}: harness: {harness_err}")
        return
    py_code, py_out, py_err = py
    if (py_code, py_out, py_err) == (sh_code, sh_out, sh_err):
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        detail = []
        if py_code != sh_code:
            detail.append(f"exit py={py_code} sh={sh_code}")
        if py_out != sh_out:
            detail.append(f"stdout diff:\n--- py ---\n{py_out[:800]}\n--- sh ---\n{sh_out[:800]}")
        if py_err != sh_err:
            detail.append(f"stderr diff:\n--- py ---\n{py_err[:800]}\n--- sh ---\n{sh_err[:800]}")
        FAILURES.append((label, " | ".join(detail)[:1200]))
        print(f"  FAIL {label}: {' | '.join(detail)[:400]}")


def main():
    for rel in CORPUS:
        check_one(rel, os.path.join(ROOT, rel))

    for rel in STDLIB_TESTS:
        check_stdlib("stdlib:" + rel, rel)

    # -- inline edge cases -------------------------------------------
    check_inline("uncaught-runtime-error", 'print(undefined_var_xyz)\n')
    check_inline("uncaught-throw-pos",
                 'let x = 1\nthrow {code: "E999", message: "boom"}\n')
    check_inline("throw-in-fn-uncaught",
                 'fn f() {\n    throw {code: "E7", message: "deep"}\n}\nf()\n')
    check_inline("nested-try-rethrow",
                 'try {\n    try {\n        throw {code: "E1", message: "a"}\n    } catch e {\n        throw e\n    }\n} catch e {\n    print("{e.code}={e.message}")\n}\n')
    check_inline("recursion",
                 'fn fact(n: Int) -> Int {\n    if n <= 1 {\n        return 1\n    }\n    return n * fact(n - 1)\n}\nprint(fact(10))\n')
    check_inline("mutual-recursion",
                 'fn is_even(n: Int) -> Bool {\n    if n == 0 {\n        return true\n    }\n    return is_odd(n - 1)\n}\nfn is_odd(n: Int) -> Bool {\n    if n == 0 {\n        return false\n    }\n    return is_even(n - 1)\n}\nprint(is_even(20))\n')
    check_inline("closures-over-globals",
                 'var total = 0\nfn bump(n: Int) {\n    total = total + n\n}\nfor i in [1, 2, 3] {\n    bump(i)\n}\nprint(total)\n')
    check_inline("record-field-assign",
                 'let r = {x: 1, y: 2}\nr.x = 99\nr.z = 3\nprint(r)\n')
    check_inline("list-index-assign",
                 'var xs = [1, 2, 3]\nxs[0] = 9\nxs[2] = xs[2] + 1\nprint(xs)\n')
    check_inline("map-assign",
                 'let m = {"a": 1}\nm["b"] = 2\nm["a"] = 10\nprint(m)\n')
    check_inline("string-index",
                 'let s = "hello"\nprint(s[0])\nprint(s[4])\nfor c in s {\n    print(c)\n}\n')
    check_inline("interp-nested",
                 'let a = 1\nlet b = 2\nprint("{a} + {b} = {a + b}")\nprint("{{literal}}")\n')
    check_inline("lambda-value",
                 'let f = |x| x + 1\nprint(f)\n')
    check_inline("screen-value-render",
                 'screen S(n: Text) {\n    Text(text: "{n}")\n}\nprint(S("hi"))\n')
    check_inline("process-run",
                 'let r = process.run(["echo", "hey"])\nprint(r.code)\nprint(r.stdout)\n')
    check_inline("str-ns",
                 'print(str.slice("hello", 1, 4))\nprint(str.split("a,b", ","))\nprint(str.ord("z"))\n')
    check_inline("div-float",
                 'print(7 / 2)\nprint(7 % 3)\nprint(2.5 - 1)\n')
    check_inline("equality",
                 'print(1 == 1.0)\nprint([1, [2]] == [1, [2]])\nprint({a: 1} == {a: 1})\nprint(nil == nil)\n')
    check_inline("assert-fail-uncaught", 'assert(1 == 2, "math broke")\n')
    check_inline("while-return",
                 'fn f(n: Int) -> Int {\n    var i = 0\n    while true {\n        if i >= n {\n            return i * 2\n        }\n        i = i + 1\n    }\n    return -1\n}\nprint(f(5))\n')
    check_inline("for-text", 'for c in "ab" {\n    print(c)\n}\n')
    check_inline("shadow",
                 'let x = 1\nif true {\n    let x = 2\n    print(x)\n}\nprint(x)\n')

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for label, detail in FAILURES:
            print(f"- {label}: {detail[:500]}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
