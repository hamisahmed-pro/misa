"""
Misa Method JIT — bytecode -> LLVM IR -> native code (Phase 3).

Compiles a BytecodeFunction to LLVM IR operating on MVal* (the unified
representation), JIT-executes it via llvmlite MCJIT, and links against
the MVal* runtime (misa_vm_mval.so).

Prototype scope: arithmetic, comparisons, jumps, vars, consts, return.
CALL/LIST/PRINT lower via runtime helpers where feasible; unsupported
opcodes raise NotImplementedError (caller falls back to the VM).
"""

from llvmlite import ir
from llvmlite.binding import (
    initialize_native_target, initialize_native_asmprinter,
    Target, TargetMachine, load_library_permanently,
)
import ctypes
import os

from .bytecode import (
    LOAD_CONST, LOAD_VAR, STORE_VAR,
    BINARY_ADD, BINARY_SUB, BINARY_MUL, BINARY_DIV, BINARY_MOD,
    BINARY_AND, BINARY_OR, BINARY_XOR, BINARY_SHL, BINARY_SHR,
    COMPARE_LT, COMPARE_LE, COMPARE_GT, COMPARE_GE, COMPARE_EQ, COMPARE_NE,
    JUMP, JUMP_IF_FALSE, JUMP_IF_TRUE,
    CALL, RETURN, LIST_GET, LIST_SET, BUILD_LIST, PRINT, POP, DUP,
)

_initialized = False

def _ensure_init():
    global _initialized
    if _initialized:
        return
    initialize_native_target()
    initialize_native_asmprinter()
    # Make MVal* runtime symbols resolvable to JIT'd code.
    rt = os.path.join(os.path.dirname(__file__), "misa_vm_mval.so")
    if os.path.isfile(rt):
        load_library_permanently(rt)
    _initialized = True


PTR = ir.IntType(8).as_pointer()  # MVal* is opaque
I64 = ir.IntType(64)
I32 = ir.IntType(32)

# Runtime functions we call: name -> (ret, [args])
_RT_SIGS = {
    "misa_add": (PTR, [PTR, PTR, I32, I32]),
    "misa_sub": (PTR, [PTR, PTR, I32, I32]),
    "misa_mul": (PTR, [PTR, PTR, I32, I32]),
    "misa_div": (PTR, [PTR, PTR, I32, I32]),
    "misa_mod": (PTR, [PTR, PTR, I32, I32]),
    "misa_lt":  (PTR, [PTR, PTR, I32, I32]),
    "misa_le":  (PTR, [PTR, PTR, I32, I32]),
    "misa_gt":  (PTR, [PTR, PTR, I32, I32]),
    "misa_ge":  (PTR, [PTR, PTR, I32, I32]),
    "misa_eq":  (PTR, [PTR, PTR]),
    "misa_ne":  (PTR, [PTR, PTR]),
    "misa_truthy": (I32, [PTR]),
    "misa_pending": (I32, []),
    "misa_list_new": (PTR, []),
    "misa_list_push": (ir.VoidType(), [PTR, PTR]),
    "misa_list_get_raw": (PTR, [PTR, I64, I32, I32]),
    "misa_list_set_raw": (ir.VoidType(), [PTR, I64, PTR, I32, I32]),
    "misa_index_get": (PTR, [PTR, PTR, I32, I32]),
    "misa_index_set": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_render": (PTR, [PTR]),
    "misa_int": (PTR, [I64]),
    "misa_tag": (I32, [PTR]),
    "misa_int_val": (I64, [PTR]),
}


