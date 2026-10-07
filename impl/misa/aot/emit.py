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

# MVal layout mirror (must match struct MVal in misa_rt.c):
#   uint8_t tag; uint8_t pad[7]; union { int64_t i; ... } u;
# Only the tag and u.i offsets are used (unboxing); the union tail is
# modeled as a single i64 since GEP only needs correct field offsets.
_MVAL_T = ir.LiteralStructType([I8, ir.ArrayType(I8, 7), I64])


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
        # -- int unboxing (perf gap P-2) --------------------------------
        # int_mode: True when emitting a raw i64-specialized function
        # body. In int_mode, Int-typed expressions produce raw i64
        # values instead of boxed MVal*.
        self.int_mode = False
        # int_vars: names currently holding a (provably) Int value.
        # In normal mode the values are still boxed MVal*; in int_mode
        # they are raw i64 allocas.
        self.int_vars: set = set()
        # raw_int_vars: subset of int_vars whose alloca is a raw i64
        # (not MVal*). Loads produce i64; stores expect i64. Applies in
        # both normal mode (for `: Int` locals) and int_mode.
        self.raw_int_vars: set = set()
        # -- record field index optimization ---------------------------
        # var_types: variable name -> type expression (for record field
        # index resolution). Enables misa_record_get_idx fast path.
        self.var_types: dict = {}
        # -- string builder optimization -------------------------------
        # string_builders: set of variable names that should use Builder
        # instead of Text (for `s = s + x` pattern in loops).
        self.string_builders: set = set()
        # -- float unboxing ------------------------------------------
        # raw_float_vars: variables holding raw double (not MVal*).
        # Like raw_int_vars but for Float. Enables direct LLVM fadd/
        # fsub/fmul/fdiv without boxing overhead.
        self.raw_float_vars: set = set()
        # Phase 5a: names of `var`s cell-allocated (1-elem lists) because
        # a nested lambda may capture them. Reads/writes go through
        # misa_list_get/misa_index_set instead of a plain alloca.
        self.cell_vars: set = set()
        # Phase 4 (M4): handler compilation state. id_slots maps
        # screen id: names -> binding slots; in_handler marks a
        # compiled event handler (restricted scope).
        self.id_slots = None
        self.in_handler = False
        # Screen state (`state()`): name -> slot for the screen body
        # being emitted; screen_state_vars is the same map, visible to
        # compiled event handlers (resolved via the runtime frame).
        self.state_vars: dict = {}
        self.screen_state_vars: dict = {}
        # Overtake Wave 2 (FFI): var name -> FFI type name, for names
        # provably holding an FFI-scalar or C-struct value (mirrors
        # int_vars). Drives struct field reads (`p.x` via GEP) and
        # `...` vararg marshalling. Annotations and struct literals
        # populate it; assignments clear it (sound).
        self.ffi_types: dict = {}


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
    "misa_list_bool_new": (PTR, []),
    "misa_list_int_new": (PTR, []),
    "misa_list_float_new": (PTR, []),
    "misa_list_bool_push": (ir.VoidType(), [PTR, I32]),
    "misa_list_int_push": (ir.VoidType(), [PTR, I64]),
    "misa_list_push": (ir.VoidType(), [PTR, PTR]),
    "misa_list_get": (PTR, [PTR, I64]),
    # Phase 8 (overtake): wrap each element of a list as an interface
    # value with the given impl tag.
    "misa_iface_wrap_list": (PTR, [I64, PTR]),
    "misa_as_int": (I64, [PTR]),
    "misa_as_float": (DBL, [PTR]),
    # Phase 7: opaque C pointer (FFI `Ptr`).
    "misa_ptr": (PTR, [PTR]),
    "misa_as_ptr": (PTR, [PTR]),
    # Overtake Wave 2 (FFI): C-layout struct values.
    "misa_struct_new": (PTR, [PTR]),
    "misa_struct_bytes": (PTR, [PTR]),
    "misa_alloc": (PTR, [I64]),
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
    "misa_list_get_raw": (PTR, [PTR, I64, I32, I32]),
    "misa_list_set_raw": (ir.VoidType(), [PTR, I64, PTR, I32, I32]),
    "misa_record_get": (PTR, [PTR, PTR, I32, I32]),
    "misa_record_get_idx": (PTR, [PTR, I64, I32, I32]),
    "misa_record_set": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_iter_new": (PTR, [PTR, I32, I32]),
    "misa_iter_next": (PTR, [PTR]),
    "misa_print": (PTR, [PTR]),
    "misa_builtin_len": (PTR, [PTR, I32, I32]),
    "misa_builtin_range": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_builtin_str": (PTR, [PTR]),
    "misa_builtin_int": (PTR, [PTR, I32, I32]),
    "misa_builtin_float": (PTR, [PTR, I32, I32]),
    "misa_builtin_assert": (PTR, [PTR, PTR, I32, I32]),
    "misa_builtin_assert_eq": (PTR, [PTR, PTR, I32, I32]),
    "misa_builtin_assert_ne": (PTR, [PTR, PTR, I32, I32]),
    "misa_builtin_argv": (PTR, []),
    "misa_argv_init": (ir.VoidType(), [I32, PTR.as_pointer()]),
    "misa_builtin_env": (PTR, [PTR, I32, I32]),
    "misa_builtin_media": (PTR, [I32, PTR, I32, I32]),
    "misa_fs_read": (PTR, [PTR, I32, I32]),
    "misa_fs_write": (PTR, [PTR, PTR, I32, I32]),
    "misa_http_get": (PTR, [PTR, I32, I32]),
    # Phase 5 (stdlib2): JSON, HTTP POST, time, math.
    "misa_http_post": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_json_parse": (PTR, [PTR, I32, I32]),
    "misa_json_stringify": (PTR, [PTR, I32, I32]),
    "misa_time_now": (PTR, []),
    "misa_time_now_ms": (PTR, []),
    "misa_time_format": (PTR, [PTR, PTR, I32, I32]),
    "misa_time_parse": (PTR, [PTR, PTR, I32, I32]),
    "misa_math_sqrt": (PTR, [PTR, I32, I32]),
    "misa_math_cos": (PTR, [PTR, I32, I32]),
    "misa_math_sin": (PTR, [PTR, I32, I32]),
    "misa_math_tan": (PTR, [PTR, I32, I32]),
    "misa_math_pow": (PTR, [PTR, PTR, I32, I32]),
    "misa_math_abs": (PTR, [PTR, I32, I32]),
    "misa_math_floor": (PTR, [PTR, I32, I32]),
    "misa_math_ceil": (PTR, [PTR, I32, I32]),
    "misa_math_round": (PTR, [PTR, I32, I32]),
    # str namespace (code-point semantics; Phase 5 self-hosting prereq).
    "misa_str_slice": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_str_starts_with": (PTR, [PTR, PTR, I32, I32]),
    "misa_str_split": (PTR, [PTR, PTR, I32, I32]),
    "misa_str_trim": (PTR, [PTR, I32, I32]),
    "misa_str_chars": (PTR, [PTR, I32, I32]),
    "misa_str_ord": (PTR, [PTR, I32, I32]),
    "misa_str_chr": (PTR, [PTR, I32, I32]),
    # Wave 3 (stdlib expansion).
    "misa_regex_new": (PTR, [PTR, I32, I32]),
    "misa_regex_matches": (PTR, [PTR, PTR, I32, I32]),
    "misa_regex_find": (PTR, [PTR, PTR, I32, I32]),
    "misa_regex_find_all": (PTR, [PTR, PTR, I32, I32]),
    "misa_regex_replace": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_regex_captures": (PTR, [PTR, PTR, I32, I32]),
    "misa_crypto_sha256": (PTR, [PTR, I32, I32]),
    "misa_crypto_sha1": (PTR, [PTR, I32, I32]),
    "misa_crypto_md5": (PTR, [PTR, I32, I32]),
    "misa_crypto_hmac_sha256": (PTR, [PTR, PTR, I32, I32]),
    "misa_random_seed": (PTR, [PTR, I32, I32]),
    "misa_random_int": (PTR, [PTR, PTR, I32, I32]),
    "misa_random_float": (PTR, []),
    "misa_random_choice": (PTR, [PTR, I32, I32]),
    "misa_random_shuffle": (PTR, [PTR, I32, I32]),
    "misa_time_now_utc": (PTR, []),
    "misa_time_to_utc": (PTR, [PTR, I32, I32]),
    "misa_time_from_parts": (PTR, [PTR, PTR, PTR, PTR, PTR, PTR,
                                   I32, I32]),
    "misa_time_add": (PTR, [PTR, PTR, I32, I32]),
    "misa_time_add_hours": (PTR, [PTR, PTR, I32, I32]),
    "misa_time_in_tz": (PTR, [PTR, PTR, I32, I32]),
    "misa_time_weekday": (PTR, [PTR, I32, I32]),
    "misa_time_is_leap": (PTR, [PTR, I32, I32]),
    "misa_fs_temp_file": (PTR, [I32, I32]),
    # Final wave (stdlib): compression.
    "misa_bytes_new": (PTR, [PTR, I64]),
    "misa_compress_gzip": (PTR, [PTR, I32, I32]),
    "misa_compress_gunzip": (PTR, [PTR, I32, I32]),
    "misa_compress_gunzip_bytes": (PTR, [PTR, I32, I32]),
    "misa_compress_gzip_file": (PTR, [PTR, PTR, I32, I32]),
    "misa_compress_gunzip_file": (PTR, [PTR, PTR, I32, I32]),
    "misa_fs_temp_dir": (PTR, [I32, I32]),
    "misa_set_new": (PTR, []),
    "misa_cli_parser": (PTR, [PTR, PTR, I32, I32]),
    "misa_cli_flag": (ir.VoidType(), [PTR, PTR, PTR, PTR, I32, PTR, I32, I32]),
    "misa_cli_bool_flag": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_cli_short": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_cli_command": (ir.VoidType(), [PTR, PTR, PTR, I32, I32]),
    "misa_cli_parse": (PTR, [PTR, PTR, I32, I32]),
    "misa_cli_get": (PTR, [PTR, PTR, I32, I32]),
    "misa_cli_get_bool": (PTR, [PTR, PTR, I32, I32]),
    "misa_cli_get_command": (PTR, [PTR, I32, I32]),
    "misa_cli_positionals": (PTR, [PTR, I32, I32]),
    "misa_set_add": (ir.VoidType(), [PTR, PTR, I32, I32]),
    "misa_set_contains": (PTR, [PTR, PTR, I32, I32]),
    "misa_set_remove": (ir.VoidType(), [PTR, PTR, I32, I32]),
    "misa_set_union": (PTR, [PTR, PTR, I32, I32]),
    "misa_set_intersect": (PTR, [PTR, PTR, I32, I32]),
    "misa_set_diff": (PTR, [PTR, PTR, I32, I32]),
    "misa_set_to_list": (PTR, [PTR, I32, I32]),
    "misa_set_len": (I64, [PTR, I32, I32]),
    "misa_list_map": (PTR, [PTR, PTR, I32, I32]),
    "misa_list_filter": (PTR, [PTR, PTR, I32, I32]),
    "misa_list_fold": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_list_sort_by": (PTR, [PTR, PTR, I32, I32]),
    "misa_list_flat": (PTR, [PTR, I32, I32]),
    "misa_list_zip": (PTR, [PTR, PTR, I32, I32]),
    "misa_list_unique": (PTR, [PTR, I32, I32]),
    "misa_str_join": (PTR, [PTR, PTR, I32, I32]),
    "misa_str_replace": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_str_to_upper": (PTR, [PTR, I32, I32]),
    "misa_str_to_lower": (PTR, [PTR, I32, I32]),
    "misa_str_ends_with": (PTR, [PTR, PTR, I32, I32]),
    "misa_str_pad_left": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_str_pad_right": (PTR, [PTR, PTR, PTR, I32, I32]),
    # Phase 5 (DB): SQLite bindings.
    "misa_db_open": (PTR, [PTR, I32, I32]),
    "misa_db_exec": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_db_query": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_db_close": (PTR, [PTR, I32, I32]),
    # P-3: bulk inserts + explicit transactions.
    "misa_db_exec_many": (PTR, [PTR, PTR, PTR, I32, I32]),
    "misa_db_begin": (PTR, [PTR, I32, I32]),
    "misa_db_commit": (PTR, [PTR, I32, I32]),
    "misa_db_rollback": (PTR, [PTR, I32, I32]),
    # Phase 5 DB2: migrations + query plans.
    "misa_db_migrate": (PTR, [PTR, PTR, I32, I32]),
    "misa_db_explain": (PTR, [PTR, PTR, PTR, I32, I32]),
    # Text.Builder: amortized string building (perf gap P-1).
    "misa_builder_new": (PTR, []),
    "misa_builder_append": (ir.VoidType(), [PTR, PTR, I32, I32]),
    "misa_builder_build": (PTR, [PTR, I32, I32]),
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
    # Screen state (`state()`): per-screen-instance ephemeral cells.
    "misa_screen_frame_push": (ir.VoidType(),
                               [PTR, PTR, I32, PTR.as_pointer()]),
    "misa_screen_frame_argv": (PTR.as_pointer(), []),
    "misa_screen_state_cell": (PTR, [I32, PTR]),
    "misa_state_get": (PTR, [PTR]),
    "misa_state_set": (ir.VoidType(), [PTR, PTR]),
    "misa_screen_state_read_slot": (PTR, [I32]),
    "misa_screen_state_write_slot": (ir.VoidType(), [I32, PTR]),
    "misa_screen_after_handler": (ir.VoidType(), []),
    # Phase 4 (M4): driver interface used by compiled handlers.
    "misa_ui_active": (PTR, []),
    "misa_ui_find_bound": (I32, [PTR, I32]),
    "misa_ui_get_text": (PTR, [PTR, I32]),
    "misa_ui_report_error": (ir.VoidType(), [PTR, PTR]),
    "misa_model_call_meta": (PTR, [PTR, PTR.as_pointer(), I32, I32, I32]),
    # Phase 7 (concurrency): task fibers (misa_task.c). misa_task_spawn
    # takes the compiled `misa_spawn_N(env)` address as a raw pointer.
    "misa_task_init": (ir.VoidType(), []),
    "misa_task_shutdown": (ir.VoidType(), []),
    "misa_scope_enter": (ir.VoidType(), []),
    "misa_scope_exit": (ir.VoidType(), []),
    "misa_scope_detach": (ir.VoidType(), [PTR]),
    "misa_task_spawn": (PTR, [PTR, PTR, I32, I32]),
    "misa_task_await": (PTR, [PTR, I32, I32]),
    "misa_task_await_timeout": (PTR, [PTR, I64, I32, I32]),
    "misa_task_sleep": (PTR, [I64, I32, I32]),
    "misa_chan_new": (PTR, [I32, I32]),
    "misa_chan_send": (PTR, [PTR, PTR, I32, I32]),
    "misa_chan_recv": (PTR, [PTR, I32, I32]),
    "misa_chan_close": (PTR, [PTR, I32, I32]),
    "misa_fetch_all": (PTR, [PTR, I32, I32]),
    # Wave 3 (networking): TCP sockets + HTTP server.
    "misa_net_connect": (PTR, [PTR, PTR, I32, I32]),
    "misa_net_listen": (PTR, [PTR, I32, I32]),
    "misa_net_accept": (PTR, [PTR, I32, I32]),
    "misa_net_port": (PTR, [PTR, I32, I32]),
    "misa_listener_close": (PTR, [PTR, I32, I32]),
    "misa_socket_send": (PTR, [PTR, PTR, I32, I32]),
    "misa_socket_recv": (PTR, [PTR, PTR, I32, I32]),
    "misa_socket_close": (PTR, [PTR, I32, I32]),
    "misa_socket_set_timeout": (PTR, [PTR, PTR, I32, I32]),
    "misa_http_serve": (PTR, [PTR, PTR, I32, I32]),
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
        # P-2 Level 2: raw i64 specializations for Int->Int functions.
        # int_fns[name] -> ir.Function with signature i64 (i64, ...).
        self.int_fns = {}
        self.models = {}    # model name -> (ir.Function, meta_global)
        self.screens = {}   # screen name -> ir.Function
        self.screen_bodies = {}  # screen name -> _body ir.Function
        self.screen_rerenders = {}  # screen name -> rerender thunk
        self.handlers = []  # Phase 4 (M4): handler fns, dispatch order
        # Phase 5a: first-class functions. Closure values are MVal*
        # records {__closure_fn: Int(fn ptr), __closure_env: record}.
        self.lambda_counter = 0
        # Phase 7 (concurrency): spawn bodies become misa_spawn_N fns.
        self.spawn_counter = 0
        # Phase 7: names bound to Task values (for await validation —
        # the emitter has no type info; the checker guarantees an
        # await operand is Task or TokenStream via E205).
        self._task_names = set()
        self._taskret_fns = set()
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
        # Phase 8 (overtake): interfaces.
        self.iface_decls = {}
        self.impl_decls = []
        self.impl_index = {}
        self.impl_fns = {}
        # Phase 7: extern (FFI) blocks: fn name -> FnDecl signature.
        self.extern_decls = {}
        self.extern_libs = {}  # fn name -> library string
        # Overtake Wave 2 (FFI): struct name -> StructDecl, plus
        # cached LLVM struct types and callback trampolines.
        self.extern_structs = {}
        self._llvm_struct_types = {}
        self._cb_trampolines = {}  # sig key -> (trampoline fn, slot global)
        self._abort_fn = None  # lazily declared libc abort
        self.providers = {}   # provider name -> ProviderDecl
        self.aliases = {}     # alias -> (provider_name, model_id)
        for item in prog.items:
            if isinstance(item, A.FnDecl):
                self.fn_decls[item.name] = item
            elif isinstance(item, A.ExternDecl):
                for fn in item.fns:
                    self.extern_decls[fn.name] = fn
                    self.extern_libs[fn.name] = item.lib
                # Overtake Wave 2 (FFI): C-layout struct declarations.
                for st in item.structs:
                    self.extern_structs[st.name] = st
            elif isinstance(item, A.ModelDecl):
                self.model_decls[item.name] = item
            elif isinstance(item, A.ScreenDecl):
                self.screen_decls[item.name] = item
            elif isinstance(item, A.TypeDecl):
                self.type_decls[item.name] = item
            # Phase 8 (overtake): interfaces. impl_decls in source
            # order; impl_index[(iface, target)] = tag used in the
            # interface value record.
            elif isinstance(item, A.InterfaceDecl):
                self.iface_decls[item.name] = item
            elif isinstance(item, A.ImplDecl):
                self.impl_decls.append(item)
                key = (item.iface, item.target)
                # Tag = the impl's index within its interface (source
                # order); stable across wrap and dispatch sites.
                tag = sum(1 for (i, _t) in self.impl_index
                          if i == item.iface)
                self.impl_index[key] = tag
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

    # -- Phase 7: FFI ---------------------------------------------------
    # Misa -> LLVM type mapping for extern functions. Text and Ptr
    # both cross as i8*; Bool crosses as i32 (C int).

    @staticmethod
    def _ffi_llvm_type(tname: str):
        if tname == "Int":
            return I64
        if tname == "Float":
            return DBL
        if tname == "Text":
            return PTR
        if tname == "Bool":
            return I32
        if tname == "Ptr":
            return PTR
        if tname == "Void":
            return ir.VoidType()
        raise AOTError("E225",
                       f"cannot map type '{tname}' across FFI")

    # Overtake Wave 2 (FFI): full type-expression mapping — struct
    # names become LLVM struct types, `(A, B) -> R` becomes a C
    # function pointer type.
    def _ffi_llvm_type_te(self, te):
        if te.is_func:
            fty = ir.FunctionType(
                self._ffi_llvm_type_te(te.func_ret),
                [self._ffi_llvm_type_te(p) for p in te.func_params])
            return fty.as_pointer()
        tname = te.name
        if tname in self.extern_structs:
            st = self.extern_structs[tname]
            # Overtake final wave: unions are coerced to their SysV
            # register classes at the FFI boundary (a byte array
            # would mislower to the stack).
            if st.kind == "union":
                return self._ffi_union_abi_llvm_type(tname)
            return self._ffi_struct_llvm_type(tname)
        return self._ffi_llvm_type(tname)

    def _ffi_union_abi_llvm_type(self, name: str):
        """The LLVM type used for a union at the FFI boundary (function
        declarations, call marshalling). A bare byte array mislowers:
        LLVM passes `[N x i8]` on the stack while C passes small unions
        in registers. Coerce to the SysV per-eightbyte classes instead
        (verified against `gcc -S`). Unions over 16 bytes keep the byte
        array: they travel via byval/sret, where only the size
        matters."""
        from misa import ffi_layout
        classes = ffi_layout.union_chunk_classes(name,
                                                 self.extern_structs)
        if len(classes) > 2:
            return self._ffi_struct_llvm_type(name)
        tys = [I64 if c == "int" else DBL for c in classes]
        if len(tys) == 1:
            return tys[0]
        return ir.LiteralStructType(tys)

    def _ffi_struct_llvm_type(self, name: str):
        """LLVM type for a declared C-layout aggregate (cached).

        Structs: literal struct of field LLVM types — nested
        aggregates recurse; each bitfield *unit* becomes one i64
        element (bitfields sharing a unit share the element).
        Unions: a byte array of the union size (fields are accessed
        via bitcast). Layout matches misa.ffi_layout byte-exact.
        """
        if name not in self._llvm_struct_types:
            from misa import ffi_layout
            st = self.extern_structs[name]
            if st.kind == "union":
                size = ffi_layout.aggregate_size(name,
                                                 self.extern_structs)
                self._llvm_struct_types[name] = ir.ArrayType(I8, size)
            else:
                _, _, fields = ffi_layout.layout_aggregate(
                    st, self.extern_structs, (name,))
                elems = []
                seen_units = set()
                for _, desc, _, extra in fields:
                    if desc == "bits":
                        uoff = extra[0]
                        if uoff not in seen_units:
                            seen_units.add(uoff)
                            elems.append(I64)
                    elif desc == "scalar":
                        elems.append(self._ffi_llvm_type(extra))
                    else:  # "aggr": nested struct/union
                        elems.append(
                            self._ffi_struct_llvm_type(extra))
                self._llvm_struct_types[name] = ir.LiteralStructType(
                    elems, packed=False)
        return self._llvm_struct_types[name]

    def _ffi_field_llvm_index(self, name: str, fname: str, node):
        """(llvm_element_index, desc, extra) for a struct field.

        Bitfields sharing a unit share the element index; extra for
        bits is (bit_offset, width). For scalars extra is the tname;
        for nested aggregates extra is the aggregate name.
        """
        from misa import ffi_layout
        _, _, fields = ffi_layout.layout_aggregate(
            self.extern_structs[name], self.extern_structs, (name,))
        idx = 0
        seen_units = {}
        for fn_, desc, _, extra in fields:
            if desc == "bits":
                uoff = extra[0]
                if uoff not in seen_units:
                    seen_units[uoff] = idx
                    idx += 1
                if fn_ == fname:
                    return (seen_units[uoff], "bits",
                            (extra[1], extra[2]))
            else:
                if fn_ == fname:
                    return (idx, desc, extra)
                idx += 1
        raise AOTError("E101",
                       f"struct '{name}' has no field '{fname}'",
                       node.line, node.col)

    @staticmethod
    def _ffi_struct_layout(fields) -> tuple:
        """Legacy shim: (size, [(fname, kind)]) for flat scalar field
        lists. Kept for compatibility; new code uses
        misa.ffi_layout directly."""
        size_align = {"Int": (8, 8), "Float": (8, 8),
                      "Bool": (4, 4), "Ptr": (8, 8)}
        offset, max_align, kinds = 0, 1, []
        for fname, ftype in fields:
            sz, al = size_align[ftype]
            offset = (offset + al - 1) // al * al
            kinds.append((fname, ftype))
            offset += sz
            max_align = max(max_align, al)
        size = (offset + max_align - 1) // max_align * max_align
        return size, kinds

    @staticmethod
    def _te_ffi_name(te) -> Optional[str]:
        """The FFI type name for an exact scalar/struct annotation
        (`Int`, `Float`, `Text`, `Bool`, `Ptr`, or a declared struct),
        else None."""
        if te is None or te.is_func or te.is_list or te.is_map \
                or te.optional or te.type_args:
            return None
        return te.name

    def _ffi_var_type(self, te) -> Optional[str]:
        """Like _te_ffi_name but only for names that can cross FFI."""
        t = self._te_ffi_name(te)
        if t is None:
            return None
        if t in ("Int", "Float", "Text", "Bool", "Ptr"):
            return t
        if t in self.extern_structs:
            return t
        return None

    def _track_ffi_var(self, ctx, name: str, te):
        t = self._ffi_var_type(te)
        if t is not None:
            ctx.ffi_types[name] = t

    # -- Wave 2: C callbacks ------------------------------------------
    # A `(P, ...) -> R` extern parameter takes a Misa closure. The C
    # side needs a stable function pointer with the exact C signature,
    # so each distinct callback signature gets one trampoline: it loads
    # the current closure from a per-signature global slot, wraps the
    # raw C args as MVal*s, invokes the closure, and unwraps the
    # result. The call site saves/sets/restores the slot, which keeps
    # nested callbacks sound.

    def _libc_abort(self):
        if self._abort_fn is None:
            fty = ir.FunctionType(ir.VoidType(), [])
            self._abort_fn = ir.Function(self.mod, fty, name="abort")
        return self._abort_fn

    def _ffi_struct_needs_sret(self, name: str) -> bool:
        """x86_64 SysV / AAPCS64: a struct larger than 16 bytes is
        returned via a hidden sret pointer (first argument), not in
        registers. Raw LLVM IR does not infer this — the declaration
        must use the explicit sret form."""
        return self._ffi_struct_size(name) > 16

    def _ffi_struct_indirect(self, name: str) -> bool:
        """Same 16-byte rule for parameters: a larger struct crosses
        by invisible reference (pointer to a caller-made copy)."""
        return self._ffi_struct_size(name) > 16

    def _ffi_struct_size(self, name: str) -> int:
        from misa import ffi_layout
        try:
            return ffi_layout.aggregate_size(name, self.extern_structs)
        except ffi_layout.LayoutError:
            return 8  # real error already reported; never crash

    def _cb_trampoline(self, te):
        """(trampoline_fn, slot_global) for callback type te."""
        pnames = [p.name for p in te.func_params]
        rname = te.func_ret.name
        key = (tuple(pnames), rname)
        if key in self._cb_trampolines:
            return self._cb_trampolines[key]
        idx = len(self._cb_trampolines)
        slot = ir.GlobalVariable(self.mod, PTR,
                                 name=f"misa_cb_slot_{idx}")
        slot.linkage = "internal"
        slot.initializer = NULL_PTR
        fty = ir.FunctionType(self._ffi_llvm_type(rname),
                             [self._ffi_llvm_type(p) for p in pnames])
        fn = ir.Function(self.mod, fty, name=f"misa_cb_tramp_{idx}")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        z = self.const_i32(0)
        clos = b.load(slot, name="cb_clos")
        fn_mval = b.call(self.rt["misa_record_get"],
                         [clos, self.cstr("__closure_fn"), z, z],
                         name="cb_fn_mval")
        fn_i64 = b.call(self.rt["misa_as_int"], [fn_mval],
                        name="cb_fn_i64")
        lam_ty = ir.FunctionType(PTR, [PTR, PTR]).as_pointer()
        lam_fn = b.inttoptr(fn_i64, lam_ty, name="cb_lam")
        env = b.call(self.rt["misa_record_get"],
                     [clos, self.cstr("__closure_env"), z, z],
                     name="cb_env")
        args_list = b.call(self.rt["misa_list_new"], [], name="cb_args")
        for c_arg, p in zip(fn.args, pnames):
            b.call(self.rt["misa_list_push"],
                   [args_list, self._cb_wrap(b, c_arg, p)])
        res = b.call(lam_fn, [env, args_list], name="cb_res")
        # v1: a Misa error raised inside a callback cannot unwind
        # through C frames — report it and abort (never silently
        # continue with a garbage value).
        pend = b.call(self.rt["misa_pending"], [], name="cb_pend")
        is_err = b.icmp_unsigned("!=", pend, NULL_PTR, name="cb_iserr")
        err_bb = fn.append_basic_block("cb_error")
        ok_bb = fn.append_basic_block("cb_ok")
        b.cbranch(is_err, err_bb, ok_bb)
        b.position_at_end(err_bb)
        taken = b.call(self.rt["misa_take_pending"], [],
                       name="cb_taken")
        b.call(self.rt["misa_report_uncaught"], [taken])
        b.call(self._libc_abort(), [])
        b.unreachable()
        b.position_at_end(ok_bb)
        if rname == "Void":
            b.ret_void()
        else:
            b.ret(self._cb_unwrap(b, res, rname))
        self._cb_trampolines[key] = (fn, slot)
        return fn, slot

    def _cb_wrap(self, b, c_arg, p):
        """Wrap one raw C callback argument as an MVal*."""
        if p == "Int":
            return b.call(self.rt["misa_int"], [c_arg], name="_cbw")
        if p == "Float":
            return b.call(self.rt["misa_float"], [c_arg], name="_cbw")
        if p == "Bool":
            return b.call(self.rt["misa_bool"], [c_arg], name="_cbw")
        if p == "Text":
            # v1: callback Text params must be non-null.
            return b.call(self.rt["misa_text_cstr"], [c_arg],
                          name="_cbw")
        if p == "Ptr":
            return b.call(self.rt["misa_ptr"], [c_arg], name="_cbw")
        raise AOTError("E225",
                       f"cannot wrap callback argument of type '{p}'")

    def _cb_unwrap(self, b, res, rname):
        """Unwrap a callback's MVal* result to its C return type."""
        if rname == "Int":
            return b.call(self.rt["misa_as_int"], [res], name="_cbu")
        if rname == "Float":
            return b.call(self.rt["misa_as_float"], [res], name="_cbu")
        if rname == "Bool":
            return b.call(self.rt["misa_truthy"], [res], name="_cbu")
        if rname == "Text":
            return b.call(self.rt["misa_text_z"], [res], name="_cbu")
        if rname == "Ptr":
            return b.call(self.rt["misa_as_ptr"], [res], name="_cbu")
        raise AOTError("E225",
                       f"cannot unwrap callback result of type '{rname}'")

    def _marshal_scalar(self, v, tname: str, node, ctx):
        """Marshal a boxed MVal* (or raw i64 for Int) to its raw C
        value for an extern argument."""
        b = ctx.builder
        # P-2: a raw i64 is already the C `long`.
        if tname == "Int" and v.type == I64:
            return v
        v = self._ensure_mval(v, node, ctx)
        if tname == "Int":
            return self.call(ctx, "misa_as_int", [v], node,
                             check=False)
        if tname == "Float":
            return self.call(ctx, "misa_as_float", [v], node,
                             check=False)
        if tname == "Text":
            return self.call(ctx, "misa_text_z", [v], node,
                             check=False)
        if tname == "Bool":
            return self.call(ctx, "misa_truthy", [v], node,
                             check=False)
        if tname == "Ptr":
            return self.call(ctx, "misa_as_ptr", [v], node,
                             check=False)
        raise AOTError("E225",
                       f"cannot map type '{tname}' across FFI",
                       node.line, node.col)

    def _ffi_extern_llvm_type(self, name: str):
        """LLVM type for an aggregate at the FFI boundary (calls and
        declarations). Structs use their layout type; unions use the
        SysV-coerced register-class type (see
        _ffi_union_abi_llvm_type)."""
        st = self.extern_structs[name]
        if st.kind == "union":
            return self._ffi_union_abi_llvm_type(name)
        return self._ffi_struct_llvm_type(name)

    def _marshal_struct_arg(self, v, st_name: str, node, ctx):
        """Marshal a struct MVal* for a by-value C parameter.

        <=16 bytes: the LLVM struct value (the C ABI passes it in
        registers). Larger: a pointer to the struct bytes — the
        declaration uses byval, so the backend copies it onto the
        stack per the C ABI.
        """
        b = ctx.builder
        v = self._ensure_mval(v, node, ctx)
        raw_ptr = self.call(ctx, "misa_struct_bytes", [v], node,
                            check=False)
        sty = self._ffi_extern_llvm_type(st_name)
        sptr = b.bitcast(raw_ptr, sty.as_pointer(),
                         name="_sarg_ptr")
        if self._ffi_struct_indirect(st_name):
            return sptr
        return b.load(sptr, name="_sarg")

    def _wrap_struct_ret(self, raw, st_name: str, node, ctx):
        """Box a by-value LLVM struct return into a struct MVal*."""
        b = ctx.builder
        size = self._ffi_struct_size(st_name)
        mem = self.call(ctx, "misa_alloc", [self.const_i64(size)],
                        node, check=False)
        sty = self._ffi_extern_llvm_type(st_name)
        sptr = b.bitcast(mem, sty.as_pointer(), name="_sret_ptr")
        b.store(raw, sptr)
        return self.call(ctx, "misa_struct_new", [mem], node,
                         check=False)

    def _struct_type_of(self, e, ctx) -> Optional[str]:
        """The declared struct type of an expression, if statically
        known (variables via ffi_types, struct literals, extern calls
        with struct returns)."""
        if isinstance(e, A.Ident):
            t = ctx.ffi_types.get(e.name)
            return t if t in self.extern_structs else None
        if isinstance(e, A.StructLit):
            return e.name if e.name in self.extern_structs else None
        if isinstance(e, A.Field):
            # Overtake final wave: chained access `o.p.x` — resolve
            # the inner field's declared aggregate type.
            inner = self._struct_type_of(e.obj, ctx)
            if inner is not None:
                st = self.extern_structs.get(inner)
                if st is not None:
                    for fn_, fte in st.fields:
                        if fn_ == e.field \
                                and fte.name in self.extern_structs:
                            return fte.name
            return None
        if isinstance(e, A.Call) and e.name in self.extern_decls:
            decl = self.extern_decls[e.name]
            if decl.ret is not None \
                    and decl.ret.name in self.extern_structs:
                return decl.ret.name
        return None

    def _record_field_index(self, obj_expr, field_name: str, ctx) -> Optional[int]:
        """Field index for statically-known record types."""
        # Unwrap Index: bs[i] -> bs (for [RecordType])
        base = obj_expr
        is_indexed = False
        if isinstance(base, A.Index):
            base = base.obj
            is_indexed = True
        if not isinstance(base, A.Ident):
            return None
        pte = ctx.var_types.get(base.name)
        if pte is None:
            return None
        # Extract type name
        # TypeExpr has: name='Body', is_list=True for [Body]
        #               name='Body', is_list=False for Body
        type_name = None
        if hasattr(pte, 'name') and hasattr(pte, 'is_list'):
            if pte.is_list and is_indexed:
                # var[i] where var: [T] -> T
                type_name = pte.name
            elif not pte.is_list and not is_indexed:
                # var.field where var: RecordType
                type_name = pte.name
        if type_name is None:
            return None
        # Look up the TypeDecl
        tdecl = self.type_decls.get(type_name)
        if tdecl is None or tdecl.is_sum:
            return None
        # Find field index
        for idx, (fname, _) in enumerate(tdecl.fields):
            if fname == field_name:
                return idx
        return None

    def _find_string_accumulators(self, stmts) -> set:
        """Find variables that accumulate strings via `s = s + x` in loops.

        Pattern:
          var s = ""
          while ... {
            s = s + x
          }

        Returns set of variable names to convert to Builder.
        Conservative: only transforms if ALL assignments after init
        are self-concats, and at least one is in a loop.
        """
        # First pass: find candidates (var s = "")
        candidates = {}
        def find_inits(node_list, out):
            for s in node_list:
                if isinstance(s, A.LetStmt) and s.mutable:
                    if isinstance(s.value, A.StrLit):
                        parts = s.value.parts
                        if (len(parts) == 1 and parts[0] == "") or len(parts) == 0:
                            out[s.name] = s
                if isinstance(s, A.WhileStmt):
                    find_inits(s.body, out)
                elif isinstance(s, A.ForStmt):
                    find_inits(s.body, out)
                elif isinstance(s, A.IfStmt):
                    find_inits(s.then, out)
                    if s.els:
                        find_inits(s.els, out)
        find_inits(stmts, candidates)

        if not candidates:
            return set()

        info = {name: {"self_concat": True, "in_loop": False} for name in candidates}

        def check_assigns(node_list, in_loop=False):
            for s in node_list:
                if isinstance(s, A.AssignStmt):
                    if isinstance(s.target, A.Ident):
                        name = s.target.name
                        if name in candidates:
                            v = s.value
                            is_self_concat = (
                                isinstance(v, A.BinOp) and v.op == "+" and
                                isinstance(v.left, A.Ident) and v.left.name == name
                            )
                            if not is_self_concat:
                                info[name]["self_concat"] = False
                            if in_loop:
                                info[name]["in_loop"] = True
                if isinstance(s, A.WhileStmt):
                    check_assigns(s.body, in_loop=True)
                elif isinstance(s, A.ForStmt):
                    check_assigns(s.body, in_loop=True)
                elif isinstance(s, A.IfStmt):
                    check_assigns(s.then, in_loop=in_loop)
                    if s.els:
                        check_assigns(s.els, in_loop=in_loop)
        check_assigns(stmts, in_loop=False)

        result = set()
        for name, inf in info.items():
            if inf["self_concat"] and inf["in_loop"]:
                result.add(name)
        return result

    def _vararg_kind(self, arg_e, ctx, e) -> str:
        """The FFI kind of a `...` argument for marshalling."""
        if isinstance(arg_e, A.IntLit):
            return "Int"
        if isinstance(arg_e, A.FloatLit):
            return "Float"
        if isinstance(arg_e, A.StrLit):
            return "Text"
        if isinstance(arg_e, A.BoolLit):
            return "Bool"
        if isinstance(arg_e, A.Ident):
            t = ctx.ffi_types.get(arg_e.name)
            if t in ("Int", "Float", "Text", "Bool", "Ptr") \
                    or t in self.extern_structs:
                return t
        st = self._struct_type_of(arg_e, ctx)
        if st is not None:
            return st
        if isinstance(arg_e, A.Call) and arg_e.name in self.extern_decls:
            decl = self.extern_decls[arg_e.name]
            if decl.ret is not None:
                rt = decl.ret.name
                if rt in ("Int", "Float", "Text", "Bool", "Ptr") \
                        or rt in self.extern_structs:
                    return rt
        raise AOTError("E225",
                       "cannot determine the C type of a vararg "
                       "argument: bind it to a variable with a scalar "
                       "or struct type annotation first",
                       arg_e.line, arg_e.col)

    def _memset0(self, mem, size, ctx):
        """Zero `size` bytes at `mem` (union literals: unset bytes are
        zero). Uses the llvm.memset intrinsic; no C runtime change."""
        b = ctx.builder
        i8p = ir.PointerType(I8)
        ms = self.mod.declare_intrinsic('llvm.memset', [i8p, I64])
        b.call(ms, [b.bitcast(mem, i8p), ir.Constant(I8, 0),
                    self.const_i64(size), ir.Constant(ir.IntType(1), 0)])

    def _bitfield_checked(self, v, width, fv_node, e, ctx):
        """MVal* -> i64 for a `:width` bitfield, raising catchable E300
        when the value is outside the unsigned range 0..2**width-1
        (bitfields are unsigned, matching C `unsigned long`)."""
        b = ctx.builder
        ival = self._marshal_scalar(v, "Int", fv_node, ctx)
        if width >= 64:
            bad = b.icmp_signed("<", ival, self.const_i64(0),
                               name="_bf_neg")
        else:
            bad = b.icmp_unsigned(">=", ival,
                                  self.const_i64(1 << width),
                                  name="_bf_big")
        ok_b = ctx.fn.append_basic_block("bf.ok")
        err_b = ctx.fn.append_basic_block("bf.err")
        b.cbranch(bad, err_b, ok_b)
        b.position_at_end(err_b)
        line, col = self.loc(e)
        b.call(self.rt["misa_raise"],
               [self.cstr("E300"),
                self.cstr(f"bitfield value out of range for :{width}"),
                line, col])
        # misa_raise always sets the pending error: branch directly to
        # the handler (check() would emit a dead, unterminated block).
        b.branch(ctx.err_stack[-1])
        b.position_at_end(ok_b)
        return ival

    def _store_bitfield(self, sptr, idx, extra, v, fv_node, e, ctx):
        """Read-modify-write a bitfield into its unit (struct case)."""
        b = ctx.builder
        bit_off, width = extra
        uptr = b.gep(sptr, [self.const_i32(0), self.const_i32(idx)],
                     name="_bf_gep")
        unit = b.load(uptr, name="_bf_unit")
        ival = self._bitfield_checked(v, width, fv_node, e, ctx)
        mask = ((1 << width) - 1) << bit_off
        cleared = b.and_(unit,
                         self.const_i64(mask ^ 0xFFFFFFFFFFFFFFFF),
                         name="_bf_clr")
        new = b.or_(cleared,
                    b.shl(ival, self.const_i64(bit_off)),
                    name="_bf_new")
        b.store(new, uptr)

    def _marshal_aggr_value(self, v, aggr_name, node, ctx):
        """MVal* aggregate box -> LLVM aggregate value (nested
        struct/union field in a literal)."""
        b = ctx.builder
        v = self._ensure_mval(v, node, ctx)
        raw_ptr = self.call(ctx, "misa_struct_bytes", [v], node,
                            check=False)
        sty = self._ffi_struct_llvm_type(aggr_name)
        sptr = b.bitcast(raw_ptr, sty.as_pointer(), name="_aggr_ptr")
        return b.load(sptr, name="_aggr")

    def _box_aggr_value(self, raw, aggr_name, node, ctx):
        """Box an LLVM aggregate value into fresh bytes + MVal*."""
        b = ctx.builder
        size = self._ffi_struct_size(aggr_name)
        mem = self.call(ctx, "misa_alloc", [self.const_i64(size)],
                        node, check=False)
        sptr = b.bitcast(mem, raw.type.as_pointer(), name="_box_ptr")
        b.store(raw, sptr)
        return self.call(ctx, "misa_struct_new", [mem], node,
                         check=False)

    def _emit_struct_lit(self, e: A.StructLit, ctx):
        """`Point { x: 1.0, y: 2.0 }` — build the raw bytes on the
        heap and box them as a struct MVal*.

        Overtake final wave: nested aggregates (inner MVal* unboxed to
        its LLVM value), unions (memory zeroed first; any subset of
        fields), bitfields (unsigned; read-modify-write into the unit;
        out-of-range values are E300).
        """
        st = self.extern_structs.get(e.name)
        if st is None:
            raise AOTError("E101", f"unknown struct '{e.name}'",
                           e.line, e.col)
        b = ctx.builder
        size = self._ffi_struct_size(e.name)
        mem = self.call(ctx, "misa_alloc", [self.const_i64(size)], e,
                        check=False)
        sty = self._ffi_struct_llvm_type(e.name)
        if st.kind == "union":
            self._memset0(mem, size, ctx)
        sptr = b.bitcast(mem, sty.as_pointer(), name="_slit_ptr")
        have = {fn_: fv for fn_, fv in e.fields}
        if st.kind == "union":
            from misa import ffi_layout
            fmap = ffi_layout.field_map(e.name, self.extern_structs)
            for fn_, fv in e.fields:
                desc, _, extra = fmap[fn_]
                v = self.emit_expr(fv, ctx)
                self.check(ctx, e)
                self._store_union_field(sptr, desc, extra, v, fv, e,
                                        ctx)
        else:
            for fn_, fte in st.fields:
                if fn_ not in have:
                    continue  # unions only; structs require all fields
                # The checker guarantees every struct field is present.
                v = self.emit_expr(have[fn_], ctx)
                self.check(ctx, e)
                idx, desc, extra = self._ffi_field_llvm_index(
                    e.name, fn_, have[fn_])
                if desc == "bits":
                    self._store_bitfield(sptr, idx, extra, v,
                                         have[fn_], e, ctx)
                elif desc == "aggr":
                    raw = self._marshal_aggr_value(v, extra,
                                                   have[fn_], ctx)
                    fp = b.gep(sptr, [self.const_i32(0),
                                      self.const_i32(idx)],
                               name="_slit_gep")
                    b.store(raw, fp)
                else:
                    raw = self._marshal_scalar(v, extra, have[fn_],
                                               ctx)
                    fp = b.gep(sptr, [self.const_i32(0),
                                      self.const_i32(idx)],
                               name="_slit_gep")
                    b.store(raw, fp)
        return self.call(ctx, "misa_struct_new", [mem], e,
                         check=False)

    def _store_union_field(self, sptr, desc, extra, v, fv_node, e,
                           ctx):
        """Store one field of a union literal (memory already zeroed).
        sptr points at the union's byte array."""
        b = ctx.builder
        raw_ptr = b.bitcast(sptr, ir.PointerType(I8), name="_uf_base")
        if desc == "bits":
            bit_off, width = extra[1], extra[2]
            uptr = b.bitcast(raw_ptr, ir.PointerType(I64),
                             name="_uf_ptr")
            unit = b.load(uptr, name="_uf_unit")
            ival = self._bitfield_checked(v, width, fv_node, e, ctx)
            mask = ((1 << width) - 1) << bit_off
            cleared = b.and_(
                unit, self.const_i64(mask ^ 0xFFFFFFFFFFFFFFFF),
                name="_uf_clr")
            new = b.or_(cleared, b.shl(ival, self.const_i64(bit_off)),
                        name="_uf_new")
            b.store(new, uptr)
        elif desc == "aggr":
            nty = self._ffi_struct_llvm_type(extra)
            fptr = b.bitcast(raw_ptr, nty.as_pointer(),
                             name="_uf_ptr")
            raw = self._marshal_aggr_value(v, extra, fv_node, ctx)
            b.store(raw, fptr)
        else:  # scalar
            fty = self._ffi_llvm_type(extra)
            fptr = b.bitcast(raw_ptr, fty.as_pointer(),
                             name="_uf_ptr")
            raw = self._marshal_scalar(v, extra, fv_node, ctx)
            b.store(raw, fptr)

    def _box_scalar(self, raw, ft, e, ctx):
        if ft == "Int":
            return self.call(ctx, "misa_int", [raw], e, check=False)
        if ft == "Float":
            return self.call(ctx, "misa_float", [raw], e, check=False)
        if ft == "Bool":
            return self.call(ctx, "misa_bool", [raw], e, check=False)
        if ft == "Ptr":
            return self.call(ctx, "misa_ptr", [raw], e, check=False)
        raise AOTError("E225",
                       f"cannot read struct field of type '{ft}'",
                       e.line, e.col)

    def _emit_struct_field(self, e: A.Field, st_name: str, ctx):
        """`p.x` on a statically-known aggregate value: GEP + load
        (structs), bitcast + load (unions), or bit extract
        (bitfields) — boxed back to an MVal*."""
        b = ctx.builder
        obj = self.emit_expr(e.obj, ctx)
        self.check(ctx, e)
        obj = self._ensure_mval(obj, e.obj, ctx)
        st = self.extern_structs[st_name]
        raw_ptr = self.call(ctx, "misa_struct_bytes", [obj], e,
                            check=False)
        sty = self._ffi_struct_llvm_type(st_name)
        sptr = b.bitcast(raw_ptr, sty.as_pointer(),
                         name="_sfield_ptr")
        if st.kind == "union":
            return self._load_union_field(sptr, st_name, e, ctx)
        idx, desc, extra = self._ffi_field_llvm_index(st_name, e.field,
                                                      e)
        if desc == "bits":
            bit_off, width = extra
            uptr = b.gep(sptr, [self.const_i32(0),
                                self.const_i32(idx)],
                         name="_bf_gep")
            unit = b.load(uptr, name="_bf_unit")
            shr = b.lshr(unit, self.const_i64(bit_off), name="_bf_shr")
            val = b.and_(shr, self.const_i64((1 << width) - 1),
                         name="_bf_val")
            return self.call(ctx, "misa_int", [val], e, check=False)
        fp = b.gep(sptr, [self.const_i32(0), self.const_i32(idx)],
                   name="_sfield_gep")
        raw = b.load(fp, name="_sfield")
        if desc == "aggr":
            return self._box_aggr_value(raw, extra, e, ctx)
        return self._box_scalar(raw, extra, e, ctx)

    def _load_union_field(self, sptr, st_name: str, e: A.Field, ctx):
        """Field read on a union value (byte array): bitcast to the
        field type and load; bitfields extract from the unit."""
        from misa import ffi_layout
        b = ctx.builder
        fmap = ffi_layout.field_map(st_name, self.extern_structs)
        desc, _, extra = fmap[e.field]
        raw_ptr = b.bitcast(sptr, ir.PointerType(I8), name="_uf_base")
        if desc == "bits":
            bit_off, width = extra[1], extra[2]
            uptr = b.bitcast(raw_ptr, ir.PointerType(I64),
                             name="_uf_ptr")
            unit = b.load(uptr, name="_uf_unit")
            shr = b.lshr(unit, self.const_i64(bit_off), name="_uf_shr")
            val = b.and_(shr, self.const_i64((1 << width) - 1),
                         name="_uf_val")
            return self.call(ctx, "misa_int", [val], e, check=False)
        if desc == "aggr":
            nty = self._ffi_struct_llvm_type(extra)
            fptr = b.bitcast(raw_ptr, nty.as_pointer(),
                             name="_uf_ptr")
            return self._box_aggr_value(b.load(fptr, name="_uf"), extra,
                                        e, ctx)
        fty = self._ffi_llvm_type(extra)
        fptr = b.bitcast(raw_ptr, fty.as_pointer(), name="_uf_ptr")
        return self._box_scalar(b.load(fptr, name="_uf"), extra, e,
                                ctx)

    def emit_extern_call(self, e: A.Call, ctx, name: str):
        """Phase 7: call an extern (FFI) function. Marshals MVal*
        args to raw C values, calls the declared symbol, and wraps
        the raw return back into an MVal*.

        Overtake Wave 2: struct args/returns cross by value;
        `(A, B) -> R` params take Misa closures via per-signature C
        trampolines (the closure slot is saved/set/restored around
        the call, keeping nested callbacks sound); `...` tails
        marshal per the mapping table.
        """
        decl = self.extern_decls[name]
        line, col = self.loc(e)
        b = ctx.builder
        c_args = []
        cb_restores = []  # (slot_global, saved MVal*)
        fixed = decl.params
        for (pn, pte), arg_e in zip(fixed, e.args):
            v = self.emit_expr(arg_e, ctx)
            self.check(ctx, e)
            if pte.is_func:
                # Callback: stash the closure in the signature's
                # slot and pass the trampoline pointer.
                tramp, slot = self._cb_trampoline(pte)
                saved = b.load(slot, name="_cb_saved")
                b.store(v, slot)
                cb_restores.append((slot, saved))
                c_args.append(tramp.bitcast(
                    self._ffi_llvm_type_te(pte)))
            elif pte.name in self.extern_structs:
                c_args.append(self._marshal_struct_arg(
                    v, pte.name, arg_e, ctx))
            else:
                c_args.append(self._marshal_scalar(
                    v, pte.name, arg_e, ctx))
        # `...` tail: kind derived per argument (literals, tracked
        # variables, struct literals, extern calls).
        for arg_e in e.args[len(fixed):]:
            v = self.emit_expr(arg_e, ctx)
            self.check(ctx, e)
            kind = self._vararg_kind(arg_e, ctx, e)
            if kind in self.extern_structs:
                c_args.append(self._marshal_struct_arg(
                    v, kind, arg_e, ctx))
            else:
                c_args.append(self._marshal_scalar(
                    v, kind, arg_e, ctx))
        target = self.mod.get_global(name)
        ret_te = decl.ret
        ret_t = ret_te.name if ret_te else "Void"
        # Large struct returns: pass the hidden sret slot first; the
        # declared function returns void.
        sret_mem = None
        if ret_t in self.extern_structs \
                and self._ffi_struct_needs_sret(ret_t):
            size = self._ffi_struct_size(ret_t)
            sret_mem = self.call(ctx, "misa_alloc",
                                 [self.const_i64(size)], e, check=False)
            sty = self._ffi_struct_llvm_type(ret_t)
            c_args.insert(0, b.bitcast(sret_mem, sty.as_pointer(),
                                       name="_sret_slot"))
            raw = b.call(target, c_args, name="_extern_ret")
        else:
            raw = b.call(target, c_args, name="_extern_ret")
        for slot, saved in reversed(cb_restores):
            b.store(saved, slot)
        if ret_t == "Void":
            return self.call(ctx, "misa_null", [], e, check=False)
        if ret_t in self.extern_structs:
            if sret_mem is not None:
                return self.call(ctx, "misa_struct_new", [sret_mem], e,
                                 check=False)
            return self._wrap_struct_ret(raw, ret_t, e, ctx)
        if ret_t == "Int":
            return self.call(ctx, "misa_int", [raw], e, check=False)
        if ret_t == "Float":
            return self.call(ctx, "misa_float", [raw], e, check=False)
        if ret_t == "Text":
            # NULL char* -> nil; otherwise copy into Misa memory.
            is_null = b.icmp_unsigned("==", raw, NULL_PTR,
                                      name="_text_null")
            null_bb = b.append_basic_block("text_null")
            copy_bb = b.append_basic_block("text_copy")
            merge_bb = b.append_basic_block("text_merge")
            b.cbranch(is_null, null_bb, copy_bb)
            b.position_at_end(null_bb)
            null_v = self.call(ctx, "misa_null", [], e, check=False)
            b.branch(merge_bb)
            b.position_at_end(copy_bb)
            copy_v = self.call(ctx, "misa_text_cstr", [raw], e,
                                check=False)
            b.branch(merge_bb)
            b.position_at_end(merge_bb)
            phi = b.phi(PTR, name="_text_ret")
            phi.add_incoming(null_v, null_bb)
            phi.add_incoming(copy_v, copy_bb)
            return phi
        if ret_t == "Bool":
            return self.call(ctx, "misa_bool", [raw], e, check=False)
        if ret_t == "Ptr":
            return self.call(ctx, "misa_ptr", [raw], e, check=False)
        raise AOTError("E225",
                       f"cannot map return type '{ret_t}' across FFI",
                       e.line, e.col)

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
        # Phase 7: collect Task-bound names before validating awaits
        # (check_no_streaming distinguishes Task-await from stream-await
        # syntactically; see _collect_task_names).
        self._collect_task_names()
        self.check_no_streaming(self.prog)
        # declare Misa functions first (recursion)
        # Phase 5c: generic fns are monomorphized lazily on first use.
        for name, decl in self.fn_decls.items():
            if decl.type_params:
                continue
            fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
            self.fns[name] = ir.Function(
                self.mod, fty, name=f"misa_fn_{name}")
            # P-2 Level 2: raw i64 specialization for Int->Int functions.
            if self._is_int_fn(decl):
                rty = ir.FunctionType(I64, [I64] * len(decl.params))
                self.int_fns[name] = ir.Function(
                    self.mod, rty, name=f"misa_fn_{name}__int")
        # Phase 8 (overtake): declare impl method functions.
        self.declare_impl_fns()
        for name, decl in self.model_decls.items():
            self.emit_model_decl(name, decl)
        for name, decl in self.screen_decls.items():
            fty = ir.FunctionType(PTR, [PTR] * len(decl.params))
            self.screens[name] = ir.Function(
                self.mod, fty, name=f"misa_screen_{name}")
            # Screen state: the public fn pushes a fresh frame; the
            # _body fn renders; the rerender thunk re-runs _body with
            # the frame's stashed args (state preserved).
            self.screen_bodies[name] = ir.Function(
                self.mod, fty, name=f"misa_screen_{name}_body")
            self.screen_rerenders[name] = ir.Function(
                self.mod, ir.FunctionType(PTR, []),
                name=f"misa_rerender_{name}")
        # Phase 7: declare extern (FFI) functions with raw C types.
        # The linker resolves them against the shared libraries
        # (build.py adds -l<lib>). Overtake Wave 2: struct-typed and
        # callback-typed params, plus var_arg for `...` declarations.
        # Large struct returns (>16 bytes) use the explicit sret form
        # (hidden first pointer arg); the C ABI requires it.
        for name, decl in self.extern_decls.items():
            ret_te = decl.ret if decl.ret \
                else A.TypeExpr(name="Void")
            sret_struct = None
            if ret_te.name in self.extern_structs \
                    and self._ffi_struct_needs_sret(ret_te.name):
                sret_struct = ret_te.name
            param_tys = []
            byval_idxs = []
            for i, (_, pte) in enumerate(decl.params):
                if not pte.is_func and pte.name in self.extern_structs \
                        and self._ffi_struct_indirect(pte.name):
                    # >16-byte struct param: the C ABI passes it on
                    # the stack; declare the slot as a byval pointer
                    # (raw LLVM struct args mislower — see sret).
                    sty = self._ffi_struct_llvm_type(pte.name)
                    param_tys.append(sty.as_pointer())
                    # +1 when the hidden sret slot leads.
                    byval_idxs.append(i + (1 if sret_struct else 0))
                else:
                    param_tys.append(self._ffi_llvm_type_te(pte))
            if sret_struct is not None:
                sty = self._ffi_struct_llvm_type(sret_struct)
                fty = ir.FunctionType(ir.VoidType(),
                                      [sty.as_pointer()] + param_tys,
                                      var_arg=decl.varargs)
            else:
                fty = ir.FunctionType(
                    self._ffi_llvm_type_te(ret_te), param_tys,
                    var_arg=decl.varargs)
            efn = ir.Function(self.mod, fty, name=name)
            for bi in byval_idxs:
                efn.args[bi].attributes.add("byval")
        for name, decl in self.fn_decls.items():
            if decl.type_params:
                continue
            self.emit_fn_body(name, decl)
        # Phase 8 (overtake): emit impl method bodies.
        self.emit_impl_bodies()
        for name, decl in self.screen_decls.items():
            self.emit_screen_body(name, decl)
        # Phase 4 (M4): the handler dispatch table the drivers call.
        self.emit_handler_table()
        self.emit_main_fn()
        self.emit_entry_main()
        return self.mod

    def _collect_task_names(self):
        """Collect names bound to Task values, for await validation.

        The emitter carries no type info on nodes, so await-on-Task vs
        await-on-TokenStream is decided syntactically: a name bound by
        a `Task<...>` annotation or initialized from `spawn {...}`, a
        Task-annotated parameter, or a call to a `-> Task<...>` fn.
        Anything else awaiting (the checker already rejected
        non-awaitables with E205) is a TokenStream -> E402.

        Over-approximate and shadowing-blind by design: pathological
        shadowing of a Task name by a non-Task may misclassify and fail
        at runtime (E200) instead of at compile time (E402).
        """
        self._task_names = set()
        self._taskret_fns = set()

        def visit_fns(node):
            if isinstance(node, A.FnDecl):
                if node.ret is not None and \
                        getattr(node.ret, "name", "") == "Task":
                    self._taskret_fns.add(node.name)
            for child in self.children(node):
                visit_fns(child)

        def is_task_call(value):
            return isinstance(value, A.Call) and \
                value.name in self._taskret_fns

        def visit(node):
            if isinstance(node, A.LetStmt):
                ann = node.ann
                if ann is not None and getattr(ann, "name", "") == "Task":
                    self._task_names.add(node.name)
                elif isinstance(node.value, A.Spawn) or \
                        is_task_call(node.value):
                    self._task_names.add(node.name)
            elif isinstance(node, A.FnDecl):
                for pn, pte in node.params:
                    if pte is not None and \
                            getattr(pte, "name", "") == "Task":
                        self._task_names.add(pn)
            for child in self.children(node):
                visit(child)

        visit_fns(self.prog)
        visit(self.prog)

    def _is_task_expr(self, node) -> bool:
        """Syntactic Task check for an expression (see _collect_task_names)."""
        if isinstance(node, A.Spawn):
            return True
        if isinstance(node, A.Ident):
            return node.name in self._task_names
        if isinstance(node, A.Call) and node.name in self._taskret_fns:
            return True
        return False

    def _await_is_task(self, node: A.Await) -> bool:
        """Syntactic Task check for an await operand (see above)."""
        return self._is_task_expr(node.operand)

    def check_no_streaming(self, node):
        """Reject stream/await-on-stream/on anywhere (E402).

        Phase 7: `await` on a Task<T> is compiled (fibers); await on
        anything else is still E402 here (the checker enforced E205
        upstream, so it can only be a TokenStream).

        Lambdas are allowed as component args (event handlers, printed
        as <handler>); anywhere else they are rejected at emit time.
        """
        if isinstance(node, A.StreamDecl):
            raise AOTError("E402",
                           "streaming is not supported in AOT v0.3 "
                           "(use `misa run`)",
                           node.line, node.col)
        if isinstance(node, A.OnStmt):
            raise AOTError("E402",
                           "streaming is not supported in AOT v0.3 "
                           "(use `misa run`)",
                           node.line, node.col)
        if isinstance(node, A.Await) and not self._await_is_task(node):
            raise AOTError("E402",
                           "await on streams is not supported in AOT "
                           "v0.3 (use `misa run`)",
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
        """True if any Lambda or Spawn occurs anywhere under node.

        Spawn bodies capture like lambdas (see emit_spawn), so a `var`
        captured by a task needs the same shared heap cell.
        """
        if isinstance(node, (A.Lambda, A.Spawn)):
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

    def new_fn_ctx(self, fn: ir.Function, int_mode=False) -> FnCtx:
        entry = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry)
        ctx = FnCtx(self, b, fn)
        ctx.int_mode = int_mode
        err = fn.append_basic_block("err")
        ctx.err_stack.append(err)
        if not int_mode:
            # Phase 7 (concurrency): every compiled function opens a task
            # scope on entry; emit_ret() closes it on every return path so
            # a returning scope cancels its still-running child tasks.
            # (int_mode functions have restricted bodies: no spawn, so no
            # scope is needed.)
            b.call(self.rt["misa_scope_enter"], [])
        return ctx

    def emit_ret(self, ctx: FnCtx, v):
        """Return from a Misa function: close the task scope, then ret.

        Must be used for EVERY return out of a function created via
        new_fn_ctx (normal returns, error epilogues, fallthroughs) so
        scope_enter/scope_exit stay balanced.
        """
        if not ctx.int_mode:
            ctx.builder.call(self.rt["misa_scope_exit"], [])
        ctx.builder.ret(v)

    def finish_fn_ctx(self, ctx: FnCtx):
        b = ctx.builder
        # error epilogue: return NULL (pending flag is set)
        if not ctx.err_stack:
            return
        err = ctx.err_stack[0]
        b.position_at_end(err)
        # P-2: int_mode functions return i64; the error path is
        # unreachable for restricted bodies, but must be well-typed.
        fb = self.const_i64(0) if ctx.int_mode else NULL_PTR
        self.emit_ret(ctx, fb)
        # normal fallthrough: return nil
        b.position_at_end(ctx.fn.basic_blocks[0])
        # find the last non-terminated block to append fallthrough ret
        for blk in reversed(ctx.fn.blocks):
            if not blk.is_terminated:
                b.position_at_end(blk)
                fv = self.const_i64(0) if ctx.int_mode \
                    else self.get_nil(ctx)
                self.emit_ret(ctx, fv)
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
        # P-2 Level 2: Int->Int functions get a raw i64 specialization
        # plus a thin MVal* wrapper for generic callers.
        if name in self.int_fns:
            self.emit_int_fn(name, decl)
            return
        fn = self.fns[name]
        self.emit_fn_body_direct(fn, decl)

    def emit_int_fn(self, name: str, decl: A.FnDecl):
        """Emit `misa_fn_<name>__int(i64...) -> i64` and the MVal* wrapper."""
        raw_fn = self.int_fns[name]
        ctx = self.new_fn_ctx(raw_fn, int_mode=True)
        b = ctx.builder
        # Params are raw i64; alloca as i64 at entry.
        for (pn, _), arg in zip(decl.params, raw_fn.args):
            arg.name = f"p_{pn}"
            save = b.block
            b.position_at_start(raw_fn.basic_blocks[0])
            a = b.alloca(I64, name=f"i_{pn}")
            b.position_at_end(save)
            b.store(arg, a)
            ctx.scope.declare(pn, a)
            ctx.int_vars.add(pn)
        self.emit_block(decl.body, ctx)
        self.finish_fn_ctx(ctx)
        # MVal* wrapper for generic callers: unbox args, call raw, box.
        fn = self.fns[name]
        wctx = self.new_fn_ctx(fn)
        wb = wctx.builder
        raw_args = []
        for (pn, _), arg in zip(decl.params, fn.args):
            uv = self.call(wctx, "misa_as_int", [arg], decl, check=True)
            raw_args.append(uv)
        r = wb.call(raw_fn, raw_args, name="_raw")
        self.check(wctx, decl)
        boxed = self.call(wctx, "misa_int", [r], decl, check=False)
        self.emit_ret(wctx, boxed)
        self.finish_fn_ctx(wctx)

    def emit_fn_body_direct(self, fn, decl: A.FnDecl):
        ctx = self.new_fn_ctx(fn)
        # String builder optimization: detect `var s = ""` followed by
        # `s = s + x` in loops, convert to builder pattern automatically.
        ctx.string_builders = self._find_string_accumulators(decl.body)
        # Phase 5a: cell-allocate `var`s captured by nested lambdas.
        ctx.cell_vars = self._cell_vars_for(decl.body)
        b = ctx.builder
        for (pn, pte), arg in zip(decl.params, fn.args):
            arg.name = f"p_{pn}"
            a = self.alloca_var(ctx, pn)
            b.store(arg, a)
            ctx.scope.declare(pn, a)
            # Track param type for record field index optimization.
            if pte is not None:
                ctx.var_types[pn] = pte
            # P-2: track provably-Int variables for unboxing.
            if self._te_is_int(pte):
                ctx.int_vars.add(pn)
            # Wave 2 (FFI): track FFI-scalar/struct params for
            # struct field reads and vararg marshalling.
            self._track_ffi_var(ctx, pn, pte)
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
                                             A.TableDecl,
                                             A.ModelDecl, A.ScreenDecl,
                                             A.EvalDecl, A.TestDecl,
                                             A.FnDecl,
                                             A.ExternDecl,
                                             A.InterfaceDecl, A.ImplDecl,
                                             A.ImportDecl))]
        ctx.cell_vars = self._cell_vars_for(top_stmts)
        self.get_nil(ctx)
        scope = ctx.scope
        for item in self.prog.items:
            if isinstance(item, (A.AppDecl, A.ProviderDecl, A.ModelsDecl,
                                 A.TypeDecl, A.TableDecl, A.ModelDecl,
                                 A.ScreenDecl,
                                 A.EvalDecl, A.TestDecl, A.FnDecl,
                                 A.ExternDecl,
                                 A.InterfaceDecl, A.ImplDecl,
                                 A.ImportDecl)):
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
        fty = ir.FunctionType(I32, [I32, PTR.as_pointer()])
        fn = ir.Function(self.mod, fty, name="main")
        b = ir.IRBuilder(fn.append_basic_block("entry"))
        b.call(self.rt["misa_rt_init"], [])
        # argv(): stash the C argv for the `argv()` builtin. Targets
        # whose entry isn't this main (Android/iOS/web) never call
        # misa_argv_init, so argv() is [] there (documented).
        b.call(self.rt["misa_argv_init"], [fn.args[0], fn.args[1]])
        main = self.mod.get_global("misa_main")
        r = b.call(main, [], name="r")
        # Phase 7: reap outstanding tasks (bounded), cancel stragglers,
        # and report never-awaited task errors to stderr.
        b.call(self.rt["misa_task_shutdown"], [])
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
        # Phase 7: reap outstanding tasks (web: no-op beyond reporting).
        b.call(self.rt["misa_task_shutdown"], [])
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
            # String builder optimization: `var s = ""` where s is an
            # accumulator → `var s = builder()`
            if s.name in ctx.string_builders:
                # Emit builder() instead of ""
                v = self.call(ctx, "misa_builder_new", [], s, check=False)
            else:
                v = self.emit_expr(val_node, ctx)
            self.check(ctx, s)
            # P-2: Int-typed locals use raw i64 allocas (no per-access
            # boxing). In int_mode any i64 value qualifies; in normal mode
            # the declared/inferred type must be Int.
            if ctx.int_mode:
                is_int_local = (v.type == I64)
            else:
                # Normal mode: explicit `: Int`, or an int-valued
                # initializer (the checker rejects later type changes,
                # so the i64 alloca stays sound). Excludes cell_vars
                # (closure-captured vars stay boxed MVal*).
                is_int_local = (s.name not in ctx.cell_vars
                                and (self._te_is_int(s.ann)
                                     or self._is_int_expr(val_node, ctx)))
            # Float unboxing: `: Float` annotated locals use raw double
            # allocas. Like int, but for floating-point. Also infer from
            # float-valued initializer (e.g., `var x = 0.0`).
            # (Checker rejects later type changes, so the double alloca
            # stays sound.)
            is_float_local = (s.name not in ctx.cell_vars
                              and s.name not in ctx.string_builders
                              and (self._te_is_float(s.ann)
                                   or self._is_float_expr(val_node, ctx)))
            int_local = is_int_local
            if int_local:
                save = b.block
                b.position_at_start(ctx.fn.basic_blocks[0])
                a = b.alloca(I64, name=f"i_{s.name}")
                b.position_at_end(save)
                # In normal mode v may be boxed (MVal*); unbox it.
                if v.type != I64:
                    v = self._unbox_int(b, v, name=f"init_{s.name}")
                b.store(v, a)
                ctx.scope.declare(s.name, a)
                ctx.int_vars.add(s.name)
                ctx.raw_int_vars.add(s.name)
            elif is_float_local:
                # Float unboxing: raw double alloca
                save = b.block
                b.position_at_start(ctx.fn.basic_blocks[0])
                a = b.alloca(DBL, name=f"f_{s.name}")
                b.position_at_end(save)
                # Unbox if needed (v may be MVal*)
                if v.type != DBL:
                    v = self._unbox_float(b, v, name=f"init_{s.name}")
                b.store(v, a)
                ctx.scope.declare(s.name, a)
                ctx.raw_float_vars.add(s.name)
            else:
                a = self.alloca_var(ctx, s.name)
                if s.name in ctx.cell_vars:
                    # Phase 5a: heap cell (1-elem list) shared with closures.
                    cell = self.call(ctx, "misa_list_new", [], s,
                                     check=False)
                    # P-2: box raw i64 for the cell (cells hold MVal*).
                    v = self._ensure_mval(v, val_node, ctx)
                    self.call(ctx, "misa_list_push", [cell, v], s,
                              check=False)
                    b.store(cell, a)
                else:
                    b.store(v, a)
                ctx.scope.declare(s.name, a)
            # P-2: track provably-Int variables for unboxing. Explicit
            # `: Int` annotation, or `let` (immutable) with an int-valued
            # initializer. (`var` without annotation is only trusted via
            # annotation; later assignments re-evaluate below.)
            if self._te_is_int(s.ann):
                ctx.int_vars.add(s.name)
            elif not s.mutable and self._is_int_expr(val_node, ctx):
                ctx.int_vars.add(s.name)
            # Wave 2 (FFI): annotated FFI-scalar/struct locals, and
            # struct literals, get static FFI types (struct field
            # reads via GEP, vararg marshalling).
            self._track_ffi_var(ctx, s.name, s.ann)
            # Record field index optimization: track var type.
            # If ann is present, use it. Otherwise, infer from RHS:
            # - If RHS is Ident(var2) with known type, propagate.
            # - If RHS is Index(Ident(var2)) and var2: [T], then var: T.
            if s.ann is not None:
                ctx.var_types[s.name] = s.ann
            elif isinstance(val_node, A.Ident):
                src_type = ctx.var_types.get(val_node.name)
                if src_type is not None:
                    ctx.var_types[s.name] = src_type
            elif isinstance(val_node, A.Index):
                base = val_node.obj
                if isinstance(base, A.Ident):
                    arr_type = ctx.var_types.get(base.name)
                    # Extract element type from [T]
                    if arr_type is not None and hasattr(arr_type, 'name'):
                        if arr_type.name == 'List' and hasattr(arr_type, 'args'):
                            if arr_type.args:
                                ctx.var_types[s.name] = arr_type.args[0]
            if isinstance(val_node, A.StructLit) \
                    and val_node.name in self.extern_structs:
                ctx.ffi_types[s.name] = val_node.name
            elif isinstance(val_node, A.Call) \
                    and val_node.name in self.extern_decls:
                # `let q = get_point()` — struct-typed extern call.
                rdecl = self.extern_decls[val_node.name]
                if rdecl.ret is not None \
                        and rdecl.ret.name in self.extern_structs:
                    ctx.ffi_types[s.name] = rdecl.ret.name
        elif isinstance(s, A.AssignStmt):
            # String builder optimization: `s = s + x` where s is a
            # builder → `s.append(x)`
            tgt = s.target
            if (isinstance(tgt, A.Ident) and tgt.name in ctx.string_builders):
                v = s.value
                if (isinstance(v, A.BinOp) and v.op == "+" and
                    isinstance(v.left, A.Ident) and v.left.name == tgt.name):
                    # s = s + x → s.append(x)
                    builder_val = self.emit_expr(tgt, ctx, _no_builder_build=True)
                    x_val = self.emit_expr(v.right, ctx)
                    self.check(ctx, s)
                    x_val = self._ensure_mval(x_val, v.right, ctx)
                    line, col = self.loc(s)
                    self.call(ctx, "misa_builder_append",
                              [builder_val, x_val, line, col], s)
                    return
            v = self.emit_expr(s.value, ctx)
            self.check(ctx, s)
            tgt = s.target
            if isinstance(tgt, A.Ident):
                # Screen state: write through the cell (marks dirty).
                # In handlers the cell is resolved via the frame slot.
                if ctx.in_handler and tgt.name in ctx.screen_state_vars:
                    self.call(ctx, "misa_screen_state_write_slot",
                              [self.const_i32(
                                  ctx.screen_state_vars[tgt.name]), v],
                              s, check=False)
                elif tgt.name in ctx.state_vars:
                    a = ctx.scope.lookup(tgt.name)
                    cell = b.load(a, name=f"statecell_{tgt.name}")
                    self.call(ctx, "misa_state_set", [cell, v], s,
                              check=False)
                else:
                    a = ctx.scope.lookup(tgt.name)
                    if tgt.name in ctx.cell_vars:
                        # Phase 5a: store through the shared cell.
                        cell = b.load(a, name=f"cell_{tgt.name}")
                        line, col = self.loc(s)
                        zero = self.call(ctx, "misa_int",
                                         [self.const_i64(0)], s, check=False)
                        # P-2: box raw i64 for the cell (cells hold MVal*).
                        cv = self._ensure_mval(v, s.value, ctx)
                        self.call(ctx, "misa_index_set",
                                  [cell, zero, cv, line, col], s)
                    else:
                        # P-2: raw i64 vars store i64 directly; other
                        # targets need a boxed MVal*.
                        if tgt.name in ctx.raw_int_vars:
                            if v.type != I64:
                                v = self._unbox_int(
                                    b, v, name=f"assign_{tgt.name}")
                        elif tgt.name in ctx.raw_float_vars:
                            # Float unboxing: raw double vars store double directly.
                            if v.type != DBL:
                                v = self._unbox_float(
                                    b, v, name=f"assign_{tgt.name}")
                        else:
                            v = self._ensure_mval(v, s.value, ctx)
                        b.store(v, a)
                    # P-2: keep int_vars sound across assignments.
                    if self._is_int_expr(s.value, ctx):
                        ctx.int_vars.add(tgt.name)
                    else:
                        ctx.int_vars.discard(tgt.name)
                    # Wave 2 (FFI): assignments clear static FFI types
                    # (a struct literal RHS re-establishes it).
                    ctx.ffi_types.pop(tgt.name, None)
                    if isinstance(s.value, A.StructLit) \
                            and s.value.name in self.extern_structs:
                        ctx.ffi_types[tgt.name] = s.value.name
            elif isinstance(tgt, A.Field):
                obj = self.emit_expr(tgt.obj, ctx)
                self.check(ctx, s)
                # box the value if it's a raw int/bool
                bv = self._ensure_mval(v, s.value, ctx)
                line, col = self.loc(s)
                self.call(ctx, "misa_record_set",
                          [obj, self.cstr(tgt.field), bv, line, col], s)
            elif isinstance(tgt, A.Index):
                obj = self.emit_expr(tgt.obj, ctx)
                self.check(ctx, s)
                idx = self.emit_expr(tgt.idx, ctx)
                self.check(ctx, s)
                line, col = self.loc(s)
                # box the value
                bv = self._ensure_mval(v, s.value, ctx)
                # Fast path: raw i64 index avoids boxing
                if idx.type == I64:
                    self.call(ctx, "misa_list_set_raw",
                              [obj, idx, bv, line, col], s)
                else:
                    idx = self._ensure_mval(idx, tgt.idx, ctx)
                    self.call(ctx, "misa_index_set",
                              [obj, idx, bv, line, col], s)
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
                # P-2: box raw i64 returns in normal mode (int_mode
                # functions return i64 natively).
                if not ctx.int_mode:
                    v = self._ensure_mval(v, s.value, ctx)
                # Returning a Task transfers ownership to the caller:
                # detach it from this scope so scope_exit doesn't cancel it.
                if self._is_task_expr(s.value):
                    ctx.builder.call(self.rt["misa_scope_detach"], [v])
            else:
                v = self.get_nil(ctx)
            self.emit_ret(ctx, v)
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
        # P-2: unbox integer comparisons directly to i1 (skips the
        # misa_bool + misa_truthy round trip). Applies in both modes.
        if isinstance(s.cond, A.BinOp) \
                and s.cond.op in ("<", "<=", ">", ">=", "==", "!=") \
                and self._is_int_expr(s.cond.left, ctx) \
                and self._is_int_expr(s.cond.right, ctx):
            li = self._to_i64(s.cond.left, ctx)
            ri = self._to_i64(s.cond.right, ctx)
            cond = b.icmp_signed(s.cond.op, li, ri, name="c")
        else:
            c = self.emit_expr(s.cond, ctx)
            self.check(ctx, s)
            if c.type == I64:
                # int_mode: raw 0/1 boolean.
                cond = b.icmp_signed("!=", c, self.const_i64(0), name="c")
            else:
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
        # P-2: unbox integer comparisons directly to i1 (skips the
        # misa_bool + misa_truthy round trip). Applies in both modes.
        if isinstance(s.cond, A.BinOp) \
                and s.cond.op in ("<", "<=", ">", ">=", "==", "!=") \
                and self._is_int_expr(s.cond.left, ctx) \
                and self._is_int_expr(s.cond.right, ctx):
            li = self._to_i64(s.cond.left, ctx)
            ri = self._to_i64(s.cond.right, ctx)
            ok = b.icmp_signed(s.cond.op, li, ri, name="c")
        else:
            c = self.emit_expr(s.cond, ctx)
            self.check(ctx, s)
            if c.type == I64:
                ok = b.icmp_signed("!=", c, self.const_i64(0), name="c")
            else:
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
        # P-2: box raw i64 iterables.
        v = self._ensure_mval(v, s.iter, ctx)
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

    def emit_expr(self, e, ctx: FnCtx, _no_builder_build=False):
        b = ctx.builder
        # P-2 Level 2: in int_mode, Int-typed expressions produce raw i64.
        if ctx.int_mode and self._is_int_expr(e, ctx):
            return self._emit_int(e, ctx)
        # String builder optimization: loading a builder variable
        # inserts .build() automatically (unless suppressed).
        if (isinstance(e, A.Ident) and not _no_builder_build and
            e.name in ctx.string_builders):
            # Load the builder, then build it to Text
            a = ctx.scope.lookup(e.name)
            builder_val = b.load(a, name=f"l_{e.name}")
            line, col = self.loc(e)
            return self.call(ctx, "misa_builder_build",
                             [builder_val, line, col], e)
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
            # a value (checker rejects with E201 first); screen state()
            # bindings resolve via the runtime screen frame; anything
            # that is neither a lambda param, an id: binding, nor a
            # state binding is a closure over locals, which handlers do
            # not support (their stack frame is gone when they run).
            if ctx.in_handler:
                if ctx.id_slots and e.name in ctx.id_slots:
                    raise AOTError(
                        "E201",
                        f"component id '{e.name}' cannot be used as a "
                        f"value (use '{e.name}.text' to read a TextInput)",
                        e.line, e.col)
                if e.name in ctx.screen_state_vars:
                    slot = ctx.screen_state_vars[e.name]
                    return self.call(
                        ctx, "misa_screen_state_read_slot",
                        [self.const_i32(slot)], e, check=False)
                try:
                    a = ctx.scope.lookup(e.name)
                except AOTError:
                    raise AOTError(
                        "E600",
                        f"handler cannot capture '{e.name}': event "
                        f"handlers see only their params, screen "
                        f"id: bindings, and screen state() bindings "
                        f"(use 'let {e.name} = state(...)' for mutable "
                        f"screen state)",
                        e.line, e.col)
                return b.load(a, name=f"l_{e.name}")
            # Screen state: the alloca holds the cell; read through it.
            if e.name in ctx.state_vars:
                a = ctx.scope.lookup(e.name)
                cell = b.load(a, name=f"statecell_{e.name}")
                return self.call(ctx, "misa_state_get", [cell], e,
                                 check=False)
            try:
                a = ctx.scope.lookup(e.name)
            except AOTError:
                # Phase 5a: an `fn` declaration used as a value.
                if e.name in self.fns:
                    return self.emit_fn_value(e.name, e, ctx)
                raise
            v = b.load(a, name=f"l_{e.name}")
            # P-2: raw i64 locals stay unboxed (callers box via
            # _ensure_mval when a boxed MVal* is required).
            if e.name in ctx.raw_int_vars:
                return v
            # Float unboxing: raw double locals stay unboxed.
            if e.name in ctx.raw_float_vars:
                return v
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
            # Overtake Wave 2 (FFI): `p.x` on a statically-known
            # C-layout struct compiles to GEP + load (not a record
            # lookup). Anything else falls through to records.
            st_name = self._struct_type_of(e.obj, ctx)
            if st_name is not None:
                return self._emit_struct_field(e, st_name, ctx)
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
            # Fast path: if obj is Index(Ident(var)) and var has a
            # statically-known [RecordType], use misa_record_get_idx.
            idx = self._record_field_index(e.obj, e.field, ctx)
            obj = self.emit_expr(e.obj, ctx)
            self.check(ctx, e)
            line, col = self.loc(e)
            if idx is not None:
                return self.call(ctx, "misa_record_get_idx",
                                 [obj, self.const_i64(idx), line, col], e)
            return self.call(ctx, "misa_record_get",
                             [obj, self.cstr(e.field), line, col], e)
        if isinstance(e, A.Index):
            obj = self.emit_expr(e.obj, ctx)
            self.check(ctx, e)
            obj = self._ensure_mval(obj, e.obj, ctx)
            idx = self.emit_expr(e.idx, ctx)
            self.check(ctx, e)
            line, col = self.loc(e)
            # Fast path: raw i64 index avoids boxing — ONLY for lists.
            # For Text (and other types), use the generic path which
            # dispatches correctly. Using misa_list_get_raw on a Text
            # causes "index on non-list".
            if idx.type == I64 and self._is_list_expr(e.obj, ctx):
                return self.call(ctx, "misa_list_get_raw",
                                 [obj, idx, line, col], e)
            idx = self._ensure_mval(idx, e.idx, ctx)
            return self.call(ctx, "misa_index_get",
                             [obj, idx, line, col], e)
        if isinstance(e, A.ListLit):
            # Check if all elements are Bool literals -> use unboxed [Bool] list
            # (int unboxing reverted: caused regressions in json/richards via
            # mixed-type concat paths; bool unboxing is the proven win)
            all_bool = e.items and all(
                isinstance(it, A.BoolLit) for it in e.items
            )
            if all_bool:
                l = self.call(ctx, "misa_list_bool_new", [], e, check=False)
                for it in e.items:
                    v = 1 if it.value else 0
                    self.call(ctx, "misa_list_bool_push",
                              [l, self.const_i32(v)], e, check=False)
                return l
            l = self.call(ctx, "misa_list_new", [], e, check=False)
            for it in e.items:
                v = self.emit_expr(it, ctx)
                self.check(ctx, e)
                v = self._ensure_mval(v, it, ctx)
                self.call(ctx, "misa_list_push", [l, v], e, check=False)
            return l
        # Phase 8 (overtake): interface conversions (checker-inserted).
        if isinstance(e, A.IfaceWrap):
            return self.emit_iface_wrap(e, ctx)
        if isinstance(e, A.IfaceWrapList):
            return self.emit_iface_wrap_list(e, ctx)
        if isinstance(e, A.RecordLit):
            r = self.call(ctx, "misa_record_new", [], e, check=False)
            for fname, fv in e.fields:
                v = self.emit_expr(fv, ctx)
                self.check(ctx, e)
                v = self._ensure_mval(v, fv, ctx)
                self.call(ctx, "misa_record_set_cstr",
                          [r, self.cstr(fname), v], e, check=False)
            return r
        if isinstance(e, A.StructLit):
            # Overtake Wave 2 (FFI): `Point { x: 1.0, y: 2.0 }`.
            return self._emit_struct_lit(e, ctx)
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
                v = self._ensure_mval(v, ve, ctx)
                self.call(ctx, "misa_index_set",
                          [m, kv, v, line, col], e)
            return m
        if isinstance(e, A.BinOp):
            return self.emit_binop(e, ctx)
        if isinstance(e, A.Lambda):
            # Phase 5a: first-class lambdas become closure values
            # (record {__closure_fn, __closure_env}).
            return self.emit_closure(e, ctx)
        if isinstance(e, A.Spawn):
            # Phase 7: `spawn { ... }` — the body is compiled to a
            # separate misa_spawn_N(env) function; the runtime runs it
            # on a fiber and returns a Task<T> handle.
            return self.emit_spawn(e, ctx)
        if isinstance(e, A.Await):
            # Phase 7: `await t` / `await t timeout ms` on a Task<T>.
            # (check_no_streaming already rejected stream awaits.)
            return self.emit_await(e, ctx)
        if isinstance(e, A.Match):
            # Phase 5b: `match` expression over a sum value.
            return self.emit_match(e, ctx)
        if isinstance(e, A.UnOp):
            v = self.emit_expr(e.operand, ctx)
            self.check(ctx, e)
            if e.op == "not":
                v = self._ensure_mval(v, e.operand, ctx)
                return self.call(ctx, "misa_not", [v], e, check=False)
            if e.op == "-":
                line, col = self.loc(e)
                # P-2: raw i64 negation (no boxing).
                if v.type == I64:
                    return b.neg(v, name="ineg")
                # Float unboxing: raw double negation (no boxing).
                if v.type == DBL:
                    return b.fneg(v, name="fneg")
                v = self._ensure_mval(v, e.operand, ctx)
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
                # P-2: box raw i64 interpolations.
                pv = self._ensure_mval(pv, part, ctx)
            acc = self.call(ctx, "misa_text_concat", [acc, pv],
                            e, check=False)
        return acc

    # -- int unboxing (perf gap P-2) -------------------------------------
    # Type-directed unboxing: when the checker has proven both operands
    # are Int, emit raw i64 arithmetic instead of tagged helper calls.
    # Wrapping semantics are preserved (LLVM i64 ops wrap by definition).

    @staticmethod
    def _te_is_int(te) -> bool:
        """True if a TypeExpr is exactly `Int` (not optional/list/etc)."""
        return (te is not None and te.name == "Int" and not te.is_list
                and not te.is_map and not te.is_func
                and not te.optional and not te.type_args)

    @staticmethod
    def _te_is_float(te) -> bool:
        """True if a TypeExpr is exactly `Float` (not optional/list/etc)."""
        return (te is not None and te.name == "Float" and not te.is_list
                and not te.is_map and not te.is_func
                and not te.optional and not te.type_args)

    @staticmethod
    def _te_is_int_list(te) -> bool:
        """True if a TypeExpr is exactly `[Int]`."""
        return (te is not None and te.name == "Int" and te.is_list
                and not te.is_map and not te.is_func
                and not te.optional and not te.type_args)

    def _is_int_expr(self, e, ctx: FnCtx) -> bool:
        """Conservative provability: is e definitely an Int value?"""
        if isinstance(e, A.IntLit):
            return True
        if isinstance(e, A.Ident):
            return e.name in ctx.int_vars
        if isinstance(e, A.BinOp):
            if e.op in ("+", "-", "*", "/", "%"):
                return (self._is_int_expr(e.left, ctx)
                        and self._is_int_expr(e.right, ctx))
            return False
        if isinstance(e, A.UnOp):
            return e.op == "-" and self._is_int_expr(e.operand, ctx)
        if isinstance(e, A.Call):
            # `int(x)` / `len(x)` builtins return Int.
            if e.name in ("int", "len") and e.callee is None:
                return True
            # Direct call to a known Int->Int function.
            if e.callee is None and e.name in self.fn_decls:
                decl = self.fn_decls[e.name]
                if (not decl.type_params and self._te_is_int(decl.ret)
                        and all(self._te_is_int(p[1])
                                for p in decl.params)):
                    return True
            return False
        return False

    def _is_list_expr(self, e, ctx: FnCtx) -> bool:
        """Conservative: is e definitely a List? Used for index fast path."""
        # TODO: track list-typed variables. For now, only ListLit is certain.
        # This disables the misa_list_get_raw fast path for variables,
        # fixing the Text indexing bug (was incorrectly using list fast path).
        if isinstance(e, A.ListLit):
            return True
        return False

    def _is_float_expr(self, e, ctx: FnCtx) -> bool:
        """Conservative provability: is e definitely a Float value?"""
        if isinstance(e, A.FloatLit):
            return True
        if isinstance(e, A.Ident):
            return e.name in ctx.raw_float_vars
        if isinstance(e, A.BinOp):
            if e.op in ("+", "-", "*", "/"):
                return (self._is_float_expr(e.left, ctx)
                        and self._is_float_expr(e.right, ctx))
            return False
        return False

    def _to_f64(self, e, ctx: FnCtx):
        """Emit e and produce its double value (unboxing if boxed)."""
        if isinstance(e, A.FloatLit):
            return ir.Constant(DBL, e.value)
        v = self.emit_expr(e, ctx)
        if v.type == DBL:
            return v
        b = ctx.builder
        return self._unbox_float(b, v, name="tof64")

    def _unbox_int(self, b, mval, name="unbox"):
        """Unchecked i64 extraction from an MVal* known to be Int."""
        p = b.bitcast(mval, _MVAL_T.as_pointer())
        ptr = b.gep(p, [self.const_i32(0), self.const_i32(2)],
                    name=f"{name}.ptr")
        return b.load(ptr, name=name)

    def _unbox_float(self, b, mval, name="unbox"):
        """Unchecked double extraction from an MVal* known to be Float."""
        # MVal layout: tag (i8), pad[7], union { ... double f; ... }
        # The double is at the same offset as i64 in the union.
        p = b.bitcast(mval, _MVAL_T.as_pointer())
        ptr = b.gep(p, [self.const_i32(0), self.const_i32(2)],
                    name=f"{name}.ptr")
        # Bitcast the i64* to double* and load
        dptr = b.bitcast(ptr, DBL.as_pointer(), name=f"{name}.dptr")
        return b.load(dptr, name=name)

    def _to_i64(self, e, ctx: FnCtx):
        """Emit e and produce its i64 value (unboxing if boxed)."""
        # Fast path: integer literals need no boxing at all.
        if isinstance(e, A.IntLit):
            return self.const_i64(e.value)
        v = self.emit_expr(e, ctx)
        if v.type == I64:
            return v
        return self._unbox_int(ctx.builder, v)

    def _ensure_mval(self, v, e, ctx: FnCtx):
        """Box raw i64/i1/double to MVal* when a boxed value is required."""
        if v.type == I64:
            return self.call(ctx, "misa_int", [v], e, check=False)
        if v.type == I1:
            # booleans are i1; extend to i32 for misa_bool
            b = ctx.builder
            ext = b.zext(v, ir.IntType(32), name="_bext")
            return self.call(ctx, "misa_bool", [ext], e, check=False)
        if v.type == DBL:
            return self.call(ctx, "misa_float", [v], e, check=False)
        return v

    # -- Level 2: raw i64 function specialization ----------------------
    # For `fn f(a: Int, b: Int) -> Int` with a restricted body, emit
    # `misa_fn_f__int(i64, i64) -> i64` alongside the normal MVal*
    # version. Recursive calls and int arithmetic stay unboxed end to
    # end (no GC allocation in the hot path).

    _INT_SIMPLE_STMTS = (A.LetStmt, A.AssignStmt, A.IfStmt, A.WhileStmt,
                         A.ReturnStmt, A.ExprStmt)

    def _is_int_fn(self, decl) -> bool:
        """Can this function get a raw i64 specialization?"""
        if decl.type_params:
            return False
        if not self._te_is_int(decl.ret):
            return False
        if not decl.params:
            return False
        if not all(self._te_is_int(p[1]) for p in decl.params):
            return False
        return self._is_simple_int_body(decl.body)

    def _is_simple_int_body(self, stmts) -> bool:
        """Restricted subset: no lambdas, no try/for/match/spawn."""
        for s in stmts:
            if not isinstance(s, self._INT_SIMPLE_STMTS):
                return False
            if self._subtree_has_lambda(s):
                return False
            # Also reject spawn/await/try expressions conservatively by
            # checking statement kinds already covered; expressions are
            # validated during emission (fallback to tagged on unknown).
            if isinstance(s, A.IfStmt):
                if not (self._is_simple_int_body(s.then)
                        and self._is_simple_int_body(s.els)):
                    return False
            elif isinstance(s, A.WhileStmt):
                if not self._is_simple_int_body(s.body):
                    return False
        return True

    def _emit_int(self, e, ctx: FnCtx):
        """Emit an Int-typed expression as raw i64 (int_mode only).

        Caller must have verified `self._is_int_expr(e, ctx)`.
        Comparisons produce i64 0/1 for uniformity.
        """
        b = ctx.builder
        if isinstance(e, A.IntLit):
            return self.const_i64(e.value)
        if isinstance(e, A.Ident):
            a = ctx.scope.lookup(e.name)
            return b.load(a, name=f"i_{e.name}")
        if isinstance(e, A.BinOp):
            if e.op in ("+", "-", "*"):
                l = self._emit_int(e.left, ctx)
                r = self._emit_int(e.right, ctx)
                return {"+": b.add, "-": b.sub, "*": b.mul}[e.op](l, r,
                                                                 name="iop")
            if e.op in ("<", "<=", ">", ">=", "==", "!="):
                l = self._emit_int(e.left, ctx)
                r = self._emit_int(e.right, ctx)
                c = b.icmp_signed(e.op, l, r, name="icmp")
                return b.zext(c, I64, name="bext")
            if e.op in ("/", "%"):
                l = self._emit_int(e.left, ctx)
                r = self._emit_int(e.right, ctx)
                # E300 on division by zero (matches misa_div/misa_mod).
                is_zero = b.icmp_signed(
                    "==", r, self.const_i64(0), name="zchk")
                z_b = ctx.fn.append_basic_block("div.z")
                c_b = ctx.fn.append_basic_block("div.c")
                b.cbranch(is_zero, z_b, c_b)
                b.position_at_end(z_b)
                line, col = self.loc(e)
                msg = ("division by zero" if e.op == "/"
                       else "integer modulo by zero")
                b.call(self.rt["misa_raise"],
                       [self.cstr("E300"), self.cstr(msg), line, col])
                # misa_raise always sets the pending error: branch
                # directly to the handler. (check() would emit a dead,
                # unterminated "cont" block here — invalid IR.)
                b.branch(ctx.err_stack[-1])
                b.position_at_end(c_b)
                return (b.sdiv if e.op == "/" else b.srem)(
                    l, r, name="iop")
        if isinstance(e, A.UnOp) and e.op == "-":
            v = self._emit_int(e.operand, ctx)
            return b.neg(v, name="ineg")
        if isinstance(e, A.Call) and e.callee is None:
            # Raw call to an int-function specialization.
            if e.name in self.int_fns and all(
                    self._is_int_expr(a, ctx) for a in e.args):
                args = [self._emit_int(a, ctx) for a in e.args]
                return b.call(self.int_fns[e.name], args,
                              name=f"_raw_{e.name}")
            # `int(x)` / `len(x)` on int values: identity/unbox.
            if e.name in ("int", "len") and len(e.args) == 1 \
                    and self._is_int_expr(e.args[0], ctx):
                return self._emit_int(e.args[0], ctx)
        # Fallback (should be unreachable for well-typed bodies):
        # emit boxed with the fast path disabled, then unbox.
        ctx.int_mode = False
        try:
            v = self.emit_expr(e, ctx)
        finally:
            ctx.int_mode = True
        self.check(ctx, e)
        return self._unbox_int(b, v)

    def emit_binop(self, e: A.BinOp, ctx: FnCtx):
        b = ctx.builder
        op = e.op
        if op == "and":
            return self.emit_shortcircuit(e, ctx, is_and=True)
        if op == "or":
            return self.emit_shortcircuit(e, ctx, is_and=False)
        # P-2 (Level 1): both operands provably Int -> raw i64 arithmetic.
        # Types are checker-proven, so the tagged helpers' E200 paths are
        # unreachable; wrapping is preserved (LLVM i64 wraps).
        # (Checked before emitting operands: each side is emitted once.)
        # Returns raw i64; callers box via _ensure_mval when MVal* is needed.
        if op in ("+", "-", "*") and self._is_int_expr(e.left, ctx) \
                and self._is_int_expr(e.right, ctx):
            li = self._to_i64(e.left, ctx)
            self.check(ctx, e)
            ri = self._to_i64(e.right, ctx)
            self.check(ctx, e)
            return {"+": b.add, "-": b.sub, "*": b.mul}[op](li, ri,
                                                          name="iop")
        # Float unboxing: both operands are raw doubles -> direct LLVM
        # fadd/fsub/fmul/fdiv. Returns raw double; callers box via
        # _ensure_mval when MVal* is needed.
        if op in ("+", "-", "*", "/") and self._is_float_expr(e.left, ctx) \
                and self._is_float_expr(e.right, ctx):
            lf = self._to_f64(e.left, ctx)
            self.check(ctx, e)
            rf = self._to_f64(e.right, ctx)
            self.check(ctx, e)
            return {"+": b.fadd, "-": b.fsub, "*": b.fmul, "/": b.fdiv}[op](
                lf, rf, name="fop")
        # Bitwise ops on raw i64 (both operands provably Int)
        if op in ("&", "|", "^", "<<", ">>") \
                and self._is_int_expr(e.left, ctx) \
                and self._is_int_expr(e.right, ctx):
            li = self._to_i64(e.left, ctx)
            self.check(ctx, e)
            ri = self._to_i64(e.right, ctx)
            self.check(ctx, e)
            if op == "&":
                return b.and_(li, ri, name="band")
            if op == "|":
                return b.or_(li, ri, name="bor")
            if op == "^":
                return b.xor(li, ri, name="bxor")
            if op == "<<":
                return b.shl(li, ri, name="shl")
            if op == ">>":
                return b.ashr(li, ri, name="ashr")
        if op in ("<", "<=", ">", ">=", "==", "!=") \
                and self._is_int_expr(e.left, ctx) \
                and self._is_int_expr(e.right, ctx):
            li = self._to_i64(e.left, ctx)
            self.check(ctx, e)
            ri = self._to_i64(e.right, ctx)
            self.check(ctx, e)
            pred = {"<": "<", "<=": "<=", ">": ">", ">=": ">=",
                    "==": "==", "!=": "!="}[op]  # llvmlite 0.50: no s-prefix
            c = b.icmp_signed(pred, li, ri, name="icmp")
            return self.call(ctx, "misa_bool",
                             [b.zext(c, I32, name="bext")], e, check=False)
        l = self.emit_expr(e.left, ctx)
        self.check(ctx, e)
        r = self.emit_expr(e.right, ctx)
        self.check(ctx, e)
        line, col = self.loc(e)
        # P-2: box raw i64 operands for the tagged helpers.
        l = self._ensure_mval(l, e.left, ctx)
        r = self._ensure_mval(r, e.right, ctx)
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
        # Screen state: `state(initial)` allocates a per-screen cell.
        # The checker stamps the slot and guarantees screen context.
        if name == "state":
            slot = getattr(e, "state_slot", None)
            if slot is None:
                raise AOTError(
                    "E600", "state() is only usable in a screen body",
                    e.line, e.col)
            init = self.emit_expr(e.args[0], ctx)
            self.check(ctx, e)
            return self.call(ctx, "misa_screen_state_cell",
                             [self.const_i32(slot), init], e,
                             check=False)
        # Phase 5c: generic fns go through monomorphization.
        if name in self.fn_decls and self.fn_decls[name].type_params:
            return self.emit_mono_call(name, self.fn_decls[name], e,
                                       ctx)
        # Phase 8 (overtake): interface method calls.
        if name.startswith("iface.") and name.count(".") == 2:
            _p, iname, mname = name.split(".")
            return self.emit_iface_call(iname, mname, e, ctx)
        if name.startswith("impl.") and name.count(".") == 3:
            _p, iname, tgt, mname = name.split(".")
            return self.emit_impl_call(iname, tgt, mname, e, ctx)
        if name in self.fns:
            decl = self.fn_decls[name]
            if len(e.args) != len(decl.params):
                raise AOTError("E600",
                               f"fn '{name}' takes {len(decl.params)} "
                               f"args, got {len(e.args)}", e.line, e.col)
            # P-2 Level 2: in int_mode, call the raw specialization
            # directly when all args are int-valued (no boxing).
            if ctx.int_mode and name in self.int_fns and all(
                    self._is_int_expr(a, ctx) for a in e.args):
                raw_args = [self._emit_int(a, ctx) for a in e.args]
                return ctx.builder.call(
                    self.int_fns[name], raw_args, name=f"_raw_{name}")
            args = []
            for a in e.args:
                v = self.emit_expr(a, ctx)
                # P-2: in int_mode, box raw i64 args for MVal* callees.
                v = self._ensure_mval(v, a, ctx)
                self.check(ctx, e)
                args.append(v)
            return self.call(ctx, f"misa_fn_{name}", args, e,
                             check=True, direct=True)
        # Phase 7: extern (FFI) function calls. Overtake Wave 2:
        # varargs functions take at least the fixed prefix.
        if name in self.extern_decls:
            decl = self.extern_decls[name]
            n_fixed = len(decl.params)
            ok = (len(e.args) >= n_fixed) if decl.varargs \
                else (len(e.args) == n_fixed)
            if not ok:
                want = (f"at least {n_fixed}" if decl.varargs
                        else str(n_fixed))
                raise AOTError("E600",
                               f"extern fn '{name}' takes "
                               f"{want} args, got "
                               f"{len(e.args)}", e.line, e.col)
            return self.emit_extern_call(e, ctx, name)
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
            # P-2: box raw i64 args for builtins (they take MVal*).
            return self._ensure_mval(v, e.args[i], ctx)

        if name == "print":
            return self.call(ctx, "misa_print", [arg(0)], e)
        if name == "len":
            return self.call(ctx, "misa_builtin_len", [arg(0), line, col],
                             e)
        if name == "range":
            # range(n) / range(a, b) / range(a, b, step)
            nargs = len(e.args)
            a0 = arg(0)
            # Use nil placeholder for missing args
            nil = self.get_nil(ctx)
            a1 = arg(1) if nargs > 1 else nil
            a2 = arg(2) if nargs > 2 else nil
            return self.call(ctx, "misa_builtin_range",
                             [a0, a1, a2,
                              self.const_i32(1 if nargs > 1 else 0),
                              self.const_i32(1 if nargs > 2 else 0)],
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
        if name in ("assert_eq", "assert_ne"):
            return self.call(ctx, "misa_builtin_" + name,
                             [arg(0), arg(1), line, col], e)
        if name == "argv":
            return self.call(ctx, "misa_builtin_argv", [], e)
        if name == "env":
            v = arg(0)
            z = self.call(ctx, "misa_text_z", [v], e)
            return self.call(ctx, "misa_builtin_env", [z, line, col], e)
        if name == "fs.read":
            return self.call(ctx, "misa_fs_read", [arg(0), line, col], e)
        # Bonus (audit): process.run has no AOT lowering — subprocess in
        # compiled binaries is out of scope; the self-hosted build driver
        # runs on the interpreter. E600 (not E101) is the contract for
        # unsupported-on-target.
        if name == "process.run":
            raise AOTError("E600",
                           "process.run is not supported in AOT-compiled "
                           "binaries (use the interpreter)",
                           e.line, e.col)
        if name == "fs.write":
            return self.call(ctx, "misa_fs_write",
                             [arg(0), arg(1), line, col], e)
        if name == "http.get":
            return self.call(ctx, "misa_http_get", [arg(0), line, col],
                             e)
        # Phase 7 (concurrency): tasks, channels, sleep, fetch_all.
        # `chan.send`/`chan.recv` are the checker's canonical rewrites
        # of `ch.send(v)` / `ch.recv()`.
        if name == "sleep":
            ms = self.call(ctx, "misa_as_int", [arg(0)], e)
            return self.call(ctx, "misa_task_sleep", [ms, line, col], e)
        if name == "chan":
            return self.call(ctx, "misa_chan_new", [line, col], e)
        if name == "chan.send":
            return self.call(ctx, "misa_chan_send",
                             [arg(0), arg(1), line, col], e)
        if name == "chan.recv":
            return self.call(ctx, "misa_chan_recv", [arg(0), line, col],
                             e)
        if name == "close":
            return self.call(ctx, "misa_chan_close",
                             [arg(0), line, col], e)
        if name == "fetch_all":
            return self.call(ctx, "misa_fetch_all", [arg(0), line, col],
                             e)
        # Wave 3 (networking): TCP sockets + HTTP server. `net.accept`
        # / `net.port` / `listener.close` are the checker's canonical
        # rewrites of `lis.accept()` / `lis.port()` / `lis.close()`;
        # `socket.*` likewise for `sock.send()` etc.
        if name == "net.connect":
            return self.call(ctx, "misa_net_connect",
                             [arg(0), arg(1), line, col], e)
        if name == "net.listen":
            return self.call(ctx, "misa_net_listen",
                             [arg(0), line, col], e)
        if name == "net.accept":
            return self.call(ctx, "misa_net_accept",
                             [arg(0), line, col], e)
        if name == "net.port":
            return self.call(ctx, "misa_net_port",
                             [arg(0), line, col], e)
        if name == "listener.close":
            return self.call(ctx, "misa_listener_close",
                             [arg(0), line, col], e)
        if name == "socket.send":
            return self.call(ctx, "misa_socket_send",
                             [arg(0), arg(1), line, col], e)
        if name == "socket.recv":
            return self.call(ctx, "misa_socket_recv",
                             [arg(0), arg(1), line, col], e)
        if name == "socket.close":
            return self.call(ctx, "misa_socket_close",
                             [arg(0), line, col], e)
        if name == "socket.set_timeout":
            return self.call(ctx, "misa_socket_set_timeout",
                             [arg(0), arg(1), line, col], e)
        if name == "http.serve":
            return self.call(ctx, "misa_http_serve",
                             [arg(0), arg(1), line, col], e)
        # Phase 5 (stdlib2): JSON, HTTP POST, time, math.
        if name == "json.parse":
            return self.call(ctx, "misa_json_parse",
                             [arg(0), line, col], e)
        if name == "json.stringify":
            return self.call(ctx, "misa_json_stringify",
                             [arg(0), line, col], e)
        if name == "http.post":
            ct = self.cstr("text/plain")
            return self.call(ctx, "misa_http_post",
                             [arg(0), arg(1), ct, line, col], e)
        if name == "http.post_json":
            body = self.call(ctx, "misa_json_stringify",
                             [arg(1), line, col], e)
            self.check(ctx, e)
            ct = self.cstr("application/json")
            return self.call(ctx, "misa_http_post",
                             [arg(0), body, ct, line, col], e)
        if name == "time.now":
            return self.call(ctx, "misa_time_now", [], e)
        if name == "time.now_ms":
            return self.call(ctx, "misa_time_now_ms", [], e)
        if name == "time.format":
            return self.call(ctx, "misa_time_format",
                             [arg(0), arg(1), line, col], e)
        if name == "time.parse":
            return self.call(ctx, "misa_time_parse",
                             [arg(0), arg(1), line, col], e)
        if name == "math.sqrt":
            return self.call(ctx, "misa_math_sqrt",
                             [arg(0), line, col], e)
        if name == "math.cos":
            return self.call(ctx, "misa_math_cos",
                             [arg(0), line, col], e)
        if name == "math.sin":
            return self.call(ctx, "misa_math_sin",
                             [arg(0), line, col], e)
        if name == "math.tan":
            return self.call(ctx, "misa_math_tan",
                             [arg(0), line, col], e)
        if name == "math.pow":
            return self.call(ctx, "misa_math_pow",
                             [arg(0), arg(1), line, col], e)
        if name == "math.abs":
            return self.call(ctx, "misa_math_abs",
                             [arg(0), line, col], e)
        if name == "math.floor":
            return self.call(ctx, "misa_math_floor",
                             [arg(0), line, col], e)
        if name == "math.ceil":
            return self.call(ctx, "misa_math_ceil",
                             [arg(0), line, col], e)
        if name == "math.round":
            return self.call(ctx, "misa_math_round",
                             [arg(0), line, col], e)
        if name == "str.slice":
            return self.call(ctx, "misa_str_slice",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "str.starts_with":
            return self.call(ctx, "misa_str_starts_with",
                             [arg(0), arg(1), line, col], e)
        if name == "str.split":
            return self.call(ctx, "misa_str_split",
                             [arg(0), arg(1), line, col], e)
        if name == "str.chars":
            return self.call(ctx, "misa_str_chars",
                             [arg(0), line, col], e)
        if name == "str.trim":
            return self.call(ctx, "misa_str_trim",
                             [arg(0), line, col], e)
        if name == "str.ord":
            return self.call(ctx, "misa_str_ord",
                             [arg(0), line, col], e)
        if name == "str.chr":
            return self.call(ctx, "misa_str_chr",
                             [arg(0), line, col], e)
        # Wave 3 (stdlib expansion).
        if name == "regex":
            return self.call(ctx, "misa_regex_new",
                             [arg(0), line, col], e)
        if name == "regex.matches":
            return self.call(ctx, "misa_regex_matches",
                             [arg(0), arg(1), line, col], e)
        if name == "regex.find":
            return self.call(ctx, "misa_regex_find",
                             [arg(0), arg(1), line, col], e)
        if name == "regex.find_all":
            return self.call(ctx, "misa_regex_find_all",
                             [arg(0), arg(1), line, col], e)
        if name == "regex.replace":
            return self.call(ctx, "misa_regex_replace",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "regex.captures":
            return self.call(ctx, "misa_regex_captures",
                             [arg(0), arg(1), line, col], e)
        if name == "crypto.sha256":
            return self.call(ctx, "misa_crypto_sha256",
                             [arg(0), line, col], e)
        if name == "crypto.sha1":
            return self.call(ctx, "misa_crypto_sha1",
                             [arg(0), line, col], e)
        if name == "crypto.md5":
            return self.call(ctx, "misa_crypto_md5",
                             [arg(0), line, col], e)
        if name == "crypto.hmac_sha256":
            return self.call(ctx, "misa_crypto_hmac_sha256",
                             [arg(0), arg(1), line, col], e)
        if name == "random.seed":
            return self.call(ctx, "misa_random_seed",
                             [arg(0), line, col], e)
        if name == "random.int":
            return self.call(ctx, "misa_random_int",
                             [arg(0), arg(1), line, col], e)
        if name == "random.float":
            return self.call(ctx, "misa_random_float", [], e,
                             check=False)
        if name == "random.choice":
            return self.call(ctx, "misa_random_choice",
                             [arg(0), line, col], e)
        if name == "random.shuffle":
            return self.call(ctx, "misa_random_shuffle",
                             [arg(0), line, col], e)
        if name == "time.now_utc":
            return self.call(ctx, "misa_time_now_utc", [], e,
                             check=False)
        if name == "time.to_utc":
            return self.call(ctx, "misa_time_to_utc",
                             [arg(0), line, col], e)
        if name == "time.from_parts":
            return self.call(ctx, "misa_time_from_parts",
                             [arg(0), arg(1), arg(2), arg(3), arg(4),
                              arg(5), line, col], e)
        if name == "time.add":
            return self.call(ctx, "misa_time_add",
                             [arg(0), arg(1), line, col], e)
        if name == "time.add_hours":
            return self.call(ctx, "misa_time_add_hours",
                             [arg(0), arg(1), line, col], e)
        if name == "time.in_tz":
            return self.call(ctx, "misa_time_in_tz",
                             [arg(0), arg(1), line, col], e)
        if name == "time.weekday":
            return self.call(ctx, "misa_time_weekday",
                             [arg(0), line, col], e)
        if name == "time.is_leap":
            return self.call(ctx, "misa_time_is_leap",
                             [arg(0), line, col], e)
        if name == "fs.temp_file":
            return self.call(ctx, "misa_fs_temp_file",
                             [line, col], e)
        if name == "fs.temp_dir":
            return self.call(ctx, "misa_fs_temp_dir",
                             [line, col], e)
        # Final wave (stdlib): compression.
        if name == "compress.gzip":
            return self.call(ctx, "misa_compress_gzip",
                             [arg(0), line, col], e)
        if name == "compress.gunzip":
            return self.call(ctx, "misa_compress_gunzip",
                             [arg(0), line, col], e)
        if name == "compress.gunzip_bytes":
            return self.call(ctx, "misa_compress_gunzip_bytes",
                             [arg(0), line, col], e)
        if name == "compress.gzip_file":
            return self.call(ctx, "misa_compress_gzip_file",
                             [arg(0), arg(1), line, col], e)
        if name == "compress.gunzip_file":
            return self.call(ctx, "misa_compress_gunzip_file",
                             [arg(0), arg(1), line, col], e)
        if name == "set":
            return self.call(ctx, "misa_set_new", [], e, check=False)
        if name == "set.add":
            self.call(ctx, "misa_set_add",
                      [arg(0), arg(1), line, col], e)
            return self.get_nil(ctx)
        if name == "set.contains":
            return self.call(ctx, "misa_set_contains",
                             [arg(0), arg(1), line, col], e)
        if name == "set.remove":
            self.call(ctx, "misa_set_remove",
                      [arg(0), arg(1), line, col], e)
            return self.get_nil(ctx)
        if name == "set.union":
            return self.call(ctx, "misa_set_union",
                             [arg(0), arg(1), line, col], e)
        if name == "set.intersect":
            return self.call(ctx, "misa_set_intersect",
                             [arg(0), arg(1), line, col], e)
        if name == "set.diff":
            return self.call(ctx, "misa_set_diff",
                             [arg(0), arg(1), line, col], e)
        if name == "set.to_list":
            return self.call(ctx, "misa_set_to_list",
                             [arg(0), line, col], e)
        if name == "set.len":
            # int64 return; box it.
            v = self.call(ctx, "misa_set_len",
                          [arg(0), line, col], e)
            return self.call(ctx, "misa_int", [v], e, check=False)
        # cli: declarative args parsing.
        if name == "cli.parser":
            return self.call(ctx, "misa_cli_parser",
                             [arg(0), arg(1), line, col], e)
        if name == "cli.flag":
            # Optional 5th arg (default); NULL when absent.
            defv = arg(4) if len(e.args) > 4 else NULL_PTR
            has_def = self.const_i32(1) if len(e.args) > 4 \
                else self.const_i32(0)
            self.call(ctx, "misa_cli_flag",
                      [arg(0), arg(1), arg(2), arg(3),
                       has_def, defv, line, col], e)
            return self.get_nil(ctx)
        if name == "cli.bool_flag":
            self.call(ctx, "misa_cli_bool_flag",
                      [arg(0), arg(1), arg(2), line, col], e)
            return self.get_nil(ctx)
        if name == "cli.short":
            self.call(ctx, "misa_cli_short",
                      [arg(0), arg(1), arg(2), line, col], e)
            return self.get_nil(ctx)
        if name == "cli.command":
            self.call(ctx, "misa_cli_command",
                      [arg(0), arg(1), arg(2), line, col], e)
            return self.get_nil(ctx)
        if name == "cli.parse":
            return self.call(ctx, "misa_cli_parse",
                             [arg(0), arg(1), line, col], e)
        if name == "cli.get":
            return self.call(ctx, "misa_cli_get",
                             [arg(0), arg(1), line, col], e)
        if name == "cli.get_bool":
            return self.call(ctx, "misa_cli_get_bool",
                             [arg(0), arg(1), line, col], e)
        if name == "cli.get_command":
            return self.call(ctx, "misa_cli_get_command",
                             [arg(0), line, col], e)
        if name == "cli.positionals":
            return self.call(ctx, "misa_cli_positionals",
                             [arg(0), line, col], e)
        if name == "map":
            return self.call(ctx, "misa_list_map",
                             [arg(0), arg(1), line, col], e)
        if name == "filter":
            return self.call(ctx, "misa_list_filter",
                             [arg(0), arg(1), line, col], e)
        if name == "fold":
            return self.call(ctx, "misa_list_fold",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "sort_by":
            return self.call(ctx, "misa_list_sort_by",
                             [arg(0), arg(1), line, col], e)
        if name == "list.flat":
            return self.call(ctx, "misa_list_flat",
                             [arg(0), line, col], e)
        if name == "list.zip":
            return self.call(ctx, "misa_list_zip",
                             [arg(0), arg(1), line, col], e)
        if name == "list.unique":
            return self.call(ctx, "misa_list_unique",
                             [arg(0), line, col], e)
        if name == "str.join":
            return self.call(ctx, "misa_str_join",
                             [arg(0), arg(1), line, col], e)
        if name == "str.replace":
            return self.call(ctx, "misa_str_replace",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "str.to_upper":
            return self.call(ctx, "misa_str_to_upper",
                             [arg(0), line, col], e)
        if name == "str.to_lower":
            return self.call(ctx, "misa_str_to_lower",
                             [arg(0), line, col], e)
        if name == "str.ends_with":
            return self.call(ctx, "misa_str_ends_with",
                             [arg(0), arg(1), line, col], e)
        if name == "str.pad_left":
            return self.call(ctx, "misa_str_pad_left",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "str.pad_right":
            return self.call(ctx, "misa_str_pad_right",
                             [arg(0), arg(1), arg(2), line, col], e)
        # Phase 5 (DB): SQLite bindings. The optional `args` list
        # defaults to an empty list.
        if name == "db.open":
            return self.call(ctx, "misa_db_open", [arg(0), line, col], e)
        if name == "builder":
            return self.call(ctx, "misa_builder_new", [], e, check=False)
        if name == "builder.append":
            return self.call(ctx, "misa_builder_append",
                             [arg(0), arg(1), line, col], e)
        if name == "builder.build":
            return self.call(ctx, "misa_builder_build",
                             [arg(0), line, col], e)
        if name == "db.exec":
            alist = arg(2) if len(e.args) > 2 else self.call(
                ctx, "misa_list_new", [], e, check=False)
            return self.call(ctx, "misa_db_exec",
                             [arg(0), arg(1), alist, line, col], e)
        if name == "db.exec_many":
            return self.call(ctx, "misa_db_exec_many",
                             [arg(0), arg(1), arg(2), line, col], e)
        if name == "db.begin":
            return self.call(ctx, "misa_db_begin",
                             [arg(0), line, col], e)
        if name == "db.commit":
            return self.call(ctx, "misa_db_commit",
                             [arg(0), line, col], e)
        if name == "db.rollback":
            return self.call(ctx, "misa_db_rollback",
                             [arg(0), line, col], e)
        if name == "db.query":
            alist = arg(2) if len(e.args) > 2 else self.call(
                ctx, "misa_list_new", [], e, check=False)
            return self.call(ctx, "misa_db_query",
                             [arg(0), arg(1), alist, line, col], e)
        if name == "db.migrate":
            return self.call(ctx, "misa_db_migrate",
                             [arg(0), arg(1), line, col], e)
        if name == "db.explain":
            alist = arg(2) if len(e.args) > 2 else self.call(
                ctx, "misa_list_new", [], e, check=False)
            return self.call(ctx, "misa_db_explain",
                             [arg(0), arg(1), alist, line, col], e)
        if name == "db.close":
            return self.call(ctx, "misa_db_close", [arg(0), line, col],
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
        self.emit_ret(ctx, r)
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
            # P-2: box raw i64 captures.
            if name in ctx.raw_int_vars:
                v = self.call(ctx, "misa_int", [v], node, check=False)
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
            self.emit_ret(lctx, v)
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

    # -- Phase 7: concurrency ----------------------------------------

    def emit_spawn(self, e: A.Spawn, ctx: FnCtx):
        """Compile `spawn { ... }` to misa_spawn_N(env) and spawn it.

        Capture mirrors emit_closure: the enclosing scope chain is
        packed into an env record (heap cells shared, values copied),
        rebound as locals in the new function. The body runs on a
        fiber; `return` in the body completes the Task<T>.
        """
        b = ctx.builder
        node = e
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
        # 2. build the env record.
        env = self.call(ctx, "misa_record_new", [], node, check=False)
        for name, _ in names:
            a = ctx.scope.lookup(name)
            v = b.load(a, name=f"cap_{name}")
            self.call(ctx, "misa_record_set_cstr",
                      [env, self.cstr(name), v], node, check=False)
        # 3. emit the body as misa_spawn_N(env).
        fn = ir.Function(self.mod, ir.FunctionType(PTR, [PTR]),
                         name=f"misa_spawn_{self.spawn_counter}")
        self.spawn_counter += 1
        sctx = self.new_fn_ctx(fn)  # scope_enter hook included
        sb = sctx.builder
        self.get_nil(sctx)
        for name, is_cell in names:
            if is_cell:
                sctx.cell_vars.add(name)
        senv = fn.args[0]
        senv.name = "env"
        line, col = self.loc(node)
        for name, _ in names:
            v = self.call(sctx, "misa_record_get",
                          [senv, self.cstr(name), line, col],
                          node, check=False)
            a = self.alloca_var(sctx, f"cap_{name}")
            sb.store(v, a)
            sctx.scope.declare(name, a)
        # a nested spawn inside this body shares `var`s the same way.
        sctx.cell_vars |= self._cell_vars_for(e.body)
        self.emit_block(e.body, sctx)
        self.finish_fn_ctx(sctx)  # scope_exit on every path + nil default
        # 4. spawn: the runtime takes the function address.
        fn_ptr = b.bitcast(fn, PTR, name="spawn_fn")
        return self.call(ctx, "misa_task_spawn",
                         [fn_ptr, env, line, col], e)

    def emit_await(self, e: A.Await, ctx: FnCtx):
        """`await t` / `await t timeout ms` — yields to the scheduler."""
        v = self.emit_expr(e.operand, ctx)
        self.check(ctx, e)
        line, col = self.loc(e)
        if e.timeout is not None:
            ms = self.emit_expr(e.timeout, ctx)
            self.check(ctx, e)
            ms_i64 = self.call(ctx, "misa_as_int", [ms], e)
            return self.call(ctx, "misa_task_await_timeout",
                             [v, ms_i64, line, col], e)
        return self.call(ctx, "misa_task_await", [v, line, col], e)

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
            # P-2: box raw i64 args (closure frames hold MVal*).
            v = self._ensure_mval(v, a, ctx)
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
        self.emit_ret(ctx, r)
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

    # -- Phase 8 (overtake): interfaces -------------------------------
    #
    # An interface value is an MVal* record:
    #   {__tag: Int(impl index), __fields: [payload], __iface_disp: Text}.
    # Method calls compile to an LLVM switch on __tag (closed world:
    # every impl is known at compile time).

    @staticmethod
    def _impl_sym(iface: str, target: str, method: str) -> str:
        return f"misa_impl_{iface}_{target}_{method}"

    def _iface_impls(self, iface: str):
        """[(tag, target)] for an interface, in tag order."""
        items = [(tag, tgt) for (i, tgt), tag in self.impl_index.items()
                 if i == iface]
        items.sort()
        return items

    def declare_impl_fns(self):
        """Declare LLVM functions for every impl method."""
        for impl in self.impl_decls:
            for m in impl.methods:
                sym = self._impl_sym(impl.iface, impl.target, m.name)
                fty = ir.FunctionType(PTR, [PTR] * len(m.params))
                self.impl_fns[(impl.iface, impl.target,
                               m.name)] = ir.Function(self.mod, fty,
                                                      name=sym)

    def emit_impl_bodies(self):
        """Emit bodies for every impl method."""
        for impl in self.impl_decls:
            for m in impl.methods:
                fn = self.impl_fns[(impl.iface, impl.target, m.name)]
                self.emit_fn_body_direct(fn, m)

    def emit_iface_wrap(self, e, ctx):
        """Build the {__tag, __fields, __iface_disp} record for an
        interface value."""
        b = ctx.builder
        tag = self.impl_index.get((e.iface, e.concrete))
        if tag is None:
            raise AOTError("E600",
                           f"no impl of '{e.iface}' for '{e.concrete}'",
                           e.line, e.col)
        payload = self.emit_expr(e.expr, ctx)
        self.check(ctx, e)
        # P-2: box raw i64 payloads (interface records hold MVal*).
        payload = self._ensure_mval(payload, e.expr, ctx)
        r = self.call(ctx, "misa_record_new", [], e, check=False)
        tagv = self.call(ctx, "misa_int", [self.const_i64(tag)], e,
                         check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr(self.SUM_TAG), tagv], e, check=False)
        l = self.call(ctx, "misa_list_new", [], e, check=False)
        self.call(ctx, "misa_list_push", [l, payload], e, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr(self.SUM_FIELDS), l], e, check=False)
        # Display hint so print/str renders the payload (mirrors
        # __sum_disp handling in the C renderer).
        disp = self.call(ctx, "misa_text_cstr",
                         [self.cstr("__iface_payload")], e, check=False)
        self.call(ctx, "misa_record_set_cstr",
                  [r, self.cstr("__iface_disp"), disp], e, check=False)
        return r

    def emit_iface_wrap_list(self, e, ctx):
        """Element-wise [T] -> [I] conversion via a runtime helper."""
        tag = self.impl_index.get((e.iface, e.concrete))
        if tag is None:
            raise AOTError("E600",
                           f"no impl of '{e.iface}' for '{e.concrete}'",
                           e.line, e.col)
        lv = self.emit_expr(e.expr, ctx)
        self.check(ctx, e)
        return self.call(ctx, "misa_iface_wrap_list",
                         [self.const_i64(tag), lv], e, check=False)

    def emit_iface_call(self, iface: str, method: str, e, ctx):
        """Dynamic dispatch: switch on the interface value's __tag."""
        b = ctx.builder
        line, col = self.loc(e)
        recv = self.emit_expr(e.args[0], ctx)
        self.check(ctx, e)
        tag_val = self.call(ctx, "misa_record_get",
                            [recv, self.cstr(self.SUM_TAG), line, col],
                            e)
        tag = self.call(ctx, "misa_as_int", [tag_val], e, check=False)
        fields = self.call(ctx, "misa_record_get",
                           [recv, self.cstr(self.SUM_FIELDS), line, col],
                           e)
        payload = self.call(ctx, "misa_list_get",
                            [fields, self.const_i64(0)], e, check=False)
        rest = []
        for a in e.args[1:]:
            v = self.emit_expr(a, ctx)
            self.check(ctx, e)
            rest.append(v)
        impls = self._iface_impls(iface)
        if not impls:
            raise AOTError("E600",
                           f"interface '{iface}' has no implementations",
                           e.line, e.col)
        # The call returns a value; use a phi over the case blocks.
        end_b = ctx.fn.append_basic_block("iface.end")
        case_bs = [ctx.fn.append_basic_block(f"iface.case{i}")
                   for i, _ in enumerate(impls)]
        default_b = ctx.fn.append_basic_block("iface.unreachable")
        sw = b.switch(tag, default_b)
        for (tg, tgt), cb in zip(impls, case_bs):
            sw.add_case(self.const_i64(tg), cb)
        b.position_at_end(default_b)
        b.unreachable()
        incomings = []
        for (tg, tgt), cb in zip(impls, case_bs):
            b.position_at_end(cb)
            fn = self.impl_fns.get((iface, tgt, method))
            if fn is None:
                raise AOTError("E600",
                               f"no impl of '{iface}' for '{tgt}' "
                               f"providing '{method}'", e.line, e.col)
            cv = b.call(fn, [payload] + rest, name="_iface_call")
            self.check(ctx, e)
            cur = b.block
            b.branch(end_b)
            incomings.append((cv, cur))
        b.position_at_end(end_b)
        phi = b.phi(PTR, name="_iface_res")
        for cv, blk in incomings:
            phi.add_incoming(cv, blk)
        return phi

    def emit_impl_call(self, iface: str, target: str, method: str,
                       e, ctx):
        """Static dispatch: direct call to the impl method."""
        fn = self.impl_fns.get((iface, target, method))
        if fn is None:
            raise AOTError("E600",
                           f"no impl of '{iface}' for '{target}' "
                           f"providing '{method}'", e.line, e.col)
        args = []
        for a in e.args:
            v = self.emit_expr(a, ctx)
            self.check(ctx, e)
            args.append(v)
        return self.call(ctx, self._impl_sym(iface, target, method),
                         args, e, check=True, direct=True)

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
            # P-2: box raw i64 variant args (list payload holds MVal*).
            av = self._ensure_mval(av, a, ctx)
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
        # Phase 5d: `if` guards. When any arm has a guard, source order
        # matters in ways a tag switch cannot express (a guarded arm
        # may fail and fall through to a LATER arm), so emit a linear
        # chain of tag checks. The guardless fast path keeps the
        # switch below.
        if any(arm.guard is not None for arm in e.arms):
            return self._emit_match_guarded(e, ctx, vmap, tag, fields)
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

    def _emit_match_guarded(self, e: A.Match, ctx: FnCtx, vmap: dict,
                            tag, fields):
        """Phase 5d: linear match chain for guarded arms. Arm i is
        tried in source order: the tag must match (unless `_`), then
        the guard (if any) must be true; otherwise control falls to
        arm i+1. The checker guarantees exhaustiveness ignoring
        guards, so falling off the end is unreachable."""
        b = ctx.builder
        n = len(e.arms)
        arm_blocks = [ctx.fn.append_basic_block(f"match.garm{i}")
                      for i in range(n)]
        end_b = ctx.fn.append_basic_block("match.gend")
        dead_b = ctx.fn.append_basic_block("match.gunreachable")
        # Enter the chain: the entry block falls through to arm 0.
        if not b.block.is_terminated:
            b.branch(arm_blocks[0])
        incomings = []
        for i, arm in enumerate(e.arms):
            nxt = arm_blocks[i + 1] if i + 1 < n else dead_b
            b.position_at_end(arm_blocks[i])
            old_scope = ctx.scope
            # Tag check (wildcards skip it).
            if not arm.wildcard:
                want = self.const_i64(vmap[arm.variant])
                is_tag = b.icmp_unsigned("==", tag, want,
                                         name="matchtag")
                tag_ok = ctx.fn.append_basic_block(f"match.gtag{i}")
                b.cbranch(is_tag, tag_ok, nxt)
                b.position_at_end(tag_ok)
                child = ctx.scope.child()
                ctx.scope = child
                for j, bname in enumerate(arm.binds):
                    fv = self.call(ctx, "misa_list_get",
                                   [fields, self.const_i64(j)],
                                   arm, check=False)
                    a = self.alloca_var(ctx, bname)
                    b.store(fv, a)
                    child.declare(bname, a)
            # Guard check.
            if arm.guard is not None:
                gv = self.emit_expr(arm.guard, ctx)
                self.check(ctx, arm.guard)
                gbool = self.call(ctx, "misa_truthy", [gv], arm.guard,
                                  check=False)
                is_true = b.icmp_unsigned("==", gbool,
                                          self.const_i32(1),
                                          name="matchguard")
                body_b = ctx.fn.append_basic_block(f"match.gbody{i}")
                b.cbranch(is_true, body_b, nxt)
                b.position_at_end(body_b)
            rv = self.emit_expr(arm.body, ctx)
            self.check(ctx, arm)
            if not b.block.is_terminated:
                cur = b.block
                b.branch(end_b)
                incomings.append((rv, cur))
            ctx.scope = old_scope
        b.position_at_end(dead_b)
        b.unreachable()
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
        # Screen state: three generated functions per screen.
        #   misa_screen_<name>(params...) — public entry: pushes a FRESH
        #       state frame (navigate() semantics: state resets), then
        #       renders the body.
        #   misa_screen_<name>_body(params...) — the render itself.
        #   misa_rerender_<name>() — re-runs the body with the frame's
        #       stashed args (state preserved); called by the runtime
        #       after a state-mutating event handler.
        state_map = dict(getattr(decl, "state_bindings", {}) or {})

        # --- wrapper: fresh frame + render
        fn = self.screens[name]
        ctx = self.new_fn_ctx(fn)
        b = ctx.builder
        self.get_nil(ctx)
        nargs = len(decl.params)
        arrty = ir.ArrayType(PTR, max(nargs, 1))
        arr = b.alloca(arrty, name="sargv")
        for i, arg in enumerate(fn.args):
            p = b.gep(arr, [self.const_i32(0), self.const_i32(i)],
                      name=f"sargv{i}")
            b.store(arg, p)
        argv = b.gep(arr, [self.const_i32(0), self.const_i32(0)],
                     name="sargv0") if nargs else b.bitcast(
                         NULL_PTR, PTR.as_pointer(), name="sargv_null")
        thunk = b.bitcast(self.screen_rerenders[name], PTR,
                          name="rerender_thunk")
        self.call(ctx, "misa_screen_frame_push",
                  [self.cstr(name), thunk, self.const_i32(nargs), argv],
                  decl, check=False)
        body_fn = self.screen_bodies[name]
        r = b.call(body_fn, list(fn.args), name="body_ret")
        self.check(ctx, decl)
        self.emit_ret(ctx, r)
        self.finish_fn_ctx(ctx)

        # --- body: the render itself
        bfn = self.screen_bodies[name]
        bctx = self.new_fn_ctx(bfn)
        bctx.state_vars = {}
        bb = bctx.builder
        self.get_nil(bctx)
        for (pn, _), arg in zip(decl.params, bfn.args):
            a = self.alloca_var(bctx, pn)
            bb.store(arg, a)
            bctx.scope.declare(pn, a)
        # id: name -> slot (recorded by the checker, M4)
        id_slots = {n: i for i, (n, _k) in enumerate(decl.id_bindings)}
        self.call(bctx, "misa_screen_begin", [self.cstr(name)], decl,
                  check=False)
        for item in decl.body:
            self.emit_screen_item(item, bctx, name, id_slots, state_map)
        self.call(bctx, "misa_screen_end", [], decl, check=False)
        self.emit_ret(bctx, self.get_nil(bctx))
        self.finish_fn_ctx(bctx)

        # --- rerender thunk: body with stashed args, state preserved
        tfn = self.screen_rerenders[name]
        tctx = self.new_fn_ctx(tfn)
        tb = tctx.builder
        self.get_nil(tctx)
        targv = self.call(tctx, "misa_screen_frame_argv", [], decl,
                          check=False)
        targs = []
        for i in range(nargs):
            p = tb.gep(targv, [self.const_i32(i)], name=f"targ{i}")
            targs.append(tb.load(p, name=f"targv{i}"))
        tr = tb.call(body_fn, targs, name="rerender_ret")
        self.check(tctx, decl)
        self.emit_ret(tctx, tr)
        self.finish_fn_ctx(tctx)

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
                         id_slots: dict, state_map: dict):
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
                self.emit_screen_item(x, ctx, screen_name, id_slots,
                                      state_map)
            if not b.block.is_terminated:
                b.branch(end_b)
            b.position_at_end(else_b)
            for x in node.els:
                self.emit_screen_item(x, ctx, screen_name, id_slots,
                                      state_map)
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
            # Screen state: `state()` inside a for-body would share one
            # cell across iterations (one slot per call site). Forbid
            # with a clean error rather than surprising aliasing.
            if self._screen_items_have_state(node.body):
                raise AOTError(
                    "E211",
                    "state() is not supported inside a for-body "
                    "(one call site = one cell; hoist it above the loop)",
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
                self.emit_screen_item(y, ctx, screen_name, id_slots,
                                      state_map)
            ctx.scope = old
            if not b.block.is_terminated:
                b.branch(loop_b)
            b.position_at_end(end_b)
            return
        if isinstance(node, A.LetStmt):
            self.emit_screen_let(node, ctx, state_map)
            return
        self.emit_component(node, ctx, screen_name, id_slots, state_map)

    def _screen_items_have_state(self, items) -> bool:
        """True if any `state()` call appears under items (recursive,
        not descending into lambdas — those are rejected separately)."""
        for it in items:
            if isinstance(it, A.LetStmt) and isinstance(it.value, A.Call) \
                    and it.value.name == "state":
                return True
            if isinstance(it, A.ScreenIf):
                if self._screen_items_have_state(it.then) \
                        or self._screen_items_have_state(it.els):
                    return True
            if isinstance(it, A.ScreenFor):
                if self._screen_items_have_state(it.body):
                    return True
            if isinstance(it, A.Component):
                if self._screen_items_have_state(it.children):
                    return True
        return False

    def emit_screen_let(self, s: A.LetStmt, ctx: FnCtx, state_map: dict):
        """`let`/`var` inside a screen body. A `state(...)` initializer
        allocates a screen state cell (slot stamped by the checker);
        emit_expr already returns the cell, so just record the name."""
        b = ctx.builder
        v = self.emit_expr(s.value, ctx)
        self.check(ctx, s)
        a = self.alloca_var(ctx, s.name)
        b.store(v, a)
        slot = getattr(s.value, "state_slot", None)
        if slot is not None:
            ctx.state_vars[s.name] = slot
        ctx.scope.declare(s.name, a)

    def emit_component(self, comp: A.Component, ctx: FnCtx,
                       screen_name: str, id_slots: dict, state_map: dict):
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
                hid = self.emit_handler(screen_name, av, id_slots,
                                        state_map)
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
            self.emit_screen_item(child, ctx, screen_name, id_slots,
                                  state_map)
        self.call(ctx, "misa_screen_comp_end", [], comp, check=False)

    def emit_handler(self, screen_name: str, lam: A.Lambda,
                     id_slots: dict, state_map: dict) -> int:
        """Compile an event-handler lambda to a real function.

        Returns the handler id (index into misa_handler_table[]).
        Emitted as misa_handler_<screen>_<n>(MVal *event_arg) -> MVal*
        with the standard FnCtx (check() after every fallible call,
        error epilogue). Scope is the lambda params (bound to the
        event arg) plus the screen's id: bindings plus the screen's
        state() bindings (resolved via the runtime screen frame);
        anything else is a closure over locals and is rejected (E600).
        """
        hid = len(self.handlers)
        fn = ir.Function(self.mod, ir.FunctionType(PTR, [PTR]),
                         name=f"misa_handler_{screen_name}_{hid}")
        self.handlers.append(fn)
        ctx = self.new_fn_ctx(fn)
        ctx.in_handler = True
        ctx.id_slots = dict(id_slots)
        ctx.screen_state_vars = dict(state_map)
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
            self.emit_ret(ctx, v)
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
