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
from typing import Optional, Tuple, Dict, Set

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
    """Return structural and basic type errors; empty means well-formed."""
    errors = []
    for fn in module.functions:
        if not fn.blocks:
            errors.append(f"{fn.name}: function has no blocks")
            continue
        ids = {}
        for p in fn.params:
            if p.id in ids:
                errors.append(f"{fn.name}: duplicate value %{p.id}")
            ids[p.id] = p.type
        for block in fn.blocks:
            if block.terminator is not None and block.instructions and                     block.instructions[-1].op in {"return", "branch", "cond_branch", "throw"}:
                errors.append(f"{fn.name}:{block.name}: terminator must be stored in block.terminator")
            for ins in block.instructions:
                for a in ins.args:
                    if a.id not in ids:
                        errors.append(f"{fn.name}:{block.name}: use of unknown value %{a.id}")
                if ins.result is not None:
                    if ins.result.id in ids:
                        errors.append(f"{fn.name}:{block.name}: duplicate value %{ins.result.id}")
                    ids[ins.result.id] = ins.result.type
                if ins.op in {"add", "sub", "mul"}:
                    if len(ins.args) != 2 or ins.result is None:
                        errors.append(f"{fn.name}:{block.name}: {ins.op} requires two args and a result")
                    elif ins.args[0].type != ins.args[1].type or ins.args[0].type != ins.result.type:
                        errors.append(f"{fn.name}:{block.name}: {ins.op} has incompatible types")
                    elif ins.result.type not in {I64, F64}:
                        errors.append(f"{fn.name}:{block.name}: {ins.op} requires i64 or f64")
                elif ins.op == "box":
                    if len(ins.args) != 1 or ins.result is None or ins.result.type != MVAL:
                        errors.append(f"{fn.name}:{block.name}: box must produce MVal* from one value")
                elif ins.op == "unbox":
                    if len(ins.args) != 1 or ins.result is None or ins.args[0].type != MVAL:
                        errors.append(f"{fn.name}:{block.name}: unbox requires one MVal* argument")
                elif ins.op == "return":
                    if ins.result is not None:
                        errors.append(f"{fn.name}:{block.name}: return cannot define a result")
                    if len(ins.args) > 1:
                        errors.append(f"{fn.name}:{block.name}: return accepts at most one value")
                    elif fn.return_type == VOID and ins.args:
                        errors.append(f"{fn.name}:{block.name}: void function cannot return a value")
                    elif fn.return_type != VOID and (not ins.args or ins.args[0].type != fn.return_type):
                        errors.append(f"{fn.name}:{block.name}: return type mismatch")
            if block.terminator is not None:
                term = block.terminator
                if term.op == "return":
                    if len(term.args) > 1 or (fn.return_type == VOID and term.args) or                             (fn.return_type != VOID and (not term.args or term.args[0].type != fn.return_type)):
                        errors.append(f"{fn.name}:{block.name}: return type mismatch")
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

# ---------------------------------------------------------------------------
# MIR optimization passes

_BINARY_FOLD = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
}

def constant_fold(module: MIRModule) -> bool:
    """Fold integer/float constants for pure arithmetic operations."""
    changed = False
    for fn in module.functions:
        constants: Dict[int, object] = {}
        for block in fn.blocks:
            rewritten = []
            for ins in block.instructions:
                if ins.op == "const" and ins.result is not None:
                    constants[ins.result.id] = ins.imm
                    rewritten.append(ins)
                    continue
                if ins.op in _BINARY_FOLD and len(ins.args) == 2 and all(a.id in constants for a in ins.args):
                    value = _BINARY_FOLD[ins.op](constants[ins.args[0].id], constants[ins.args[1].id])
                    if ins.result is not None:
                        constants[ins.result.id] = value
                        rewritten.append(MIRInstr("const", ins.result, (), value))
                        changed = True
                        continue
                rewritten.append(ins)
            block.instructions = rewritten
    return changed

def eliminate_box_unbox(module: MIRModule) -> bool:
    """Replace box -> unbox pairs with the original primitive value."""
    changed = False
    for fn in module.functions:
        replacements: Dict[int, MIRValue] = {}
        boxes: Dict[int, MIRValue] = {}
        for block in fn.blocks:
            out = []
            for ins in block.instructions:
                args = tuple(replacements.get(a.id, a) for a in ins.args)
                if ins.op == "box" and ins.result is not None and len(args) == 1:
                    boxes[ins.result.id] = args[0]
                if ins.op == "unbox" and ins.result is not None and len(args) == 1:
                    source = boxes.get(args[0].id)
                    if source is not None and source.type == ins.result.type:
                        replacements[ins.result.id] = source
                        changed = True
                        continue
                out.append(MIRInstr(ins.op, ins.result, args, ins.imm))
            block.instructions = out
        if replacements:
            for block in fn.blocks:
                block.instructions = [
                    MIRInstr(i.op, i.result, tuple(replacements.get(a.id, a) for a in i.args), i.imm)
                    for i in block.instructions
                ]
    return changed

def dead_code_eliminate(module: MIRModule) -> bool:
    """Remove pure instructions whose results are never consumed."""
    side_effecting = {"call", "store", "branch", "cond_branch", "return", "throw", "box", "unbox"}
    changed = False
    for fn in module.functions:
        used: Set[int] = set()
        for block in fn.blocks:
            for ins in block.instructions:
                used.update(a.id for a in ins.args)
            if block.terminator:
                used.update(a.id for a in block.terminator.args)
        for block in fn.blocks:
            kept = []
            for ins in block.instructions:
                if ins.result is not None and ins.result.id not in used and ins.op not in side_effecting:
                    changed = True
                    continue
                kept.append(ins)
            block.instructions = kept
    return changed

def optimize(module: MIRModule, max_rounds: int = 8) -> int:
    """Run core MIR passes to a fixpoint; return number of changed rounds."""
    rounds = 0
    for _ in range(max_rounds):
        changed = constant_fold(module) or False
        changed = eliminate_box_unbox(module) or changed
        changed = dead_code_eliminate(module) or changed
        if not changed:
            break
        rounds += 1
    return rounds