def analyze_int_only(fn):
    """Static analysis: True if the function operates purely on Ints.

    Checks: all consts are ints (or None for implicit return), all opcodes
    are int-compatible (arithmetic, comparison, jumps, var load/store,
    return). Rejects CALL (functions aren't i64 values in specialized mode),
    LIST/PRINT (need MVal*), etc.
    Used to select the type-specialized i64 codegen path.
    """
    from .bytecode import (
        LOAD_CONST, LOAD_VAR, STORE_VAR,
        BINARY_ADD, BINARY_SUB, BINARY_MUL, BINARY_DIV, BINARY_MOD,
        BINARY_AND, BINARY_OR, BINARY_XOR, BINARY_SHL, BINARY_SHR,
        COMPARE_LT, COMPARE_LE, COMPARE_GT, COMPARE_GE, COMPARE_EQ,
        COMPARE_NE, JUMP, JUMP_IF_FALSE, JUMP_IF_TRUE, RETURN, POP, DUP,
    )
    INT_OPS = {
        LOAD_CONST, LOAD_VAR, STORE_VAR,
        BINARY_ADD, BINARY_SUB, BINARY_MUL, BINARY_DIV, BINARY_MOD,
        BINARY_AND, BINARY_OR, BINARY_XOR, BINARY_SHL, BINARY_SHR,
        COMPARE_LT, COMPARE_LE, COMPARE_GT, COMPARE_GE, COMPARE_EQ,
        COMPARE_NE, JUMP, JUMP_IF_FALSE, JUMP_IF_TRUE, RETURN, POP, DUP,
    }
    # NOTE: CALL is intentionally excluded. In specialized i64 mode,
    # function values cannot be represented as i64. Supporting CALL
    # requires the generic MVal* JIT path (future work).
    for op, _ in fn.code:
        if op not in INT_OPS:
            return False
    for c in fn.consts:
        # Allow None (implicit return const, unreachable in int-only fns
        # with explicit returns). Reject bools (subclass of int).
        if c is None:
            continue
        if not isinstance(c, int) or isinstance(c, bool):
            return False
    return True


