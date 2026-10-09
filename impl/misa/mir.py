"""Misa MIR v0.1 — typed scalar IR with a narrow native LLVM consumer.

MIR lowering, verification and optimization currently cover a conservative
single-block scalar subset. Eligible pure Int/Float functions use this path in
the AOT compiler; unsupported functions retain the established emitter. See
spec/MIR.md for the exact boundary and staged CFG/effect roadmap.

Values carry explicit static types and boxing is explicit. The verifier is a
fail-closed gate before optimization and LLVM emission, not a substitute for
the AOT eligibility check.
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
    """Fail closed on malformed or unsupported MIR before any LLVM emission.

    MIR v0.1's production subset is one entry block with scalar SSA values
    and an explicit final return instruction. CFG terminators and cross-block
    SSA are intentionally rejected until their verifier/codegen land together.
    """
    errors = []
    if not isinstance(module, MIRModule):
        return ["module: expected MIRModule"]
    if not isinstance(module.functions, (list, tuple)):
        return ["module: functions must be a sequence"]
    function_names = set()
    scalar_types = {I64, F64, BOOL}
    value_types = scalar_types | {MVAL}
    supported_ops = {"const", "add", "sub", "mul", "div", "mod",
                     "cmp_eq", "cmp_ne", "cmp_lt", "cmp_le", "cmp_gt",
                     "cmp_ge", "box", "unbox", "return"}

    for fn in module.functions:
        prefix = getattr(fn, "name", "<unnamed>")
        if not isinstance(fn, MIRFunction):
            errors.append(f"{prefix}: expected MIRFunction")
            continue
        if not isinstance(fn.name, str) or not fn.name:
            errors.append("<unnamed>: function name must be non-empty text")
            continue
        if fn.name in function_names:
            errors.append(f"{fn.name}: duplicate function name")
        function_names.add(fn.name)
        if (not isinstance(fn.return_type, MIRType) or
                fn.return_type not in value_types | {VOID}):
            errors.append(f"{fn.name}: unsupported return type")
        if not isinstance(fn.blocks, (list, tuple)):
            errors.append(f"{fn.name}: blocks must be a sequence")
            continue
        if len(fn.blocks) != 1:
            errors.append(f"{fn.name}: MIR v0.1 requires exactly one entry block")
            continue
        block = fn.blocks[0]
        if (not isinstance(block, MIRBlock) or
                not isinstance(block.name, str) or not block.name):
            errors.append(f"{fn.name}: invalid entry block")
            continue
        if not isinstance(block.instructions, (list, tuple)):
            errors.append(f"{fn.name}:{block.name}: instructions must be a sequence")
            continue
        if block.terminator is not None:
            errors.append(f"{fn.name}:{block.name}: separate CFG terminators are unsupported in MIR v0.1")

        defined = {}
        if not isinstance(fn.params, (list, tuple)):
            errors.append(f"{fn.name}:{block.name}: parameters must be a sequence")
            continue
        for p in fn.params:
            if not isinstance(p, MIRValue):
                errors.append(f"{fn.name}:{block.name}: invalid parameter value")
                continue
            if type(p.id) is not int or p.id < 0:
                errors.append(f"{fn.name}:{block.name}: invalid parameter value id")
                continue
            if p.id in defined:
                errors.append(f"{fn.name}:{block.name}: duplicate or negative value id %{p.id}")
            if not isinstance(p.type, MIRType) or p.type not in value_types:
                errors.append(f"{fn.name}:{block.name}: unsupported parameter type for %{p.id}")
            defined[p.id] = p.type

        terminated = False
        saw_return = False
        for index, ins in enumerate(block.instructions):
            if not isinstance(ins, MIRInstr):
                errors.append(f"{fn.name}:{block.name}: instruction {index} is not MIRInstr")
                continue
            if terminated:
                errors.append(f"{fn.name}:{block.name}: instruction appears after return")
            if not isinstance(ins.op, str):
                errors.append(f"{fn.name}:{block.name}: instruction operation must be text")
                continue
            if not isinstance(ins.args, (list, tuple)):
                errors.append(f"{fn.name}:{block.name}: operands must be a sequence")
                continue
            if ins.op not in supported_ops:
                errors.append(f"{fn.name}:{block.name}: unsupported MIR operation '{ins.op}'")
                # Still validate uses/results below to report multiple defects.

            arg_types = []
            for arg in ins.args:
                if not isinstance(arg, MIRValue):
                    errors.append(f"{fn.name}:{block.name}: malformed operand")
                    arg_types.append(None)
                    continue
                if type(arg.id) is not int or arg.id < 0:
                    errors.append(f"{fn.name}:{block.name}: malformed operand id")
                    arg_types.append(None)
                    continue
                known = defined.get(arg.id)
                if known is None:
                    errors.append(f"{fn.name}:{block.name}: use-before-definition or unknown value %{arg.id}")
                elif known != arg.type:
                    errors.append(f"{fn.name}:{block.name}: operand %{arg.id} claims "
                                  f"{getattr(arg.type, 'name', repr(arg.type))}, "
                                  f"defined as {getattr(known, 'name', repr(known))}")
                arg_types.append(known)

            result = ins.result
            if result is not None:
                if not isinstance(result, MIRValue):
                    errors.append(f"{fn.name}:{block.name}: malformed instruction result")
                    result = None
                else:
                    if type(result.id) is not int or result.id < 0:
                        errors.append(f"{fn.name}:{block.name}: malformed result id")
                        result = None
                    elif result.id in defined:
                        errors.append(f"{fn.name}:{block.name}: duplicate or negative value id %{result.id}")
                    if result is None:
                        continue
                    if (not isinstance(result.type, MIRType) or
                            result.type not in value_types):
                        errors.append(f"{fn.name}:{block.name}: unsupported result type for %{result.id}")

            if ins.op == "const":
                valid = not ins.args and result is not None
                if result is not None:
                    if result.type == I64:
                        valid = valid and type(ins.imm) is int and -(1 << 63) <= ins.imm < (1 << 63)
                    elif result.type == F64:
                        valid = valid and type(ins.imm) in (int, float)
                    elif result.type == BOOL:
                        valid = valid and type(ins.imm) is bool
                    else:
                        valid = False
                if not valid:
                    errors.append(f"{fn.name}:{block.name}: invalid const signature or immediate")
            elif ins.op in {"add", "sub", "mul"}:
                if (len(ins.args) != 2 or result is None or
                        result.type not in {I64, F64} or
                        len(arg_types) != 2 or arg_types[0] != result.type or
                        arg_types[1] != result.type):
                    errors.append(f"{fn.name}:{block.name}: {ins.op} requires two same-typed scalar operands")
            elif ins.op == "div":
                if (len(ins.args) != 2 or result is None or result.type != F64 or
                        arg_types != [F64, F64]):
                    errors.append(f"{fn.name}:{block.name}: div currently requires two f64 operands and an f64 result")
            elif ins.op == "mod":
                errors.append(f"{fn.name}:{block.name}: mod is not supported by MIR-to-LLVM v0.1")
            elif ins.op.startswith("cmp_"):
                pred = ins.op[4:]
                valid_pred = pred in {"eq", "ne", "lt", "le", "gt", "ge"}
                valid_types = (len(arg_types) == 2 and arg_types[0] == arg_types[1]
                               and arg_types[0] in {I64, F64, BOOL})
                bool_order = arg_types and arg_types[0] == BOOL and pred not in {"eq", "ne"}
                if (not valid_pred or not valid_types or bool_order or
                        result is None or result.type != BOOL or len(ins.args) != 2):
                    errors.append(f"{fn.name}:{block.name}: invalid {ins.op} signature")
            elif ins.op == "box":
                if len(ins.args) != 1 or result is None or result.type != MVAL or arg_types[0] not in scalar_types:
                    errors.append(f"{fn.name}:{block.name}: box requires one scalar and an MVal* result")
            elif ins.op == "unbox":
                if len(ins.args) != 1 or arg_types[0] != MVAL or result is None or result.type not in scalar_types:
                    errors.append(f"{fn.name}:{block.name}: unbox requires one MVal* operand and a scalar result")
            elif ins.op == "return":
                if result is not None or len(ins.args) > 1:
                    errors.append(f"{fn.name}:{block.name}: malformed return")
                elif fn.return_type == VOID and ins.args:
                    errors.append(f"{fn.name}:{block.name}: void function cannot return a value")
                elif fn.return_type != VOID and (len(ins.args) != 1 or arg_types[0] != fn.return_type):
                    errors.append(f"{fn.name}:{block.name}: return type mismatch")
                if saw_return:
                    errors.append(f"{fn.name}:{block.name}: multiple returns in single-block MIR")
                saw_return = True
                terminated = True

            # A result becomes available only after this instruction, so
            # same-instruction and forward references are rejected above.
            if isinstance(result, MIRValue):
                if result.id not in defined:
                    defined[result.id] = result.type

        if not block.instructions or not saw_return:
            errors.append(f"{fn.name}:{block.name}: function must end with a return")
        elif (not isinstance(block.instructions[-1], MIRInstr) or
              block.instructions[-1].op != "return"):
            errors.append(f"{fn.name}:{block.name}: return must be the final instruction")
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
                        if ins.result.type == I64:
                            # Misa Int arithmetic is signed 64-bit wrapping,
                            # not Python's arbitrary-precision arithmetic.
                            value = ((int(value) + (1 << 63)) & ((1 << 64) - 1)) - (1 << 63)
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
    errors = verify(module)
    if errors:
        raise ValueError("refusing to optimize invalid MIR: " + "; ".join(errors))
    rounds = 0
    for _ in range(max_rounds):
        changed = constant_fold(module) or False
        changed = eliminate_box_unbox(module) or changed
        changed = dead_code_eliminate(module) or changed
        errors = verify(module)
        if errors:
            raise RuntimeError("MIR optimization produced invalid IR: " +
                               "; ".join(errors))
        if not changed:
            break
        rounds += 1
    return rounds
