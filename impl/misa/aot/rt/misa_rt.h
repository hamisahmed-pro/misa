/* Misa AOT runtime — public interface.
 *
 * Every Misa value is a heap-allocated, garbage-collected MVal.
 * Compiled code calls these helpers; it never touches MVal internals.
 *
 * Error model: any helper may fail. On failure it records a pending
 * error (code + message + source line/col) in thread-local state and
 * returns NULL (or another documented sentinel). Generated code checks
 * misa_pending() after every call and unwinds; misa_take_pending()
 * retrieves the error inside a catch handler. An error that reaches
 * main() is reported as "<line>:<col>: <code>: <message>" on stderr
 * and the process exits 1 — the same shape as `misa run`.
 */
#ifndef MISA_RT_H
#define MISA_RT_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct MVal MVal;

/* Value tags (must match misa_rt.c). */
enum {
    M_TAG_INT    = 1,
    M_TAG_FLOAT  = 2,
    M_TAG_BOOL   = 3,
    M_TAG_TEXT   = 4,
    M_TAG_LIST   = 5,
    M_TAG_MAP    = 6,
    M_TAG_RECORD = 7,
    M_TAG_ERROR  = 8,   /* catchable Error value {code, message} */
    M_TAG_NULL   = 9,
    M_TAG_MEDIA  = 10,  /* Photo | Audio | Video | Document handle */
    M_TAG_ITER   = 11,  /* for-loop iterator (internal) */
    M_TAG_PTR    = 12,  /* Phase 7: opaque C pointer (FFI `Ptr`) */
    M_TAG_TASK   = 13,  /* Phase 7: Task<T> handle (wraps MisaTask*) */
    M_TAG_CHAN   = 14,  /* Phase 7: Chan<T> handle (wraps MisaChan*) */
    M_TAG_BUILDER = 15, /* Text.Builder handle (wraps Str*) */
    M_TAG_STRUCT = 16, /* Wave 2 FFI: C-layout struct (wraps raw bytes) */
};

/* Media kinds. */
enum { M_MEDIA_PHOTO = 0, M_MEDIA_AUDIO, M_MEDIA_VIDEO, M_MEDIA_DOCUMENT };

/* -- lifecycle -------------------------------------------------- */
void  misa_rt_init(void);          /* call once from main() */
void *misa_alloc(size_t n);        /* GC_malloc when available */
void misa_free(void *p);          /* GC_free / free: explicit release */

/* -- errors ------------------------------------------------------ */
void  misa_raise(const char *code, const char *msg, int line, int col);
void  misa_raise_fmt(const char *code, int line, int col,
                     const char *fmt, ...);
MVal *misa_pending(void);          /* NULL or pending MVal error */
MVal *misa_take_pending(void);     /* fetch + clear (for catch) */
void  misa_do_throw(MVal *err, int line, int col); /* `throw` statement */
void  misa_report_uncaught(MVal *err);             /* stderr + used by main */
/* Phase 7: stderr report for a never-awaited task's captured error. */
void  misa_report_task_error(MVal *err, int task_id, int line, int col);

/* Phase 7 (concurrency): a pending-error slot. Fibers each own one so
 * a raise in one fiber is invisible to the others; without fibers the
 * single thread-local slot below is used. The task system installs the
 * per-fiber slot via misa_set_pending_slot_fn() at init. */
typedef struct { MVal *err; int line; int col; } MisaPending;
void misa_set_pending_slot_fn(MisaPending *(*fn)(void));

/* Build a catchable Error value (also used for pending errors). */
MVal *misa_error_new(const char *code, const char *msg);

/* `throw {code, message}`: validate the record, produce a T_ERROR. */
MVal *misa_make_thrown(MVal *rec, int line, int col);
/* catch binding: T_ERROR -> record {code: Text, message: Text}. */
MVal *misa_catch_record(MVal *err);

