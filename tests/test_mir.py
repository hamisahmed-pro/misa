#!/usr/bin/env python3
import os, sys
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT,"impl"))
from misa.mir import MIRBuilder, MIRInstr, MIRValue, I64, F64, MVAL, verify, dump, optimize

def main():
    b=MIRBuilder()
    fn=b.function("add", [I64,I64], I64)
    x=fn.params[0]; y=fn.params[1]
    z=b.emit(fn.blocks[0],"add",I64,[x,y])
    boxed=b.emit(fn.blocks[0],"box",MVAL,[z])
    unboxed=b.emit(fn.blocks[0],"unbox",I64,[boxed])
    b.emit(fn.blocks[0],"return",None,[unboxed])
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
    bad.emit(bad_block,"add",I64,(bad_fn.params[0],bad_float))
    assert verify(bad.module), "invalid MIR type graph was accepted"

    # The verifier is fail-closed: uses must follow a valid definition,
    # IDs are unique, and every stage-1 function terminates in a typed return.
    forward = MIRBuilder()
    ff = forward.function("forward", [I64], I64)
    future = MIRValue(99, I64)
    value = forward.emit(ff.blocks[0], "add", I64, (future, ff.params[0]))
    forward.emit(ff.blocks[0], "return", None, (value,))
    assert any("use-before-definition" in e for e in verify(forward.module))

    dup = MIRBuilder()
    df = dup.function("duplicate", [I64], I64)
    dup.emit(df.blocks[0], "const", I64, imm=1)
    df.blocks[0].instructions.append(MIRInstr("const", MIRValue(0, I64), (), 2))
    dup.emit(df.blocks[0], "return", None, (df.params[0],))
    assert any("duplicate" in e for e in verify(dup.module))

    no_return = MIRBuilder()
    nf = no_return.function("incomplete", [], I64)
    no_return.emit(nf.blocks[0], "const", I64, imm=1)
    assert any("must end with a return" in e for e in verify(no_return.module))

    cfg = MIRBuilder()
    cf = cfg.function("cfg", [], I64)
    cf.blocks.append(type(cf.blocks[0])("dead"))
    assert any("exactly one entry block" in e for e in verify(cfg.module))

    # Constant folding must agree with Misa Int's documented i64 wrapping.
    overflow = MIRBuilder()
    of = overflow.function("wrap", [], I64)
    oblock = of.blocks[0]
    hi = overflow.emit(oblock, "const", I64, imm=(1 << 63) - 1)
    one = overflow.emit(oblock, "const", I64, imm=1)
    total = overflow.emit(oblock, "add", I64, (hi, one))
    overflow.emit(oblock, "return", None, (total,))
    assert not verify(overflow.module), verify(overflow.module)
    optimize(overflow.module)
    folded = next(i for i in oblock.instructions if i.op == "const"
                  and i.result is not None and i.result.id == total.id)
    assert folded.imm == -(1 << 63), folded.imm

    malformed_type = MIRBuilder()
    mf = malformed_type.function("mismatch", [I64], I64)
    wrong_ref = MIRValue(mf.params[0].id, F64)
    out = malformed_type.emit(mf.blocks[0], "add", I64,
                              (wrong_ref, mf.params[0]))
    malformed_type.emit(mf.blocks[0], "return", None, (out,))
    assert any("claims f64, defined as i64" in e
               for e in verify(malformed_type.module))
    print("MIR foundation + optimization: PASS")
if __name__=="__main__":
    main()
