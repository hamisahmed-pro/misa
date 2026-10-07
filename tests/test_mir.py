#!/usr/bin/env python3
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"impl"))
from misa.mir import MIRBuilder, I64, MVAL, verify, dump, optimize

def main():
    b=MIRBuilder()
    fn=b.function("add", [I64,I64], I64)
    x=fn.params[0]; y=fn.params[1]
    z=b.emit(fn.blocks[0],"add",I64,[x,y])
    boxed=b.emit(fn.blocks[0],"box",MVAL,[z])
    unboxed=b.emit(fn.blocks[0],"unbox",I64,[boxed])
    assert not verify(b.module), verify(b.module)
    text=dump(b.module)
    assert "add %0, %1" in text
    assert "box %2" in text
    assert "unbox %3" in text
    opt = MIRBuilder()
    ofn = opt.function("fold", [], I64)
    ob = ofn.blocks[0]
    a = opt.emit(ob, "const", I64, imm=2)
    b2 = opt.emit(ob, "const", I64, imm=3)
    s = opt.emit(ob, "add", I64, (a, b2))
    boxed = opt.emit(ob, "box", MVAL, (s,))
    unboxed = opt.emit(ob, "unbox", I64, (boxed,))
    opt.emit(ob, "return", None, (unboxed,))
    assert not verify(opt.module), verify(opt.module)
    assert optimize(opt.module) >= 1
    assert any(i.op == "const" and i.imm == 5 for i in ob.instructions)
    assert not any(i.op == "unbox" for i in ob.instructions)
    bad = MIRBuilder()
    bad_fn = bad.function("bad", [I64], I64)
    bad_block = bad_fn.blocks[0]
    bad_float = bad.emit(bad_block, "const", MVAL, imm=1)
    bad.emit(bad_block, "add", I64, (bad_fn.params[0], bad_float))
    assert verify(bad.module), "invalid MIR type graph was accepted"
    print("MIR foundation + optimization: PASS")
if __name__=="__main__":
    main()