/* -- constructors ------------------------------------------------ */
MVal *misa_int(int64_t v);
MVal *misa_float(double v);
MVal *misa_bool(int v);
MVal *misa_null(void);
MVal *misa_text(const char *bytes, size_t len);  /* copies bytes */
MVal *misa_text_cstr(const char *s);             /* copies NUL string */
MVal *misa_builder_new(void);                    /* Text.Builder handle */
void  misa_builder_append(MVal *b, MVal *v, int line, int col);
MVal *misa_builder_build(MVal *b, int line, int col);
MVal *misa_list_new(void);
void  misa_list_push(MVal *list, MVal *item);
MVal *misa_map_new(void);
MVal *misa_record_new(void);
void  misa_record_set_cstr(MVal *rec, const char *field, MVal *val);
MVal *misa_media(int kind, const char *path);
int   misa_media_kind(MVal *v);   /* M_MEDIA_* or -1 */
const char *misa_media_path(MVal *v);
/* Phase 7: opaque C pointer. The pointer is NOT owned/traced; the
 * owning C library manages its lifetime. */
MVal *misa_ptr(void *p);
void *misa_as_ptr(MVal *v);       /* E200 if not Ptr */
/* Overtake Wave 2 (FFI): C-layout struct values. `bytes` must be
 * misa_alloc'd; ownership transfers to the value. */
MVal *misa_struct_new(void *bytes);
void *misa_struct_bytes(MVal *v);  /* E200 if not a struct value */

/* -- inspection -------------------------------------------------- */
int      misa_tag(MVal *v);
int64_t  misa_as_int(MVal *v);     /* E200 if not Int */
double   misa_as_float(MVal *v);   /* E200 if not Float */
/* NUL-terminated copy of text content (valid until next call per slot). */
const char *misa_text_z(MVal *v);
size_t   misa_text_len(MVal *v);   /* bytes */
size_t   misa_text_nchars(MVal *v);/* unicode code points */
int      misa_truthy(MVal *v);

/* -- arithmetic / comparison (E200 on type mismatch) -------------- */
MVal *misa_add(MVal *a, MVal *b, int line, int col);
MVal *misa_sub(MVal *a, MVal *b, int line, int col);
MVal *misa_mul(MVal *a, MVal *b, int line, int col);
MVal *misa_div(MVal *a, MVal *b, int line, int col); /* E300 on div by zero */
MVal *misa_mod(MVal *a, MVal *b, int line, int col); /* Python semantics */
MVal *misa_neg(MVal *a, int line, int col);
MVal *misa_not(MVal *a);
MVal *misa_eq(MVal *a, MVal *b);
MVal *misa_ne(MVal *a, MVal *b);
MVal *misa_lt(MVal *a, MVal *b, int line, int col);
MVal *misa_le(MVal *a, MVal *b, int line, int col);
MVal *misa_gt(MVal *a, MVal *b, int line, int col);
MVal *misa_ge(MVal *a, MVal *b, int line, int col);

/* -- text --------------------------------------------------------- */
/* render_value() semantics; the returned MVal is Text. */
MVal *misa_render(MVal *v);
/* Format a double exactly like Python str(float) (shortest round-trip;
 * see misa_rt.c). Shared by the display path and json.stringify. */
void misa_fmt_float(char *out, size_t n, double f);
MVal *misa_text_concat(MVal *a, MVal *b);
/* code-point indexed char (new 1-char Text); E300 if out of range */
MVal *misa_text_char_at(MVal *t, int64_t idx, int line, int col);

/* -- collections --------------------------------------------------- */
int64_t misa_len(MVal *v, int line, int col);  /* code points for Text */
MVal *misa_index_get(MVal *obj, MVal *idx, int line, int col);
void  misa_index_set(MVal *obj, MVal *idx, MVal *val, int line, int col);
MVal *misa_record_get(MVal *obj, const char *field, int line, int col);
void  misa_record_set(MVal *obj, const char *field, MVal *val,
                      int line, int col);
