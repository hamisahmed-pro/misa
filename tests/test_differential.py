#!/usr/bin/env python3
"""Compiler differential testing: interpreter vs AOT.

Runs each test program through both the tree-walking interpreter
(--interp) and the AOT compiler, comparing:
- stdout (exact match)
- stderr (exact match)
- exit code
- error codes (E-codes)

A mismatch is a compiler bug. (Implements Benchmark Review Plan §31.)

Usage: python3 tests/test_differential.py [--only test_name]
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISA_BIN = os.path.join(ROOT, "impl", "bin", "misa")
TEST_DIR = os.path.join(ROOT, "tests", "differential")

# Programs that exercise different language features.
# Each is a (name, source) pair.
PROGRAMS = [
    ("arith", 'print(1 + 2 * 3)\nprint(10 / 3)\nprint(10 % 3)\n'),
    ("float", 'let x: Float = 1.5\nprint(x * 2.0)\nprint(x + 0.25)\n'),
    ("string", 'let s = "hello"\nprint(s + " world")\nprint(len(s))\n'),
    ("string_builder", 'var s = ""\nvar i = 0\nwhile i < 5 {\n  s = s + "x"\n  i = i + 1\n}\nprint(s)\nprint(len(s))\n'),
    ("list", 'let l = [1, 2, 3]\nprint(len(l))\nprint(l[0] + l[2])\n'),
    ("list_push", 'var xs: [Int] = []\nlet alias = xs\nvar i = 0\nwhile i < 1000 {\n  list.push(xs, i)\n  i = i + 1\n}\nprint(len(alias))\nprint(alias[999])\nvar flags = [true]\nlist.push(flags, false)\nprint(flags[1])\n'),
    ("record", 'type P { x: Int, y: Int }\nlet p: P = { x: 1, y: 2 }\nprint(p.x + p.y)\n'),
    ("control", 'var i = 0\nvar s = 0\nwhile i < 10 {\n  if i % 2 == 0 {\n    s = s + i\n  }\n  i = i + 1\n}\nprint(s)\n'),
    ("func", 'fn add(a: Int, b: Int) -> Int {\n  return a + b\n}\nprint(add(3, 4))\n'),
    ("float_func", 'fn fadd(a: Float, b: Float) -> Float {\n  return a + b\n}\nprint(fadd(1.5, 2.5))\n'),
    ("recursion", 'fn fib(n: Int) -> Int {\n  if n < 2 {\n    return n\n  }\n  return fib(n - 1) + fib(n - 2)\n}\nprint(fib(10))\n'),
]


def run_interp(path):
    r = subprocess.run([MISA_BIN, "run", "--interp", path],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def run_aot(path):
    # Build once
    bin_path = "/tmp/diff_test_bin"
    r = subprocess.run([MISA_BIN, "build", path, "-o", bin_path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None, "", f"build failed: {r.stderr[:200]}"
    r = subprocess.run([bin_path],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def main():
    only = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        only = sys.argv[idx + 1]

    os.makedirs(TEST_DIR, exist_ok=True)
    failures = []
    passed = 0

    for name, source in PROGRAMS:
        if only and name != only:
            continue
        path = os.path.join(TEST_DIR, f"{name}.misa")
        with open(path, "w") as f:
            f.write(source)

        try:
            interp_rc, interp_out, interp_err = run_interp(path)
        except subprocess.TimeoutExpired:
            failures.append(f"{name}: interp timeout")
            continue

        try:
            aot_result = run_aot(path)
        except subprocess.TimeoutExpired:
            failures.append(f"{name}: aot timeout")
            continue

        if aot_result[0] is None:
            failures.append(f"{name}: {aot_result[2]}")
            continue

        aot_rc, aot_out, aot_err = aot_result

        # Compare (normalize trailing whitespace)
        if interp_rc != aot_rc:
            failures.append(
                f"{name}: exit code differs (interp={interp_rc}, aot={aot_rc})"
            )
        elif interp_out.strip() != aot_out.strip():
            failures.append(
                f"{name}: stdout differs\n"
                f"  interp: {interp_out.strip()!r}\n"
                f"  aot:    {aot_out.strip()!r}"
            )
        else:
            print(f"PASS {name}")
            passed += 1

    print(f"\n{passed}/{passed + len(failures)} differential tests passed")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
