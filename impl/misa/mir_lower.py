"""Stage-1 typed AST -> MIR lowering.

This deliberately covers the side-effect-free scalar subset first:
Int/Float/Bool parameters, lets, arithmetic/comparison expressions and
returns. Unsupported control flow is rejected explicitly rather than
silently falling back to the old LLVM emitter.
"""
from __future__ import annotations
from . import ast as A
from .mir import MIRBuilder, MIRModule, MIRType, I64, F64, BOOL, VOID, verify


class MIRLowerError(Exception):
    pass


def _type(te):
    if te is None:
        return VOID
    if te.is_list or te.is_map or te.is_func or te.type_args:
        raise MIRLowerError("aggregate/function types are not in MIR stage-1")
    return {"Int": I64, "Float": F64, "Bool": BOOL, "Void": VOID}.get(te.name)


def _expr_type(e, env):
    if isinstance(e, A.IntLit): return I64
    if isinstance(e, A.FloatLit): return F64
    if isinstance(e, A.BoolLit): return BOOL
    if isinstance(e, A.Ident):
        if e.name not in env: raise MIRLowerError(f"unknown value '{e.name}'")
        return env[e.name][0]
    if isinstance(e, A.BinOp):
        lt, rt = _expr_type(e.left, env), _expr_type(e.right, env)
        if e.op in ("==", "!=", "<", "<=", ">", ">="):
            if lt != rt: raise MIRLowerError(f"comparison type mismatch: {lt.name} vs {rt.name}")
            return BOOL
        if lt != rt or lt not in (I64, F64):
            raise MIRLowerError(f"arithmetic requires matching Int/Float operands")
        return lt
    raise MIRLowerError(f"unsupported MIR expression: {type(e).__name__}")


def lower_program(prog: A.Program) -> MIRModule:
    b = MIRBuilder()
    for item in prog.items:
        if not isinstance(item, A.FnDecl):
            continue
        params = [_type(te) for _, te in item.params]
        if any(t is None for t in params):
            raise MIRLowerError(f"function '{item.name}' has unsupported parameter type")
        ret = _type(item.ret)
        if ret is None:
            raise MIRLowerError(f"function '{item.name}' has unsupported return type")
        fn = b.function(item.name, params, ret)
        block = fn.blocks[0]
        env = {name: (ty, val) for (name, _), ty, val in
               zip(item.params, params, fn.params)}

        def emit_expr(e):
            if isinstance(e, A.IntLit):
                return b.emit(block, "const", I64, imm=e.value)
            if isinstance(e, A.FloatLit):
                return b.emit(block, "const", F64, imm=e.value)
            if isinstance(e, A.BoolLit):
                return b.emit(block, "const", BOOL, imm=e.value)
            if isinstance(e, A.Ident):
                return env[e.name][1]
            if isinstance(e, A.BinOp):
                l, r = emit_expr(e.left), emit_expr(e.right)
                ops = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod",
                       "==": "cmp_eq", "!=": "cmp_ne", "<": "cmp_lt",
                       "<=": "cmp_le", ">": "cmp_gt", ">=": "cmp_ge"}
                op = ops.get(e.op)
                if op is None: raise MIRLowerError(f"unsupported operator '{e.op}'")
                ty = _expr_type(e, env)
                return b.emit(block, op, ty if op in ("add","sub","mul","div","mod") else BOOL, (l, r))
            raise MIRLowerError(f"unsupported MIR expression: {type(e).__name__}")

        returned = False
        for stmt in item.body:
            if isinstance(stmt, A.LetStmt):
                ty = _type(stmt.ann) if stmt.ann is not None else _expr_type(stmt.value, env)
                if ty is None: raise MIRLowerError(f"unsupported type for '{stmt.name}'")
                v = emit_expr(stmt.value)
                if v.type != ty: raise MIRLowerError(f"type mismatch in let '{stmt.name}'")
                env[stmt.name] = (ty, v)
            elif isinstance(stmt, A.ReturnStmt):
                v = emit_expr(stmt.value) if stmt.value is not None else None
                if ret == VOID:
                    if v is not None: raise MIRLowerError(f"void function '{item.name}' returns a value")
                elif v is None or v.type != ret:
                    raise MIRLowerError(f"return type mismatch in '{item.name}'")
                b.emit(block, "return", None, (v,) if v is not None else ())
                returned = True
            else:
                raise MIRLowerError(f"function '{item.name}' uses unsupported statement: {type(stmt).__name__}")
        if not returned:
            if ret != VOID:
                raise MIRLowerError(f"function '{item.name}' has no return")
            b.emit(block, "return", None, ())
    errors = verify(b.module)
    if errors:
        raise MIRLowerError("\n".join(errors))
    return b.module