int64_t misa_list_len(MVal *l);
MVal   *misa_list_get(MVal *l, int64_t i);
/* Phase 5 (stdlib2): struct iteration for misa_json.c. */
int64_t misa_record_len(MVal *rec);
const char *misa_record_field_name(MVal *rec, int64_t i);
MVal   *misa_record_field_val(MVal *rec, int64_t i);
int64_t misa_map_len(MVal *m);
MVal   *misa_map_key(MVal *m, int64_t i);
MVal   *misa_map_val(MVal *m, int64_t i);
int     misa_bool_val(MVal *v);
/* Raw text bytes (NOT NUL-terminated; use with misa_text_len). */
const char *misa_text_data(MVal *v);

/* -- for-loop iterator --------------------------------------------- */
/* Returns NULL (no pending error) when exhausted. */
MVal *misa_iter_new(MVal *v, int line, int col);
MVal *misa_iter_next(MVal *it);

/* -- builtins ------------------------------------------------------- */
MVal *misa_print(MVal *v);          /* writes render(v)+"\n", returns Text */
MVal *misa_builtin_len(MVal *v, int line, int col);
MVal *misa_builtin_str(MVal *v);
MVal *misa_builtin_int(MVal *v, int line, int col);
MVal *misa_builtin_float(MVal *v, int line, int col);
MVal *misa_builtin_assert(MVal *cond, MVal *msg, int line, int col);
MVal *misa_builtin_assert_eq(MVal *a, MVal *b, int line, int col);
MVal *misa_builtin_assert_ne(MVal *a, MVal *b, int line, int col);
void  misa_argv_init(int argc, char **argv);
MVal *misa_builtin_argv(void);
MVal *misa_builtin_env(const char *name, int line, int col);
MVal *misa_builtin_media(int kind, MVal *path, int line, int col);

/* -- io / net -------------------------------------------------------- */
MVal *misa_fs_read(MVal *path, int line, int col);          /* E303 */
MVal *misa_fs_write(MVal *path, MVal *text, int line, int col); /* E303 */
/* Phase 7: the http.get builtin lives in misa_task.c (fiber-aware).
 * Shared response validation (status + UTF-8, E304). */
MVal *misa_http_get(MVal *url, int line, int col);           /* E304 */
MVal *misa_http_validate(const char *url, const char *body, size_t len,
                         long status, int line, int col);
/* Phase 7: Task/Chan handle construction (struct MVal is private to
 * misa_rt.c). */
MVal *misa_val_new(uint8_t tag);
void  misa_val_set_ptr(MVal *v, void *p);
void *misa_val_get_ptr(MVal *v);
/* Phase 5 (stdlib2): HTTP POST, JSON, time, math. */
MVal *misa_http_post(MVal *url, MVal *body, const char *content_type,
                     int line, int col);                    /* E304 */
MVal *misa_json_parse(MVal *s, int line, int col);          /* E300 */
MVal *misa_json_stringify(MVal *v, int line, int col);      /* E300 */
MVal *misa_time_now(void);
MVal *misa_time_now_ms(void);
MVal *misa_time_format(MVal *epoch, MVal *fmt, int line, int col);
MVal *misa_time_parse(MVal *s, MVal *fmt, int line, int col); /* E300 */
MVal *misa_math_sqrt(MVal *x, int line, int col);
MVal *misa_math_pow(MVal *x, MVal *y, int line, int col);
MVal *misa_math_abs(MVal *x, int line, int col);
MVal *misa_math_floor(MVal *x, int line, int col);
MVal *misa_math_ceil(MVal *x, int line, int col);
MVal *misa_math_round(MVal *x, int line, int col);
/* str namespace (code-point semantics, mirroring the interpreter). */
MVal *misa_str_slice(MVal *s, MVal *i, MVal *j, int line, int col);
MVal *misa_str_starts_with(MVal *s, MVal *prefix, int line, int col);
MVal *misa_str_split(MVal *s, MVal *sep, int line, int col);
MVal *misa_str_trim(MVal *s, int line, int col);
/* W-4: explicit code-point list; `for x in text` iterates the same. */
MVal *misa_str_chars(MVal *s, int line, int col);
MVal *misa_str_ord(MVal *c, int line, int col);
MVal *misa_str_chr(MVal *code, int line, int col);

