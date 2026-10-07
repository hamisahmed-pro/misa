#!/usr/bin/env python3
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))
from misa.parser import parse_source
from misa.mir_lower import lower_program, MIRLowerError
from misa.mir import optimize, dump

def main():
    prog = parse_source("""
fn add(a: Int, b: Int) -> Int {
    let x: Int = a + b;
    return x * 2;
}
""")
    mod = lower_program(prog)
    assert not MIRLowerError
    assert "fn add" in dump(mod)
    optimize(mod)
    assert "mul" in dump(mod)
    print("AST -> MIR lowering: PASS")

if __name__ == "__main__":
    main()
