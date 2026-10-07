#!/usr/bin/env python3
"""Validate platform ABI contracts from spec/platform.json.

Checks that every platform runtime file implements the functions declared
in misa_platform.h with matching signatures. A mismatch is an E600-class
error (not a runtime crash).

Usage: python3 tests/test_platform_abi.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "spec", "platform.json")
RT_DIR = os.path.join(ROOT, "impl", "misa", "aot", "rt")


def count_params(decl_text):
    """Count parameters in a C function declaration."""
    # Find the parens
    m = re.search(r'\((.*?)\)', decl_text, re.DOTALL)
    if not m:
        return -1
    params = m.group(1).strip()
    if params == "void" or params == "":
        return 0
    # Count top-level commas (not inside parens)
    depth = 0
    count = 1
    for c in params:
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == ',' and depth == 0:
            count += 1
    return count


def find_impl_params(filepath, func_name):
    """Find the function implementation and count its params."""
    with open(filepath) as f:
        content = f.read()
    # Match: "int misa_platform_http_post(" ... ")"
    pattern = rf'\b{func_name}\s*\('
    m = re.search(pattern, content)
    if not m:
        return None
    # Get from match to closing paren (handle nested)
    start = m.end() - 1  # at '('
    depth = 0
    for i in range(start, len(content)):
        if content[i] == '(':
            depth += 1
        elif content[i] == ')':
            depth -= 1
            if depth == 0:
                decl = content[m.start():i + 1]
                return count_params(decl)
    return None


def main():
    with open(SPEC) as f:
        spec = json.load(f)

    contracts = spec.get("abi_contracts", {})
    targets = spec.get("targets", {})

    failures = []
    for target, info in targets.items():
        rt_file = os.path.join(RT_DIR, info["runtime_file"])
        if not os.path.isfile(rt_file):
            print(f"SKIP {target}: {info['runtime_file']} not found")
            continue
        for func, contract in contracts.items():
            expected = contract["param_count"]
            actual = find_impl_params(rt_file, func)
            if actual is None:
                failures.append(f"{target}: {func} not found in {info['runtime_file']}")
            elif actual != expected:
                failures.append(
                    f"{target}: {func} has {actual} params, "
                    f"expected {expected} (ABI MISMATCH)"
                )
            else:
                print(f"OK {target}: {func} ({actual} params)")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nAll ABI contracts validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
