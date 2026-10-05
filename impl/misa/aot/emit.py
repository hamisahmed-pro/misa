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
        for name, decl in self.fn_decls.items():
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
        ctx = self.new_fn_ctx(fn)
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

    def emit_main_fn(self):
        fty = ir.FunctionType(PTR, [])
        fn = ir.Function(self.mod, fty, name="misa_main")
        ctx = self.new_fn_ctx(fn)
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
        if self.entry == "misa_ui_boot":
            self.emit_entry_boot()
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
            b.store(v, a)
            ctx.scope.declare(s.name, a)
        elif isinstance(s, A.AssignStmt):
            v = self.emit_expr(s.value, ctx)
            self.check(ctx, s)
            tgt = s.target
            if isinstance(tgt, A.Ident):
                a = ctx.scope.lookup(tgt.name)
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
            a = ctx.scope.lookup(e.name)
            return b.load(a, name=f"l_{e.name}")
        if isinstance(e, A.Call):
            return self.emit_call(e, ctx)
        if isinstance(e, A.Field):
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
            raise AOTError("E402",
                           "first-class lambdas are not supported "
                           "in AOT v0.3 (lambdas work as component "
                           "event handlers)",
                           e.line, e.col)
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
        name = e.name
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
        if kind not in ("openai", "gemini", "mock"):
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
        for pn, pte in decl.params:
            pkind = "Photo" if pte.name == "Photo" else ""
            lines.append(f"param: {pn}\t{pkind}")
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
        for pn in lam.params:
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
