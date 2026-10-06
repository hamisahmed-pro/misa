"""Misa AOT: AST -> LLVM IR via llvmlite.

Every Misa value is an i8* (MVal*). Fallible runtime calls are followed
by a pending-error check that branches to the innermost error target
(function epilogue returning NULL, or a try/catch handler). The check
inspects the pending flag, not the return value, so sentinels like
NULL from misa_iter_next are safe.
"""
from __future__ import annotations

from llvmlite import ir

from .. import ast as A

PTR = ir.IntType(8).as_pointer()   # MVal*
I1 = ir.IntType(1)
I8 = ir.IntType(8)
I32 = ir.IntType(32)
I64 = ir.IntType(64)
DBL = ir.DoubleType()

NULL_PTR = ir.Constant(PTR, None)


class AOTError(Exception):
    """Compile-time rejection: carries (code, message, line, col)."""

    def __init__(self, code, message, line=0, col=0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.col = col


# ---------------------------------------------------------------------
# lexical scopes

class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.names = {}

    def child(self):
        return Scope(self)

    def declare(self, name, alloca):
        self.names[name] = alloca

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        raise AOTError("E101", f"undefined name '{name}'")


class FnCtx:
    def __init__(self, emitter, builder, fn):
        self.em = emitter
        self.builder = builder
        self.fn = fn
        self.scope = Scope()
        self.err_stack = []     # basic blocks: innermost error target
        self.nil = None         # cached nil value
        # Phase 5a: names of `var`s cell-allocated (1-elem lists) because
        # a nested lambda may capture them. Reads/writes go through
        # misa_list_get/misa_index_set instead of a plain alloca.
        self.cell_vars: set = set()
        # Phase 4 (M4): handler compilation state. id_slots maps
        # screen id: names -> binding slots; in_handler marks a
        # compiled event handler (restricted scope).
        self.id_slots = None
        self.in_handler = False


# ---------------------------------------------------------------------
# emitter

_RT_SIGS = {
    # name: (ret, [args])
    "misa_rt_init": (ir.VoidType(), []),
    "misa_int": (PTR, [I64]),
    "misa_float": (PTR, [DBL]),
    "misa_bool": (PTR, [I32]),
    "misa_null": (PTR, []),
    "misa_text": (PTR, [PTR, I64]),
    "misa_text_cstr": (PTR, [PTR]),
    "misa_list_new": (PTR, []),
    "misa_list_push": (ir.VoidType(), [PTR, PTR]),
    "misa_list_get": (PTR, [PTR, I64]),
    "misa_as_int": (I64, [PTR]),
    "misa_map_new": (PTR, []),
    "misa_record_new": (PTR, []),
    "misa_record_set_cstr": (ir.VoidType(), [PTR, PTR, PTR]),
    "misa_media": (PTR, [I32, PTR]),
    "misa_pending": (PTR, []),
    "misa_take_pending": (PTR, []),
    "misa_do_throw": (ir.VoidType(), [PTR, I32, I32]),
    "misa_make_thrown": (PTR, [PTR, I32, I32]),
    "misa_catch_record": (PTR, [PTR]),
    "misa_report_uncaught": (ir.VoidType(), [PTR]),
    "misa_raise": (ir.VoidType(), [PTR, PTR, I32, I32]),
    "misa_truthy": (I32, [PTR]),
    "misa_add": (PTR, [PTR, PTR, I32, I32]),
    "misa_sub": (PTR, [PTR, PTR, I32, I32]),
    "misa_mul": (PTR, [PTR, PTR, I32, I32]),
    "misa_div": (PTR, [PTR, PTR, I32, I32]),
    "misa_mod": (PTR, [PTR, PTR, I32, I32]),
    "misa_neg": (PTR, [PTR, I32, I32]),
    "misa_not": (PTR, [PTR]),
    "misa_eq": (PTR, [PTR, PTR]),
    "misa_ne": (PTR, [PTR, PTR]),
    "misa_lt": (PTR, [PTR, PTR, I32, I32]),
    "misa_le": (PTR, [PTR, PTR, I32, I32]),
    "misa_gt": (PTR, [PTR, PTR, I32, I32]),
    "misa_ge": (PTR, [PTR, PTR, I32, I32]),
    "misa_render": (PTR, [PTR]),
    "misa_text_concat": (PTR, [PTR, PTR]),
    "misa_len": (I64, [PTR, I32, I32]),
    "misa_index_get": (PTR, [PTR, PTR, I32, I32]),
    "misa_index_set": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_record_get": (PTR, [PTR, PTR, I32, I32]),
    "misa_record_set": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_iter_new": (PTR, [PTR, I32, I32]),
    "misa_iter_next": (PTR, [PTR]),
    "misa_print": (PTR, [PTR]),
    "misa_builtin_len": (PTR, [PTR, I32, I32]),
    "misa_builtin_str": (PTR, [PTR]),
    "misa_builtin_int": (PTR, [PTR, I32, I32]),
    "misa_builtin_float": (PTR, [PTR, I32, I32]),
    "misa_builtin_assert": (PTR, [PTR, PTR, I32, I32]),
    "misa_builtin_env": (PTR, [PTR, I32, I32]),
    "misa_builtin_media": (PTR, [I32, PTR, I32, I32]),
    "misa_fs_read": (PTR, [PTR, I32, I32]),
    "misa_fs_write": (PTR, [PTR, PTR, I32, I32]),
    "misa_http_get": (PTR, [PTR, I32, I32]),
    "misa_text_z": (PTR, [PTR]),
    "misa_screen_begin": (ir.VoidType(), [PTR]),
    "misa_screen_comp_begin": (ir.VoidType(), [PTR, I32]),
    "misa_screen_comp_arg": (ir.VoidType(), [PTR, PTR, I32]),
    # Phase 4 (M4): handler args carry the compiled handler's id.
    "misa_screen_comp_arg_handler": (ir.VoidType(), [PTR, I32, I32]),
    # Phase 4 (M4): bind an id: slot to the current component.
    "misa_screen_comp_bind_id": (ir.VoidType(), [I32]),
    "misa_screen_comp_head_end": (ir.VoidType(), [I32]),
    "misa_screen_comp_end": (ir.VoidType(), []),
    "misa_screen_end": (ir.VoidType(), []),
    # Phase 4 (M4): driver interface used by compiled handlers.
    "misa_ui_active": (PTR, []),
    "misa_ui_find_bound": (I32, [PTR, I32]),
    "misa_ui_get_text": (PTR, [PTR, I32]),
    "misa_ui_report_error": (ir.VoidType(), [PTR, PTR]),
    "misa_model_call_meta": (PTR, [PTR, PTR.as_pointer(), I32, I32, I32]),
}


class Emitter:
    def __init__(self, prog: A.Program, entry: str = "main"):
        self.prog = prog
        self.mod = ir.Module("misa")
        self.mod.triple = ""  # filled by build.py
        # Phase 4 (M3): program entry point. "main" for hosted targets;
        # "misa_ui_boot" for the Emscripten web target (the page boots
        # us; there is no main).
        self.entry = entry
        self.rt = {}
        self.str_cache = {}
        self.fns = {}       # misa fn name -> ir.Function
        self.models = {}    # model name -> (ir.Function, meta_global)
        self.screens = {}   # screen name -> ir.Function
        self.handlers = []  # Phase 4 (M4): handler fns, dispatch order
        # Phase 5a: first-class functions. Closure values are MVal*
        # records {__closure_fn: Int(fn ptr), __closure_env: record}.
        self.lambda_counter = 0
        self.trampolines = {}  # fn name -> ir.Function (value wrapper)
        self.CLOSURE_FN = "__closure_fn"
        self.CLOSURE_ENV = "__closure_env"
        self.CLOSURE_DISP = "__closure_disp"
        # Phase 5b: sum types. Values are MVal* records
        # {__tag: Int(variant index), __fields: [MVal* payload]}.
        self.SUM_TAG = "__tag"
        self.SUM_FIELDS = "__fields"
        self.SUM_DISP = "__sum_disp"
        # Phase 5c: generics — lazy monomorphization. (name, arg-tuple)
        # -> ir.Function. Type args are erased at runtime (all MVal*),
        # so specializations share code shape; the mangled symbols keep
        # them distinct and future-proof for unboxed representations.
        self.mono_fns = {}
        self._mono_subst = {}  # type var -> concrete type (current spec)
        self._mono_depth = 0
        self._MONO_MAX_DEPTH = 32
        self.fn_decls = {}
        self.model_decls = {}
        self.screen_decls = {}
        self.type_decls = {}
        self.providers = {}   # provider name -> ProviderDecl
        self.aliases = {}     # alias -> (provider_name, model_id)
        for item in prog.items:
            if isinstance(item, A.FnDecl):
                self.fn_decls[item.name] = item
            elif isinstance(item, A.ModelDecl):
                self.model_decls[item.name] = item
            elif isinstance(item, A.ScreenDecl):
                self.screen_decls[item.name] = item
            elif isinstance(item, A.TypeDecl):
                self.type_decls[item.name] = item
            elif isinstance(item, A.ProviderDecl):
                self.providers[item.name] = item
            elif isinstance(item, A.ModelsDecl):
                for alias, prov, mid in item.entries:
                    self.aliases[alias] = (prov, mid)

    # -- module scaffolding -------------------------------------------

    def declare_runtime(self):
        for name, (ret, args) in _RT_SIGS.items():
            fty = ir.FunctionType(ret, args)
            self.rt[name] = ir.Function(self.mod, fty, name=name)

    def cstr(self, s: str):
        """i8* global constant for a (NUL-free) string."""
        if s in self.str_cache:
            return self.str_cache[s]
        data = s.encode("utf-8") + b"\x00"
        ty = ir.ArrayType(I8, len(data))
        g = ir.GlobalVariable(self.mod, ty,
                              name=f"misa_str_{len(self.str_cache)}")
        g.linkage = "internal"
        g.global_constant = True
        g.initializer = ir.Constant(ty, bytearray(data))
        ptr = g.bitcast(PTR)
        self.str_cache[s] = ptr
        return ptr

    def const_i32(self, v):
        return ir.Constant(I32, v)

    def const_i64(self, v):
        return ir.Constant(I64, v)

    def loc(self, node):
        return (self.const_i32(node.line or 0),
                self.const_i32(node.col or 0))

    # -- error checking ------------------------------------------------

    def check(self, ctx: FnCtx, node):
        """Branch to the innermost error target if a pending error exists."""
        b = ctx.builder
        pe = b.call(self.rt["misa_pending"], [])
        ok = b.icmp_unsigned("==", pe, NULL_PTR,
                             name="noerr")
        cont = b.append_basic_block("cont")
        b.cbranch(ok, cont, ctx.err_stack[-1])
        b.position_at_end(cont)

    # -- top-level ------------------------------------------------------

    def emit(self) -> ir.Module:
        self.declare_runtime()
        self.check_no_streaming(self.prog)
        # declare Misa functions first (recursion)
        # Phase 5c: generic fns are monomorphized lazily on first use.
        for name, decl in self.fn_decls.items():
            if decl.type_params:
                continue
            fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
            self.fns[name] = ir.Function(
                self.mod, fty, name=f"misa_fn_{name}")
        for name, decl in self.model_decls.items():
            self.emit_model_decl(name, decl)
        for name, decl in self.screen_decls.items():
            fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
            self.screens[name] = ir.Function(
                self.mod, fty, name=f"misa_screen_{name}")
        for name, decl in self.fn_decls.items():
            if decl.type_params:
                continue
            self.emit_fn_body(name, decl)
        for name, decl in self.screen_decls.items():
            self.emit_screen_body(name, decl)
        # Phase 4 (M4): the handler dispatch table the drivers call.
        self.emit_handler_table()
        self.emit_main_fn()
        self.emit_entry_main()
        return self.mod

    def check_no_streaming(self, node):
        """Reject stream/await/on anywhere (E402).

        Lambdas are allowed as component args (event handlers, printed
        as <handler>); anywhere else they are rejected at emit time.
        """
        if isinstance(node, (A.StreamDecl, A.Await, A.OnStmt)):
            raise AOTError("E402",
                           "streaming is not supported in AOT v0.3 "
                           "(use `misa run`)",
                           node.line, node.col)
        if isinstance(node, A.Component):
            # lambdas as component args are fine; check the rest
            for an, av in node.args:
                if not isinstance(av, A.Lambda):
                    self.check_no_streaming(av)
            for child in node.children:
                self.check_no_streaming(child)
            return
        for child in self.children(node):
            self.check_no_streaming(child)

    def children(self, node):
        for f in getattr(node, "__dataclass_fields__", {}):
            if f in ("line", "col", "leading_comments"):
                continue
            v = getattr(node, f)
            if isinstance(v, A.Node):
                yield v
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, A.Node):
                        yield x
                    elif isinstance(x, tuple):
                        for y in x:
                            if isinstance(y, A.Node):
                                yield y

    # -- functions ------------------------------------------------------

    # -- Phase 5a: closure capture analysis -------------------------------
    #
    # Over-approximate but sound: if a function body contains ANY lambda
    # expression, every `var` declared in that body becomes a heap cell
    # (1-elem list) so closures share mutable state with the defining
    # scope. Lambdas capture the whole scope chain by walking it at the
    # creation site (immutable vars by value, cell vars by shared cell).

    def _subtree_has_lambda(self, node) -> bool:
        """True if any Lambda occurs anywhere under node."""
        if isinstance(node, A.Lambda):
            return True
        for child in self.children(node):
            if self._subtree_has_lambda(child):
                return True
        return False

    def _cell_vars_for(self, stmts) -> set:
        """Names of `var`s to cell-allocate in this function body."""
        if not any(self._subtree_has_lambda(s) for s in stmts):
            return set()
        out = set()

        def walk(node):
            if isinstance(node, A.LetStmt) and node.mutable:
                out.add(node.name)
            for child in self.children(node):
                walk(child)

        for st in stmts:
            walk(st)
        return out

    def new_fn_ctx(self, fn: ir.Function) -> FnCtx:
        entry = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry)
        ctx = FnCtx(self, b, fn)
        err = fn.append_basic_block("err")
        ctx.err_stack.append(err)
        return ctx

    def finish_fn_ctx(self, ctx: FnCtx):
        b = ctx.builder
        # error epilogue: return NULL (pending flag is set)
        if not ctx.err_stack:
            return
        err = ctx.err_stack[0]
        b.position_at_end(err)
        b.ret(NULL_PTR)
        # normal fallthrough: return nil
        b.position_at_end(ctx.fn.basic_blocks[0])
        # find the last non-terminated block to append fallthrough ret
        for blk in reversed(ctx.fn.blocks):
            if not blk.is_terminated:
                b.position_at_end(blk)
                b.ret(self.get_nil(ctx))
                break

    def get_nil(self, ctx: FnCtx):
        if ctx.nil is None:
            save = ctx.builder.block
            ctx.builder.position_at_end(ctx.fn.basic_blocks[0])
            # insert at start of entry, before other code
            ctx.builder.position_at_start(ctx.fn.basic_blocks[0])
            ctx.nil = ctx.builder.call(self.rt["misa_null"], [],
                                       name="nil")
            ctx.builder.position_at_end(save)
        return ctx.nil

    def alloca_var(self, ctx: FnCtx, name: str):
        b = ctx.builder
        save = b.block
        b.position_at_start(ctx.fn.basic_blocks[0])
        a = b.alloca(PTR, name=f"v_{name}")
        b.position_at_end(save)
        return a

    def emit_fn_body(self, name: str, decl: A.FnDecl):
        fn = self.fns[name]
        self.emit_fn_body_direct(fn, decl)

    def emit_fn_body_direct(self, fn, decl: A.FnDecl):
        ctx = self.new_fn_ctx(fn)
        # Phase 5a: cell-allocate `var`s captured by nested lambdas.
        ctx.cell_vars = self._cell_vars_for(decl.body)
        b = ctx.builder
        for (pn, _), arg in zip(decl.params, fn.args):
            arg.name = f"p_{pn}"
            a = self.alloca_var(ctx, pn)
            b.store(arg, a)
            ctx.scope.declare(pn, a)
        # make nil available
        self.get_nil(ctx)
        b.position_at_end(fn.basic_blocks[0])
        # reposition after allocas: builder is at end of entry already
        self.emit_block(decl.body, ctx)
        self.finish_fn_ctx(ctx)

    # -- Phase 5c: monomorphization ------------------------------------

    def _te_str(self, te) -> str:
        """Render a TypeExpr to a canonical string (for mangling)."""
        if te.is_func:
            ps = ", ".join(self._te_str(p) for p in te.func_params)
            r = self._te_str(te.func_ret) if te.func_ret else "Void"
            s = f"({ps}) -> {r}"
        elif te.is_list:
            if te.list_elem is not None:
                s = f"[{self._te_str(te.list_elem)}]"
            else:
                s = f"[{te.name}]"
        elif te.is_map:
            s = (f"Map<{self._te_str(te.map_key)}, "
                 f"{self._te_str(te.map_val)}>")
        elif te.type_args:
            inner = ", ".join(self._te_str(a) for a in te.type_args)
            s = f"{te.name}<{inner}>"
        else:
            s = te.name
        if te.optional:
            s += "?"
        return s

    @staticmethod
    def _mangle(s: str) -> str:
        """Make a type string symbol-safe, preserving structure."""
        out = []
        i = 0
        while i < len(s):
            ch = s[i]
            if ch.isalnum() or ch == "_":
                out.append(ch)
            elif ch == "[":
                out.append("L")
            elif ch == "<":
                out.append("_")
            elif ch in ", ":
                if out and out[-1] != "_":
                    out.append("_")
            elif ch == "(":
                out.append("F")
            elif ch == "?":
                out.append("Q")
            elif ch == "-" and s[i:i + 2] == "->":
                out.append("R")
                i += 1
            # drop: ] > ) and stray chars
            i += 1
        return "".join(out).strip("_")

    def _get_mono_fn(self, name: str, decl, arg_strs: tuple,
                     node) -> object:
        """Return the monomorphized ir.Function for a generic fn,
        emitting its body on first use (lazy)."""
        key = (name, arg_strs)
        if key in self.mono_fns:
            return self.mono_fns[key]
        if self._mono_depth >= self._MONO_MAX_DEPTH:
            raise AOTError("E223",
                           f"monomorphization depth exceeded for "
                           f"'{name}' (recursive generic instantiation)",
                           node.line, node.col)
        mangled = "__".join([name] + [self._mangle(a)
                                      for a in arg_strs])
        fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
        fn = ir.Function(self.mod, fty, name=f"misa_fn_{mangled}")
        # register before emitting the body (recursive calls)
        self.mono_fns[key] = fn
        saved_subst = self._mono_subst
        self._mono_subst = dict(zip(decl.type_params, arg_strs))
        self._mono_depth += 1
        try:
            self.emit_fn_body_direct(fn, decl)
        finally:
            self._mono_subst = saved_subst
            self._mono_depth -= 1
        return fn

    def _resolve_targs(self, type_args) -> tuple:
        """Render call-site type args to concrete strings, substituting
        the current specialization's type variables."""
        import re
        out = []
        for ta in type_args:
            s = self._te_str(ta)
            for var, concrete in self._mono_subst.items():
                s = re.sub(r"\b" + re.escape(var) + r"\b", concrete, s)
            out.append(s)
        return tuple(out)

    def emit_mono_call(self, name: str, decl, e, ctx):
        """Phase 5c: emit a call to a generic fn via monomorphization."""
        if not e.type_args:
            raise AOTError("E600",
                           f"generic fn '{name}' has no type arguments "
                           f"(checker should have inferred them)",
                           e.line, e.col)
        arg_strs = self._resolve_targs(e.type_args)
        if len(arg_strs) != len(decl.type_params):
            raise AOTError("E600",
                           f"generic fn '{name}' takes "
                           f"{len(decl.type_params)} type args, got "
                           f"{len(arg_strs)}", e.line, e.col)
        fn = self._get_mono_fn(name, decl, arg_strs, e)
        args = []
        for a in e.args:
            v = self.emit_expr(a, ctx)
            self.check(ctx, e)
            args.append(v)
        v = ctx.builder.call(fn, args, name=f"_mono_{name}")
        self.check(ctx, e)
        return v

    def emit_main_fn(self):
        fty = ir.FunctionType(PTR, [])
        fn = ir.Function(self.mod, fty, name="misa_main")
        ctx = self.new_fn_ctx(fn)
        # Phase 5a: cell-allocate top-level `var`s captured by lambdas.
        top_stmts = [item for item in self.prog.items
                     if not isinstance(item, (A.AppDecl, A.ProviderDecl,
                                             A.ModelsDecl, A.TypeDecl,
                                             A.ModelDecl, A.ScreenDecl,
                                             A.EvalDecl, A.FnDecl,
                                             A.ImportDecl))]
        ctx.cell_vars = self._cell_vars_for(top_stmts)
        self.get_nil(ctx)
        scope = ctx.scope
        for item in self.prog.items:
            if isinstance(item, (A.AppDecl, A.ProviderDecl, A.ModelsDecl,
                                 A.TypeDecl, A.ModelDecl, A.ScreenDecl,
                                 A.EvalDecl, A.FnDecl, A.ImportDecl)):
                continue
            self.emit_stmt(item, ctx)
        self.finish_fn_ctx(ctx)

    def emit_entry_main(self):
        # Phase 4 (M3): the web target boots via misa_ui_boot (exported
        # to JS); hosted targets keep int main().
        # Phase 5 (M3): iOS ("misa_main" entry) emits no wrapper at all —
        # rt/main.m defines main() -> UIApplicationMain, and the app
        # delegate calls misa_rt_init() + misa_main() itself.
        if self.entry == "misa_ui_boot":
            self.emit_entry_boot()
            return
        if self.entry == "misa_main":
            return
        fty = ir.FunctionType(I32, [])
        fn = ir.Function(self.mod, fty, name="main")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.call(self.rt["misa_rt_init"], [])
        main = self.mod.get_global("misa_main")
        r = b.call(main, [], name="r")
        pe = b.call(self.rt["misa_pending"], [], name="pe")
        bad = b.icmp_unsigned("!=", pe, NULL_PTR, name="bad")
        fail = fn.append_basic_block("fail")
        ok = fn.append_basic_block("ok")
        b.cbranch(bad, fail, ok)
        b.position_at_end(fail)
        b.call(self.rt["misa_report_uncaught"], [pe])
        b.ret(self.const_i32(1))
        b.position_at_end(ok)
        b.ret(self.const_i32(0))

    def emit_entry_boot(self):
        """Web entry (Phase 4, M3): exported `misa_ui_boot`.

        Calls misa_rt_init() + misa_main() with the linked UI driver
        active; the first misa_ui_show_screen paints the initial
        screen. Uncaught top-level errors go to the driver
        (overlay/banner) — the page is never killed.
        """
        fty = ir.FunctionType(ir.VoidType(), [])
        fn = ir.Function(self.mod, fty, name="misa_ui_boot")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.call(self.rt["misa_rt_init"], [])
        main = self.mod.get_global("misa_main")
        b.call(main, [], name="r")
        pe = b.call(self.rt["misa_pending"], [], name="pe")
        bad = b.icmp_unsigned("!=", pe, NULL_PTR, name="bad")
        fail = fn.append_basic_block("fail")
        ok = fn.append_basic_block("ok")
        b.cbranch(bad, fail, ok)
        b.position_at_end(fail)
        ui = b.call(self.rt["misa_ui_active"], [], name="ui")
        err = b.call(self.rt["misa_take_pending"], [], name="err")
        b.call(self.rt["misa_ui_report_error"], [ui, err])
        b.branch(ok)
        b.position_at_end(ok)
        b.ret_void()

    # -- blocks & statements ----------------------------------------------

    def emit_block(self, stmts, ctx: FnCtx):
        for s in stmts:
            self.emit_stmt(s, ctx)
            # stop emitting into terminated blocks
            if ctx.builder.block.is_terminated:
                # create a fresh unreachable block so later code has
                # somewhere to go (it will be pruned)
                nb = ctx.fn.append_basic_block("dead")
                ctx.builder.position_at_end(nb)

    def emit_stmt(self, s, ctx: FnCtx):
        b = ctx.builder
        if isinstance(s, A.LetStmt):
            # annotated empty map: {} parses as a record literal; the
            # interpreter coerces it to a Map — mirror that here.
            val_node = s.value
            if (s.ann is not None and s.ann.is_map
                    and isinstance(val_node, A.RecordLit)):
                val_node = A.MapLit(
                    entries=[(k, v) for k, v in val_node.fields],
                    line=val_node.line, col=val_node.col)
            v = self.emit_expr(val_node, ctx)
            self.check(ctx, s)
            a = self.alloca_var(ctx, s.name)
            if s.name in ctx.cell_vars:
                # Phase 5a: heap cell (1-elem list) shared with closures.
                cell = self.call(ctx, "misa_list_new", [], s, check=False)
                self.call(ctx, "misa_list_push", [cell, v], s, check=False)
                b.store(cell, a)
            else:
                b.store(v, a)
            ctx.scope.declare(s.name, a)
        elif isinstance(s, A.AssignStmt):
            v = self.emit_expr(s.value, ctx)
            self.check(ctx, s)
            tgt = s.target
            if isinstance(tgt, A.Ident):
                a = ctx.scope.lookup(tgt.name)
                if tgt.name in ctx.cell_vars:
                    # Phase 5a: store through the shared cell.
                    cell = b.load(a, name=f"cell_{tgt.name}")
                    line, col = self.loc(s)
                    zero = self.call(ctx, "misa_int",
                                     [self.const_i64(0)], s, check=False)
                    self.call(ctx, "misa_index_set",
                              [cell, zero, v, line, col], s)
                else:
                    b.store(v, a)
            elif isinstance(tgt, A.Field):
                obj = self.emit_expr(tgt.obj, ctx)
                self.check(ctx, s)
                line, col = self.loc(s)
                self.call(ctx, "misa_record_set",
                          [obj, self.cstr(tgt.field), v, line, col], s)
            elif isinstance(tgt, A.Index):
                obj = self.emit_expr(tgt.obj, ctx)
                self.check(ctx, s)
                idx = self.emit_expr(tgt.idx, ctx)
                self.check(ctx, s)
                line, col = self.loc(s)
                self.call(ctx, "misa_index_set",
                          [obj, idx, v, line, col], s)
            else:
                raise AOTError("E201", "invalid assignment target",
                               s.line, s.col)
        elif isinstance(s, A.IfStmt):
            self.emit_if(s, ctx)
        elif isinstance(s, A.WhileStmt):
            self.emit_while(s, ctx)
        elif isinstance(s, A.ForStmt):
            self.emit_for(s, ctx)
        elif isinstance(s, A.ReturnStmt):
            if s.value is not None:
                v = self.emit_expr(s.value, ctx)
                self.check(ctx, s)
            else:
                v = self.get_nil(ctx)
            b.ret(v)
        elif isinstance(s, A.TryStmt):
            self.emit_try(s, ctx)
        elif isinstance(s, A.ThrowStmt):
            v = self.emit_expr(s.value, ctx)
            self.check(ctx, s)
            line, col = self.loc(s)
            err = self.call(ctx, "misa_make_thrown", [v, line, col], s)
            b.call(self.rt["misa_do_throw"], [err, line, col])
            self.check(ctx, s)
        elif isinstance(s, A.ExprStmt):
            self.emit_expr(s.expr, ctx)
            self.check(ctx, s)
        else:
            raise AOTError("E600",
                           f"unsupported statement {type(s).__name__} "
                           f"in AOT v0.3",
                           s.line, s.col)

    def emit_if(self, s: A.IfStmt, ctx: FnCtx):
        b = ctx.builder
        c = self.emit_expr(s.cond, ctx)
        self.check(ctx, s)
        t = self.call(ctx, "misa_truthy", [c], s, check=False)
        cond = b.icmp_unsigned("!=", t, self.const_i32(0), name="c")
        then_b = ctx.fn.append_basic_block("if.then")
        else_b = ctx.fn.append_basic_block("if.else")
        end_b = ctx.fn.append_basic_block("if.end")
        b.cbranch(cond, then_b, else_b)
        b.position_at_end(then_b)
        self.emit_block(s.then, ctx)
        if not b.block.is_terminated:
            b.branch(end_b)
        b.position_at_end(else_b)
        self.emit_block(s.els, ctx)
        if not b.block.is_terminated:
            b.branch(end_b)
        b.position_at_end(end_b)

    def emit_while(self, s: A.WhileStmt, ctx: FnCtx):
        b = ctx.builder
        cond_b = ctx.fn.append_basic_block("while.cond")
        body_b = ctx.fn.append_basic_block("while.body")
        end_b = ctx.fn.append_basic_block("while.end")
        b.branch(cond_b)
        b.position_at_end(cond_b)
        c = self.emit_expr(s.cond, ctx)
        self.check(ctx, s)
        t = self.call(ctx, "misa_truthy", [c], s, check=False)
        ok = b.icmp_unsigned("!=", t, self.const_i32(0), name="c")
        b.cbranch(ok, body_b, end_b)
        b.position_at_end(body_b)
        self.emit_block(s.body, ctx)
        if not b.block.is_terminated:
            b.branch(cond_b)
        b.position_at_end(end_b)

    def emit_for(self, s: A.ForStmt, ctx: FnCtx):
        b = ctx.builder
        v = self.emit_expr(s.iter, ctx)
        self.check(ctx, s)
        line, col = self.loc(s)
        it = self.call(ctx, "misa_iter_new", [v, line, col], s)
        loop_b = ctx.fn.append_basic_block("for.loop")
        body_b = ctx.fn.append_basic_block("for.body")
        end_b = ctx.fn.append_basic_block("for.end")
        b.branch(loop_b)
        b.position_at_end(loop_b)
        # no check: NULL is the exhaustion sentinel, not an error
        x = b.call(self.rt["misa_iter_next"], [it], name="itn")
        done = b.icmp_unsigned("==", x, NULL_PTR, name="done")
        b.cbranch(done, end_b, body_b)
        b.position_at_end(body_b)
        var = self.alloca_var(ctx, s.var)
        b.store(x, var)
        child = ctx.scope.child()
        old = ctx.scope
        ctx.scope = child
        child.declare(s.var, var)
        self.emit_block(s.body, ctx)
        ctx.scope = old
        if not b.block.is_terminated:
            b.branch(loop_b)
        b.position_at_end(end_b)

    def emit_try(self, s: A.TryStmt, ctx: FnCtx):
        b = ctx.builder
        catch_b = ctx.fn.append_basic_block("try.catch")
        end_b = ctx.fn.append_basic_block("try.end")
        ctx.err_stack.append(catch_b)
        self.emit_block(s.body, ctx)
        ctx.err_stack.pop()
        if not b.block.is_terminated:
            b.branch(end_b)
        b.position_at_end(catch_b)
        e = self.call(ctx, "misa_take_pending", [], s, check=False)
        rec = self.call(ctx, "misa_catch_record", [e], s, check=False)
        var = self.alloca_var(ctx, s.catch_var)
        b.store(rec, var)
        child = ctx.scope.child()
        old = ctx.scope
        ctx.scope = child
        child.declare(s.catch_var, var)
        self.emit_block(s.handler, ctx)
        ctx.scope = old
        if not b.block.is_terminated:
            b.branch(end_b)
        b.position_at_end(end_b)

    # -- expressions ------------------------------------------------------

    def emit_expr(self, e, ctx: FnCtx):
        b = ctx.builder
        if isinstance(e, A.IntLit):
            return self.call(ctx, "misa_int",
                             [self.const_i64(e.value)], e, check=False)
        if isinstance(e, A.FloatLit):
            return self.call(ctx, "misa_float",
                             [ir.Constant(DBL, e.value)], e, check=False)
        if isinstance(e, A.BoolLit):
            return self.call(ctx, "misa_bool",
                             [self.const_i32(1 if e.value else 0)],
                             e, check=False)
        if isinstance(e, A.NilLit):
            return self.get_nil(ctx)
        if isinstance(e, A.DurationLit):
            return self.call(ctx, "misa_int",
                             [self.const_i64(e.ms)], e, check=False)
        if isinstance(e, A.MoneyLit):
            return self.call(ctx, "misa_float",
                             [ir.Constant(DBL, e.usd)], e, check=False)
        if isinstance(e, A.StrLit):
            return self.emit_str(e, ctx)
        if isinstance(e, A.Ident):
            # Phase 4 (M4): handler scope. A bare component id is not
            # a value (checker rejects with E201 first); anything that
            # is neither a lambda param nor an id: binding is a
            # closure over locals, which handlers do not support.
            if ctx.in_handler:
                if ctx.id_slots and e.name in ctx.id_slots:
                    raise AOTError(
                        "E201",
                        f"component id '{e.name}' cannot be used as a "
                        f"value (use '{e.name}.text' to read a TextInput)",
                        e.line, e.col)
                try:
                    a = ctx.scope.lookup(e.name)
                except AOTError:
                    raise AOTError(
                        "E600",
                        f"handler cannot capture '{e.name}': event "
                        f"handlers see only their params and screen "
                        f"id: bindings",
                        e.line, e.col)
                return b.load(a, name=f"l_{e.name}")
            try:
                a = ctx.scope.lookup(e.name)
            except AOTError:
                # Phase 5a: an `fn` declaration used as a value.
                if e.name in self.fns:
                    return self.emit_fn_value(e.name, e, ctx)
                raise
            v = b.load(a, name=f"l_{e.name}")
            if e.name in ctx.cell_vars:
                # Phase 5a: read through the shared heap cell.
                v = self.call(ctx, "misa_list_get",
                              [v, self.const_i64(0)], e, check=False)
            return v
        if isinstance(e, A.Call):
            return self.emit_call(e, ctx)
        if isinstance(e, A.Field):
            # Phase 5b: bare zero-payload variant, e.g. `Opt.None`.
            if isinstance(e.obj, A.Ident):
                decl = self.type_decls.get(e.obj.name)
                if decl is not None and decl.is_sum:
                    return self.emit_bare_variant(decl, e, ctx)
            # Phase 4 (M4): `<id>.text` readback inside handlers goes
            # to the active UI driver. (The checker guarantees `.text`
            # on a TextInput here.)
            if ctx.in_handler and isinstance(e.obj, A.Ident) \
                    and ctx.id_slots and e.obj.name in ctx.id_slots:
                ui = self.call(ctx, "misa_ui_active", [], e, check=False)
                w = self.call(ctx, "misa_ui_find_bound",
                              [ui, self.const_i32(
                                  ctx.id_slots[e.obj.name])],
                              e, check=False)
                return self.call(ctx, "misa_ui_get_text", [ui, w], e)
            obj = self.emit_expr(e.obj, ctx)
            self.check(ctx, e)
            line, col = self.loc(e)
            return self.call(ctx, "misa_record_get",
                             [obj, self.cstr(e.field), line, col], e)
        if isinstance(e, A.Index):
            obj = self.emit_expr(e.obj, ctx)
            self.check(ctx, e)
            idx = self.emit_expr(e.idx, ctx)
            self.check(ctx, e)
            line, col = self.loc(e)
            return self.call(ctx, "misa_index_get",
                             [obj, idx, line, col], e)
        if isinstance(e, A.ListLit):
            l = self.call(ctx, "misa_list_new", [], e, check=False)
            for it in e.items:
                v = self.emit_expr(it, ctx)
                self.check(ctx, e)
                self.call(ctx, "misa_list_push", [l, v], e, check=False)
            return l
        if isinstance(e, A.RecordLit):
            r = self.call(ctx, "misa_record_new", [], e, check=False)
            for fname, fv in e.fields:
                v = self.emit_expr(fv, ctx)
                self.check(ctx, e)
                self.call(ctx, "misa_record_set_cstr",
                          [r, self.cstr(fname), v], e, check=False)
            return r
        if isinstance(e, A.MapLit):
            m = self.call(ctx, "misa_map_new", [], e, check=False)
            line, col = self.loc(e)
            for k, ve in e.entries:
                if isinstance(k, bool):
                    kv = self.call(ctx, "misa_bool",
                                   [self.const_i32(1 if k else 0)],
                                   e, check=False)
                elif isinstance(k, int):
                    kv = self.call(ctx, "misa_int",
                                   [self.const_i64(k)], e, check=False)
                else:
                    kv = self.call(ctx, "misa_text_cstr",
                                   [self.cstr(str(k))], e, check=False)
                v = self.emit_expr(ve, ctx)
                self.check(ctx, e)
                self.call(ctx, "misa_index_set",
                          [m, kv, v, line, col], e)
            return m
        if isinstance(e, A.BinOp):
            return self.emit_binop(e, ctx)
        if isinstance(e, A.Lambda):
            # Phase 5a: first-class lambdas become closure values
            # (record {__closure_fn, __closure_env}).
            return self.emit_closure(e, ctx)
        if isinstance(e, A.Match):
            # Phase 5b: `match` expression over a sum value.
            return self.emit_match(e, ctx)
        if isinstance(e, A.UnOp):
            v = self.emit_expr(e.operand, ctx)
            self.check(ctx, e)
            if e.op == "not":
                return self.call(ctx, "misa_not", [v], e, check=False)
            if e.op == "-":
                line, col = self.loc(e)
                return self.call(ctx, "misa_neg", [v, line, col], e)
            raise AOTError("E600", f"unknown unary '{e.op}'",
                           e.line, e.col)
        raise AOTError("E600",
                       f"unsupported expression {type(e).__name__} "
                       f"in AOT v0.3",
                       e.line, e.col)

    def emit_str(self, e: A.StrLit, ctx: FnCtx):
        acc = self.call(ctx, "misa_text_cstr", [self.cstr("")],
                        e, check=False)
        for part in e.parts:
            if isinstance(part, str):
                pv = self.call(ctx, "misa_text_cstr",
                               [self.cstr(part)], e, check=False)
            else:
                pv = self.emit_expr(part, ctx)
                self.check(ctx, e)
            acc = self.call(ctx, "misa_text_concat", [acc, pv],
                            e, check=False)
        return acc

    def emit_binop(self, e: A.BinOp, ctx: FnCtx):
        b = ctx.builder
        op = e.op
        if op == "and":
            return self.emit_shortcircuit(e, ctx, is_and=True)
        if op == "or":
            return self.emit_shortcircuit(e, ctx, is_and=False)
        l = self.emit_expr(e.left, ctx)
        self.check(ctx, e)
        r = self.emit_expr(e.right, ctx)
        self.check(ctx, e)
        line, col = self.loc(e)
        table = {
            "+": "misa_add", "-": "misa_sub", "*": "misa_mul",
            "/": "misa_div", "%": "misa_mod",
            "<": "misa_lt", "<=": "misa_le",
            ">": "misa_gt", ">=": "misa_ge",
        }
        if op in ("==", "!="):
            name = "misa_eq" if op == "==" else "misa_ne"
            return self.call(ctx, name, [l, r], e, check=False)
        if op in table:
            return self.call(ctx, table[op], [l, r, line, col], e)
        raise AOTError("E600", f"unknown operator '{op}'", e.line, e.col)

    def emit_shortcircuit(self, e: A.BinOp, ctx: FnCtx, is_and: bool):
        b = ctx.builder
        l = self.emit_expr(e.left, ctx)
        self.check(ctx, e)
        t = self.call(ctx, "misa_truthy", [l], e, check=False)
        c = b.icmp_unsigned("!=", t, self.const_i32(0), name="sc")
        rhs_b = ctx.fn.append_basic_block("sc.rhs")
        end_b = ctx.fn.append_basic_block("sc.end")
        # and: falsy -> short (false); truthy -> rhs
        # or:  truthy -> short (l);     falsy -> rhs
        short_b = ctx.fn.append_basic_block("sc.short")
        if is_and:
            b.cbranch(c, rhs_b, short_b)
        else:
            b.cbranch(c, short_b, rhs_b)
        b.position_at_end(short_b)
        short_v = (self.call(ctx, "misa_bool", [self.const_i32(0)],
                             e, check=False) if is_and else l)
        b.branch(end_b)
        b.position_at_end(rhs_b)
        r = self.emit_expr(e.right, ctx)
        self.check(ctx, e)
        rhs_end = b.block  # check() may have inserted a cont block
        b.branch(end_b)
        b.position_at_end(end_b)
        phi = b.phi(PTR, name="scv")
        phi.add_incoming(short_v, short_b)
        phi.add_incoming(r, rhs_end)
        return phi

    # -- calls ------------------------------------------------------

    _MEDIA_KIND = {"Photo": 0, "Audio": 1, "Video": 2, "Document": 3}

    def emit_call(self, e: A.Call, ctx: FnCtx):
        # Phase 5a: calling the result of an expression, e.g. `mk(9)(1)`.
        if e.callee is not None:
            clos = self.emit_expr(e.callee, ctx)
            self.check(ctx, e)
            return self.emit_closure_call(e, ctx, clos)
        name = e.name
        # Phase 5c: generic fns go through monomorphization.
        if name in self.fn_decls and self.fn_decls[name].type_params:
            return self.emit_mono_call(name, self.fn_decls[name], e,
                                       ctx)
        if name in self.fns:
            decl = self.fn_decls[name]
            if len(e.args) != len(decl.params):
                raise AOTError("E600",
                               f"fn '{name}' takes {len(decl.params)} "
                               f"args, got {len(e.args)}", e.line, e.col)
            args = []
            for a in e.args:
                v = self.emit_expr(a, ctx)
                self.check(ctx, e)
                args.append(v)
            return self.call(ctx, f"misa_fn_{name}", args, e,
                             check=True, direct=True)
        if name in self.models:
            return self.emit_model_call(name, e, ctx)
        if name in self.screens:
            decl = self.screen_decls[name]
            args = []
            for a in e.args:
                v = self.emit_expr(a, ctx)
                self.check(ctx, e)
                args.append(v)
            return self.call(ctx, f"misa_screen_{name}", args, e,
                             check=True, direct=True)
        # Phase 5a: call through a variable holding a closure value.
        # (Builtins aren't scope variables, so they still fall through.)
        if "." not in name:
            try:
                target = ctx.scope.lookup(name)
            except AOTError:
                target = None
            if target is not None:
                return self.emit_closure_call_var(e, ctx, name, target)
        # Phase 5c: generic record construction `Box<Int>(42)`.
        # Erasure: just build the record; type args are checking-time.
        if name in self.type_decls:
            tdecl = self.type_decls[name]
            if tdecl.type_params and not tdecl.is_sum:
                if len(e.args) != len(tdecl.fields):
                    raise AOTError("E600",
                                   f"'{name}' takes {len(tdecl.fields)} "
                                   f"field arg(s), got {len(e.args)}",
                                   e.line, e.col)
                r = self.call(ctx, "misa_record_new", [], e,
                              check=False)
                for (fname, _), a in zip(tdecl.fields, e.args):
                    v = self.emit_expr(a, ctx)
                    self.check(ctx, e)
                    self.call(ctx, "misa_record_set_cstr",
                              [r, self.cstr(fname), v], e, check=False)
                return r
        # Phase 5b: sum variant construction `Type.Variant(args)`.
        if "." in name:
            head, _, var = name.partition(".")
            decl = self.type_decls.get(head)
            if decl is not None and decl.is_sum:
                return self.emit_sum_construct(decl, var, e, ctx)
        return self.emit_builtin(e, ctx)

    def call(self, ctx: FnCtx, name, args, node, check=True,
             direct=False):
        if direct:
            target = self.mod.get_global(name)
            v = ctx.builder.call(target, args, name=f"_{name}")
        else:
            v = ctx.builder.call(self.rt[name], args, name=f"_{name}")
        if check:
            self.check(ctx, node)
        return v

    def emit_builtin(self, e: A.Call, ctx: FnCtx):
        name = e.name
        line, col = self.loc(e)

        def arg(i):
            v = self.emit_expr(e.args[i], ctx)
            self.check(ctx, e)
            return v

        if name == "print":
            return self.call(ctx, "misa_print", [arg(0)], e)
        if name == "len":
            return self.call(ctx, "misa_builtin_len", [arg(0), line, col],
                             e)
        if name == "str":
            return self.call(ctx, "misa_builtin_str", [arg(0)], e,
                             check=False)
        if name == "int":
            return self.call(ctx, "misa_builtin_int",
                             [arg(0), line, col], e)
        if name == "float":
            return self.call(ctx, "misa_builtin_float",
                             [arg(0), line, col], e)
        if name == "assert":
            c = arg(0)
            m = arg(1) if len(e.args) > 1 else self.get_nil(ctx)
            return self.call(ctx, "misa_builtin_assert",
                             [c, m, line, col], e)
        if name == "env":
            v = arg(0)
            z = self.call(ctx, "misa_text_z", [v], e)
            return self.call(ctx, "misa_builtin_env", [z, line, col], e)
        if name == "fs.read":
            return self.call(ctx, "misa_fs_read", [arg(0), line, col], e)
        if name == "fs.write":
            return self.call(ctx, "misa_fs_write",
                             [arg(0), arg(1), line, col], e)
        if name == "http.get":
            return self.call(ctx, "misa_http_get", [arg(0), line, col],
                             e)
        if name in self._MEDIA_KIND:
            return self.call(ctx, "misa_builtin_media",
                             [self.const_i32(self._MEDIA_KIND[name]),
                              arg(0), line, col], e)
        if name == "navigate":
            tgt = e.args[0] if e.args else None
            if not (isinstance(tgt, A.Call) and
                    tgt.name in self.screens):
                raise AOTError(
                    "E600", "navigate needs a screen call, "
                    "e.g. navigate(Home())", e.line, e.col)
            args = []
            for a in tgt.args:
                v = self.emit_expr(a, ctx)
                self.check(ctx, e)
                args.append(v)
            return self.call(ctx, f"misa_screen_{tgt.name}", args, e,
                             check=True, direct=True)
        raise AOTError("E101", f"undefined function '{name}'",
                       e.line, e.col)

    # -- models ------------------------------------------------------

    def emit_model_decl(self, name: str, decl: A.ModelDecl):
        """Create the wrapper fn and the metadata global."""
        fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
        fn = ir.Function(self.mod, fty, name=f"misa_model_{name}")
        meta_text = self.model_meta_text(name, decl)
        g = self.cstr(meta_text)
        self.models[name] = (fn, g)

        # wrapper body (emitted inline here; no FnCtx err stack needed
        # beyond a simple epilogue)
        ctx = self.new_fn_ctx(fn)
        b = ctx.builder
        self.get_nil(ctx)
        nargs = len(decl.params)
        arr_ty = ir.ArrayType(PTR, max(nargs, 1))
        arr = b.alloca(arr_ty, name="margs")
        for i, arg in enumerate(fn.args):
            p = b.gep(arr, [self.const_i32(0), self.const_i32(i)],
                      name=f"mp{i}")
            b.store(arg, p)
        first = b.gep(arr, [self.const_i32(0), self.const_i32(0)],
                      name="margs0")
        line, col = self.loc(decl)
        r = b.call(self.rt["misa_model_call_meta"],
                   [g, first, self.const_i32(nargs), line, col],
                   name="_misa_model_call_meta")
        self.check(ctx, decl)
        b.ret(r)
        self.finish_fn_ctx(ctx)

    # -- Phase 5a: first-class functions (closures) -----------------------
    #
    # A closure value is an MVal* record:
    #   {__closure_fn: Int(fn address as i64), __closure_env: record}
    # Every closure body is emitted as `misa_lambda_N(env, args)` where
    # args is an MVal* list — one uniform signature, so calls through
    # values need no per-arity dispatch. `fn` decls used as values get
    # a trampoline with the same shape.

    def emit_closure(self, lam: A.Lambda, ctx: FnCtx):
        b = ctx.builder
        node = lam
        # 1. capture the scope chain: (name, is_cell), innermost first.
        names = []
        seen = set()
        s = ctx.scope
        while s is not None:
            for name in s.names:
                if name not in seen:
                    seen.add(name)
                    names.append((name, name in ctx.cell_vars))
            s = s.parent
        # 2. build the env record (cells shared, values copied).
        env = self.call(ctx, "misa_record_new", [], node, check=False)
        for name, _ in names:
            a = ctx.scope.lookup(name)
            v = b.load(a, name=f"cap_{name}")
            self.call(ctx, "misa_record_set_cstr",
                      [env, self.cstr(name), v], node, check=False)
        # 3. emit the body as misa_lambda_N(env, args).
        fn = ir.Function(self.mod, ir.FunctionType(PTR, [PTR, PTR]),
                         name=f"misa_lambda_{self.lambda_counter}")
        self.lambda_counter += 1
        lctx = self.new_fn_ctx(fn)
        lb = lctx.builder
        self.get_nil(lctx)
        for name, is_cell in names:
            if is_cell:
                lctx.cell_vars.add(name)
        lenv, largs = fn.args[0], fn.args[1]
        lenv.name, largs.name = "env", "args"
        line, col = self.loc(node)
        for name, _ in names:
            v = self.call(lctx, "misa_record_get",
                          [lenv, self.cstr(name), line, col],
                          node, check=False)
            a = self.alloca_var(lctx, f"cap_{name}")
            lb.store(v, a)
            lctx.scope.declare(name, a)
        for i, (pn, _) in enumerate(lam.params):
            v = self.call(lctx, "misa_list_get",
                          [largs, self.const_i64(i)], node, check=False)
            a = self.alloca_var(lctx, pn)
            lb.store(v, a)
            lctx.scope.declare(pn, a)
        # nested lambdas inside this body get their own cell analysis
        if lam.expr_body is None:
            lctx.cell_vars |= self._cell_vars_for(lam.body)
        if lam.expr_body is not None:
            v = self.emit_expr(lam.expr_body, lctx)
            self.check(lctx, lam)
            lb.ret(v)
        else:
            self.emit_block(lam.body, lctx)
        self.finish_fn_ctx(lctx)
        # 4. build the closure record {__closure_fn, __closure_env}.
        clos = self.call(ctx, "misa_record_new", [], node, check=False)
        fn_i64 = b.ptrtoint(fn, I64, name="lambdafn")
        fn_val = self.call(ctx, "misa_int", [fn_i64], node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_FN), fn_val],
                  node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_ENV), env],
                  node, check=False)
        disp = self.call(ctx, "misa_text_cstr",
                         [self.cstr("<lambda>")], node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_DISP), disp],
                  node, check=False)
        return clos

    def emit_closure_call(self, e: A.Call, ctx: FnCtx, clos):
        """Call a closure MVal* value."""
        b = ctx.builder
        line, col = self.loc(e)
        fn_val = self.call(ctx, "misa_record_get",
                            [clos, self.cstr(self.CLOSURE_FN),
                             line, col], e)
        fn_i64 = self.call(ctx, "misa_as_int", [fn_val], e)
        env = self.call(ctx, "misa_record_get",
                         [clos, self.cstr(self.CLOSURE_ENV), line, col], e)
        args_list = self.call(ctx, "misa_list_new", [], e, check=False)
        for a in e.args:
            v = self.emit_expr(a, ctx)
            self.check(ctx, e)
            self.call(ctx, "misa_list_push", [args_list, v], e,
                      check=False)
        fnty = ir.FunctionType(PTR, [PTR, PTR])
        fnptr = b.inttoptr(fn_i64, fnty.as_pointer(), name="closure_fn")
        r = b.call(fnptr, [env, args_list], name="_closure_call")
        self.check(ctx, e)
        return r

    def emit_closure_call_var(self, e: A.Call, ctx: FnCtx, name: str,
                              target):
        """Call a variable holding a closure value."""
        b = ctx.builder
        clos = b.load(target, name=f"l_{name}")
        if name in ctx.cell_vars:
            # `var f = <closure>` — load through the cell.
            clos = self.call(ctx, "misa_list_get",
                              [clos, self.const_i64(0)], e, check=False)
        return self.emit_closure_call(e, ctx, clos)

    def emit_fn_trampoline(self, name: str) -> ir.Function:
        """Uniform `(env, args)` wrapper around an `fn` declaration."""
        if name in self.trampolines:
            return self.trampolines[name]
        decl = self.fn_decls[name]
        fn = ir.Function(self.mod, ir.FunctionType(PTR, [PTR, PTR]),
                         name=f"misa_tramp_{name}")
        ctx = self.new_fn_ctx(fn)
        b = ctx.builder
        self.get_nil(ctx)
        fn.args[0].name, fn.args[1].name = "env", "args"
        call_args = []
        for i in range(len(decl.params)):
            v = self.call(ctx, "misa_list_get",
                          [fn.args[1], self.const_i64(i)],
                          decl, check=False)
            call_args.append(v)
        r = self.call(ctx, f"misa_fn_{name}", call_args, decl,
                      check=True, direct=True)
        b.ret(r)
        self.finish_fn_ctx(ctx)
        self.trampolines[name] = fn
        return fn

    def emit_fn_value(self, name: str, node: A.Node, ctx: FnCtx):
        """A closure value wrapping an `fn` declaration."""
        # Phase 5c: generic fns as values need a specialization context;
        # v1 AOT rejects them (the interpreter supports `let g = first`).
        decl = self.fn_decls.get(name)
        if decl is not None and decl.type_params:
            raise AOTError("E600",
                           f"generic fn '{name}' used as a value is not "
                           f"supported in AOT v1 (call it directly)",
                           node.line, node.col)
        tramp = self.emit_fn_trampoline(name)
        b = ctx.builder
        clos = self.call(ctx, "misa_record_new", [], node, check=False)
        fn_i64 = b.ptrtoint(tramp, I64, name="trampfn")
        fn_val = self.call(ctx, "misa_int", [fn_i64], node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_FN), fn_val],
                  node, check=False)
        env = self.call(ctx, "misa_record_new", [], node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_ENV), env],
                  node, check=False)
        disp = self.call(ctx, "misa_text_cstr",
                         [self.cstr(f"<function {name}>")],
                         node, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [clos, self.cstr(self.CLOSURE_DISP), disp],
                  node, check=False)
        return clos

    # -- Phase 5b: sum types -------------------------------------------
    #
    # A sum value is an MVal* record:
    #   {__tag: Int(variant index), __fields: [MVal* payload]}.
    # `match` compiles to an LLVM switch on the tag.

    def _variant_index(self, decl, vname: str) -> int:
        for i, v in enumerate(decl.variants):
            if v.name == vname:
                return i
        raise AOTError("E600", f"unknown variant '{vname}' of sum type "
                               f"'{decl.name}'", decl.line, decl.col)

    def emit_sum_construct(self, decl, vname: str, e: A.Call,
                           ctx: FnCtx):
        """Build the {__tag, __fields} record for `Type.Variant(args)`."""
        b = ctx.builder
        idx = self._variant_index(decl, vname)
        v = decl.variants[idx]
        if len(e.args) != len(v.fields):
            raise AOTError("E600",
                           f"variant '{decl.name}.{vname}' takes "
                           f"{len(v.fields)} args, got {len(e.args)}",
                           e.line, e.col)
        r = self.call(ctx, "misa_record_new", [], e, check=False)
        tag = self.call(ctx, "misa_int", [self.const_i64(idx)], e,
                        check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr(self.SUM_TAG), tag], e, check=False)
        l = self.call(ctx, "misa_list_new", [], e, check=False)
        for a in e.args:
            av = self.emit_expr(a, ctx)
            self.check(ctx, e)
            self.call(ctx, "misa_list_push", [l, av], e, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr(self.SUM_FIELDS), l], e, check=False)
        # Phase 5b: display prefix for print/str parity with the
        # interpreter (`Type.Variant`); the C renderer appends payload.
        disp = self.call(ctx, "misa_text_cstr",
                         [self.cstr(f"{decl.name}.{vname}")],
                         e, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr(self.SUM_DISP), disp], e, check=False)
        return r

    def emit_bare_variant(self, decl, e: A.Field, ctx: FnCtx):
        """Build the {__tag, __fields} record for a bare `Type.Variant`."""
        # zero-field: reuse the construct path with no args.
        fake = A.Call(name=f"{decl.name}.{e.field}", args=[],
                      line=e.line, col=e.col)
        return self.emit_sum_construct(decl, e.field, fake, ctx)

    def emit_match(self, e: A.Match, ctx: FnCtx):
        b = ctx.builder
        line, col = self.loc(e)
        sv = self.emit_expr(e.scrutinee, ctx)
        self.check(ctx, e)
        tag_val = self.call(ctx, "misa_record_get",
                            [sv, self.cstr(self.SUM_TAG), line, col], e)
        tag = self.call(ctx, "misa_as_int", [tag_val], e, check=False)
        fields = self.call(ctx, "misa_record_get",
                           [sv, self.cstr(self.SUM_FIELDS), line, col],
                           e)
        # variant name -> index, from the first non-wildcard arm's type
        # (the checker guarantees every arm targets the scrutinee type).
        vmap = {}
        for arm in e.arms:
            if not arm.wildcard:
                decl = self.type_decls[arm.type_name]
                vmap = {v.name: i for i, v in
                        enumerate(decl.variants)}
                break
        arm_blocks = [ctx.fn.append_basic_block(f"match.arm{i}")
                      for i in range(len(e.arms))]
        end_b = ctx.fn.append_basic_block("match.end")
        wild_idx = next((i for i, a in enumerate(e.arms)
                         if a.wildcard), None)
        if wild_idx is not None:
            default_b = arm_blocks[wild_idx]
        else:
            # Unreachable: the checker enforces exhaustiveness.
            default_b = ctx.fn.append_basic_block("match.unreachable")
        sw = b.switch(tag, default_b)
        seen_idx = set()
        for i, arm in enumerate(e.arms):
            if arm.wildcard:
                continue
            idx = vmap[arm.variant]
            if idx not in seen_idx:
                seen_idx.add(idx)
                sw.add_case(self.const_i64(idx), arm_blocks[i])
        if wild_idx is None:
            b.position_at_end(default_b)
            b.unreachable()
        incomings = []
        for i, arm in enumerate(e.arms):
            b.position_at_end(arm_blocks[i])
            old_scope = ctx.scope
            if not arm.wildcard:
                child = ctx.scope.child()
                ctx.scope = child
                for j, bname in enumerate(arm.binds):
                    fv = self.call(ctx, "misa_list_get",
                                   [fields, self.const_i64(j)],
                                   arm, check=False)
                    a = self.alloca_var(ctx, bname)
                    b.store(fv, a)
                    child.declare(bname, a)
            rv = self.emit_expr(arm.body, ctx)
            self.check(ctx, arm)
            if not b.block.is_terminated:
                cur = b.block
                b.branch(end_b)
                incomings.append((rv, cur))
            ctx.scope = old_scope
        b.position_at_end(end_b)
        phi = b.phi(PTR, name="matchv")
        for rv, blk in incomings:
            phi.add_incoming(rv, blk)
        return phi

    def emit_model_call(self, name: str, e: A.Call, ctx: FnCtx):
        fn, _g = self.models[name]
        decl = self.model_decls[name]
        if len(e.args) != len(decl.params):
            raise AOTError("E600",
                           f"model '{name}' takes {len(decl.params)} "
                           f"args, got {len(e.args)}", e.line, e.col)
        args = []
        for a in e.args:
            v = self.emit_expr(a, ctx)
            self.check(ctx, e)
            args.append(v)
        return self.call(ctx, f"misa_model_{name}", args, e,
                         check=True, direct=True)

    @staticmethod
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("\t", "\\t") \
                .replace("\n", "\\n")

    def model_meta_text(self, name: str, decl: A.ModelDecl) -> str:
        if decl.alias not in self.aliases:
            raise AOTError("E600",
                           f"unknown model alias '{decl.alias}'",
                           decl.line, decl.col)
        prov_name, model_id = self.aliases[decl.alias]
        prov = self.providers.get(prov_name)
        if prov is None:
            raise AOTError("E600",
                           f"unknown provider '{prov_name}'",
                           decl.line, decl.col)
        kind = prov_name  # provider names double as kinds
        if prov.model_file:
            kind = "ondevice"
        if kind not in ("openai", "gemini", "mock", "ondevice"):
            raise AOTError("E600",
                           f"provider kind '{kind}' not supported "
                           f"in AOT v0.3",
                           decl.line, decl.col)
        base_env = {"openai": "MISA_OPENAI_BASE_URL",
                    "gemini": "MISA_GEMINI_BASE_URL"}.get(kind, "")
        b = decl.budget
        lines = [
            "MISA-MODEL v1",
            f"alias: {decl.alias}",
            f"kind: {kind}",
            f"model: {model_id}",
            f"key_env: {prov.key_env}",
            f"base_env: {base_env}",
            f"tokens: {b.tokens if b and b.tokens is not None else -1}",
            f"latency_ms: {b.latency_ms if b and b.latency_ms is not None else -1}",
            f"cost_usd: {b.cost_usd if b and b.cost_usd is not None else -1}",
            f"schema: {self.schema_of(decl.ret)}",
        ]
        if kind == "ondevice":
            lines.append(f"model_file: {prov.model_file}")
        for pn, pte in decl.params:
            pkind = "Photo" if pte.name == "Photo" else ""
            lines.append(f"param: {pn}\t{pkind}\t{self.schema_of(pte)}")
        for part in decl.prompt:
            if isinstance(part, str):
                lines.append(f"prompt: L\t{self._esc(part)}")
            else:
                # interpolation: find the param index
                idx = -1
                if isinstance(part, A.Ident):
                    for i, (pn, _pte) in enumerate(decl.params):
                        if pn == part.name:
                            idx = i
                            break
                if idx < 0:
                    # general expression: not supported in prompts for v0.3
                    raise AOTError(
                        "E600",
                        "model prompt interpolation supports parameter "
                        "names only in AOT v0.3",
                        part.line, part.col)
                lines.append(f"prompt: V\t{idx}")
        return "\n".join(lines) + "\n"

    def schema_of(self, te: A.TypeExpr) -> str:
        if te.is_map:
            return "Map"
        if te.is_list:
            inner = A.TypeExpr(name=te.name, optional=te.optional,
                               line=te.line, col=te.col)
            s = "[" + self.schema_of(inner) + "]"
        elif te.name in ("Text", "Int", "Float", "Bool"):
            s = te.name
        elif te.name in self.type_decls:
            decl = self.type_decls[te.name]
            parts = []
            for fn, fte in decl.fields:
                parts.append(f"{fn}:{self.schema_of(fte)}")
            s = "{" + ",".join(parts) + "}"
        else:
            # media types etc: runtime rejects with E400, like interp
            s = te.name
        if te.optional:
            s += "?"
        return s

    # -- screens (Phase 4, M4: compiled handlers + id: registry) --------

    def emit_screen_body(self, name: str, decl: A.ScreenDecl):
        fn = self.screens[name]
        ctx = self.new_fn_ctx(fn)
        b = ctx.builder
        self.get_nil(ctx)
        for (pn, _), arg in zip(decl.params, fn.args):
            a = self.alloca_var(ctx, pn)
            b.store(arg, a)
            ctx.scope.declare(pn, a)
        # id: name -> slot (recorded by the checker, M4)
        id_slots = {n: i for i, (n, _k) in enumerate(decl.id_bindings)}
        self.call(ctx, "misa_screen_begin", [self.cstr(name)], decl,
                  check=False)
        for item in decl.body:
            self.emit_screen_item(item, ctx, name, id_slots)
        self.call(ctx, "misa_screen_end", [], decl, check=False)
        b.ret(self.get_nil(ctx))
        self.finish_fn_ctx(ctx)

    def _screen_items_have_id(self, items) -> bool:
        """True if any Component under items has an id: arg (recursive)."""
        for it in items:
            if isinstance(it, A.Component):
                if any(an == "id" and isinstance(av, A.Ident)
                       for an, av in it.args):
                    return True
                if self._screen_items_have_id(it.children):
                    return True
            elif isinstance(it, A.ScreenIf):
                if self._screen_items_have_id(it.then) \
                        or self._screen_items_have_id(it.els):
                    return True
            elif isinstance(it, A.ScreenFor):
                if self._screen_items_have_id(it.body):
                    return True
        return False

    def emit_screen_item(self, node, ctx: FnCtx, screen_name: str,
                         id_slots: dict):
        b = ctx.builder
        if isinstance(node, A.ScreenIf):
            c = self.emit_expr(node.cond, ctx)
            self.check(ctx, node)
            t = self.call(ctx, "misa_truthy", [c], node, check=False)
            cond = b.icmp_unsigned("!=", t, self.const_i32(0))
            then_b = ctx.fn.append_basic_block("sc.if.then")
            else_b = ctx.fn.append_basic_block("sc.if.else")
            end_b = ctx.fn.append_basic_block("sc.if.end")
            b.cbranch(cond, then_b, else_b)
            b.position_at_end(then_b)
            for x in node.then:
                self.emit_screen_item(x, ctx, screen_name, id_slots)
            if not b.block.is_terminated:
                b.branch(end_b)
            b.position_at_end(else_b)
            for x in node.els:
                self.emit_screen_item(x, ctx, screen_name, id_slots)
            if not b.block.is_terminated:
                b.branch(end_b)
            b.position_at_end(end_b)
            return
        if isinstance(node, A.ScreenFor):
            # M4: ids inside ScreenFor have no static slot (dynamic
            # replication needs a keyed registry — later work).
            if self._screen_items_have_id(node.body):
                raise AOTError(
                    "E600",
                    "id: inside ScreenFor is not supported in AOT "
                    "(dynamic replication needs a keyed registry)",
                    node.line, node.col)
            v = self.emit_expr(node.iter, ctx)
            self.check(ctx, node)
            line, col = self.loc(node)
            it = self.call(ctx, "misa_iter_new", [v, line, col], node)
            loop_b = ctx.fn.append_basic_block("sc.for.loop")
            body_b = ctx.fn.append_basic_block("sc.for.body")
            end_b = ctx.fn.append_basic_block("sc.for.end")
            b.branch(loop_b)
            b.position_at_end(loop_b)
            x = b.call(self.rt["misa_iter_next"], [it], name="itn")
            done = b.icmp_unsigned("==", x, NULL_PTR, name="done")
            b.cbranch(done, end_b, body_b)
            b.position_at_end(body_b)
            var = self.alloca_var(ctx, node.var)
            b.store(x, var)
            child = ctx.scope.child()
            old = ctx.scope
            ctx.scope = child
            child.declare(node.var, var)
            for y in node.body:
                self.emit_screen_item(y, ctx, screen_name, id_slots)
            ctx.scope = old
            if not b.block.is_terminated:
                b.branch(loop_b)
            b.position_at_end(end_b)
            return
        self.emit_component(node, ctx, screen_name, id_slots)

    def emit_component(self, comp: A.Component, ctx: FnCtx,
                       screen_name: str, id_slots: dict):
        # filter `id` args (not rendered, interpreter parity)
        args = [(an, av) for an, av in comp.args
                if not (an == "id" and isinstance(av, A.Ident))]
        has_args = 1 if args else 0
        self.call(ctx, "misa_screen_comp_begin",
                  [self.cstr(comp.name), self.const_i32(has_args)],
                  comp, check=False)
        # M4: bind the id: slot to this widget (no visible output; the
        # driver records it for handler readback).
        for an, av in comp.args:
            if an == "id" and isinstance(av, A.Ident):
                slot = id_slots.get(av.name)
                # None only inside ScreenFor bodies, rejected above.
                if slot is not None:
                    self.call(ctx, "misa_screen_comp_bind_id",
                              [self.const_i32(slot)], comp, check=False)
        for i, (an, av) in enumerate(args):
            first = self.const_i32(1 if i == 0 else 0)
            if isinstance(av, A.Lambda):
                hid = self.emit_handler(screen_name, av, id_slots)
                self.call(ctx, "misa_screen_comp_arg_handler",
                          [self.cstr(an), first, self.const_i32(hid)],
                          comp, check=False)
            else:
                v = self.emit_expr(av, ctx)
                self.check(ctx, comp)
                self.call(ctx, "misa_screen_comp_arg",
                          [self.cstr(an), v, first], comp, check=False)
        self.call(ctx, "misa_screen_comp_head_end",
                  [self.const_i32(has_args)], comp, check=False)
        for child in comp.children:
            self.emit_screen_item(child, ctx, screen_name, id_slots)
        self.call(ctx, "misa_screen_comp_end", [], comp, check=False)

    def emit_handler(self, screen_name: str, lam: A.Lambda,
                     id_slots: dict) -> int:
        """Compile an event-handler lambda to a real function.

        Returns the handler id (index into misa_handler_table[]).
        Emitted as misa_handler_<screen>_<n>(MVal *event_arg) -> MVal*
        with the standard FnCtx (check() after every fallible call,
        error epilogue). Scope is the lambda params (bound to the
        event arg) plus the screen's id: bindings; anything else is a
        closure over locals and is rejected (E600).
        """
        hid = len(self.handlers)
        fn = ir.Function(self.mod, ir.FunctionType(PTR, [PTR]),
                         name=f"misa_handler_{screen_name}_{hid}")
        self.handlers.append(fn)
        ctx = self.new_fn_ctx(fn)
        ctx.in_handler = True
        ctx.id_slots = dict(id_slots)
        b = ctx.builder
        self.get_nil(ctx)
        if len(lam.params) > 1:
            # the checker rejects this first (E206); backstop only.
            raise AOTError("E600", "event handler takes at most 1 param",
                           lam.line, lam.col)
        for pn, _ in lam.params:
            a = self.alloca_var(ctx, pn)
            b.store(fn.args[0], a)
            ctx.scope.declare(pn, a)
        if lam.expr_body is not None:
            v = self.emit_expr(lam.expr_body, ctx)
            self.check(ctx, lam)
            b.ret(v)
        else:
            self.emit_block(lam.body, ctx)
        self.finish_fn_ctx(ctx)
        return hid

    def emit_handler_table(self):
        """misa_handler_table[] + misa_num_handlers for the drivers.

        The per-platform driver implements misa_dispatch_handler();
        the table itself is generated per program (empty when the
        program has no handlers).
        """
        hty = ir.FunctionType(PTR, [PTR])
        pty = hty.as_pointer()
        n = len(self.handlers)
        arrty = ir.ArrayType(pty, n)
        g = ir.GlobalVariable(self.mod, arrty,
                              name="misa_handler_table")
        g.global_constant = True
        g.initializer = ir.Constant(
            arrty, [h.bitcast(pty) for h in self.handlers])
        cnt = ir.GlobalVariable(self.mod, I32,
                                name="misa_num_handlers")
        cnt.global_constant = True
        cnt.initializer = self.const_i32(n)