class JitCompiler:
    """BytecodeFunction -> LLVM module with `misa_jit_<name>`.

    Two modes:
    - Generic (MVal*): all values are MVal*, calls runtime helpers.
    - Specialized (i64): for int-only functions (analyze_int_only),
      values are raw i64, arithmetic is native LLVM. Result is boxed
      to MVal* on return. Much faster.
    """

    def __init__(self, fn, specialize_int=False):
        _ensure_init()
        self.fn = fn
        self.specialize_int = specialize_int and analyze_int_only(fn)
        self.mod = ir.Module(name=f"jit_{fn.name}")
        self.mod.triple = Target.from_default_triple().triple
        self._decls = {}

    def _decl(self, name):
        if name not in self._decls:
            ret, args = _RT_SIGS[name]
            fty = ir.FunctionType(ret, args)
            self._decls[name] = ir.Function(self.mod, fty, name=name)
        return self._decls[name]

    def compile(self):
        if self.specialize_int:
            return self.compile_specialized()
        return self.compile_generic()

    def compile_specialized(self):
        """i64-specialized codegen for int-only functions.

        Signature: MVal* jit_fn(i64* vars, i64* consts).
        All values are raw i64; result boxed via misa_int on return.
        """
        fn = self.fn
        # Stubs for emitter-generated symbols.
        htable = ir.GlobalVariable(self.mod, I32, "misa_num_handlers")
        htable.initializer = ir.Constant(I32, 0)
        htable.linkage = "common"
        htab = ir.GlobalVariable(self.mod, PTR, "misa_handler_table")
        htab.initializer = ir.Constant(PTR, None)
        htab.linkage = "common"

        # Declare misa_int for boxing the result.
        misa_int_fn = ir.Function(
            self.mod,
            ir.FunctionType(PTR, [I64]),
            name="misa_int")

        # MVal* jit_fn(i64* vars, i64* consts)
        fty = ir.FunctionType(PTR, [I64.as_pointer(), I64.as_pointer()])
        jfn = ir.Function(self.mod, fty, name=f"misa_jit_{fn.name}")
        vars_p, consts_p = jfn.args
        vars_p.name = "vars"
        consts_p.name = "consts"

        blocks = {}
        builder = ir.IRBuilder()
        entry = jfn.append_basic_block("entry")
        for i in range(len(fn.code)):
            blocks[i] = jfn.append_basic_block(f"pc{i}")
        end_block = jfn.append_basic_block("end")

        builder.position_at_end(entry)
        stack = builder.alloca(I64, ir.Constant(I64, 1024), name="stack")
        sp = builder.alloca(I64, name="sp")
        builder.store(ir.Constant(I64, 0), sp)
        builder.branch(blocks[0])

        def push(val, b):
            spv = b.load(sp, name="spv")
            ptr = b.gep(stack, [spv], name="sptr")
            b.store(val, ptr)
            b.store(b.add(spv, ir.Constant(I64, 1)), sp)

        def pop(b):
            spv = b.load(sp, name="spv2")
            newsp = b.sub(spv, ir.Constant(I64, 1), name="newsp")
            b.store(newsp, sp)
            ptr = b.gep(stack, [newsp], name="spptr")
            return b.load(ptr, name="popv")

        for pc, (op, arg) in enumerate(fn.code):
            b = ir.IRBuilder(blocks[pc])
            nxt = blocks[pc + 1] if pc + 1 < len(fn.code) else end_block

            if op == LOAD_CONST:
                ptr = b.gep(consts_p, [ir.Constant(I64, arg)], name="cptr")
                v = b.load(ptr, name="cval")
                push(v, b)
                b.branch(nxt)
            elif op == LOAD_VAR:
                ptr = b.gep(vars_p, [ir.Constant(I64, arg)], name="vptr")
                v = b.load(ptr, name="vval")
                push(v, b)
                b.branch(nxt)
            elif op == STORE_VAR:
                v = pop(b)
                ptr = b.gep(vars_p, [ir.Constant(I64, arg)], name="vsptr")
                b.store(v, ptr)
                b.branch(nxt)
            elif op == BINARY_ADD:
                rb, ra = pop(b), pop(b)
                push(b.add(ra, rb, name="add"), b)
                b.branch(nxt)
            elif op == BINARY_SUB:
                rb, ra = pop(b), pop(b)
                push(b.sub(ra, rb, name="sub"), b)
                b.branch(nxt)
            elif op == BINARY_MUL:
                rb, ra = pop(b), pop(b)
                push(b.mul(ra, rb, name="mul"), b)
                b.branch(nxt)
            elif op == BINARY_DIV:
                # Misa / on ints: check semantics — use sdiv (trunc).
                # Note: div by zero must raise; omitted in prototype
                # (caller ensures non-zero or accepts UB).
                rb, ra = pop(b), pop(b)
                push(b.sdiv(ra, rb, name="div"), b)
                b.branch(nxt)
            elif op == BINARY_MOD:
                rb, ra = pop(b), pop(b)
                push(b.srem(ra, rb, name="mod"), b)
                b.branch(nxt)
            elif op == BINARY_AND:
                rb, ra = pop(b), pop(b)
                push(b.and_(ra, rb, name="and"), b)
                b.branch(nxt)
            elif op == BINARY_OR:
                rb, ra = pop(b), pop(b)
                push(b.or_(ra, rb, name="or"), b)
                b.branch(nxt)
            elif op == BINARY_XOR:
                rb, ra = pop(b), pop(b)
                push(b.xor(ra, rb, name="xor"), b)
                b.branch(nxt)
            elif op == BINARY_SHL:
                rb, ra = pop(b), pop(b)
                push(b.shl(ra, rb, name="shl"), b)
                b.branch(nxt)
            elif op == BINARY_SHR:
                rb, ra = pop(b), pop(b)
                push(b.ashr(ra, rb, name="shr"), b)
                b.branch(nxt)
            elif op in (COMPARE_LT, COMPARE_LE, COMPARE_GT, COMPARE_GE,
                        COMPARE_EQ, COMPARE_NE):
                pred = {
                    COMPARE_LT: "<", COMPARE_LE: "<=",
                    COMPARE_GT: ">", COMPARE_GE: ">=",
                    COMPARE_EQ: "==", COMPARE_NE: "!=",
                }[op]
                rb, ra = pop(b), pop(b)
                cmp = b.icmp_signed(pred, ra, rb, name="cmp")
                # i1 -> i64 (0/1)
                push(b.zext(cmp, I64, name="bool"), b)
                b.branch(nxt)
            elif op == JUMP:
                b.branch(blocks[arg])
            elif op in (JUMP_IF_FALSE, JUMP_IF_TRUE):
                v = pop(b)
                is_zero = b.icmp_signed("==", v, ir.Constant(I64, 0),
                                        name="iszero")
                # JUMP_IF_FALSE: jump if zero. JUMP_IF_TRUE: jump if non-zero.
                cond = (b.icmp_signed("!=", v, ir.Constant(I64, 0),
                                      name="isnonzero")
                        if op == JUMP_IF_TRUE else is_zero)
                b.cbranch(cond, blocks[arg], nxt)
            elif op == RETURN:
                v = pop(b)
                boxed = b.call(misa_int_fn, [v], name="boxed")
                b.ret(boxed)
            elif op == POP:
                pop(b)
                b.branch(nxt)
            elif op == DUP:
                spv = b.load(sp, name="dspv")
                ptr = b.gep(stack, [b.sub(spv, ir.Constant(I64, 1))],
                            name="dptr")
                v = b.load(ptr, name="dval")
                push(v, b)
                b.branch(nxt)
            else:
                raise NotImplementedError(
                    f"JIT specialized: opcode {op} not supported")

        b = ir.IRBuilder(end_block)
        b.ret(ir.Constant(PTR, None))
        return self.mod

    def compile_generic(self):
        """MVal*-generic codegen for arbitrary functions.

        Signature: MVal* jit_fn(MVal** vars, MVal** consts).
        All values are MVal*; calls runtime helpers. Slower than specialized
        but handles any type. Includes deopt via misa_pending() checks.
        """
        fn = self.fn
        # Stubs for emitter-generated symbols.
        htable = ir.GlobalVariable(self.mod, I32, "misa_num_handlers")
        htable.initializer = ir.Constant(I32, 0)
        htable.linkage = "common"
        htab = ir.GlobalVariable(self.mod, PTR, "misa_handler_table")
        htab.initializer = ir.Constant(PTR, None)
        htab.linkage = "common"

        fty = ir.FunctionType(PTR, [PTR.as_pointer(), PTR.as_pointer()])
        jfn = ir.Function(self.mod, fty, name=f"misa_jit_{fn.name}")
        vars_p, consts_p = jfn.args
        vars_p.name = "vars"
        consts_p.name = "consts"

        # Blocks: one per bytecode offset (for jumps). Build map first.
        blocks = {}
        builder = ir.IRBuilder()
        entry = jfn.append_basic_block("entry")
        for i in range(len(fn.code)):
            blocks[i] = jfn.append_basic_block(f"pc{i}")
        end_block = jfn.append_basic_block("end")
        # Exception block for misa_pending()
        err_block = jfn.append_basic_block("misa_error")

        # Stack: alloca [1024 x MVal*], sp as alloca i64
        builder.position_at_end(entry)
        stack = builder.alloca(PTR, ir.Constant(I64, 1024), name="stack")
        sp = builder.alloca(I64, name="sp")
        builder.store(ir.Constant(I64, 0), sp)
        builder.branch(blocks[0])

        # Helpers as closures over builder
        def stack_push(val, b):
            spv = b.load(sp, name="spv")
            ptr = b.gep(stack, [spv], name="sptr")
            b.store(val, ptr)
            b.store(b.add(spv, ir.Constant(I64, 1)), sp)

        def stack_pop(b):
            spv = b.load(sp, name="spv2")
            newsp = b.sub(spv, ir.Constant(I64, 1), name="newsp")
            b.store(newsp, sp)
            ptr = b.gep(stack, [newsp], name="spptr")
            return b.load(ptr, name="popv")

        def check_pending(b, next_block):
            p = b.call(self._decl("misa_pending"), [], name="pend")
            is_err = b.icmp_signed("!=", p, ir.Constant(I32, 0), name="iserr")
            b.cbranch(is_err, err_block, next_block)

        zero32 = ir.Constant(I32, 0)

        for pc, (op, arg) in enumerate(fn.code):
            b = ir.IRBuilder(blocks[pc])
            nxt = blocks[pc + 1] if pc + 1 < len(fn.code) else end_block

            if op == LOAD_CONST:
                # consts[arg] -- consts are pre-converted MVal* array
                ptr = b.gep(consts_p, [ir.Constant(I64, arg)], name="cptr")
                v = b.load(ptr, name="cval")
                # consts_p is MVal**; gep gives MVal**; load gives MVal*
                stack_push(v, b)
                b.branch(nxt)
            elif op == LOAD_VAR:
                ptr = b.gep(vars_p, [ir.Constant(I64, arg)], name="vptr")
                v = b.load(ptr, name="vval")
                stack_push(v, b)
                b.branch(nxt)
            elif op == STORE_VAR:
                v = stack_pop(b)
                ptr = b.gep(vars_p, [ir.Constant(I64, arg)], name="vsptr")
                b.store(v, ptr)
                b.branch(nxt)
            elif op in (BINARY_ADD, BINARY_SUB, BINARY_MUL, BINARY_DIV,
                        BINARY_MOD):
                rname = {BINARY_ADD: "misa_add", BINARY_SUB: "misa_sub",
                         BINARY_MUL: "misa_mul", BINARY_DIV: "misa_div",
                         BINARY_MOD: "misa_mod"}[op]
                rb = stack_pop(b)
                ra = stack_pop(b)
                r = b.call(self._decl(rname), [ra, rb, zero32, zero32],
                           name="binr")
                stack_push(r, b)
                check_pending(b, nxt)
            elif op in (COMPARE_LT, COMPARE_LE, COMPARE_GT, COMPARE_GE):
                rname = {COMPARE_LT: "misa_lt", COMPARE_LE: "misa_le",
                         COMPARE_GT: "misa_gt", COMPARE_GE: "misa_ge"}[op]
                rb = stack_pop(b)
                ra = stack_pop(b)
                r = b.call(self._decl(rname), [ra, rb, zero32, zero32],
                           name="cmpr")
                stack_push(r, b)
                check_pending(b, nxt)
            elif op in (COMPARE_EQ, COMPARE_NE):
                rname = "misa_eq" if op == COMPARE_EQ else "misa_ne"
                rb = stack_pop(b)
                ra = stack_pop(b)
                r = b.call(self._decl(rname), [ra, rb], name="cmpr")
                stack_push(r, b)
                check_pending(b, nxt)
            elif op == JUMP:
                b.branch(blocks[arg])
            elif op in (JUMP_IF_FALSE, JUMP_IF_TRUE):
                v = stack_pop(b)
                t = b.call(self._decl("misa_truthy"), [v], name="truthy")
                cond = b.icmp_signed("!=" if op == JUMP_IF_TRUE else "==",
                                     t, ir.Constant(I32, 0), name="jcond")
                b.cbranch(cond, blocks[arg], nxt)
            elif op == RETURN:
                v = stack_pop(b)
                b.ret(v)
            elif op == POP:
                stack_pop(b)
                b.branch(nxt)
            elif op == DUP:
                spv = b.load(sp, name="dspv")
                ptr = b.gep(stack, [b.sub(spv, ir.Constant(I64, 1))],
                            name="dptr")
                v = b.load(ptr, name="dval")
                stack_push(v, b)
                b.branch(nxt)
            elif op == LIST_GET:
                # Stack: [list, idx] -> [value]
                idx = stack_pop(b)
                lst = stack_pop(b)
                # Try raw int index first; fall back to generic.
                # For simplicity, use misa_index_get (handles all cases).
                r = b.call(self._decl("misa_index_get"),
                           [lst, idx, zero32, zero32], name="idxget")
                stack_push(r, b)
                check_pending(b, nxt)
            elif op == LIST_SET:
                # Stack: [list, idx, val] -> []
                val = stack_pop(b)
                idx = stack_pop(b)
                lst = stack_pop(b)
                b.call(self._decl("misa_index_set"),
                       [lst, idx, val, zero32, zero32])
                check_pending(b, nxt)
            elif op == BUILD_LIST:
                # Stack: [item0, ..., itemN-1] -> [list]; arg = N
                lst = b.call(self._decl("misa_list_new"), [], name="newlist")
                # Pop N items in reverse, store temporarily, then push.
                # Simpler: pop into a temp alloca array, then push in order.
                n = arg
                if n > 0:
                    tmp = b.alloca(PTR, ir.Constant(I64, n), name="tmpitems")
                    for i in range(n - 1, -1, -1):
                        it = stack_pop(b)
                        ptr = b.gep(tmp, [ir.Constant(I64, i)], name="tptr")
                        b.store(it, ptr)
                    for i in range(n):
                        ptr = b.gep(tmp, [ir.Constant(I64, i)], name="tptr2")
                        it = b.load(ptr, name="tit")
                        b.call(self._decl("misa_list_push"), [lst, it])
                stack_push(lst, b)
                b.branch(nxt)
            elif op == PRINT:
                # Stack: [value] -> []; renders via misa_render.
                # NOTE: Actual stdout writing needs a runtime helper;
                # for now, we render and drop (VM handles real print).
                # This is a limitation documented for future work.
                v = stack_pop(b)
                r = b.call(self._decl("misa_render"), [v], name="rendered")
                # Push a null to keep stack balanced for ExprStmt POP.
                # Actually PRINT in our compiler pushes None after; the
                # generic path here just drops the value.
                b.branch(nxt)
            else:
                raise NotImplementedError(
                    f"JIT: opcode {op} not supported (pc={pc})")

        # end_block: implicit return null (unreachable in well-formed code)
        b = ir.IRBuilder(end_block)
        b.ret(ir.Constant(PTR, None))
        # err_block: return null on misa error (caller checks pending)
        b = ir.IRBuilder(err_block)
        b.ret(ir.Constant(PTR, None))

        return self.mod