/* -- concurrency (Phase 7): cooperative fibers (misa_task.c) ------------ */
/* Task body signature: the emitter compiles `spawn { ... }` to
 * `MVal *misa_spawn_N(MVal *env)` and passes its address. */
typedef MVal *(*MisaTaskFn)(MVal *env);
void  misa_task_init(void);        /* called once from misa_rt_init() */
void  misa_task_shutdown(void);    /* program end: reap tasks, report */
/* Structured scopes: every compiled function calls enter on entry and
 * exit on every return path; exit cancels the scope's live children. */
void  misa_scope_enter(void);
void  misa_scope_exit(void);
void  misa_scope_detach(MVal *task);
MVal *misa_task_spawn(MisaTaskFn fn, MVal *env, int line, int col);
MVal *misa_task_await(MVal *task, int line, int col);
MVal *misa_task_await_timeout(MVal *task, int64_t ms, int line, int col);
MVal *misa_task_sleep(int64_t ms, int line, int col);   /* yields */
MVal *misa_chan_new(int line, int col);
MVal *misa_chan_send(MVal *ch, MVal *v, int line, int col);
MVal *misa_chan_recv(MVal *ch, int line, int col);      /* yields */
MVal *misa_chan_close(MVal *ch, int line, int col);
MVal *misa_fetch_all(MVal *urls, int line, int col);    /* [Text] */

/* -- db (Phase 5): SQLite bindings (misa_db.c) ------------------------ */
MVal *misa_db_open(MVal *path, int line, int col);           /* E303 */
MVal *misa_db_exec(MVal *h, MVal *sql, MVal *args,
                   int line, int col);                       /* E303 */
MVal *misa_db_query(MVal *h, MVal *sql, MVal *args,
                    int line, int col);                      /* E303 */
MVal *misa_db_close(MVal *h, int line, int col);              /* E303 */
/* P-3: bulk inserts + explicit transactions. */
MVal *misa_db_exec_many(MVal *h, MVal *sql, MVal *rows,
                        int line, int col);                   /* E303 */
MVal *misa_db_begin(MVal *h, int line, int col);               /* E303 */
MVal *misa_db_commit(MVal *h, int line, int col);              /* E303 */
MVal *misa_db_rollback(MVal *h, int line, int col);            /* E303 */

/* -- screens (text preview tree, same as the interpreter) ------------- */
/* Emitter filters out `id` args at compile time. */
void  misa_screen_begin(const char *name);
void  misa_screen_comp_begin(const char *name, int has_args);
void  misa_screen_comp_arg(const char *name, MVal *v, int is_first);
/* Phase 4 (M4): handler_id indexes the generated misa_handler_table[]. */
void  misa_screen_comp_arg_handler(const char *name, int is_first,
                                   int handler_id);
void  misa_screen_comp_bind_id(int32_t slot);
void  misa_screen_comp_head_end(int has_args);
void  misa_screen_comp_end(void);
void  misa_screen_end(void);

/* -- screen state (ephemeral per-screen-instance `state()` cells) ---- */
/* One current frame (v1: navigate() replaces it). Cells are 1-element
 * lists (heap-allocated, GC-managed — same trick as Phase 5a closure
 * cells). The states/args arrays use misa_alloc so they work in both
 * GC and malloc builds. */
typedef MVal *(*MisaRerenderThunk)(void);
void  misa_screen_frame_push(const char *name, void *rerender_thunk,
                             int32_t nargs, MVal **args);
