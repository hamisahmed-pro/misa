"""Misa MIR v0.1 — typed SSA-neutral intermediate representation.

This module is intentionally small and dependency-free. It establishes the
compiler boundary described by spec/MIR.md without changing code generation
yet. The first consumer is structural validation/dumping; lowering to LLVM
will be introduced only after parity tests cover the MIR representation.

Design:
  Typed AST -> MIR -> (future optimization passes) -> LLVM

MIR values carry an explicit static type. Boxing is represented explicitly
so later box/unbox elimination can be a mechanical dataflow pass.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Tuple

Primitive = str

@dataclass(frozen=True)
class MIRType:
    name: Primitive

I64 = MIRType("i64")
F64 = MIRType("f64")
BOOL = MIRType("bool")
MVAL = MIRType("MVal*")
VOID = MIRType("void")

@dataclass(frozen=True)
class MIRValue:
    id: int
    type: MIRType

@dataclass(frozen=True)
class MIRInstr:
    op: str
    result: Optional[MIRValue] = None
    args: Tuple[MIRValue, ...] = ()
    imm: object = None

@dataclass
class MIRBlock:
    name: str
    instructions: list[MIRInstr] = field(default_factory=list)
    terminator: Optional[MIRInstr] = None

@dataclass
class MIRFunction:
    name: str
    params: list[MIRValue]
    return_type: MIRType
    blocks: list[MIRBlock] = field(default_factory=list)

@dataclass
class MIRModule:
    functions: list[MIRFunction] = field(default_factory=list)

class MIRBuilder:
    """Small typed builder used by the first AST lowering pass."""

    def __init__(self):
        self.module = MIRModule()
        self._next_value = 0

    def value(self, ty: MIRType) -> MIRValue:
        v = MIRValue(self._next_value, ty)
        self._next_value += 1
        return v

    def function(self, name: str, params: list[MIRType],
                 return_type: MIRType) -> MIRFunction:
        fn = MIRFunction(
            name=name,
            params=[self.value(t) for t in params],
            return_type=return_type,
            blocks=[MIRBlock("entry")],
        )
        self.module.functions.append(fn)
        return fn

    def emit(self, block: MIRBlock, op: str, ty: Optional[MIRType],
             args=(), imm=None) -> Optional[MIRValue]:
        result = self.value(ty) if ty is not None else None
        block.instructions.append(MIRInstr(op, result, tuple(args), imm))
        return result

def verify(module: MIRModule) -> list[str]:
    """Return structural/type errors; empty means MIR is well-formed."""
    errors = []
    for fn in module.functions:
        if not fn.blocks:
            errors.append(f"{fn.name}: function has no blocks")
            continue
        ids = set()
        for p in fn.params:
            if p.id in ids: errors.append(f"{fn.name}: duplicate value %{p.id}")
            ids.add(p.id)
        for block in fn.blocks:
            for ins in block.instructions:
                if ins.result is not None:
                    if ins.result.id in ids:
                        errors.append(f"{fn.name}: duplicate value %{ins.result.id}")
                    ids.add(ins.result.id)
                for a in ins.args:
                    if a.id not in ids:
                        errors.append(f"{fn.name}: use of unknown value %{a.id}")
    return errors

def dump(module: MIRModule) -> str:
    lines = []
    for fn in module.functions:
        params = ", ".join(f"%{p.id}: {p.type.name}" for p in fn.params)
        lines.append(f"fn {fn.name}({params}) -> {fn.return_type.name} {{")
        for block in fn.blocks:
            lines.append(f"{block.name}:")
            for ins in block.instructions:
                lhs = f"%{ins.result.id} = " if ins.result else ""
                args = ", ".join(f"%{a.id}" for a in ins.args)
                suffix = f" {ins.imm!r}" if ins.imm is not None else ""
                lines.append(f"  {lhs}{ins.op} {args}{suffix}".rstrip())
            lines.append("}")
    return "\n".join(lines)