def jit_cache_key(fn):
    """Content hash for a BytecodeFunction's JIT cache entry."""
    import hashlib
    h = hashlib.sha256()
    h.update(fn.name.encode())
    for op, arg in fn.code:
        h.update(f"{op}:{arg};".encode())
    for c in fn.consts:
        h.update(f"{type(c).__name__}:{c};".encode())
    h.update(f"vars:{len(fn.varnames)};".encode())
    return h.hexdigest()[:16]


def jit_cache_path(fn):
    """Path to the cached .so for a BytecodeFunction."""
    import os
    d = os.path.join(os.path.expanduser("~"), ".cache", "misa", "jit")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{fn.name}_{jit_cache_key(fn)}.so")


def jit_compile_to_so(fn, so_path, specialize_int=False):
    """Compile fn (BytecodeFunction) to a shared object via LLVM.

    Uses the existing AOT build infrastructure (llvmlite -> object ->
    link with runtime). Returns True on success.

    Generic: exports `misa_jit_<name>(MVal**, MVal**) -> MVal*`.
    Specialized (specialize_int=True, int-only fns):
    exports `misa_jit_<name>(i64*, i64*) -> MVal*`.
    """
    import subprocess
    import tempfile
    _ensure_init()
    # Check cache first.
    if so_path is None:
        so_path = jit_cache_path(fn)
        if os.path.isfile(so_path):
            return True  # cache hit
    comp = JitCompiler(fn, specialize_int=specialize_int)
    mod = comp.compile()
    llvm_ir = str(mod)

    from llvmlite import binding as _b
    target = Target.from_default_triple()
    tm = target.create_target_machine()
    backing_mod = _b.parse_assembly(llvm_ir)
    backing_mod.verify()
    # NOTE: LLVM optimization passes skipped in prototype (llvmlite 0.50
    # new-pass-manager API is fiddly). The IR is correct; O2 is future work.
    obj_bytes = tm.emit_object(backing_mod)

    rt_dir = os.path.join(os.path.dirname(__file__), "aot", "rt")
    rt_srcs = ["misa_rt.c", "misa_task.c", "misa_json.c", "misa_model.c",
               "platform_linux.c", "ui_text.c"]
    with tempfile.TemporaryDirectory() as td:
        obj_path = os.path.join(td, "jit.o")
        with open(obj_path, "wb") as f:
            f.write(obj_bytes)
        # Compile runtime sources to objects
        rt_objs = []
        for src in rt_srcs:
            o = os.path.join(td, src.replace(".c", ".o"))
            r = subprocess.run(
                ["gcc", "-fPIC", "-O2", "-c",
                 os.path.join(rt_dir, src), "-o", o,
                 "-I", rt_dir],
                capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"runtime compile failed: {r.stderr[:500]}")
            rt_objs.append(o)
        # Link shared object
        r = subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", obj_path] + rt_objs +
            ["-o", so_path],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"link failed: {r.stderr[:500]}")
    return True