MVal **misa_screen_frame_argv(void);
MVal *misa_screen_state_cell(int32_t slot, MVal *initial);
MVal *misa_state_get(MVal *cell);
void  misa_state_set(MVal *cell, MVal *v);
MVal *misa_screen_state_read_slot(int32_t slot);
void  misa_screen_state_write_slot(int32_t slot, MVal *v);
/* Called by each driver's misa_dispatch_handler after the handler ran:
 * re-renders the current screen if state was mutated. */
void  misa_screen_after_handler(void);

/* -- models (blocking calls; see misa_model.c) ------------------------ */

typedef struct MModelParam {
    const char *name;
    const char *kind;      /* "Photo" for media params, else "" */
    const char *type;      /* declared type, e.g. "Float", "[Float]" */
} MModelParam;

typedef struct MPromptPart {
    int         is_lit;    /* 1 = literal text, 0 = interpolate param */
    const char *text;      /* literal text (is_lit) */
    int         param_idx; /* param index (interpolation) */
} MPromptPart;

typedef struct MModelMeta {
    const char       *alias;
    const char       *provider_kind;  /* "openai" | "gemini" | "mock" */
    const char       *model_id;
    const char       *key_env;
    const char       *base_url_env;   /* "" if none */
    int               has_tokens;
    int64_t           budget_tokens;
    int               has_latency;
    int64_t           budget_latency_ms;
    int               has_cost;
    double            budget_cost_usd;
    const char       *schema;         /* return-type schema string */
    const char       *model_file;    /* on-device model path (kind=ondevice) */
    int               nparams;
    const MModelParam *params;
    int               nprompt;
    const MPromptPart *prompt;
} MModelMeta;

/* Blocking model call. args has nargs entries (MVal*). Raises E400/E401/E500. */
MVal *misa_model_call(const MModelMeta *meta, MVal **args, int nargs,
                      int line, int col);
/* Same, but the metadata arrives as an emitter-generated text block
 * (parsed once per call; see misa_model.c for the format). */
MVal *misa_model_call_meta(const char *meta_text, MVal **args, int nargs,
                           int line, int col);

/* CoreML on-device backend (platform_apple.m; Apple targets only).
 * Mirrors the v1 type mapping of the ORT backend: Float -> scalar,
 * [Float] -> 1-D float32, Text -> string; first model output is
 * serialized to JSON in *out_json (misa_alloc'd).
 * Returns 1 on success, 0 when an E50x failure applies (ecode set to
 * "E501"/"E502"/"E503", errbuf carries the message), -1 on
 * load/inference failure (errbuf carries the message; the caller
 * raises E400). Never called on non-Apple targets (the call site in
 * misa_model.c is guarded by #ifdef __APPLE__). */
int misa_coreml_infer(const MModelMeta *meta, MVal **args, int nargs,
                      char **out_json, char *ecode,
                      char *errbuf, size_t errcap);

/* TFLite on-device backend (platform_android.c; Android targets only).
 * The model runs through the Java org.tensorflow.lite.Interpreter with
 * the NNAPI delegate when available (CPU fallback otherwise); args are
 * marshalled as JSON through com.misa.app.MisaTflite.infer(assetName,
 * inputJson, namesJson). Same v1 type mapping and JSON contract as the
 * CoreML backend above.
 * Returns 1 on success, 0 when an E50x failure applies (ecode set to
 * "E501"/"E502"/"E503", errbuf carries the message), -1 on
 * load/inference failure (errbuf carries the message; the caller
 * raises E400). Never called on non-Android targets (the call site in
 * misa_model.c is guarded by #ifdef __ANDROID__). */
int misa_tflite_infer(const MModelMeta *meta, MVal **args, int nargs,
                      char **out_json, char *ecode,
                      char *errbuf, size_t errcap);

#ifdef __cplusplus
}
#endif
#endif /* MISA_RT_H */
