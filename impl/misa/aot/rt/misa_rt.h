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
    M_TAG_ITER   = 11   /* for-loop iterator (internal) */
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
MVal *misa_list_new(void);
void  misa_list_push(MVal *list, MVal *item);
MVal *misa_map_new(void);
MVal *misa_record_new(void);
void  misa_record_set_cstr(MVal *rec, const char *field, MVal *val);
MVal *misa_media(int kind, const char *path);
int   misa_media_kind(MVal *v);   /* M_MEDIA_* or -1 */
const char *misa_media_path(MVal *v);

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
MVal *misa_builtin_env(const char *name, int line, int col);
MVal *misa_builtin_media(int kind, MVal *path, int line, int col);

/* -- io / net -------------------------------------------------------- */
MVal *misa_fs_read(MVal *path, int line, int col);          /* E303 */
MVal *misa_fs_write(MVal *path, MVal *text, int line, int col); /* E303 */
MVal *misa_http_get(MVal *url, int line, int col);           /* E304 */

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

#ifdef __cplusplus
}
#endif
#endif /* MISA_RT_H */