def jit_load_and_run(so_path, fn_name, vars_vals, consts_vals,
                     specialize_int=False):
    """dlopen the .so from jit_compile_to_so and call the function.

    Generic: vars_vals/consts_vals are lists of MVal* addresses (ints).
    Specialized: vars_vals/consts_vals are lists of Python ints (i64).
    Returns the MVal* address of the result, or None if the JIT'd code
    hit a misa error (deopt signal — caller should fall back to VM).
    """
    lib = ctypes.CDLL(so_path)
    # Ensure runtime is initialized (idempotent)
    if hasattr(lib, "misa_rt_init"):
        lib.misa_rt_init()
    if specialize_int:
        cfunc = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int64),
            ctypes.POINTER(ctypes.c_int64),
        )(getattr(lib, f"misa_jit_{fn_name}"))
        VarsArr = (ctypes.c_int64 * max(1, len(vars_vals)))(*vars_vals)
        ConstsArr = (ctypes.c_int64 * max(1, len(consts_vals)))(*consts_vals)
    else:
        cfunc = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )(getattr(lib, f"misa_jit_{fn_name}"))
        VarsArr = (ctypes.c_void_p * max(1, len(vars_vals)))(*vars_vals)
        ConstsArr = (ctypes.c_void_p * max(1, len(consts_vals)))(*consts_vals)
    result = cfunc(VarsArr, ConstsArr)
    # Deopt: NULL return means the JIT'd code hit misa_pending()
    # (runtime error). Caller must fall back to the VM/interpreter.
    if not result:
        return None
    return result
