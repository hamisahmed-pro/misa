#!/usr/bin/env python3
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"impl"))
from misa.mir import MIRBuilder, I64, MVAL, verify, dump

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
    print("MIR foundation: PASS")
if __name__=="__main__":
    main()
