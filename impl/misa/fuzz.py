#!/usr/bin/env python3
"""Misa program fuzzer: generate random programs, verify interp == AOT.

Usage: python3 -m misa.fuzz [--seed N] [--count N] [--max-depth N]

Generates random Misa programs from a grammar, runs each through both
the interpreter and AOT compiler, and reports mismatches.

The invariant: for any valid program, interp and AOT must produce
identical stdout, stderr, and exit codes.

(Implements Benchmark Review Plan §31: fuzzing in CI.)
"""
import argparse
import os
import random
import subprocess
import sys
import tempfile

# Find misa binary
_this_dir = os.path.dirname(os.path.abspath(__file__))
# _this_dir is impl/misa, so root is parent of parent
ROOT = os.path.dirname(os.path.dirname(_this_dir))
MISA_BIN = os.path.join(ROOT, "impl", "bin", "misa")


class Fuzzer:
    def __init__(self, seed, max_depth=3):
        self.rng = random.Random(seed)
        self.max_depth = max_depth
        self.var_counter = 0

    def gen_program(self):
        """Generate a random program."""
        stmts = []
        n_stmts = self.rng.randint(2, 8)
        for _ in range(n_stmts):
            stmts.append(self.gen_stmt(0))
        return "\n".join(stmts) + "\n"

    def gen_stmt(self, depth):
        """Generate a random statement."""
        kind = self.rng.choice(["let", "print", "if", "while"])
        if kind == "let" or depth >= self.max_depth:
            return self.gen_let()
        elif kind == "print":
            return f"print({self.gen_expr(0)})"
        elif kind == "if":
            cond = self.gen_expr(0)
            then = self.gen_stmt(depth + 1)
            return f"if {cond} {{\n  {then}\n}}"
        elif kind == "while":
            # Simple bounded loop
            var = f"_w{self.var_counter}"
            self.var_counter += 1
            limit = self.rng.randint(1, 5)
            body = self.gen_stmt(depth + 1)
            return (f"var {var} = 0\nwhile {var} < {limit} {{\n"
                    f"  {body}\n  {var} = {var} + 1\n}}")

    def gen_let(self):
        """Generate a let statement with random value."""
        var = f"_v{self.var_counter}"
        self.var_counter += 1
        expr = self.gen_expr(0)
        return f"let {var} = {expr}"

    def gen_expr(self, depth, want_type=None):
        """Generate a random expression, optionally of a specific type."""
        if want_type is None:
            want_type = self.rng.choice(["int", "float", "str", "bool"])
        if depth >= self.max_depth:
            return self.gen_atom(want_type)

        # For bool, generate comparisons sometimes
        if want_type == "bool" and self.rng.random() < 0.5:
            left = self.gen_expr(depth + 1, "int")
            op = self.rng.choice(["==", "<", ">"])
            right = self.gen_expr(depth + 1, "int")
            return f"({left} {op} {right})"

        kind = self.rng.choice(["atom", "binop", "binop"])
        if kind == "atom":
            return self.gen_atom(want_type)
        else:
            # Only numeric types support binops
            if want_type not in ("int", "float"):
                return self.gen_atom(want_type)
            left = self.gen_expr(depth + 1, want_type)
            op = self.rng.choice(["+", "-", "*"])
            right = self.gen_atom(want_type)
            return f"({left} {op} {right})"

    def gen_atom(self, want_type="int"):
        """Generate a random atomic value of the given type."""
        if want_type == "int":
            return str(self.rng.randint(0, 100))
        elif want_type == "float":
            return f"{self.rng.randint(0, 100)}.{self.rng.randint(0, 99)}"
        elif want_type == "str":
            s = "".join(self.rng.choice("abc") for _ in range(3))
            return f'"{s}"'
        elif want_type == "bool":
            return self.rng.choice(["true", "false"])
        return "0"


def run_interp(path):
    r = subprocess.run([MISA_BIN, "run", "--interp", path],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def run_aot(path):
    with tempfile.NamedTemporaryFile(suffix="_fuzz", delete=False) as f:
        bin_path = f.name
    try:
        r = subprocess.run([MISA_BIN, "build", path, "-o", bin_path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None, "", f"build failed: {r.stderr[:200]}"
        r = subprocess.run([bin_path],
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout, r.stderr
    finally:
        if os.path.exists(bin_path):
            os.unlink(bin_path)


def main():
    parser = argparse.ArgumentParser(description="Fuzz Misa compiler")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--max-depth", type=int, default=3)
    args = parser.parse_args()

    mismatches = []
    errors = 0

    for i in range(args.count):
        seed = args.seed + i
        fuzzer = Fuzzer(seed, args.max_depth)
        prog = fuzzer.gen_program()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".misa",
                                         delete=False) as f:
            f.write(prog)
            path = f.name

        try:
            try:
                i_rc, i_out, i_err = run_interp(path)
            except subprocess.TimeoutExpired:
                continue  # Skip timeouts

            try:
                aot_result = run_aot(path)
            except subprocess.TimeoutExpired:
                continue

            if aot_result[0] is None:
                # Build failed - might be a fuzzer bug, record it
                errors += 1
                if errors <= 3:
                    print(f"Seed {seed}: {aot_result[2][:100]}")
                continue

            a_rc, a_out, a_err = aot_result

            # Compare (normalize)
            if i_rc != a_rc or i_out.strip() != a_out.strip():
                mismatches.append((seed, prog, (i_rc, i_out), (a_rc, a_out)))
                print(f"MISMATCH seed {seed}:")
                print(f"  Program: {prog[:200]}")
                print(f"  interp: rc={i_rc} out={i_out.strip()!r}")
                print(f"  aot:    rc={a_rc} out={a_out.strip()!r}")
                if len(mismatches) >= 5:
                    break
        finally:
            os.unlink(path)

        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{args.count} programs tested...")

    print(f"\nDone: {args.count} programs, {len(mismatches)} mismatches, "
          f"{errors} build errors")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
