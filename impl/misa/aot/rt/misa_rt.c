/* Misa AOT runtime — core values, errors, arithmetic, collections.
 *
 * Memory: every MVal comes from misa_alloc(), which is GC_malloc() when
 * the runtime is built with -DMISA_USE_GC (Boehm), plain malloc()
 * otherwise. Boehm is conservative: all MVal* held in generated-code
 * allocas, the C stack, and registers are visible roots.
 */
/* C-3: expose strptime()/timegm() declarations. Without these, gcc emits
 * implicit-declaration warnings and the truncated pointer returns can
 * segfault at runtime. Must precede ALL system includes. */
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif
#ifndef _XOPEN_SOURCE
#define _XOPEN_SOURCE 700
#endif
#include "misa_rt.h"
#include "misa_platform.h" /* P1-P4: logging, fatal, env, fs root */
#include "misa_ui.h"       /* misa_ui_active/report_error (drivers) */

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h> /* close() for mkstemp */

#ifdef MISA_USE_GC
#include <gc.h>
#endif

/* ------------------------------------------------------------------ */
/* values                                                             */
/* ------------------------------------------------------------------ */

struct MVal {
    uint8_t tag;
    uint8_t pad[7];
    union {
        int64_t i;
        double f;
        uint8_t b;
        struct { char *data; size_t len; } text;   /* bytes, not NUL */
        struct { MVal **items; int64_t len, cap; } list;
        struct { MVal **keys; MVal **vals; int64_t len, cap; } map;
        struct { char **names; MVal **vals; int64_t len, cap; } record;
        struct { MVal *code; MVal *message; } error;
        struct { int kind; char *path; } media;
        struct { MVal *src; int64_t idx; int64_t nchars; } iter;
        void *ptr;   /* Phase 7: M_TAG_PTR — opaque C pointer */
        /* Wave 3 (stdlib): M_TAG_SET — insertion-ordered dedup list. */
        struct { MVal **items; int64_t len, cap; } set;
        /* Wave 3 (stdlib): M_TAG_REGEX — compiled pattern + source. */
        struct { void *rx; char *pattern; } regex;
        /* Final wave (stdlib): M_TAG_BYTES — raw byte buffer. */
        struct { uint8_t *data; size_t len; } bytes;
        /* cli: M_TAG_CLIPARSER / M_TAG_CLIARGS — opaque handles. */
        struct { void *p; } cliparser;
        struct { void *a; } cliargs;
        /* Unboxed typed arrays (perf) */
        struct { uint8_t *items; int64_t len, cap; } list_bool;
        struct { int64_t *items; int64_t len, cap; } list_int;
        struct { double *items; int64_t len, cap; } list_float;
    } u;
};

/* cli: declarative args parsing — struct layouts shared by the parser
 * handle, the parse result, and render_into (defined later). */
typedef struct {
    char *name;     /* "db" */
    char *metavar;  /* "PATH"; NULL for bool flags */
    char *help;
    int   is_bool;
    char *def;      /* default or NULL */
    char  short_c;  /* 0 if none */
} CliFlag;

typedef struct {
    char *name;
    char *help;
} CliCmd;

typedef struct {
    char    *prog;
    char    *desc;
    CliFlag *flags; int64_t nflags, cflags;
    CliCmd  *cmds;  int64_t ncmds, ccmds;
} MisaCliParser;

typedef struct {
    MisaCliParser *parser;  /* borrowed */
    char  **values; /* per-flag explicit value (NULL if unset) */
    char   *isset;  /* per-flag: explicitly set on the command line */
    char   *command;/* or NULL */
    char  **pos; int64_t npos, cpos;
} MisaCliArgs;

void *misa_alloc(size_t n) {
#ifdef MISA_USE_GC
    void *p = GC_malloc(n ? n : 1);
#else
    void *p = malloc(n ? n : 1);
#endif
    if (!p) {
        /* P2: unrecoverable. Linux prints "<unknown>: E300: out of
         * memory" on stderr and exits 1 (v0.3 behavior). */
        misa_platform_fatal("E300", "out of memory");
    }
    return p;
}

/* Explicit release for misa_alloc'd memory. GC builds use GC_free
 * (raw free() on GC_malloc'd memory corrupts the collector's heap);
 * malloc builds use free(). */
void misa_free(void *p) {
    if (!p) return;
#ifdef MISA_USE_GC
    GC_free(p);
#else
    free(p);
#endif
}

static MVal *val_new(uint8_t tag) {
    MVal *v = (MVal *)misa_alloc(sizeof(MVal));
    memset(v, 0, sizeof(MVal));
    v->tag = tag;
    return v;
}

void misa_rt_init(void) {
#ifdef MISA_USE_GC
    GC_init();
#endif
    misa_task_init(); /* Phase 7: task system (idempotent) */
}

/* ------------------------------------------------------------------ */
/* errors                                                             */
/* ------------------------------------------------------------------ */

/* Phase 7: fiber-local pending errors. Each task fiber owns a
 * MisaPending slot (see misa_task.c); the task system installs the
 * accessor below. Until then (and on builds without fibers) the plain
 * thread-local slot is used. */
static _Thread_local MisaPending tl_pending = {0};
static MisaPending *(*pending_slot_fn)(void) = NULL;

void misa_set_pending_slot_fn(MisaPending *(*fn)(void)) {
    pending_slot_fn = fn;
}

static MisaPending *pend_slot(void) {
    return pending_slot_fn ? pending_slot_fn() : &tl_pending;
}

MVal *misa_error_new(const char *code, const char *msg) {
    MVal *e = val_new(M_TAG_ERROR);
    e->u.error.code = misa_text_cstr(code ? code : "E300");
    e->u.error.message = misa_text_cstr(msg ? msg : "error");
    return e;
}

void misa_raise(const char *code, const char *msg, int line, int col) {
    MisaPending *ps = pend_slot();
    if (!ps->err) {
        ps->err = misa_error_new(code, msg);
        ps->line = line;
        ps->col = col;
    }
}

void misa_raise_fmt(const char *code, int line, int col,
                    const char *fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    misa_raise(code, buf, line, col);
}

MVal *misa_pending(void) { return pend_slot()->err; }

MVal *misa_take_pending(void) {
    MisaPending *ps = pend_slot();
    MVal *e = ps->err;
    ps->err = NULL;
    ps->line = 0;
    ps->col = 0;
    return e;
}

/* `throw e`: e must be an Error value (checked by generated code via
 * misa_do_throw_check); here we just install it as pending. */
void misa_do_throw(MVal *err, int line, int col) {
    MisaPending *ps = pend_slot();
    if (!ps->err) {
        ps->err = err;
        ps->line = line;
        ps->col = col;
    }
}

/* Validate a `throw` record and turn it into a T_ERROR value. */
MVal *misa_make_thrown(MVal *rec, int line, int col) {
    if (misa_pending()) return NULL;
    if (rec->tag != M_TAG_RECORD) {
        misa_raise("E300", "throw needs { code: Text, message: Text }",
                   line, col);
        return NULL;
    }
    MVal *code = NULL, *message = NULL;
    for (int64_t i = 0; i < rec->u.record.len; i++) {
        if (strcmp(rec->u.record.names[i], "code") == 0)
            code = rec->u.record.vals[i];
        else if (strcmp(rec->u.record.names[i], "message") == 0)
            message = rec->u.record.vals[i];
    }
    if (!code || !message || code->tag != M_TAG_TEXT ||
        message->tag != M_TAG_TEXT) {
        misa_raise("E300", "throw needs { code: Text, message: Text }",
                   line, col);
        return NULL;
    }
    return misa_error_new(misa_text_z(code), misa_text_z(message));
}

/* catch binding: T_ERROR -> record {code: Text, message: Text}. */
MVal *misa_catch_record(MVal *err) {
    MVal *r = misa_record_new();
    const char *code = "E300", *msg = "error";
    if (err && err->tag == M_TAG_ERROR) {
        code = misa_text_z(err->u.error.code);
        msg = misa_text_z(err->u.error.message);
    }
    misa_record_set_cstr(r, "code", misa_text_cstr(code));
    misa_record_set_cstr(r, "message", misa_text_cstr(msg));
    return r;
}

void misa_report_uncaught(MVal *err) {
    const char *code = "?";
    const char *msg = "?";
    if (err && err->tag == M_TAG_ERROR) {
        code = misa_text_z(err->u.error.code);
        msg = misa_text_z(err->u.error.message);
    }
    /* P1: piecewise writes — no truncation risk for long messages. */
    char head[64];
    MisaPending *rps = pend_slot();
    if (rps->line > 0)
        snprintf(head, sizeof head, "%d:%d: ",
                 rps->line, rps->col);
    else
        snprintf(head, sizeof head, "<unknown>: ");
    misa_platform_log_err(head, strlen(head));
    misa_platform_log_err(code, strlen(code));
    misa_platform_log_err(": ", 2);
    misa_platform_log_err(msg, strlen(msg));
    misa_platform_log_err("\n", 1);
    misa_platform_flush();
}

/* Phase 7: stderr report for a never-awaited task's captured error.
 * Same "<line>:<col>: <code>: <message>" shape as uncaught errors,
 * with an [unawaited task #N] marker. */
void misa_report_task_error(MVal *err, int task_id, int line, int col) {
    const char *code = "?";
    const char *msg = "?";
    if (err && err->tag == M_TAG_ERROR) {
        code = misa_text_z(err->u.error.code);
        msg = misa_text_z(err->u.error.message);
    }
    char head[64];
    if (line > 0)
        snprintf(head, sizeof head, "%d:%d: ", line, col);
    else
        snprintf(head, sizeof head, "<unknown>: ");
    misa_platform_log_err(head, strlen(head));
    misa_platform_log_err(code, strlen(code));
    misa_platform_log_err(": ", 2);
    misa_platform_log_err(msg, strlen(msg));
    char tail[64];
    snprintf(tail, sizeof tail, " [unawaited task #%d]\n", task_id);
    misa_platform_log_err(tail, strlen(tail));
    misa_platform_flush();
}

/* ------------------------------------------------------------------ */
/* constructors                                                       */
/* ------------------------------------------------------------------ */

MVal *misa_int(int64_t v) {
    // Small int cache (like Python's) for -5..256 — avoids allocation in hot loops
    static MVal *cache[262];
    static int initialized = 0;
    if (!initialized) {
        for (int i = 0; i < 262; i++) {
            cache[i] = val_new(M_TAG_INT);
            cache[i]->u.i = (int64_t)(i - 5);
        }
        initialized = 1;
    }
    if (v >= -5 && v <= 256) {
        return cache[v + 5];
    }
    MVal *x = val_new(M_TAG_INT);
    x->u.i = v;
    return x;
}
MVal *misa_float(double v)  { MVal *x = val_new(M_TAG_FLOAT); x->u.f = v; return x; }
MVal *misa_bool(int v) {
    // Singleton booleans (like Python's True/False) — avoids allocation
    static MVal *true_val = NULL;
    static MVal *false_val = NULL;
    if (v) {
        if (!true_val) {
            true_val = val_new(M_TAG_BOOL);
            true_val->u.b = 1;
        }
        return true_val;
    } else {
        if (!false_val) {
            false_val = val_new(M_TAG_BOOL);
            false_val->u.b = 0;
        }
        return false_val;
    }
}
MVal *misa_null(void)       { return val_new(M_TAG_NULL); }

MVal *misa_text(const char *bytes, size_t len) {
    MVal *x = val_new(M_TAG_TEXT);
    char *d = (char *)misa_alloc(len + 1);
    if (len) memcpy(d, bytes, len);
    d[len] = '\0';
    x->u.text.data = d;
    x->u.text.len = len;
    return x;
}

MVal *misa_text_cstr(const char *s) {
    return misa_text(s ? s : "", s ? strlen(s) : 0);
}

/* Grow a pointer array; returns the (possibly new) array. Written in
 * return-value style to avoid strict-aliasing issues. */
static void *grow_array(void *arr, int64_t *cap, int64_t need,
                        size_t elem) {
    if (need <= *cap) return arr;
    int64_t ncap = *cap ? *cap * 2 : 8;
    while (ncap < need) ncap *= 2;
    void *n = misa_alloc(elem * (size_t)ncap);
    if (arr && *cap) memcpy(n, arr, elem * (size_t)*cap);
    *cap = ncap;
    return n;
}

MVal *misa_list_new(void) { return val_new(M_TAG_LIST); }

void misa_list_push(MVal *list, MVal *item) {
    list->u.list.items = (MVal **)grow_array(list->u.list.items,
                                           &list->u.list.cap,
                                           list->u.list.len + 1,
                                           sizeof(MVal *));
    list->u.list.items[list->u.list.len++] = item;
}

MVal *misa_map_new(void) { return val_new(M_TAG_MAP); }

MVal *misa_record_new(void) { return val_new(M_TAG_RECORD); }

void misa_record_set_cstr(MVal *rec, const char *field, MVal *val) {
    for (int64_t i = 0; i < rec->u.record.len; i++) {
        if (strcmp(rec->u.record.names[i], field) == 0) {
            rec->u.record.vals[i] = val;
            return;
        }
    }
    int64_t need = rec->u.record.len + 1;
    if (need > rec->u.record.cap) {
        /* names and vals share one capacity; grow both together */
        int64_t ncap = rec->u.record.cap ? rec->u.record.cap * 2 : 8;
        while (ncap < need) ncap *= 2;
        char **nn = (char **)misa_alloc(sizeof(char *) * (size_t)ncap);
        MVal **nv = (MVal **)misa_alloc(sizeof(MVal *) * (size_t)ncap);
        if (rec->u.record.names) {
            memcpy(nn, rec->u.record.names,
                   sizeof(char *) * (size_t)rec->u.record.len);
            memcpy(nv, rec->u.record.vals,
                   sizeof(MVal *) * (size_t)rec->u.record.len);
        }
        rec->u.record.names = nn;
        rec->u.record.vals = nv;
        rec->u.record.cap = ncap;
    }
    size_t fl = strlen(field);
    char *fc = (char *)misa_alloc(fl + 1);
    memcpy(fc, field, fl + 1);
    rec->u.record.names[rec->u.record.len] = fc;
    rec->u.record.vals[rec->u.record.len] = val;
    rec->u.record.len++;
}

MVal *misa_media(int kind, const char *path) {
    MVal *x = val_new(M_TAG_MEDIA);
    x->u.media.kind = kind;
    size_t l = strlen(path);
    char *c = (char *)misa_alloc(l + 1);
    memcpy(c, path, l + 1);
    x->u.media.path = c;
    return x;
}

int misa_media_kind(MVal *v) {
    return v->tag == M_TAG_MEDIA ? v->u.media.kind : -1;
}

const char *misa_media_path(MVal *v) {
    return v->tag == M_TAG_MEDIA ? v->u.media.path : "";
}

/* Phase 7: opaque C pointer (FFI `Ptr`). */
MVal *misa_ptr(void *p) {
    MVal *x = val_new(M_TAG_PTR);
    x->u.ptr = p;
    return x;
}

void *misa_as_ptr(MVal *v) {
    if (v->tag != M_TAG_PTR) {
        misa_raise("E200", "expected Ptr", 0, 0);
        return NULL;
    }
    return v->u.ptr;
}

/* Overtake Wave 2 (FFI): C-layout struct values. The payload reuses
 * the `ptr` union slot: a misa_alloc'd byte buffer holding the raw
 * struct. The emitter knows each struct's LLVM type and reads fields
 * via GEP; the runtime only boxes/unboxes the buffer. */
MVal *misa_struct_new(void *bytes) {
    MVal *x = val_new(M_TAG_STRUCT);
    x->u.ptr = bytes;
    return x;
}

void *misa_struct_bytes(MVal *v) {
    if (v->tag != M_TAG_STRUCT) {
        misa_raise("E200", "expected struct value", 0, 0);
        return NULL;
    }
    return v->u.ptr;
}

/* ------------------------------------------------------------------ */
/* inspection                                                         */
/* ------------------------------------------------------------------ */

int misa_tag(MVal *v) { return v ? (int)v->tag : 0; }

int64_t misa_as_int(MVal *v) {
    if (v->tag != M_TAG_INT) { misa_raise("E200", "expected Int", 0, 0); return 0; }
    return v->u.i;
}

double misa_as_float(MVal *v) {
    if (v->tag != M_TAG_FLOAT) { misa_raise("E200", "expected Float", 0, 0); return 0; }
    return v->u.f;
}

/* NUL-terminated copy of text bytes (GC-managed). */
const char *misa_text_z(MVal *v) {
    if (v->tag != M_TAG_TEXT) { misa_raise("E200", "expected Text", 0, 0); return ""; }
    return v->u.text.data; /* stored NUL-terminated */
}

size_t misa_text_len(MVal *v) { return v->u.text.len; }

/* UTF-8 code point length. */
size_t misa_text_nchars(MVal *v) {
    const unsigned char *p = (const unsigned char *)v->u.text.data;
    size_t n = 0;
    for (size_t i = 0; i < v->u.text.len; n++) {
        unsigned char c = p[i];
        if (c < 0x80) i += 1;
        else if ((c & 0xE0) == 0xC0) i += 2;
        else if ((c & 0xF0) == 0xE0) i += 3;
        else i += 4;
        if (i > v->u.text.len) break; /* malformed: stop */
    }
    return n;
}

int misa_truthy(MVal *v) {
    switch (v->tag) {
    case M_TAG_NULL:  return 0;
    case M_TAG_BOOL:  return v->u.b ? 1 : 0;
    case M_TAG_INT:   return v->u.i != 0;
    case M_TAG_FLOAT: return v->u.f != 0.0;
    case M_TAG_TEXT:  return v->u.text.len > 0;
    case M_TAG_LIST:  return v->u.list.len > 0;
    case M_TAG_LIST_BOOL:  return v->u.list_bool.len > 0;
    case M_TAG_LIST_INT:   return v->u.list_int.len > 0;
    case M_TAG_LIST_FLOAT: return v->u.list_float.len > 0;
    case M_TAG_MAP:   return v->u.map.len > 0;
    case M_TAG_RECORD:return v->u.record.len > 0;
    case M_TAG_TASK:  return 1;
    case M_TAG_CHAN:  return 1;
    default:          return 1;
    }
}

/* ---------------- rendering (render_value parity) ------------------ */

typedef struct {
    char  *buf;
    size_t len, cap;
} Str;

static void str_reserve(Str *s, size_t extra) {
    if (s->len + extra + 1 <= s->cap) return;
    size_t ncap = s->cap ? s->cap * 2 : 64;
    while (ncap < s->len + extra + 1) ncap *= 2;
    char *n = (char *)misa_alloc(ncap);
    if (s->buf && s->len) memcpy(n, s->buf, s->len);
    s->buf = n;
    s->cap = ncap;
}

static void str_putn(Str *s, const char *p, size_t n) {
    str_reserve(s, n);
    memcpy(s->buf + s->len, p, n);
    s->len += n;
    s->buf[s->len] = '\0';
}

static void str_puts(Str *s, const char *p) { str_putn(s, p, strlen(p)); }

/* Strip trailing zeros from a "%.*e" mantissa in place:
 * "1.500e-05" -> "1.5e-05", "1.000e+16" -> "1e+16". */
static void strip_e_mantissa_zeros(char *tmp) {
    char *e = strchr(tmp, 'e');
    if (!e) e = strchr(tmp, 'E');
    if (!e) return;
    char *dot = strchr(tmp, '.');
    if (!dot || dot > e) return;
    char *end = e - 1;
    while (end > dot && *end == '0') end--;
    if (end == dot)
        memmove(dot, e, strlen(e) + 1);       /* "1.000e+16" -> "1e+16" */
    else
        memmove(end + 1, e, strlen(e) + 1);   /* "1.500e-05" -> "1.5e-05" */
}

/* Strip trailing zeros from a "%.*f" result in place: "0.500" -> "0.5".
 * A bare "5." becomes "5" (the caller appends ".0" for integral values). */
static void strip_f_trailing_zeros(char *tmp) {
    char *dot = strchr(tmp, '.');
    if (!dot) return;
    char *end = tmp + strlen(tmp) - 1;
    while (end > dot && *end == '0') end--;
    if (end == dot) end--;                    /* "5." -> "5" */
    end[1] = '\0';
}

/* Format a double exactly like Python str(float): the shortest string that
 * round-trips through strtod; fixed notation when -4 <= decimal exponent
 * < 16, scientific otherwise; ".0" appended for integral values so a Float
 * never renders as an Int; "nan"/"inf"/"-inf" for non-finite.
 *
 * H-1: the interpreter renders via Python str(), so every AOT target must
 * produce byte-identical output here (print, str(), interpolation,
 * collection rendering, json.stringify). */
void misa_fmt_float(char *out, size_t n, double f) {
    char tmp[64];
    if (isnan(f)) { snprintf(out, n, "nan"); return; }
    if (isinf(f)) { snprintf(out, n, "%sinf", f < 0 ? "-" : ""); return; }
    if (f == 0.0) {
        snprintf(out, n, "%s0.0", signbit(f) ? "-" : "");
        return;
    }
    /* Decimal exponent decides the style, mirroring CPython: scientific
     * iff exp < -4 or exp >= 16. */
    char ebuf[32];
    snprintf(ebuf, sizeof ebuf, "%.17e", f);
    char *ep = strchr(ebuf, 'e');
    int exp = ep ? atoi(ep + 1) : 0;
    if (exp < -4 || exp >= 16) {
        for (int prec = 1; prec <= 17; prec++) {
            snprintf(tmp, sizeof tmp, "%.*e", prec - 1, f);
            strip_e_mantissa_zeros(tmp);
            if (strtod(tmp, NULL) == f) break;
        }
    } else {
        for (int prec = 1; prec <= 17; prec++) {
            int after = prec - 1 - exp;
            if (after < 0) after = 0;
            snprintf(tmp, sizeof tmp, "%.*f", after, f);
            strip_f_trailing_zeros(tmp);
            if (strtod(tmp, NULL) == f) break;
        }
        if (!strchr(tmp, '.')) {
            size_t len = strlen(tmp);
            if (len + 2 < sizeof tmp) {
                tmp[len] = '.';
                tmp[len + 1] = '0';
                tmp[len + 2] = '\0';
            }
        }
    }
    snprintf(out, n, "%s", tmp);
}

static void str_put_float(Str *s, double f) {
    char tmp[64];
    misa_fmt_float(tmp, sizeof tmp, f);
    str_puts(s, tmp);
}

static void render_into(Str *s, MVal *v);

static void render_into(Str *s, MVal *v) {
    char tmp[64];
    switch (v->tag) {
    case M_TAG_NULL:  str_puts(s, "nil"); break;
    case M_TAG_BOOL:  str_puts(s, v->u.b ? "true" : "false"); break;
    case M_TAG_INT:
        snprintf(tmp, sizeof tmp, "%lld", (long long)v->u.i);
        str_puts(s, tmp);
        break;
    case M_TAG_FLOAT: str_put_float(s, v->u.f); break;
    case M_TAG_TEXT:  str_putn(s, v->u.text.data, v->u.text.len); break;
    case M_TAG_LIST:
        str_puts(s, "[");
        for (int64_t i = 0; i < v->u.list.len; i++) {
            if (i) str_puts(s, ", ");
            render_into(s, v->u.list.items[i]);
        }
        str_puts(s, "]");
        break;
    case M_TAG_LIST_BOOL: {
        str_puts(s, "[");
        for (int64_t i = 0; i < v->u.list_bool.len; i++) {
            if (i) str_puts(s, ", ");
            str_puts(s, v->u.list_bool.items[i] ? "true" : "false");
        }
        str_puts(s, "]");
        break;
    }
    case M_TAG_LIST_INT: {
        str_puts(s, "[");
        for (int64_t i = 0; i < v->u.list_int.len; i++) {
            if (i) str_puts(s, ", ");
            snprintf(tmp, sizeof tmp, "%lld",
                     (long long)v->u.list_int.items[i]);
            str_puts(s, tmp);
        }
        str_puts(s, "]");
        break;
    }
    case M_TAG_LIST_FLOAT: {
        str_puts(s, "[");
        for (int64_t i = 0; i < v->u.list_float.len; i++) {
            if (i) str_puts(s, ", ");
            str_put_float(s, v->u.list_float.items[i]);
        }
        str_puts(s, "]");
        break;
    }
    case M_TAG_RECORD: {
        /* Phase 5a: closure values render opaquely, matching the
         * interpreter's "<lambda>" / "<function name>". */
        int64_t cdisp_idx = -1;
        for (int64_t i = 0; i < v->u.record.len; i++) {
            if (strcmp(v->u.record.names[i], "__closure_disp") == 0
                && v->u.record.vals[i]->tag == M_TAG_TEXT) {
                cdisp_idx = i;
                break;
            }
        }
        if (cdisp_idx >= 0) {
            MVal *d = v->u.record.vals[cdisp_idx];
            str_putn(s, d->u.text.data, d->u.text.len);
            break;
        }
        /* Phase 5b: sum values render as `Type.Variant(payload, ...)`,
         * matching the interpreter. */
        int64_t sdisp_idx = -1, sfields_idx = -1;
        for (int64_t i = 0; i < v->u.record.len; i++) {
            if (strcmp(v->u.record.names[i], "__sum_disp") == 0
                && v->u.record.vals[i]->tag == M_TAG_TEXT)
                sdisp_idx = i;
            else if (strcmp(v->u.record.names[i], "__fields") == 0)
                sfields_idx = i;
        }
        if (sdisp_idx >= 0) {
            MVal *d = v->u.record.vals[sdisp_idx];
            str_putn(s, d->u.text.data, d->u.text.len);
            MVal *fl = (sfields_idx >= 0)
                ? v->u.record.vals[sfields_idx] : NULL;
            if (fl && fl->tag == M_TAG_LIST && fl->u.list.len > 0) {
                str_puts(s, "(");
                for (int64_t i = 0; i < fl->u.list.len; i++) {
                    if (i) str_puts(s, ", ");
                    render_into(s, fl->u.list.items[i]);
                }
                str_puts(s, ")");
            }
            break;
        }
        /* Phase 8 (overtake): interface values render as their
         * payload (the __fields list's single element), matching the
         * interpreter. */
        int64_t idisp_idx = -1, ifields_idx = -1;
        for (int64_t i = 0; i < v->u.record.len; i++) {
            if (strcmp(v->u.record.names[i], "__iface_disp") == 0)
                idisp_idx = i;
            else if (strcmp(v->u.record.names[i], "__fields") == 0)
                ifields_idx = i;
        }
        if (idisp_idx >= 0 && ifields_idx >= 0) {
            MVal *fl = v->u.record.vals[ifields_idx];
            if (fl->tag == M_TAG_LIST && fl->u.list.len > 0) {
                render_into(s, fl->u.list.items[0]);
                break;
            }
        }
        str_puts(s, "{ ");
        for (int64_t i = 0; i < v->u.record.len; i++) {
            if (i) str_puts(s, ", ");
            str_puts(s, v->u.record.names[i]);
            str_puts(s, ": ");
            render_into(s, v->u.record.vals[i]);
        }
        str_puts(s, " }");
        break;
    }
    case M_TAG_MAP:
        str_puts(s, "{");
        for (int64_t i = 0; i < v->u.map.len; i++) {
            if (i) str_puts(s, ", ");
            str_puts(s, "\"");
            render_into(s, v->u.map.keys[i]);
            str_puts(s, "\": ");
            render_into(s, v->u.map.vals[i]);
        }
        str_puts(s, "}");
        break;
    case M_TAG_ERROR:
        str_puts(s, "<error ");
        render_into(s, v->u.error.code);
        str_puts(s, ">");
        break;
    case M_TAG_MEDIA: {
        const char *k = "?";
        switch (v->u.media.kind) {
        case M_MEDIA_PHOTO: k = "Photo"; break;
        case M_MEDIA_AUDIO: k = "Audio"; break;
        case M_MEDIA_VIDEO: k = "Video"; break;
        case M_MEDIA_DOCUMENT: k = "Document"; break;
        }
        str_puts(s, "<"); str_puts(s, k);
        str_puts(s, ": "); str_puts(s, v->u.media.path); str_puts(s, ">");
        break;
    }
    case M_TAG_ITER: str_puts(s, "<iter>"); break;
    case M_TAG_PTR: str_puts(s, "<ptr>"); break;
    case M_TAG_TASK: str_puts(s, "<task>"); break;
    case M_TAG_CHAN: str_puts(s, "<chan>"); break;
    case M_TAG_BUILDER: str_puts(s, "<builder>"); break;
    case M_TAG_STRUCT: str_puts(s, "<struct>"); break;
    case M_TAG_SET: {
        /* Render like the interpreter: {a, b, c}. */
        str_puts(s, "{");
        for (int64_t i = 0; i < v->u.set.len; i++) {
            if (i) str_puts(s, ", ");
            Str tmp = {0};
            render_into(&tmp, v->u.set.items[i]);
            str_putn(s, tmp.buf ? tmp.buf : "", tmp.len);
        }
        str_puts(s, "}");
        break;
    }
    case M_TAG_REGEX: {
        str_puts(s, "<regex: ");
        str_puts(s, v->u.regex.pattern ? v->u.regex.pattern : "?");
        str_puts(s, ">");
        break;
    }
    case M_TAG_BYTES: {
        /* Render like the interpreter: <bytes: N>. */
        char buf[48];
        snprintf(buf, sizeof buf, "<bytes: %zu>", v->u.bytes.len);
        str_puts(s, buf);
        break;
    }
    case M_TAG_CLIPARSER: {
        MisaCliParser *p = (MisaCliParser *)v->u.cliparser.p;
        str_puts(s, "<cli-parser: ");
        str_puts(s, p && p->prog ? p->prog : "?");
        str_puts(s, ">");
        break;
    }
    case M_TAG_CLIARGS: {
        str_puts(s, "<cli-args>");
        break;
    }
    default: str_puts(s, "<value>"); break;
    }
}

MVal *misa_render(MVal *v) {
    Str s = {0};
    render_into(&s, v);
    return misa_text(s.buf ? s.buf : "", s.len);
}

MVal *misa_text_concat(MVal *a, MVal *b) {
    /* Fast path: both already Text — copy bytes directly, skipping the
     * two misa_render() calls (each of which allocates + copies). */
    if (a->tag == M_TAG_TEXT && b->tag == M_TAG_TEXT) {
        Str s = {0};
        str_putn(&s, a->u.text.data, a->u.text.len);
        str_putn(&s, b->u.text.data, b->u.text.len);
        return misa_text(s.buf ? s.buf : "", s.len);
    }
    Str s = {0};
    MVal *ra = misa_render(a), *rb = misa_render(b);
    /* render() of Text is identity content-wise; use raw bytes */
    str_putn(&s, ra->u.text.data, ra->u.text.len);
    str_putn(&s, rb->u.text.data, rb->u.text.len);
    return misa_text(s.buf ? s.buf : "", s.len);
}

/* ---------------- Text.Builder ------------------ */
/* Amortized O(1) appends via the exponential-overallocation Str.
 * This is the fast path for building large strings in a loop;
 * `s = s + x` is O(n^2) by construction (fresh value each time). */

MVal *misa_builder_new(void) {
    MVal *x = val_new(M_TAG_BUILDER);
    Str *s = (Str *)misa_alloc(sizeof(Str));
    s->buf = NULL; s->len = 0; s->cap = 0;
    x->u.ptr = s;
    return x;
}

static Str *builder_str(MVal *b, int line, int col, const char *op) {
    if (b->tag != M_TAG_BUILDER) {
        misa_raise("E200", "builder.append/build needs a Builder "
                   "(from builder())", line, col);
        return NULL;
    }
    return (Str *)b->u.ptr;
}

void misa_builder_append(MVal *b, MVal *v, int line, int col) {
    Str *s = builder_str(b, line, col, "append");
    if (!s) return;
    if (v->tag == M_TAG_TEXT) {
        str_putn(s, v->u.text.data, v->u.text.len);
    } else {
        MVal *r = misa_render(v);
        str_putn(s, r->u.text.data, r->u.text.len);
    }
}

MVal *misa_builder_build(MVal *b, int line, int col) {
    Str *s = builder_str(b, line, col, "build");
    if (!s) return NULL;
    return misa_text(s->buf ? s->buf : "", s->len);
}

/* code-point indexed single char */
MVal *misa_text_char_at(MVal *t, int64_t idx, int line, int col) {
    const unsigned char *p = (const unsigned char *)t->u.text.data;
    size_t len = t->u.text.len, i = 0;
    int64_t n = 0;
    if (idx < 0) { misa_raise("E300", "index out of range", line, col); return NULL; }
    while (i < len) {
        if (n == idx) {
            unsigned char c = p[i];
            size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                      : (c & 0xF0) == 0xE0 ? 3 : 4;
            if (i + w > len) w = len - i;
            return misa_text((const char *)p + i, w);
        }
        unsigned char c = p[i];
        i += c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
           : (c & 0xF0) == 0xE0 ? 3 : 4;
        n++;
    }
    misa_raise("E300", "index out of range", line, col);
    return NULL;
}

/* ---------------- arithmetic ------------------ */

static int both_int(MVal *a, MVal *b) {
    return a->tag == M_TAG_INT && b->tag == M_TAG_INT;
}
static int both_num(MVal *a, MVal *b) {
    int na = a->tag == M_TAG_INT || a->tag == M_TAG_FLOAT;
    int nb = b->tag == M_TAG_INT || b->tag == M_TAG_FLOAT;
    return na && nb;
}
static double to_f(MVal *v) {
    return v->tag == M_TAG_INT ? (double)v->u.i : v->u.f;
}

/* wrap explicitly: two's complement via unsigned arithmetic */
static int64_t wrap_add(int64_t a, int64_t b) {
    return (int64_t)((uint64_t)a + (uint64_t)b);
}
static int64_t wrap_sub(int64_t a, int64_t b) {
    return (int64_t)((uint64_t)a - (uint64_t)b);
}
static int64_t wrap_mul(int64_t a, int64_t b) {
    return (int64_t)((uint64_t)a * (uint64_t)b);
}

MVal *misa_add(MVal *a, MVal *b, int line, int col) {
    if (a->tag == M_TAG_TEXT || b->tag == M_TAG_TEXT)
        return misa_text_concat(a, b);
    if (a->tag == M_TAG_LIST && b->tag == M_TAG_LIST) {
        MVal *out = misa_list_new();
        for (size_t i = 0; i < a->u.list.len; i++)
            misa_list_push(out, a->u.list.items[i]);
        for (size_t i = 0; i < b->u.list.len; i++)
            misa_list_push(out, b->u.list.items[i]);
        return out;
    }
    // Unboxed bool list concatenation
    if (a->tag == M_TAG_LIST_BOOL && b->tag == M_TAG_LIST_BOOL) {
        MVal *out = misa_list_bool_new();
        for (int64_t i = 0; i < a->u.list_bool.len; i++)
            misa_list_bool_push(out, a->u.list_bool.items[i]);
        for (int64_t i = 0; i < b->u.list_bool.len; i++)
            misa_list_bool_push(out, b->u.list_bool.items[i]);
        return out;
    }
    // Unboxed int list concatenation
    if (a->tag == M_TAG_LIST_INT && b->tag == M_TAG_LIST_INT) {
        MVal *out = misa_list_int_new();
        for (int64_t i = 0; i < a->u.list_int.len; i++)
            misa_list_int_push(out, a->u.list_int.items[i]);
        for (int64_t i = 0; i < b->u.list_int.len; i++)
            misa_list_int_push(out, b->u.list_int.items[i]);
        return out;
    }
    // Mixed: unboxed int + boxed list -> box the unboxed side, then
    // standard boxed concat. O(n) one-time cost on type inconsistency.
    if ((a->tag == M_TAG_LIST_INT && b->tag == M_TAG_LIST) ||
        (a->tag == M_TAG_LIST && b->tag == M_TAG_LIST_INT)) {
        MVal *ba = a, *bb = b;
        if (a->tag == M_TAG_LIST_INT) {
            ba = misa_list_new();
            for (int64_t i = 0; i < a->u.list_int.len; i++)
                misa_list_push(ba, misa_int(a->u.list_int.items[i]));
        }
        if (b->tag == M_TAG_LIST_INT) {
            bb = misa_list_new();
            for (int64_t i = 0; i < b->u.list_int.len; i++)
                misa_list_push(bb, misa_int(b->u.list_int.items[i]));
        }
        MVal *out = misa_list_new();
        for (int64_t i = 0; i < ba->u.list.len; i++)
            misa_list_push(out, ba->u.list.items[i]);
        for (int64_t i = 0; i < bb->u.list.len; i++)
            misa_list_push(out, bb->u.list.items[i]);
        return out;
    }
    if (both_int(a, b)) return misa_int(wrap_add(a->u.i, b->u.i));
    if (both_num(a, b)) return misa_float(to_f(a) + to_f(b));
    misa_raise("E200", "cannot add these values", line, col);
    return NULL;
}

MVal *misa_sub(MVal *a, MVal *b, int line, int col) {
    if (both_int(a, b)) return misa_int(wrap_sub(a->u.i, b->u.i));
    if (both_num(a, b)) return misa_float(to_f(a) - to_f(b));
    misa_raise("E200", "cannot subtract these values", line, col);
    return NULL;
}

MVal *misa_mul(MVal *a, MVal *b, int line, int col) {
    if (both_int(a, b)) return misa_int(wrap_mul(a->u.i, b->u.i));
    if (both_num(a, b)) return misa_float(to_f(a) * to_f(b));
    // List repetition: [x] * n
    if (a->tag == M_TAG_LIST && b->tag == M_TAG_INT) {
        int64_t n = b->u.i;
        if (n < 0) {
            misa_raise("E200", "negative repeat count", line, col);
            return NULL;
        }
        MVal *r = misa_list_new();
        for (int64_t i = 0; i < n; i++) {
            for (int64_t j = 0; j < a->u.list.len; j++) {
                misa_list_push(r, a->u.list.items[j]);
            }
        }
        return r;
    }
    // Unboxed bool list repetition
    if (a->tag == M_TAG_LIST_BOOL && b->tag == M_TAG_INT) {
        int64_t n = b->u.i;
        if (n < 0) {
            misa_raise("E200", "negative repeat count", line, col);
            return NULL;
        }
        MVal *r = misa_list_bool_new();
        for (int64_t i = 0; i < n; i++) {
            for (int64_t j = 0; j < a->u.list_bool.len; j++) {
                misa_list_bool_push(r, a->u.list_bool.items[j]);
            }
        }
        return r;
    }
    misa_raise("E200", "cannot multiply these values", line, col);
    return NULL;
}

MVal *misa_div(MVal *a, MVal *b, int line, int col) {
    /* Int/Int is truncating integer division (C99/LLVM sdiv semantics),
       matching the checker (Int/Int -> Int) and the AOT unboxed path.
       Mixed Int/Float is float division. */
    if (both_int(a, b)) {
        int64_t x = a->u.i, y = b->u.i;
        if (y == 0) { misa_raise("E300", "division by zero", line, col); return NULL; }
        if (x == INT64_MIN && y == -1) return misa_int(INT64_MIN); /* wraps */
        return misa_int(x / y);
    }
    if (!both_num(a, b)) {
        misa_raise("E200", "cannot divide these values", line, col);
        return NULL;
    }
    double d = to_f(b);
    if (d == 0.0) { misa_raise("E300", "division by zero", line, col); return NULL; }
    return misa_float(to_f(a) / d);
}

/* Truncated modulo (C99/LLVM srem semantics: sign follows dividend),
   matching the AOT unboxed path. */
MVal *misa_mod(MVal *a, MVal *b, int line, int col) {
    if (both_int(a, b)) {
        int64_t x = a->u.i, y = b->u.i;
        if (y == 0) { misa_raise("E300", "integer modulo by zero", line, col); return NULL; }
        if (y == -1) return misa_int(0); /* x % -1 == 0; also dodges INT64_MIN/-1 UB */
        return misa_int(x % y); /* truncated */
    }
    if (both_num(a, b)) {
        double x = to_f(a), y = to_f(b);
        if (y == 0.0) { misa_raise("E300", "float modulo by zero", line, col); return NULL; }
        double r = x - y * floor(x / y);
        /* match Python sign behaviour for edge cases */
        if (r == 0.0) r = copysign(0.0, y);
        return misa_float(r);
    }
    misa_raise("E200", "cannot take modulo of these values", line, col);
    return NULL;
}

MVal *misa_neg(MVal *a, int line, int col) {
    if (a->tag == M_TAG_INT) {
        uint64_t u = (uint64_t)a->u.i;
        return misa_int((int64_t)(~u + 1)); /* wraps INT64_MIN fine */
    }
    if (a->tag == M_TAG_FLOAT) return misa_float(-a->u.f);
    misa_raise("E200", "cannot negate this value", line, col);
    return NULL;
}

MVal *misa_not(MVal *a) { return misa_bool(!misa_truthy(a)); }

/* ---------------- comparison ------------------ */

static int val_equal(MVal *a, MVal *b);

static int val_equal(MVal *a, MVal *b) {
    if (a->tag == M_TAG_INT && b->tag == M_TAG_FLOAT)
        return (double)a->u.i == b->u.f;
    if (a->tag == M_TAG_FLOAT && b->tag == M_TAG_INT)
        return a->u.f == (double)b->u.i;
    /* Phase 7: a null Ptr equals nil. */
    if (a->tag == M_TAG_PTR && b->tag == M_TAG_NULL)
        return a->u.ptr == NULL;
    if (a->tag == M_TAG_NULL && b->tag == M_TAG_PTR)
        return b->u.ptr == NULL;
    if (a->tag != b->tag) return 0;
    switch (a->tag) {
    case M_TAG_NULL: return 1;
    case M_TAG_PTR: return a->u.ptr == b->u.ptr;
    case M_TAG_BOOL: return a->u.b == b->u.b;
    case M_TAG_INT: return a->u.i == b->u.i;
    case M_TAG_FLOAT: return a->u.f == b->u.f;
    case M_TAG_TEXT:
        return a->u.text.len == b->u.text.len &&
               memcmp(a->u.text.data, b->u.text.data, a->u.text.len) == 0;
    case M_TAG_BYTES:
        return a->u.bytes.len == b->u.bytes.len &&
               memcmp(a->u.bytes.data, b->u.bytes.data,
                      a->u.bytes.len) == 0;
    case M_TAG_LIST: {
        if (a->u.list.len != b->u.list.len) return 0;
        for (int64_t i = 0; i < a->u.list.len; i++)
            if (!val_equal(a->u.list.items[i], b->u.list.items[i])) return 0;
        return 1;
    }
    case M_TAG_RECORD: {
        if (a->u.record.len != b->u.record.len) return 0;
        for (int64_t i = 0; i < a->u.record.len; i++) {
            int found = 0;
            for (int64_t j = 0; j < b->u.record.len; j++) {
                if (strcmp(a->u.record.names[i], b->u.record.names[j]) == 0) {
                    if (!val_equal(a->u.record.vals[i], b->u.record.vals[j]))
                        return 0;
                    found = 1;
                    break;
                }
            }
            if (!found) return 0;
        }
        return 1;
    }
    case M_TAG_MAP: {
        if (a->u.map.len != b->u.map.len) return 0;
        for (int64_t i = 0; i < a->u.map.len; i++) {
            int found = 0;
            for (int64_t j = 0; j < b->u.map.len; j++) {
                if (val_equal(a->u.map.keys[i], b->u.map.keys[j])) {
                    if (!val_equal(a->u.map.vals[i], b->u.map.vals[j]))
                        return 0;
                    found = 1;
                    break;
                }
            }
            if (!found) return 0;
        }
        return 1;
    }
    case M_TAG_MEDIA:
        return a->u.media.kind == b->u.media.kind &&
               strcmp(a->u.media.path, b->u.media.path) == 0;
    default: return a == b;
    }
}

MVal *misa_eq(MVal *a, MVal *b) { return misa_bool(val_equal(a, b)); }
MVal *misa_ne(MVal *a, MVal *b) { return misa_bool(!val_equal(a, b)); }

/* returns -1/0/1 or raises E200 */
static int val_cmp(MVal *a, MVal *b, int line, int col) {
    if (both_int(a, b)) {
        if (a->u.i < b->u.i) return -1;
        if (a->u.i > b->u.i) return 1;
        return 0;
    }
    if (both_num(a, b)) {
        double x = to_f(a), y = to_f(b);
        if (x < y) return -1;
        if (x > y) return 1;
        return 0;
    }
    if (a->tag == M_TAG_TEXT && b->tag == M_TAG_TEXT) {
        size_t n = a->u.text.len < b->u.text.len ? a->u.text.len : b->u.text.len;
        int c = memcmp(a->u.text.data, b->u.text.data, n);
        if (c) return c < 0 ? -1 : 1;
        if (a->u.text.len < b->u.text.len) return -1;
        if (a->u.text.len > b->u.text.len) return 1;
        return 0;
    }
    misa_raise("E200", "cannot compare these values", line, col);
    return 0;
}

MVal *misa_lt(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    return misa_bool(val_cmp(a, b, line, col) < 0);
}
MVal *misa_le(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    return misa_bool(val_cmp(a, b, line, col) <= 0);
}
MVal *misa_gt(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    return misa_bool(val_cmp(a, b, line, col) > 0);
}
MVal *misa_ge(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    return misa_bool(val_cmp(a, b, line, col) >= 0);
}

/* ---------------- collections ------------------ */

int64_t misa_len(MVal *v, int line, int col) {
    switch (v->tag) {
    case M_TAG_TEXT:   return (int64_t)misa_text_nchars(v);
    case M_TAG_LIST:   return v->u.list.len;
    case M_TAG_LIST_BOOL: return v->u.list_bool.len;
    case M_TAG_LIST_INT: return v->u.list_int.len;
    case M_TAG_LIST_FLOAT: return v->u.list_float.len;
    case M_TAG_MAP:    return v->u.map.len;
    case M_TAG_RECORD: return v->u.record.len;
    case M_TAG_SET:    return v->u.set.len;
    case M_TAG_BYTES:  return (int64_t)v->u.bytes.len;
    default:
        misa_raise("E300", "len() needs text, list, map, set, bytes, "
                           "or record", line, col);
        return 0;
    }
}

int64_t misa_list_len(MVal *l) { return l->u.list.len; }
MVal *misa_list_get(MVal *l, int64_t i) { return l->u.list.items[i]; }

/* Phase 8 (overtake): wrap each element of a list as an interface
 * value `{__tag, __fields: [elem], __iface_disp}` with the given impl
 * tag. Used for [T] -> [I] conversions. */
MVal *misa_iface_wrap_list(int64_t tag, MVal *l) {
    MVal *out = val_new(M_TAG_LIST);
    for (int64_t i = 0; i < l->u.list.len; i++) {
        MVal *r = val_new(M_TAG_RECORD);
        misa_record_set_cstr(r, "__tag", misa_int(tag));
        MVal *fields = val_new(M_TAG_LIST);
        misa_list_push(fields, l->u.list.items[i]);
        misa_record_set_cstr(r, "__fields", fields);
        misa_record_set_cstr(r, "__iface_disp",
                             misa_text_cstr("__iface_payload"));
        misa_list_push(out, r);
    }
    return out;
}

/* Phase 5 (stdlib2): struct iteration for misa_json.c. */
int64_t misa_record_len(MVal *rec) { return rec->u.record.len; }
const char *misa_record_field_name(MVal *rec, int64_t i) {
    return rec->u.record.names[i];
}
MVal *misa_record_field_val(MVal *rec, int64_t i) {
    return rec->u.record.vals[i];
}
int64_t misa_map_len(MVal *m) { return m->u.map.len; }
MVal *misa_map_key(MVal *m, int64_t i) { return m->u.map.keys[i]; }
MVal *misa_map_val(MVal *m, int64_t i) { return m->u.map.vals[i]; }
int misa_bool_val(MVal *v) { return (int)v->u.b; }
int64_t misa_int_val(MVal *v) { return v->u.i; }
double misa_float_val(MVal *v) { return v->u.f; }
const char *misa_text_data(MVal *v) { return v->u.text.data; }

MVal *misa_index_get(MVal *obj, MVal *idx, int line, int col) {
    if (obj->tag == M_TAG_MAP) {
        for (int64_t i = 0; i < obj->u.map.len; i++) {
            if (val_equal(obj->u.map.keys[i], idx))
                return obj->u.map.vals[i];
        }
        /* E305 with a helpful key rendering (interpreter parity:
         * Text keys render Python-repr style, e.g. 'missing') */
        Str s = {0};
        str_puts(&s, "missing map key ");
        if (idx->tag == M_TAG_TEXT) {
            str_puts(&s, "'");
            render_into(&s, idx);
            str_puts(&s, "'");
        } else {
            render_into(&s, idx);
        }
        misa_raise("E305", s.buf ? s.buf : "missing map key", line, col);
        return NULL;
    }
    if (obj->tag == M_TAG_LIST) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return NULL;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return NULL;
        }
        return obj->u.list.items[i];
    }
    if (obj->tag == M_TAG_LIST_BOOL) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return NULL;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list_bool.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return NULL;
        }
        return misa_bool(obj->u.list_bool.items[i]);
    }
    if (obj->tag == M_TAG_LIST_INT) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return NULL;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list_int.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return NULL;
        }
        return misa_int(obj->u.list_int.items[i]);
    }
    if (obj->tag == M_TAG_LIST_FLOAT) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return NULL;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list_float.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return NULL;
        }
        return misa_float(obj->u.list_float.items[i]);
    }
    if (obj->tag == M_TAG_TEXT) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "text index must be Int", line, col);
            return NULL;
        }
        int64_t i = idx->u.i;
        int64_t n = (int64_t)misa_text_nchars(obj);
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return NULL;
        }
        return misa_text_char_at(obj, i, line, col);
    }
    misa_raise("E300", "cannot index into this value", line, col);
    return NULL;
}

void misa_index_set(MVal *obj, MVal *idx, MVal *val, int line, int col) {
    if (misa_pending()) return;
    if (obj->tag == M_TAG_MAP) {
        if (idx->tag != M_TAG_TEXT && idx->tag != M_TAG_INT) {
            misa_raise("E200", "map keys must be Text or Int", line, col);
            return;
        }
        for (int64_t i = 0; i < obj->u.map.len; i++) {
            if (val_equal(obj->u.map.keys[i], idx)) {
                obj->u.map.vals[i] = val;
                return;
            }
        }
        /* keys and vals share one capacity; grow both together */
        int64_t need = obj->u.map.len + 1;
        if (need > obj->u.map.cap) {
            int64_t ncap = obj->u.map.cap ? obj->u.map.cap * 2 : 8;
            while (ncap < need) ncap *= 2;
            MVal **nk = (MVal **)misa_alloc(sizeof(MVal *) * (size_t)ncap);
            MVal **nv = (MVal **)misa_alloc(sizeof(MVal *) * (size_t)ncap);
            if (obj->u.map.keys) {
                memcpy(nk, obj->u.map.keys,
                       sizeof(MVal *) * (size_t)obj->u.map.len);
                memcpy(nv, obj->u.map.vals,
                       sizeof(MVal *) * (size_t)obj->u.map.len);
            }
            obj->u.map.keys = nk;
            obj->u.map.vals = nv;
            obj->u.map.cap = ncap;
        }
        obj->u.map.keys[obj->u.map.len] = idx;
        obj->u.map.vals[obj->u.map.len] = val;
        obj->u.map.len++;
        return;
    }
    if (obj->tag == M_TAG_LIST) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return;
        }
        obj->u.list.items[i] = val;
        return;
    }
    if (obj->tag == M_TAG_LIST_BOOL) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return;
        }
        if (val->tag != M_TAG_BOOL) {
            misa_raise("E200", "type mismatch in [Bool] list", line, col);
            return;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list_bool.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return;
        }
        obj->u.list_bool.items[i] = val->u.b ? 1 : 0;
        return;
    }
    if (obj->tag == M_TAG_LIST_INT) {
        if (idx->tag != M_TAG_INT) {
            misa_raise("E200", "list index must be Int", line, col);
            return;
        }
        if (val->tag != M_TAG_INT) {
            misa_raise("E200", "type mismatch in [Int] list", line, col);
            return;
        }
        int64_t i = idx->u.i;
        int64_t n = obj->u.list_int.len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) {
            misa_raise("E300", "index out of range", line, col);
            return;
        }
        obj->u.list_int.items[i] = val->u.i;
        return;
    }
    misa_raise("E300", "cannot index-assign here", line, col);
}

MVal *misa_record_get(MVal *obj, const char *field, int line, int col) {
    if (obj->tag != M_TAG_RECORD) {
        misa_raise("E300", "cannot access field on this value", line, col);
        return NULL;
    }
    for (int64_t i = 0; i < obj->u.record.len; i++) {
        if (strcmp(obj->u.record.names[i], field) == 0)
            return obj->u.record.vals[i];
    }
    misa_raise_fmt("E300", line, col, "no field '%s'", field);
    return NULL;
}

/* Wave 3: non-raising field lookup (optional record fields). Returns
 * NULL without raising when the object is not a record or the field
 * is absent. */
MVal *misa_record_get_opt(MVal *obj, const char *field) {
    if (!obj || obj->tag != M_TAG_RECORD) return NULL;
    for (int64_t i = 0; i < obj->u.record.len; i++) {
        if (strcmp(obj->u.record.names[i], field) == 0)
            return obj->u.record.vals[i];
    }
    return NULL;
}

/* Fast path for statically-known record types: get field by index.
 * The AOT emitter computes the index at compile time from the TypeDecl,
 * avoiding the strcmp linear search. */
MVal *misa_record_get_idx(MVal *obj, int64_t idx, int line, int col) {
    if (obj->tag != M_TAG_RECORD) {
        misa_raise("E300", "cannot access field on this value", line, col);
        return NULL;
    }
    if (idx < 0 || idx >= obj->u.record.len) {
        misa_raise("E300", "field index out of bounds", line, col);
        return NULL;
    }
    return obj->u.record.vals[idx];
}

void misa_record_set(MVal *obj, const char *field, MVal *val,
                     int line, int col) {
    if (misa_pending()) return;
    if (obj->tag != M_TAG_RECORD) {
        misa_raise("E300", "cannot set field on this value", line, col);
        return;
    }
    misa_record_set_cstr(obj, field, val);
}

/* ---------------- for-loop iterator ------------------ */

MVal *misa_iter_new(MVal *v, int line, int col) {
    if (v->tag != M_TAG_LIST && v->tag != M_TAG_TEXT) {
        misa_raise("E300", "'for' needs a list or text", line, col);
        return NULL;
    }
    MVal *it = val_new(M_TAG_ITER);
    it->u.iter.src = v;
    it->u.iter.idx = 0;
    it->u.iter.nchars = v->tag == M_TAG_TEXT
        ? (int64_t)misa_text_nchars(v) : v->u.list.len;
    return it;
}

/* NULL = exhausted (never a pending error). */
MVal *misa_iter_next(MVal *it) {
    MVal *src = it->u.iter.src;
    int64_t i = it->u.iter.idx;
    if (i >= it->u.iter.nchars) return NULL;
    it->u.iter.idx = i + 1;
    if (src->tag == M_TAG_LIST) return src->u.list.items[i];
    return misa_text_char_at(src, i, 0, 0);
}

/* ---------------- builtins ------------------ */

MVal *misa_print(MVal *v) {
    MVal *t = misa_render(v);
    /* P1: print goes to the platform log (stdout on Linux). */
    misa_platform_log(t->u.text.data, t->u.text.len);
    misa_platform_log("\n", 1);
    misa_platform_flush();
    return t;
}

MVal *misa_builtin_len(MVal *v, int line, int col) {
    if (misa_pending()) return NULL;
    return misa_int(misa_len(v, line, col));
}

MVal *misa_builtin_range(MVal *a, MVal *b, MVal *c, int has_b, int has_c) {
    if (misa_pending()) return NULL;
    int64_t start = 0, stop, step = 1;
    if (!has_b) {
        stop = misa_as_int(a);
    } else if (!has_c) {
        start = misa_as_int(a);
        stop = misa_as_int(b);
    } else {
        start = misa_as_int(a);
        stop = misa_as_int(b);
        step = misa_as_int(c);
    }
    MVal *list = misa_list_new();
    if (step > 0) {
        for (int64_t i = start; i < stop; i += step)
            misa_list_push(list, misa_int(i));
    } else if (step < 0) {
        for (int64_t i = start; i > stop; i += step)
            misa_list_push(list, misa_int(i));
    }
    return list;
}

MVal *misa_builtin_str(MVal *v) { return misa_render(v); }

MVal *misa_builtin_int(MVal *v, int line, int col) {
    if (v->tag == M_TAG_INT) return v;
    if (v->tag == M_TAG_FLOAT) {
        double f = v->u.f;
        if (!(f > -9.223372036854776e18 && f < 9.223372036854776e18)) {
            misa_raise("E300", "float out of Int range", line, col);
            return NULL;
        }
        return misa_int((int64_t)f); /* truncates toward zero, like Python */
    }
    if (v->tag == M_TAG_TEXT) {
        const char *s = v->u.text.data;
        /* Python int(str) strips whitespace and needs the full string.
           C-1: 64-bit wrapping — parse with wraparound, never clamp. */
        while (*s && isspace((unsigned char)*s)) s++;
        if (!*s) goto bad;
        int neg = 0;
        if (*s == '+' || *s == '-') { neg = (*s == '-'); s++; }
        if (!isdigit((unsigned char)*s)) goto bad;
        uint64_t acc = 0;
        while (isdigit((unsigned char)*s)) {
            acc = acc * 10 + (uint64_t)(*s - '0'); /* wraps mod 2^64 */
            s++;
        }
        while (*s && isspace((unsigned char)*s)) s++;
        if (*s) goto bad;
        int64_t r = neg ? (int64_t)(0u - acc) : (int64_t)acc;
        return misa_int(r);
    bad:
        misa_raise_fmt("E300", line, col, "cannot parse int from '%s'",
                       v->u.text.data);
        return NULL;
    }
    misa_raise("E300", "cannot convert this value to Int", line, col);
    return NULL;
}

MVal *misa_builtin_float(MVal *v, int line, int col) {
    if (v->tag == M_TAG_FLOAT) return v;
    if (v->tag == M_TAG_INT) return misa_float((double)v->u.i);
    if (v->tag == M_TAG_TEXT) {
        const char *s = v->u.text.data;
        char *end = NULL;
        double r = strtod(s, &end);
        if (end == s) {
            misa_raise_fmt("E300", line, col,
                           "cannot parse float from '%s'", s);
            return NULL;
        }
        return misa_float(r);
    }
    misa_raise("E300", "cannot convert this value to Float", line, col);
    return NULL;
}

MVal *misa_builtin_assert(MVal *cond, MVal *msg, int line, int col) {
    if (misa_pending()) return NULL;
    if (!misa_truthy(cond)) {
        MVal *m = msg && msg->tag != M_TAG_NULL ? misa_render(msg)
                                                : misa_text_cstr("assertion failed");
        misa_raise("E301", misa_text_z(m), line, col);
        return NULL;
    }
    return misa_bool(1);
}

MVal *misa_builtin_assert_eq(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    MVal *eq = misa_eq(a, b);
    if (!misa_truthy(eq)) {
        misa_raise_fmt("E301", line, col, "assert_eq failed: %s != %s",
                       misa_text_z(misa_render(a)),
                       misa_text_z(misa_render(b)));
        return NULL;
    }
    return misa_bool(1);
}

MVal *misa_builtin_assert_ne(MVal *a, MVal *b, int line, int col) {
    if (misa_pending()) return NULL;
    MVal *eq = misa_eq(a, b);
    if (misa_truthy(eq)) {
        misa_raise_fmt("E301", line, col, "assert_ne failed: both are %s",
                       misa_text_z(misa_render(a)));
        return NULL;
    }
    return misa_bool(1);
}

MVal *misa_builtin_env(const char *name, int line, int col) {
    /* P3: web/mobile have no process environment; the platform reads
     * an app-supplied config table instead. */
    const char *v = misa_platform_getenv(name);
    if (!v) {
        /* M-8: STDLIB.md promises E302 for an unset variable. */
        misa_raise_fmt("E302", line, col, "env var '%s' is not set", name);
        return NULL;
    }
    return misa_text_cstr(v);
}

/* ---------------- argv ------------------ */
/* Hosted targets stash argc/argv from main(); Android/iOS/web never
 * call misa_argv_init, so argv() is [] there (documented in STDLIB.md). */
static int g_argc = 0;
static char **g_argv = NULL;

void misa_argv_init(int argc, char **argv) {
    g_argc = argc;
    g_argv = argv;
}

MVal *misa_builtin_argv(void) {
    MVal *list = misa_list_new();
    /* Skip argv[0] (the program name), like Python's sys.argv[1:]. */
    for (int i = 1; i < g_argc; i++)
        misa_list_push(list, misa_text_cstr(g_argv[i]));
    return list;
}

MVal *misa_builtin_media(int kind, MVal *path, int line, int col) {
    if (path->tag != M_TAG_TEXT) {
        misa_raise("E200", "media path must be Text", line, col);
        return NULL;
    }
    return misa_media(kind, misa_text_z(path));
}

/* ---------------- fs ------------------ */

MVal *misa_fs_read(MVal *path, int line, int col) {
    const char *p = misa_text_z(path);
    /* P4: map the app-relative path onto the platform filesystem
     * (identity on Linux: cwd-relative, v0.3 behavior). */
    char mp[4096];
    if (!misa_platform_fs_path(p, mp, sizeof mp)) {
        misa_raise_fmt("E303", line, col,
                       "fs.read failed: cannot map path '%s'", p);
        return NULL;
    }
    FILE *f = fopen(mp, "rb");
    if (!f) {
        misa_raise_fmt("E303", line, col, "fs.read failed: cannot open '%s'", p);
        return NULL;
    }
    Str s = {0};
    char buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) str_putn(&s, buf, n);
    if (ferror(f)) {
        fclose(f);
        misa_raise_fmt("E303", line, col, "fs.read failed: error reading '%s'", p);
        return NULL;
    }
    fclose(f);
    /* must be valid UTF-8 text (interpreter parity) */
    MVal *t = misa_text(s.buf ? s.buf : "", s.len);
    const unsigned char *d = (const unsigned char *)t->u.text.data;
    for (size_t i = 0; i < t->u.text.len;) {
        unsigned char c = d[i];
        size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
        if (!w || i + w > t->u.text.len) {
            misa_raise_fmt("E303", line, col,
                           "fs.read failed: '%s' is not UTF-8 text", p);
            return NULL;
        }
        i += w;
    }
    return t;
}

MVal *misa_fs_write(MVal *path, MVal *text, int line, int col) {
    const char *p = misa_text_z(path);
    if (text->tag != M_TAG_TEXT) {
        misa_raise("E200", "fs.write needs Text content", line, col);
        return NULL;
    }
    /* P4: map the app-relative path onto the platform filesystem. */
    char mp[4096];
    if (!misa_platform_fs_path(p, mp, sizeof mp)) {
        misa_raise_fmt("E303", line, col,
                       "fs.write failed: cannot map path '%s'", p);
        return NULL;
    }
    FILE *f = fopen(mp, "wb");
    if (!f) {
        misa_raise_fmt("E303", line, col, "fs.write failed: cannot open '%s'", p);
        return NULL;
    }
    size_t n = fwrite(text->u.text.data, 1, text->u.text.len, f);
    int err = ferror(f);
    fclose(f);
    if (err || n != text->u.text.len) {
        misa_raise_fmt("E303", line, col, "fs.write failed: error writing '%s'", p);
        return NULL;
    }
    /* Phase 4 (M3): let the platform persist (IDBFS sync on web). */
    misa_platform_fs_after_write(mp);
    return misa_null();
}

/* ---------------- http.get (platform transport) ------------------ */
/* Phase 4 (M3): the transport moved behind misa_platform_http_get
 * (libcurl on Linux, emscripten_fetch on web). Error shapes are
 * unchanged (E304, interpreter parity).
 * Phase 7: the builtin itself moved to misa_task.c (fiber-aware: async
 * through the scheduler's curl_multi when available, blocking
 * otherwise). The response validation below is shared. */

MVal *misa_http_validate(const char *u, const char *body, size_t len,
                         long status, int line, int col) {
    if (status < 200 || status >= 300) {
        misa_raise_fmt("E304", line, col, "http.get %s -> HTTP %d", u,
                       (int)status);
        return NULL;
    }
    /* body must decode as UTF-8 (interpreter parity) */
    const unsigned char *d = (const unsigned char *)(body ? body : "");
    for (size_t i = 0; i < len;) {
        unsigned char c = d[i];
        size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
        if (!w || i + w > len) {
            misa_raise_fmt("E304", line, col,
                           "http.get %s: body is not UTF-8 text", u);
            return NULL;
        }
        i += w;
    }
    return misa_text((const char *)d, len);
}

/* Phase 7: allocate a zeroed MVal with the given tag (Task/Chan
 * handles in misa_task.c; the struct stays private to this file). */
MVal *misa_val_new(uint8_t tag) { return val_new(tag); }
void misa_val_set_ptr(MVal *v, void *p) { v->u.ptr = p; }
void *misa_val_get_ptr(MVal *v) { return v->u.ptr; }

/* ---------------- http.post (platform transport) ------------------ */
/* Phase 5 (stdlib2): POST with a caller-chosen Content-Type. Error
 * shapes mirror http.get (E304, interpreter parity). */

MVal *misa_http_post(MVal *url, MVal *body, const char *content_type,
                     int line, int col) {
    const char *u = misa_text_z(url);
    if (strncmp(u, "http://", 7) != 0 && strncmp(u, "https://", 8) != 0) {
        misa_raise_fmt("E304", line, col,
                       "http.post needs an http(s) URL, got '%s'", u);
        return NULL;
    }
    const char *b = misa_text_z(body);
    /* content_type arrives as a raw C string constant from the
     * emitter (always "text/plain" or "application/json"). */
    const char *ct = content_type;
    MisaHttpResp r;
    memset(&r, 0, sizeof r);
    if (!misa_platform_http_post(u, ct, NULL, 0, b, 30.0, &r)) {
        misa_raise_fmt("E304", line, col, "http.post failed: %s", r.err);
        misa_platform_http_free(&r);
        return NULL;
    }
    long status = r.status;
    if (status < 200 || status >= 300) {
        misa_raise_fmt("E304", line, col, "http.post %s -> HTTP %d", u,
                       (int)status);
        misa_platform_http_free(&r);
        return NULL;
    }
    const unsigned char *d = (const unsigned char *)(r.body ? r.body : "");
    size_t len = r.len;
    for (size_t i = 0; i < len;) {
        unsigned char c = d[i];
        size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
        if (!w || i + w > len) {
            misa_raise_fmt("E304", line, col,
                           "http.post %s: body is not UTF-8 text", u);
            misa_platform_http_free(&r);
            return NULL;
        }
        i += w;
    }
    MVal *t = misa_text((const char *)d, len);
    misa_platform_http_free(&r);
    return t;
}

/* ---------------- time namespace ------------------ */
/* Phase 5 (stdlib2). time.format/parse use UTC (interpreter parity:
 * time.gmtime / calendar.timegm). */

MVal *misa_time_now(void) {
    return misa_int((int64_t)time(NULL));
}

MVal *misa_time_now_ms(void) {
    struct timespec ts;
#if defined(CLOCK_REALTIME)
    clock_gettime(CLOCK_REALTIME, &ts);
    return misa_int((int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
#else
    return misa_int((int64_t)time(NULL) * 1000);
#endif
}

MVal *misa_time_format(MVal *epoch, MVal *fmt, int line, int col) {
    time_t t = (time_t)epoch->u.i;
    struct tm tmv;
#if defined(_WIN32)
    if (gmtime_s(&tmv, &t) != 0) {
#else
    if (gmtime_r(&t, &tmv) == NULL) {
#endif
        misa_raise_fmt("E300", line, col, "time.format: bad epoch");
        return NULL;
    }
    char buf[256];
    size_t n = strftime(buf, sizeof buf, misa_text_z(fmt), &tmv);
    if (n == 0) {
        misa_raise_fmt("E300", line, col, "time.format failed");
        return NULL;
    }
    return misa_text(buf, n);
}

/* Minimal strptime for the formats we document; falls back to the
 * libc strptime where available. */
static int time_parse_fields(const char *s, const char *fmt, struct tm *tmv) {
#if defined(__EMSCRIPTEN__)
    /* Emscripten/wasm must not depend on libc strptime. Keep the
     * portable parser below so the shared runtime has no X/Open time
     * dependency on the web target. */
    memset(tmv, 0, sizeof *tmv);
    int y, mo, d, h = 0, mi = 0, se = 0;
    if (strcmp(fmt, "%Y-%m-%d") == 0) {
        if (sscanf(s, "%d-%d-%d", &y, &mo, &d) != 3) return 0;
    } else if (strcmp(fmt, "%Y-%m-%d %H:%M:%S") == 0) {
        if (sscanf(s, "%d-%d-%d %d:%d:%d", &y, &mo, &d, &h, &mi, &se) != 6) return 0;
    } else return 0;
    tmv->tm_year=y-1900; tmv->tm_mon=mo-1; tmv->tm_mday=d;
    tmv->tm_hour=h; tmv->tm_min=mi; tmv->tm_sec=se;
    return 1;
#elif defined(_XOPEN_SOURCE) || defined(__linux__) || defined(__APPLE__)
    memset(tmv, 0, sizeof *tmv);
    char *r = strptime(s, fmt, tmv);
    return r != NULL && *r == '\0';
#else
    /* Portable fallback: only "%Y-%m-%d" and "%Y-%m-%d %H:%M:%S". */
    memset(tmv, 0, sizeof *tmv);
    int y, mo, d, h = 0, mi = 0, se = 0;
    if (strcmp(fmt, "%Y-%m-%d") == 0) {
        if (sscanf(s, "%d-%d-%d", &y, &mo, &d) != 3)
            return 0;
    } else if (strcmp(fmt, "%Y-%m-%d %H:%M:%S") == 0) {
        if (sscanf(s, "%d-%d-%d %d:%d:%d", &y, &mo, &d, &h, &mi,
                   &se) != 6)
            return 0;
    } else {
        return 0;
    }
    tmv->tm_year = y - 1900;
    tmv->tm_mon = mo - 1;
    tmv->tm_mday = d;
    tmv->tm_hour = h;
    tmv->tm_min = mi;
    tmv->tm_sec = se;
    return 1;
#endif
}

MVal *misa_time_parse(MVal *s, MVal *fmt, int line, int col) {
    struct tm tmv;
    if (!time_parse_fields(misa_text_z(s), misa_text_z(fmt), &tmv)) {
        misa_raise_fmt("E300", line, col, "time.parse: bad time string");
        return NULL;
    }
    /* timegm: days since epoch in UTC. Not in strict C99; provide a
     * portable fallback via the civil-days formula. */
    time_t t;
#if defined(__linux__) || defined(__APPLE__) || defined(__FreeBSD__)
    t = timegm(&tmv);
#else
    {
        /* Portable: compute days since epoch directly (Howard Hinnant's
         * civil-days algorithm). Used for Emscripten and other targets
         * without timegm. */
        int y = tmv.tm_year + 1900, m = tmv.tm_mon + 1, d = tmv.tm_mday;
        y -= m <= 2;
        int era = (y >= 0 ? y : y - 399) / 400;
        unsigned yoe = (unsigned)(y - era * 400);
        unsigned doy =
            (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + (unsigned)(d - 1);
        unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;
        t = (time_t)(days * 86400 + tmv.tm_hour * 3600 +
                     tmv.tm_min * 60 + tmv.tm_sec);
    }
#endif
    if (t == (time_t)-1) {
        misa_raise_fmt("E300", line, col, "time.parse: bad time string");
        return NULL;
    }
    return misa_int((int64_t)t);
}

/* ---------------- math namespace ------------------ */
/* Phase 5 (stdlib2). Proper builtins (not FFI) so mobile targets get
 * them too. libm is already linked on host and Android. */

static double math_num(MVal *v, int line, int col, const char *op) {
    if (v->tag == M_TAG_INT)
        return (double)v->u.i;
    if (v->tag == M_TAG_FLOAT)
        return v->u.f;
    misa_raise_fmt("E300", line, col, "math.%s needs a numeric arg", op);
    return 0;
}

MVal *misa_math_sqrt(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "sqrt");
    if (misa_pending())
        return NULL;
    if (d < 0) {
        misa_raise_fmt("E300", line, col,
                       "math.sqrt: domain error (negative input)");
        return NULL;
    }
    return misa_float(sqrt(d));
}

MVal *misa_math_cos(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "cos");
    if (misa_pending()) return NULL;
    return misa_float(cos(d));
}

MVal *misa_math_sin(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "sin");
    if (misa_pending()) return NULL;
    return misa_float(sin(d));
}

MVal *misa_math_tan(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "tan");
    if (misa_pending()) return NULL;
    return misa_float(tan(d));
}

MVal *misa_math_pow(MVal *x, MVal *y, int line, int col) {
    double a = math_num(x, line, col, "pow");
    if (misa_pending())
        return NULL;
    double b = math_num(y, line, col, "pow");
    if (misa_pending())
        return NULL;
    errno = 0;
    double r = pow(a, b);
    if (errno == EDOM) {
        misa_raise_fmt("E300", line, col, "math.pow: domain error");
        return NULL;
    }
    return misa_float(r);
}

MVal *misa_math_abs(MVal *x, int line, int col) {
    if (x->tag == M_TAG_INT) {
        int64_t v = x->u.i;
        /* INT64_MIN abs overflows; keep as-is is wrong — raise. */
        if (v == INT64_MIN) {
            misa_raise_fmt("E300", line, col,
                           "math.abs: result out of Int range");
            return NULL;
        }
        return misa_int(v < 0 ? -v : v);
    }
    double d = math_num(x, line, col, "abs");
    if (misa_pending())
        return NULL;
    return misa_float(fabs(d));
}

static MVal *math_to_int(double d, int line, int col, const char *op) {
    if (!isfinite(d) || d > 9.223372036854776e18 ||
        d < -9.223372036854776e18) {
        misa_raise_fmt("E300", line, col,
                       "math.%s: result out of Int range", op);
        return NULL;
    }
    return misa_int((int64_t)d);
}

MVal *misa_math_floor(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "floor");
    if (misa_pending())
        return NULL;
    return math_to_int(floor(d), line, col, "floor");
}

MVal *misa_math_ceil(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "ceil");
    if (misa_pending())
        return NULL;
    return math_to_int(ceil(d), line, col, "ceil");
}

MVal *misa_math_round(MVal *x, int line, int col) {
    double d = math_num(x, line, col, "round");
    if (misa_pending())
        return NULL;
    /* C round() is already half-away-from-zero. */
    return math_to_int(round(d), line, col, "round");
}

/* ---------------- str namespace ------------------ */
/* Phase 5 prerequisite (self-hosting). Code-point semantics matching the
 * Python interpreter exactly — NOT byte semantics. */

static size_t str_utf8_decode(const unsigned char *p, size_t maxlen,
                              uint32_t *cp) {
    if (maxlen == 0) return 0;
    unsigned char c = p[0];
    if (c < 0x80) { *cp = c; return 1; }
    if ((c & 0xE0) == 0xC0 && maxlen >= 2) {
        *cp = ((uint32_t)(c & 0x1F) << 6) | (p[1] & 0x3F);
        return 2;
    }
    if ((c & 0xF0) == 0xE0 && maxlen >= 3) {
        *cp = ((uint32_t)(c & 0x0F) << 12) | ((uint32_t)(p[1] & 0x3F) << 6)
              | (p[2] & 0x3F);
        return 3;
    }
    if ((c & 0xF8) == 0xF0 && maxlen >= 4) {
        *cp = ((uint32_t)(c & 0x07) << 18) | ((uint32_t)(p[1] & 0x3F) << 12)
              | ((uint32_t)(p[2] & 0x3F) << 6) | (p[3] & 0x3F);
        return 4;
    }
    *cp = c; /* malformed: treat as single byte */
    return 1;
}

static size_t str_utf8_encode(uint32_t cp, char *out) {
    if (cp < 0x80) { out[0] = (char)cp; return 1; }
    if (cp < 0x800) {
        out[0] = (char)(0xC0 | (cp >> 6));
        out[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (char)(0xE0 | (cp >> 12));
        out[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    out[0] = (char)(0xF0 | (cp >> 18));
    out[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    out[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
    out[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

/* Byte offset of code-point index idx in [data, data+len). */
static size_t str_byte_off(const char *data, size_t len, size_t idx) {
    size_t off = 0, n = 0;
    while (off < len && n < idx) {
        uint32_t cp;
        off += str_utf8_decode((const unsigned char *)data + off,
                               len - off, &cp);
        n++;
    }
    return off;
}

/* Python str.strip() whitespace set (the Bidirectional/space separators
 * Python treats as whitespace). */
static int str_is_space(uint32_t cp) {
    if (cp == 0x20 || (cp >= 0x09 && cp <= 0x0D) || cp == 0x85 || cp == 0xA0
        || cp == 0x1680 || cp == 0x2028 || cp == 0x2029 || cp == 0x202F
        || cp == 0x205F || cp == 0x3000)
        return 1;
    if (cp >= 0x2000 && cp <= 0x200A)
        return 1;
    return 0;
}

MVal *misa_str_slice(MVal *s, MVal *i, MVal *j, int line, int col) {
    if (s->tag != M_TAG_TEXT || i->tag != M_TAG_INT || j->tag != M_TAG_INT) {
        misa_raise("E300", "str.slice(Text, Int, Int) needs text + ints",
                   line, col);
        return NULL;
    }
    const char *data = s->u.text.data;
    size_t len = s->u.text.len;
    int64_t n = (int64_t)misa_text_nchars(s);
    int64_t a = i->u.i, b = j->u.i;
    if (a < 0) a = 0;
    if (a > n) a = n;
    if (b < 0) b = 0;
    if (b > n) b = n;
    if (a >= b)
        return misa_text("", 0);
    size_t start = str_byte_off(data, len, (size_t)a);
    size_t end = str_byte_off(data, len, (size_t)b);
    return misa_text(data + start, end - start);
}

MVal *misa_str_starts_with(MVal *s, MVal *prefix, int line, int col) {
    if (s->tag != M_TAG_TEXT || prefix->tag != M_TAG_TEXT) {
        misa_raise("E300", "str.starts_with(Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    /* Byte-prefix check is correct for UTF-8 under code-point semantics. */
    size_t sl = s->u.text.len, pl = prefix->u.text.len;
    if (pl > sl)
        return misa_bool(0);
    return misa_bool(memcmp(s->u.text.data, prefix->u.text.data, pl) == 0);
}

MVal *misa_str_split(MVal *s, MVal *sep, int line, int col) {
    if (s->tag != M_TAG_TEXT || sep->tag != M_TAG_TEXT) {
        misa_raise("E300", "str.split(Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    if (sep->u.text.len == 0) {
        misa_raise("E300", "str.split separator is empty", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    const char *data = s->u.text.data, *sp = sep->u.text.data;
    size_t len = s->u.text.len, slen = sep->u.text.len;
    size_t start = 0;
    for (size_t k = 0; k + slen <= len; k++) {
        if (memcmp(data + k, sp, slen) == 0) {
            misa_list_push(out, misa_text(data + start, k - start));
            start = k + slen;
            k += slen - 1;
        }
    }
    misa_list_push(out, misa_text(data + start, len - start));
    return out;
}

/* W-4: str.chars(s) -> [Text]: the code points of s as single-char
 * texts. This is exactly what `for x in s` iterates; the explicit
 * form is discoverable and composes (len, indexing, etc.). */
MVal *misa_str_chars(MVal *s, int line, int col) {
    if (s->tag != M_TAG_TEXT) {
        misa_raise("E300", "str.chars(Text) needs a text arg",
                   line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    int64_t n = (int64_t)misa_text_nchars(s);
    for (int64_t i = 0; i < n; i++)
        misa_list_push(out, misa_text_char_at(s, i, line, col));
    return out;
}

MVal *misa_str_trim(MVal *s, int line, int col) {
    if (s->tag != M_TAG_TEXT) {
        misa_raise("E300", "str.trim(Text) needs a text arg", line, col);
        return NULL;
    }
    const char *data = s->u.text.data;
    size_t len = s->u.text.len;
    size_t lo = 0;
    while (lo < len) {
        uint32_t cp;
        size_t nb = str_utf8_decode((const unsigned char *)data + lo,
                                    len - lo, &cp);
        if (!str_is_space(cp))
            break;
        lo += nb;
    }
    size_t hi = len;
    while (hi > lo) {
        /* step back one code point: rewind over continuation bytes */
        size_t p = hi - 1;
        while (p > lo && (((unsigned char)data[p] & 0xC0) == 0x80))
            p--;
        uint32_t cp;
        str_utf8_decode((const unsigned char *)data + p, hi - p, &cp);
        if (!str_is_space(cp))
            break;
        hi = p;
    }
    return misa_text(data + lo, hi - lo);
}

MVal *misa_str_ord(MVal *c, int line, int col) {
    if (c->tag != M_TAG_TEXT || c->u.text.len == 0) {
        misa_raise("E300", "str.ord(Text) needs a non-empty text arg",
                   line, col);
        return NULL;
    }
    uint32_t cp;
    str_utf8_decode((const unsigned char *)c->u.text.data,
                    c->u.text.len, &cp);
    return misa_int((int64_t)cp);
}

MVal *misa_str_chr(MVal *code, int line, int col) {
    if (code->tag != M_TAG_INT) {
        misa_raise("E300", "str.chr(Int) needs an int arg", line, col);
        return NULL;
    }
    int64_t v = code->u.i;
    uint32_t cp = (uint32_t)v;
    if (v < 0 || v > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
        misa_raise_fmt("E300", line, col,
                       "str.chr: invalid code point %lld",
                       (long long)v);
        return NULL;
    }
    char buf[4];
    size_t n = str_utf8_encode(cp, buf);
    return misa_text(buf, n);
}

/* ---------------- screens ------------------ */
/* The 7 misa_screen_* functions now live in ui_text.c: they are thin
 * shims over the text UI driver (rt/misa_ui.h), selected at link
 * time. See that file for the implementation. */

/* ---------------- screen state ------------------ */
/* Ephemeral per-screen-instance state for the `state()` primitive.
 * One current frame (v1: navigate() replaces it — state is per
 * navigation, like React unmounting on navigation away). Cells are
 * 1-element lists (heap-allocated, GC-managed — the same trick as
 * Phase 5a closure cells). The states/args arrays use misa_alloc so the
 * collector (GC builds) scans them without explicit root
 * registration. */

typedef MVal *(*sframe_rerender_t)(void);

typedef struct {
    char *name;
    sframe_rerender_t rerender;  /* per-screen thunk (emitter-generated) */
    MVal **args;                 /* stashed screen args for re-render */
    int32_t nargs;
    MVal **states;               /* NULL = slot not yet created */
    int32_t capstates;
    int32_t dirty;               /* set by misa_state_set */
} ScreenFrame;

static ScreenFrame g_sframe = {0};

void misa_screen_frame_push(const char *name, void *rerender_thunk,
                            int32_t nargs, MVal **args) {
    ScreenFrame *f = &g_sframe;
    free(f->name);
    f->name = name ? strdup(name) : NULL;
    if (name && !f->name) misa_platform_fatal("E300", "out of memory");
    f->rerender = (sframe_rerender_t)rerender_thunk;
    f->nargs = nargs;
    if (nargs > 0 && args) {
        f->args = (MVal **)misa_alloc(sizeof(MVal *) * (size_t)nargs);
        memcpy(f->args, args, sizeof(MVal *) * (size_t)nargs);
    } else {
        f->args = NULL;
    }
    /* Fresh navigation: drop all state cells (GC reclaims them). */
    f->states = NULL;
    f->capstates = 0;
    f->dirty = 0;
}

MVal **misa_screen_frame_argv(void) {
    return g_sframe.args;
}

static void sframe_grow_states(ScreenFrame *f, int32_t slot) {
    if (slot < f->capstates) return;
    int32_t ncap = f->capstates ? f->capstates * 2 : 8;
    while (ncap <= slot) ncap *= 2;
    MVal **ns = (MVal **)misa_alloc(sizeof(MVal *) * (size_t)ncap);
    for (int32_t i = 0; i < f->capstates; i++) ns[i] = f->states[i];
    for (int32_t i = f->capstates; i < ncap; i++) ns[i] = NULL;
    f->states = ns;
    f->capstates = ncap;
}

MVal *misa_screen_state_cell(int32_t slot, MVal *initial) {
    ScreenFrame *f = &g_sframe;
    if (slot < 0) misa_platform_fatal("E300", "bad state slot");
    sframe_grow_states(f, slot);
    if (!f->states[slot]) {
        /* get-or-create: the initial value is used only the first
         * time this slot is visited (re-renders keep the cell). */
        MVal *cell = misa_list_new();
        misa_list_push(cell, initial);
        f->states[slot] = cell;
    }
    return f->states[slot];
}

MVal *misa_state_get(MVal *cell) {
    return misa_list_get(cell, 0);
}

void misa_state_set(MVal *cell, MVal *v) {
    /* The cell is always a 1-element list created above. */
    cell->u.list.items[0] = v;
    /* Mark the current screen dirty so the driver re-renders after
     * the current event handler returns. */
    g_sframe.dirty = 1;
}

MVal *misa_screen_state_read_slot(int32_t slot) {
    ScreenFrame *f = &g_sframe;
    if (slot < 0 || slot >= f->capstates || !f->states[slot])
        return misa_null();  /* branch never taken this instance */
    return misa_state_get(f->states[slot]);
}

void misa_screen_state_write_slot(int32_t slot, MVal *v) {
    ScreenFrame *f = &g_sframe;
    if (slot < 0) misa_platform_fatal("E300", "bad state slot");
    sframe_grow_states(f, slot);
    if (!f->states[slot]) {
        MVal *cell = misa_list_new();
        misa_list_push(cell, v);
        f->states[slot] = cell;
    } else {
        misa_state_set(f->states[slot], v);
        return;
    }
    f->dirty = 1;
}

void misa_screen_after_handler(void) {
    ScreenFrame *f = &g_sframe;
    if (f->dirty && f->rerender) {
        f->dirty = 0;
        f->rerender();
        if (misa_pending()) {
            /* A failed re-render must not kill the app: surface it
             * through the standard handler-boundary error channel. */
            MVal *err = misa_take_pending();
            misa_ui_report_error(misa_ui_active(), err);
        }
    }
}

/* ==================================================================
 * Wave 3 (stdlib expansion)
 * ================================================================== */

/* Forward declarations for types used before their definitions. */
typedef struct MisaRx MisaRx;

/* -- UTF-8 helpers (shared by the regex engine) -------------------- */

static uint32_t utf8_decode_one(const unsigned char **pp) {
    const unsigned char *p = *pp;
    uint32_t c = *p;
    if (c < 0x80) { *pp = p + 1; return c; }
    if ((c & 0xE0) == 0xC0) {
        *pp = p + 2;
        return ((c & 0x1F) << 6) | (p[1] & 0x3F);
    }
    if ((c & 0xF0) == 0xE0) {
        *pp = p + 3;
        return ((c & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F);
    }
    *pp = p + 4;
    return ((c & 0x07) << 18) | ((p[1] & 0x3F) << 12) |
           ((p[2] & 0x3F) << 6) | (p[3] & 0x3F);
}

/* Decode UTF-8 text to code points; `offs[i]` = byte offset of cps[i].
 * `offs[n]` = total bytes (sentinel). */
typedef struct { uint32_t *cps; size_t *offs; size_t n; } Decoded;
static Decoded decode_text(const char *data, size_t len) {
    Decoded d;
    d.cps = (uint32_t *)misa_alloc((len + 1) * sizeof(uint32_t));
    d.offs = (size_t *)misa_alloc((len + 2) * sizeof(size_t));
    d.n = 0;
    const unsigned char *p = (const unsigned char *)data;
    const unsigned char *end = p + len;
    while (p < end) {
        d.offs[d.n] = (size_t)(p - (const unsigned char *)data);
        d.cps[d.n++] = utf8_decode_one(&p);
    }
    d.offs[d.n] = len;
    return d;
}

/* -- Portable regex engine (mirrors impl/misa/regexp.py exactly) --- */

typedef enum {
    RX_LIT, RX_DOT, RX_CLASS, RX_SEQ, RX_ALT, RX_QUANT, RX_GROUP,
    RX_START, RX_END
} RxKind;

typedef struct RxNode RxNode;
struct RxNode {
    RxKind kind;
    uint32_t ch;                 /* RX_LIT */
    int neg;                     /* RX_CLASS */
    uint32_t *ranges; int nr;    /* RX_CLASS: (lo,hi) pairs */
    RxNode **kids; int nk;       /* RX_SEQ */
    RxNode *left, *right;        /* RX_ALT */
    RxNode *child; int qmin, qmax; /* RX_QUANT: qmax<0 = unbounded */
    int gidx;                    /* RX_GROUP: 1-based */
};

typedef struct MisaRx {
    RxNode *root;
    int ngroups;
    char *pattern;               /* owned copy, for rendering */
} MisaRx;

typedef struct {
    const char *pat;
    size_t n, i;
    int ngroups;
    char err[160];
    int failed;
} RxParser;

static RxNode *rx_new(RxKind k) {
    RxNode *nd = (RxNode *)misa_alloc(sizeof(RxNode));
    memset(nd, 0, sizeof(RxNode));
    nd->kind = k;
    return nd;
}

static void rx_fail(RxParser *p, const char *msg) {
    if (!p->failed) {
        snprintf(p->err, sizeof(p->err), "%s (at index %zu)", msg, p->i);
        p->failed = 1;
    }
}

static RxNode *rx_parse_alt(RxParser *p);
static RxNode *rx_parse_seq(RxParser *p);

static uint32_t rx_parse_class_char(RxParser *p) {
    char c = p->pat[p->i];
    if (c == '\\') {
        p->i++;
        if (p->i >= p->n) { rx_fail(p, "pattern ends with '\\'"); return 0; }
        char e = p->pat[p->i++];
        switch (e) {
        case 'n': return '\n'; case 't': return '\t'; case '\\': return '\\';
        case '.': return '.'; case '*': return '*'; case '+': return '+';
        case '?': return '?'; case '(': return '('; case ')': return ')';
        case '[': return '['; case ']': return ']'; case '{': return '{';
        case '}': return '}'; case '|': return '|'; case '^': return '^';
        case '$': return '$'; default: break;
        }
        rx_fail(p, "unsupported escape in class");
        return 0;
    }
    p->i++;
    return (uint32_t)(unsigned char)c;
}

static RxNode *rx_parse_class(RxParser *p) {
    p->i++; /* consume '[' */
    int neg = 0;
    if (p->i < p->n && p->pat[p->i] == '^') { neg = 1; p->i++; }
    RxNode *nd = rx_new(RX_CLASS);
    nd->neg = neg;
    int cap = 8;
    nd->ranges = (uint32_t *)misa_alloc(cap * 2 * sizeof(uint32_t));
    int first = 1;
    for (;;) {
        if (p->i >= p->n) { rx_fail(p, "unclosed '['"); return nd; }
        if (p->pat[p->i] == ']' && !first) { p->i++; break; }
        first = 0;
        uint32_t lo = rx_parse_class_char(p);
        if (p->failed) return nd;
        uint32_t hi = lo;
        if (p->i < p->n && p->pat[p->i] == '-' && p->i + 1 < p->n &&
            p->pat[p->i + 1] != ']') {
            p->i++;
            hi = rx_parse_class_char(p);
            if (p->failed) return nd;
            if (hi < lo) { rx_fail(p, "invalid range"); return nd; }
        }
        if (nd->nr * 2 + 2 > cap * 2) {
            cap *= 2;
            uint32_t *nr = (uint32_t *)misa_alloc(cap * 2 * sizeof(uint32_t));
            memcpy(nr, nd->ranges, nd->nr * 2 * sizeof(uint32_t));
            nd->ranges = nr;
        }
        nd->ranges[nd->nr * 2] = lo;
        nd->ranges[nd->nr * 2 + 1] = hi;
        nd->nr++;
    }
    if (nd->nr == 0) { rx_fail(p, "'[]' matches nothing"); return nd; }
    return nd;
}

static RxNode *rx_parse_atom(RxParser *p) {
    if (p->i >= p->n) { rx_fail(p, "unexpected end of pattern"); return rx_new(RX_SEQ); }
    char c = p->pat[p->i];
    if (c == '*' || c == '+' || c == '?') {
        char m[64]; snprintf(m, sizeof(m), "quantifier '%c' has nothing to repeat", c);
        rx_fail(p, m); return rx_new(RX_SEQ);
    }
    if (c == '{') {
        rx_fail(p, "'{' has nothing to repeat (write '\\{' for a literal brace)");
        return rx_new(RX_SEQ);
    }
    if (c == '^') { p->i++; return rx_new(RX_START); }
    if (c == '$') { p->i++; return rx_new(RX_END); }
    if (c == '.') { p->i++; return rx_new(RX_DOT); }
    if (c == '(') {
        p->i++;
        if (p->i < p->n && p->pat[p->i] == '?') {
            rx_fail(p, "'(.....)' groups are not supported in v1 (use plain '(...)' groups)");
            return rx_new(RX_SEQ);
        }
        p->ngroups++;
        int idx = p->ngroups;
        RxNode *inner = rx_parse_alt(p);
        if (p->i >= p->n || p->pat[p->i] != ')') {
            rx_fail(p, "unclosed '('"); return inner;
        }
        p->i++;
        RxNode *nd = rx_new(RX_GROUP);
        nd->gidx = idx; nd->child = inner;
        return nd;
    }
    if (c == '[') return rx_parse_class(p);
    if (c == '\\') {
        p->i++;
        if (p->i >= p->n) { rx_fail(p, "pattern ends with '\\'"); return rx_new(RX_SEQ); }
        char e = p->pat[p->i++];
        const char *hint = NULL;
        uint32_t lit = 0;
        int ok = 1;
        switch (e) {
        case 'n': lit = '\n'; break; case 't': lit = '\t'; break;
        case '\\': lit = '\\'; break; case '.': lit = '.'; break;
        case '*': lit = '*'; break; case '+': lit = '+'; break;
        case '?': lit = '?'; break; case '(': lit = '('; break;
        case ')': lit = ')'; break; case '[': lit = '['; break;
        case ']': lit = ']'; break; case '{': lit = '{'; break;
        case '}': lit = '}'; break; case '|': lit = '|'; break;
        case '^': lit = '^'; break; case '$': lit = '$'; break;
        case 'd': hint = "[0-9]"; ok = 0; break;
        case 'w': hint = "[A-Za-z0-9_]"; ok = 0; break;
        case 's': hint = "[ \\t\\n\\r]"; ok = 0; break;
        default: ok = 0; break;
        }
        if (!ok) {
            char m[128];
            if (hint) snprintf(m, sizeof(m), "unsupported escape '\\%c' -- use %s instead", e, hint);
            else snprintf(m, sizeof(m), "unsupported escape '\\%c'", e);
            rx_fail(p, m);
            return rx_new(RX_SEQ);
        }
        RxNode *nd = rx_new(RX_LIT);
        nd->ch = lit;
        return nd;
    }
    if (c == '|') { rx_fail(p, "empty branch in alternation"); return rx_new(RX_SEQ); }
    if (c == ')') { rx_fail(p, "unmatched ')'"); return rx_new(RX_SEQ); }
    /* literal (may be a UTF-8 multibyte char) */
    const unsigned char *up = (const unsigned char *)p->pat + p->i;
    uint32_t cp = utf8_decode_one(&up);
    p->i = (size_t)(up - (const unsigned char *)p->pat);
    RxNode *nd = rx_new(RX_LIT);
    nd->ch = cp;
    return nd;
}

static RxNode *rx_parse_quantified(RxParser *p) {
    RxNode *atom = rx_parse_atom(p);
    if (p->failed) return atom;
    if (p->i >= p->n) return atom;
    char c = p->pat[p->i];
    if (c == '*' || c == '+' || c == '?') {
        p->i++;
        RxNode *nd = rx_new(RX_QUANT);
        nd->child = atom;
        if (c == '*') { nd->qmin = 0; nd->qmax = -1; }
        else if (c == '+') { nd->qmin = 1; nd->qmax = -1; }
        else { nd->qmin = 0; nd->qmax = 1; }
        return nd;
    }
    if (c == '{') {
        size_t j = p->i + 1, k = j;
        while (k < p->n && p->pat[k] >= '0' && p->pat[k] <= '9') k++;
        if (k == j) {
            rx_fail(p, "expected a number after '{' (write '\\{' for a literal brace)");
            return atom;
        }
        int lo = atoi(p->pat + j);
        int hi = lo;
        if (k < p->n && p->pat[k] == ',') {
            k++;
            size_t j2 = k;
            while (k < p->n && p->pat[k] >= '0' && p->pat[k] <= '9') k++;
            hi = (k > j2) ? atoi(p->pat + j2) : -1;
        }
        if (k >= p->n || p->pat[k] != '}') {
            rx_fail(p, "expected '}' to close '{m,n}'"); return atom;
        }
        if (hi >= 0 && hi < lo) {
            char m[64]; snprintf(m, sizeof(m), "{%d,%d}: max < min", lo, hi);
            rx_fail(p, m); return atom;
        }
        p->i = k + 1;
        RxNode *nd = rx_new(RX_QUANT);
        nd->child = atom; nd->qmin = lo; nd->qmax = hi;
        return nd;
    }
    return atom;
}

static RxNode *rx_parse_seq(RxParser *p) {
    int cap = 8, nk = 0;
    RxNode **kids = (RxNode **)misa_alloc(cap * sizeof(RxNode *));
    while (p->i < p->n && p->pat[p->i] != '|' && p->pat[p->i] != ')') {
        if (nk == cap) {
            cap *= 2;
            RxNode **nk2 = (RxNode **)misa_alloc(cap * sizeof(RxNode *));
            memcpy(nk2, kids, nk * sizeof(RxNode *));
            kids = nk2;
        }
        kids[nk++] = rx_parse_quantified(p);
        if (p->failed) break;
    }
    if (nk == 1) {
        RxNode *one = kids[0];
        return one;
    }
    RxNode *nd = rx_new(RX_SEQ);
    nd->kids = kids; nd->nk = nk;
    return nd;
}

static RxNode *rx_parse_alt(RxParser *p) {
    RxNode *left = rx_parse_seq(p);
    if (p->failed) return left;
    if (p->i < p->n && p->pat[p->i] == '|') {
        p->i++;
        RxNode *right = rx_parse_alt(p);
        RxNode *nd = rx_new(RX_ALT);
        nd->left = left; nd->right = right;
        return nd;
    }
    return left;
}

/* Compile; returns NULL + message in `err` on failure. */
static MisaRx *rx_compile(const char *pat, size_t len, char *err, size_t errn) {
    RxParser p;
    char *buf = (char *)misa_alloc(len + 1);
    memcpy(buf, pat, len);
    buf[len] = '\0';
    p.pat = buf; p.n = len; p.i = 0; p.ngroups = 0;
    p.failed = 0;
    RxNode *root = rx_parse_alt(&p);
    if (!p.failed && p.i != p.n) {
        snprintf(p.err, sizeof(p.err), "unexpected '%c' (at index %zu)",
                 p.pat[p.i], p.i);
        p.failed = 1;
    }
    if (p.failed) {
        snprintf(err, errn, "%s", p.err);
        return NULL;
    }
    MisaRx *rx = (MisaRx *)misa_alloc(sizeof(MisaRx));
    rx->root = root;
    rx->ngroups = p.ngroups;
    rx->pattern = buf;
    return rx;
}

/* -- regex matching (backtracking; mirrors regexp.py) --------------- */

typedef struct {
    const uint32_t *text;
    int n;
    int *caps;      /* 2*(ngroups+1): start,end; -1 = unset */
    int ngroups;
} RxState;

/* -- regex matching: continuation-passing backtracker (PCRE) ----
 * `rx_match_node(nd, s, pos, k)` tries every way `nd` can match at
 * `pos`, calling `k(pos)` for each end position; `k` returns >= 0 on
 * success or -1 to keep backtracking. Mirrors regexp.py. */

static int *rx_snap(RxState *s) {
    int *snap = (int *)misa_alloc(2 * (s->ngroups + 1) * sizeof(int));
    memcpy(snap, s->caps, 2 * (s->ngroups + 1) * sizeof(int));
    return snap;
}

static void rx_restore(RxState *s, int *snap) {
    memcpy(s->caps, snap, 2 * (s->ngroups + 1) * sizeof(int));
}

typedef int (*RxKFn)(RxState *s, int pos, void *ctx);
typedef struct { RxKFn fn; void *ctx; } RxK;

static int rx_match_node(RxNode *nd, RxState *s, int pos, RxK k);

/* Sequence chaining frame. */
typedef struct {
    RxNode **nodes;
    int nn, idx;
    RxK next;
} RxSeqFrame;

static int rx_k_seq(RxState *s, int pos, void *ctx) {
    RxSeqFrame *f = (RxSeqFrame *)ctx;
    if (f->idx >= f->nn)
        return f->next.fn(s, pos, f->next.ctx);
    RxSeqFrame *rest = (RxSeqFrame *)misa_alloc(sizeof(RxSeqFrame));
    rest->nodes = f->nodes;
    rest->nn = f->nn;
    rest->idx = f->idx + 1;
    rest->next = f->next;
    RxK k = { rx_k_seq, rest };
    return rx_match_node(f->nodes[f->idx], s, pos, k);
}

static int rx_match_seq(RxNode **nodes, int nn, RxState *s, int pos,
                        RxK k) {
    RxSeqFrame *f = (RxSeqFrame *)misa_alloc(sizeof(RxSeqFrame));
    f->nodes = nodes; f->nn = nn; f->idx = 0; f->next = k;
    return rx_k_seq(s, pos, f);
}

/* Group capture frame. */
typedef struct {
    RxK k;
    int idx;
    int old0, old1;
} RxGroupFrame;

static int rx_k_group_done(RxState *s, int pos, void *ctx) {
    RxGroupFrame *f = (RxGroupFrame *)ctx;
    s->caps[f->idx * 2 + 1] = pos;
    int r = f->k.fn(s, pos, f->k.ctx);
    if (r < 0) {
        s->caps[f->idx * 2] = f->old0;
        s->caps[f->idx * 2 + 1] = f->old1;
    }
    return r;
}

/* Quantifier frames. */
typedef struct {
    RxNode *child;
    int lo, hi, count;
    RxK k;
} RxQuantFrame;

typedef struct {
    RxQuantFrame *qf;
    int prev_pos;
} RxProgressFrame;

static int rx_quant_rec(RxState *s, int pos, void *ctx);

static int rx_k_progress(RxState *s, int pos, void *ctx) {
    RxProgressFrame *f = (RxProgressFrame *)ctx;
    if (pos == f->prev_pos) return -1; /* no progress: stop extending */
    return rx_quant_rec(s, pos, f->qf);
}

static int rx_quant_rec(RxState *s, int pos, void *ctx) {
    RxQuantFrame *f = (RxQuantFrame *)ctx;
    /* Greedy: try to extend the run first, then stop. */
    if (f->hi < 0 || f->count < f->hi) {
        int *saved = rx_snap(s);
        RxQuantFrame *next =
            (RxQuantFrame *)misa_alloc(sizeof(RxQuantFrame));
        *next = *f;
        next->count++;
        RxProgressFrame *pf =
            (RxProgressFrame *)misa_alloc(sizeof(RxProgressFrame));
        pf->qf = next;
        pf->prev_pos = pos;
        RxK k = { rx_k_progress, pf };
        int r = rx_match_node(f->child, s, pos, k);
        if (r >= 0) return r;
        rx_restore(s, saved);
    }
    if (f->count >= f->lo)
        return f->k.fn(s, pos, f->k.ctx);
    return -1;
}

static int rx_match_node(RxNode *nd, RxState *s, int pos, RxK k) {
    int n = s->n;
    switch (nd->kind) {
    case RX_LIT:
        if (pos < n && s->text[pos] == nd->ch)
            return k.fn(s, pos + 1, k.ctx);
        return -1;
    case RX_DOT:
        if (pos < n && s->text[pos] != '\n')
            return k.fn(s, pos + 1, k.ctx);
        return -1;
    case RX_CLASS: {
        if (pos < n) {
            uint32_t cp = s->text[pos];
            int hit = 0;
            for (int i = 0; i < nd->nr; i++)
                if (nd->ranges[i * 2] <= cp &&
                    cp <= nd->ranges[i * 2 + 1]) { hit = 1; break; }
            if (hit != nd->neg)
                return k.fn(s, pos + 1, k.ctx);
        }
        return -1;
    }
    case RX_START:
        return pos == 0 ? k.fn(s, pos, k.ctx) : -1;
    case RX_END:
        return pos == n ? k.fn(s, pos, k.ctx) : -1;
    case RX_SEQ:
        return rx_match_seq(nd->kids, nd->nk, s, pos, k);
    case RX_GROUP: {
        int idx = nd->gidx;
        RxGroupFrame *f =
            (RxGroupFrame *)misa_alloc(sizeof(RxGroupFrame));
        f->k = k;
        f->idx = idx;
        f->old0 = s->caps[idx * 2];
        f->old1 = s->caps[idx * 2 + 1];
        s->caps[idx * 2] = pos;
        RxK kd = { rx_k_group_done, f };
        int r = rx_match_node(nd->child, s, pos, kd);
        if (r < 0) {
            s->caps[idx * 2] = -1;
            s->caps[idx * 2 + 1] = -1;
        }
        return r;
    }
    case RX_ALT: {
        int *saved = rx_snap(s);
        int r = rx_match_node(nd->left, s, pos, k);
        if (r >= 0) return r;
        rx_restore(s, saved);
        return rx_match_node(nd->right, s, pos, k);
    }
    case RX_QUANT: {
        RxQuantFrame *f =
            (RxQuantFrame *)misa_alloc(sizeof(RxQuantFrame));
        f->child = nd->child;
        f->lo = nd->qmin;
        f->hi = nd->qmax;
        f->count = 0;
        f->k = k;
        return rx_quant_rec(s, pos, f);
    }
    default:
        return -1;
    }
}

static int rx_k_done(RxState *s, int pos, void *ctx) {
    (void)s; (void)ctx;
    return pos;
}

/* -- regex public API ---------------------------------------------- */

static MisaRx *as_rx(MVal *v, int line, int col, const char *what) {
    if (v->tag != M_TAG_REGEX) {
        misa_raise("E300", "regex function needs a Regex value", line, col);
        return NULL;
    }
    (void)what;
    return (MisaRx *)v->u.regex.rx;
}

static const char *as_text(MVal *v, int line, int col, const char *what) {
    if (v->tag != M_TAG_TEXT) {
        misa_raise("E300", what, line, col);
        return NULL;
    }
    return v->u.text.data;
}

MVal *misa_regex_new(MVal *pat, int line, int col) {
    const char *p = as_text(pat, line, col, "regex(Text) needs text");
    if (!p) return NULL;
    char err[192];
    MisaRx *rx = rx_compile(p, pat->u.text.len, err, sizeof(err));
    if (!rx) {
        misa_raise("E300", err, line, col);
        return NULL;
    }
    MVal *v = val_new(M_TAG_REGEX);
    v->u.regex.rx = rx;
    v->u.regex.pattern = rx->pattern;
    return v;
}

/* Search for the first match at/after `start` (code-point index).
 * Returns 1 + sets *ms,*me,*caps; 0 when no match. */
static int rx_search(MisaRx *rx, Decoded *d, int start,
                     int *ms, int *me, int *caps) {
    RxState s;
    s.text = d->cps; s.n = (int)d->n; s.ngroups = rx->ngroups;
    int ncaps = 2 * (rx->ngroups + 1);
    RxK kdone = { rx_k_done, NULL };
    for (int pos = start; pos <= (int)d->n; pos++) {
        for (int i = 0; i < ncaps; i++) caps[i] = -1;
        s.caps = caps;
        int q = rx_match_node(rx->root, &s, pos, kdone);
        if (q >= 0) { *ms = pos; *me = q; return 1; }
    }
    return 0;
}

/* Build a Text from code-point range [s, e) using byte offsets. */
static MVal *rx_text_range(Decoded *d, const char *data, int s, int e) {
    return misa_text(data + d->offs[s], d->offs[e] - d->offs[s]);
}

MVal *misa_regex_matches(MVal *rxv, MVal *text, int line, int col) {
    MisaRx *rx = as_rx(rxv, line, col, "regex.matches");
    const char *t = as_text(text, line, col, "regex.matches needs text");
    if (!rx || !t) return NULL;
    Decoded d = decode_text(t, text->u.text.len);
    int ncaps = 2 * (rx->ngroups + 1);
    int *caps = (int *)misa_alloc(ncaps * sizeof(int));
    for (int i = 0; i < ncaps; i++) caps[i] = -1;
    RxState s = { d.cps, (int)d.n, caps, rx->ngroups };
    /* An implicit end-anchor INSIDE the match so backtracking
     * (alternations, quantifiers) respects the full-match. */
    RxNode end_node;
    memset(&end_node, 0, sizeof(end_node));
    end_node.kind = RX_END;
    RxNode *nodes[2] = { rx->root, &end_node };
    RxNode seq_wrap;
    memset(&seq_wrap, 0, sizeof(seq_wrap));
    seq_wrap.kind = RX_SEQ;
    seq_wrap.kids = nodes;
    seq_wrap.nk = 2;
    RxK kdone = { rx_k_done, NULL };
    int q = rx_match_node(&seq_wrap, &s, 0, kdone);
    return misa_bool(q >= 0);
}

MVal *misa_regex_find(MVal *rxv, MVal *text, int line, int col) {
    MisaRx *rx = as_rx(rxv, line, col, "regex.find");
    const char *t = as_text(text, line, col, "regex.find needs text");
    if (!rx || !t) return NULL;
    Decoded d = decode_text(t, text->u.text.len);
    int ncaps = 2 * (rx->ngroups + 1);
    int *caps = (int *)misa_alloc(ncaps * sizeof(int));
    int ms, me;
    if (!rx_search(rx, &d, 0, &ms, &me, caps)) return misa_null();
    return rx_text_range(&d, t, ms, me);
}

MVal *misa_regex_find_all(MVal *rxv, MVal *text, int line, int col) {
    MisaRx *rx = as_rx(rxv, line, col, "regex.find_all");
    const char *t = as_text(text, line, col, "regex.find_all needs text");
    if (!rx || !t) return NULL;
    Decoded d = decode_text(t, text->u.text.len);
    int ncaps = 2 * (rx->ngroups + 1);
    int *caps = (int *)misa_alloc(ncaps * sizeof(int));
    MVal *out = misa_list_new();
    int pos = 0, ms, me;
    while (pos <= (int)d.n) {
        if (!rx_search(rx, &d, pos, &ms, &me, caps)) break;
        misa_list_push(out, rx_text_range(&d, t, ms, me));
        pos = (me > ms) ? me : ms + 1;
    }
    return out;
}

MVal *misa_regex_captures(MVal *rxv, MVal *text, int line, int col) {
    MisaRx *rx = as_rx(rxv, line, col, "regex.captures");
    const char *t = as_text(text, line, col, "regex.captures needs text");
    if (!rx || !t) return NULL;
    Decoded d = decode_text(t, text->u.text.len);
    int ncaps = 2 * (rx->ngroups + 1);
    int *caps = (int *)misa_alloc(ncaps * sizeof(int));
    int ms, me;
    if (!rx_search(rx, &d, 0, &ms, &me, caps)) return misa_null();
    MVal *out = misa_list_new();
    for (int g = 1; g <= rx->ngroups; g++) {
        int a = caps[g * 2], b = caps[g * 2 + 1];
        misa_list_push(out, (a >= 0)
                       ? rx_text_range(&d, t, a, b) : misa_text("", 0));
    }
    return out;
}

/* Expand `$1`..`$9` / `$$` in a replacement template. */
static MVal *rx_expand(MisaRx *rx, const char *repl, size_t rlen,
                       Decoded *d, const char *t, int *caps) {
    /* Build into a byte buffer. */
    size_t cap = rlen + 64, len = 0;
    char *buf = (char *)misa_alloc(cap);
    size_t i = 0;
    while (i < rlen) {
        if (repl[i] == '$' && i + 1 < rlen) {
            char c2 = repl[i + 1];
            if (c2 == '$') {
                if (len + 1 >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
                buf[len++] = '$'; i += 2; continue;
            }
            if (c2 >= '1' && c2 <= '9') {
                int g = c2 - '0';
                if (g <= rx->ngroups) {
                    int a = caps[g * 2], b = caps[g * 2 + 1];
                    if (a >= 0) {
                        size_t bl = d->offs[b] - d->offs[a];
                        while (len + bl >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
                        memcpy(buf + len, t + d->offs[a], bl);
                        len += bl;
                    }
                }
                i += 2; continue;
            }
        }
        if (len + 1 >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
        buf[len++] = repl[i++];
    }
    return misa_text(buf, len);
}

MVal *misa_regex_replace(MVal *rxv, MVal *text, MVal *replv,
                         int line, int col) {
    MisaRx *rx = as_rx(rxv, line, col, "regex.replace");
    const char *t = as_text(text, line, col, "regex.replace needs text");
    if (!rx || !t) return NULL;
    if (replv->tag != M_TAG_TEXT) {
        misa_raise("E300", "regex.replace(Regex, Text, Text) needs text",
                   line, col);
        return NULL;
    }
    const char *rp = replv->u.text.data;
    Decoded d = decode_text(t, text->u.text.len);
    int ncaps = 2 * (rx->ngroups + 1);
    int *caps = (int *)misa_alloc(ncaps * sizeof(int));
    size_t cap = text->u.text.len + 64, len = 0;
    char *buf = (char *)misa_alloc(cap);
    int pos = 0, last = 0, ms, me;
    while (pos <= (int)d.n) {
        if (!rx_search(rx, &d, pos, &ms, &me, caps)) break;
        /* copy text[last:ms] */
        size_t bl = d.offs[ms] - d.offs[last];
        while (len + bl >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
        memcpy(buf + len, t + d.offs[last], bl);
        len += bl;
        /* expand replacement */
        MVal *exp = rx_expand(rx, rp, replv->u.text.len, &d, t, caps);
        bl = exp->u.text.len;
        while (len + bl >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
        memcpy(buf + len, exp->u.text.data, bl);
        len += bl;
        last = me;
        pos = (me > ms) ? me : ms + 1;
    }
    size_t tail = d.offs[d.n] - d.offs[last];
    while (len + tail >= cap) { cap *= 2; char *nb = (char *)misa_alloc(cap); memcpy(nb, buf, len); buf = nb; }
    memcpy(buf + len, t + d.offs[last], tail);
    len += tail;
    return misa_text(buf, len);
}

/* -- crypto: self-contained hashes (no OpenSSL dependency) ---------- */

static uint32_t rotr32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
static uint32_t rotl32(uint32_t x, int n) { return (x << n) | (x >> (32 - n)); }

/* SHA-256 (FIPS 180-4). */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t bl; } Sha256;
static const uint32_t SHA256_K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};
static void sha256_init(Sha256 *c) {
    c->h[0]=0x6a09e667;c->h[1]=0xbb67ae85;c->h[2]=0x3c6ef372;c->h[3]=0xa54ff53a;
    c->h[4]=0x510e527f;c->h[5]=0x9b05688c;c->h[6]=0x1f83d9ab;c->h[7]=0x5be0cd19;
    c->len=0; c->bl=0;
}
static void sha256_block(Sha256 *c, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i*4]<<24)|((uint32_t)p[i*4+1]<<16)|
               ((uint32_t)p[i*4+2]<<8)|(uint32_t)p[i*4+3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr32(w[i-15],7)^rotr32(w[i-15],18)^(w[i-15]>>3);
        uint32_t s1 = rotr32(w[i-2],17)^rotr32(w[i-2],19)^(w[i-2]>>10);
        w[i] = w[i-16]+s0+w[i-7]+s1;
    }
    uint32_t a=c->h[0],b=c->h[1],cc=c->h[2],d=c->h[3],
             e=c->h[4],f=c->h[5],g=c->h[6],h=c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1=rotr32(e,6)^rotr32(e,11)^rotr32(e,25);
        uint32_t ch=(e&f)^(~e&g);
        uint32_t t1=h+S1+ch+SHA256_K[i]+w[i];
        uint32_t S0=rotr32(a,2)^rotr32(a,13)^rotr32(a,22);
        uint32_t mj=(a&b)^(a&cc)^(b&cc);
        uint32_t t2=S0+mj;
        h=g;g=f;f=e;e=d+t1;d=cc;cc=b;b=a;a=t1+t2;
    }
    c->h[0]+=a;c->h[1]+=b;c->h[2]+=cc;c->h[3]+=d;
    c->h[4]+=e;c->h[5]+=f;c->h[6]+=g;c->h[7]+=h;
}
static void sha256_update(Sha256 *c, const uint8_t *d, size_t n) {
    c->len += n;
    while (n) {
        size_t t = 64 - c->bl;
        if (t > n) t = n;
        memcpy(c->buf + c->bl, d, t);
        c->bl += t; d += t; n -= t;
        if (c->bl == 64) { sha256_block(c, c->buf); c->bl = 0; }
    }
}
static void sha256_final(Sha256 *c, uint8_t out[32]) {
    uint64_t bitlen = c->len * 8;
    uint8_t pad = 0x80;
    sha256_update(c, &pad, 1);
    uint8_t zero = 0;
    while (c->bl != 56) sha256_update(c, &zero, 1);
    uint8_t lb[8];
    for (int i = 0; i < 8; i++) lb[i] = (uint8_t)(bitlen >> (56 - 8*i));
    sha256_update(c, lb, 8);
    for (int i = 0; i < 8; i++) {
        out[i*4]=(uint8_t)(c->h[i]>>24); out[i*4+1]=(uint8_t)(c->h[i]>>16);
        out[i*4+2]=(uint8_t)(c->h[i]>>8); out[i*4+3]=(uint8_t)c->h[i];
    }
}

/* SHA-1 (FIPS 180-4). */
typedef struct { uint32_t h[5]; uint64_t len; uint8_t buf[64]; size_t bl; } Sha1;
static void sha1_init(Sha1 *c) {
    c->h[0]=0x67452301;c->h[1]=0xefcdab89;c->h[2]=0x98badcfe;
    c->h[3]=0x10325476;c->h[4]=0xc3d2e1f0;c->len=0;c->bl=0;
}
static void sha1_block(Sha1 *c, const uint8_t *p) {
    uint32_t w[80];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i*4]<<24)|((uint32_t)p[i*4+1]<<16)|
               ((uint32_t)p[i*4+2]<<8)|(uint32_t)p[i*4+3];
    for (int i = 16; i < 80; i++)
        w[i] = rotl32(w[i-3]^w[i-8]^w[i-14]^w[i-16], 1);
    uint32_t a=c->h[0],b=c->h[1],cc=c->h[2],d=c->h[3],e=c->h[4];
    for (int i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) { f=(b&cc)|(~b&d); k=0x5a827999; }
        else if (i < 40) { f=b^cc^d; k=0x6ed9eba1; }
        else if (i < 60) { f=(b&cc)|(b&d)|(cc&d); k=0x8f1bbcdc; }
        else { f=b^cc^d; k=0xca62c1d6; }
        uint32_t t = ((a<<5)|(a>>27)) + f + e + k + w[i];
        e=d; d=cc; cc=(b<<30)|(b>>2); b=a; a=t;
    }
    c->h[0]+=a;c->h[1]+=b;c->h[2]+=cc;c->h[3]+=d;c->h[4]+=e;
}
static void sha1_update(Sha1 *c, const uint8_t *d, size_t n) {
    c->len += n;
    while (n) {
        size_t t = 64 - c->bl; if (t > n) t = n;
        memcpy(c->buf + c->bl, d, t);
        c->bl += t; d += t; n -= t;
        if (c->bl == 64) { sha1_block(c, c->buf); c->bl = 0; }
    }
}
static void sha1_final(Sha1 *c, uint8_t out[20]) {
    uint64_t bitlen = c->len * 8;
    uint8_t pad = 0x80, zero = 0;
    sha1_update(c, &pad, 1);
    while (c->bl != 56) sha1_update(c, &zero, 1);
    uint8_t lb[8];
    for (int i = 0; i < 8; i++) lb[i] = (uint8_t)(bitlen >> (56 - 8*i));
    sha1_update(c, lb, 8);
    for (int i = 0; i < 5; i++) {
        out[i*4]=(uint8_t)(c->h[i]>>24); out[i*4+1]=(uint8_t)(c->h[i]>>16);
        out[i*4+2]=(uint8_t)(c->h[i]>>8); out[i*4+3]=(uint8_t)c->h[i];
    }
}

/* MD5 (RFC 1321). */
typedef struct { uint32_t h[4]; uint64_t len; uint8_t buf[64]; size_t bl; } Md5;
static const uint32_t MD5_S[64] = {
    7,12,17,22, 7,12,17,22, 7,12,17,22, 7,12,17,22,
    5, 9,14,20, 5, 9,14,20, 5, 9,14,20, 5, 9,14,20,
    4,11,16,23, 4,11,16,23, 4,11,16,23, 4,11,16,23,
    6,10,15,21, 6,10,15,21, 6,10,15,21, 6,10,15,21
};
static const uint32_t MD5_T[64] = {
    0xd76aa478,0xe8c7b756,0x242070db,0xc1bdceee,0xf57c0faf,0x4787c62a,0xa8304613,0xfd469501,
    0x698098d8,0x8b44f7af,0xffff5bb1,0x895cd7be,0x6b901122,0xfd987193,0xa679438e,0x49b40821,
    0xf61e2562,0xc040b340,0x265e5a51,0xe9b6c7aa,0xd62f105d,0x02441453,0xd8a1e681,0xe7d3fbc8,
    0x21e1cde6,0xc33707d6,0xf4d50d87,0x455a14ed,0xa9e3e905,0xfcefa3f8,0x676f02d9,0x8d2a4c8a,
    0xfffa3942,0x8771f681,0x6d9d6122,0xfde5380c,0xa4beea44,0x4bdecfa9,0xf6bb4b60,0xbebfbc70,
    0x289b7ec6,0xeaa127fa,0xd4ef3085,0x04881d05,0xd9d4d039,0xe6db99e5,0x1fa27cf8,0xc4ac5665,
    0xf4292244,0x432aff97,0xab9423a7,0xfc93a039,0x655b59c3,0x8f0ccc92,0xffeff47d,0x85845dd1,
    0x6fa87e4f,0xfe2ce6e0,0xa3014314,0x4e0811a1,0xf7537e82,0xbd3af235,0x2ad7d2bb,0xeb86d391
};
static void md5_init(Md5 *c) {
    c->h[0]=0x67452301;c->h[1]=0xefcdab89;c->h[2]=0x98badcfe;c->h[3]=0x10325476;
    c->len=0;c->bl=0;
}
static void md5_block(Md5 *c, const uint8_t *p) {
    uint32_t w[16];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[i*4]|((uint32_t)p[i*4+1]<<8)|
               ((uint32_t)p[i*4+2]<<16)|((uint32_t)p[i*4+3]<<24);
    uint32_t a=c->h[0],b=c->h[1],cc=c->h[2],d=c->h[3];
    for (int i = 0; i < 64; i++) {
        uint32_t f; int g;
        if (i < 16) { f=(b&cc)|(~b&d); g=i; }
        else if (i < 32) { f=(d&b)|(~d&cc); g=(5*i+1)%16; }
        else if (i < 48) { f=b^cc^d; g=(3*i+5)%16; }
        else { f=cc^(b|~d); g=(7*i)%16; }
        f = f + a + MD5_T[i] + w[g];
        a = d; d = cc; cc = b;
        b = b + (((f << MD5_S[i]) | (f >> (32 - MD5_S[i]))));
    }
    c->h[0]+=a;c->h[1]+=b;c->h[2]+=cc;c->h[3]+=d;
}
static void md5_update(Md5 *c, const uint8_t *d, size_t n) {
    c->len += n;
    while (n) {
        size_t t = 64 - c->bl; if (t > n) t = n;
        memcpy(c->buf + c->bl, d, t);
        c->bl += t; d += t; n -= t;
        if (c->bl == 64) { md5_block(c, c->buf); c->bl = 0; }
    }
}
static void md5_final(Md5 *c, uint8_t out[16]) {
    uint64_t bitlen = c->len * 8;
    uint8_t pad = 0x80, zero = 0;
    md5_update(c, &pad, 1);
    while (c->bl != 56) md5_update(c, &zero, 1);
    uint8_t lb[8];
    for (int i = 0; i < 8; i++) lb[i] = (uint8_t)(bitlen >> (8*i));
    md5_update(c, lb, 8);
    for (int i = 0; i < 4; i++) {
        out[i*4]=(uint8_t)c->h[i]; out[i*4+1]=(uint8_t)(c->h[i]>>8);
        out[i*4+2]=(uint8_t)(c->h[i]>>16); out[i*4+3]=(uint8_t)(c->h[i]>>24);
    }
}

static MVal *hex_of(const uint8_t *digest, size_t n) {
    static const char *H = "0123456789abcdef";
    char *buf = (char *)misa_alloc(n * 2 + 1);
    for (size_t i = 0; i < n; i++) {
        buf[i*2] = H[digest[i] >> 4];
        buf[i*2+1] = H[digest[i] & 15];
    }
    buf[n*2] = '\0';
    MVal *v = misa_text(buf, n * 2);
    return v;
}

MVal *misa_crypto_sha256(MVal *data, int line, int col) {
    const char *t = as_text(data, line, col, "crypto.sha256(Text) needs text");
    if (!t) return NULL;
    Sha256 c; sha256_init(&c);
    sha256_update(&c, (const uint8_t *)t, data->u.text.len);
    uint8_t out[32]; sha256_final(&c, out);
    return hex_of(out, 32);
}

MVal *misa_crypto_sha1(MVal *data, int line, int col) {
    const char *t = as_text(data, line, col, "crypto.sha1(Text) needs text");
    if (!t) return NULL;
    Sha1 c; sha1_init(&c);
    sha1_update(&c, (const uint8_t *)t, data->u.text.len);
    uint8_t out[20]; sha1_final(&c, out);
    return hex_of(out, 20);
}

MVal *misa_crypto_md5(MVal *data, int line, int col) {
    const char *t = as_text(data, line, col, "crypto.md5(Text) needs text");
    if (!t) return NULL;
    Md5 c; md5_init(&c);
    md5_update(&c, (const uint8_t *)t, data->u.text.len);
    uint8_t out[16]; md5_final(&c, out);
    return hex_of(out, 16);
}

MVal *misa_crypto_hmac_sha256(MVal *key, MVal *msg, int line, int col) {
    const char *k = as_text(key, line, col, "crypto.hmac_sha256 needs text");
    const char *m = as_text(msg, line, col, "crypto.hmac_sha256 needs text");
    if (!k || !m) return NULL;
    uint8_t kbuf[64];
    memset(kbuf, 0, 64);
    if (key->u.text.len > 64) {
        Sha256 c; sha256_init(&c);
        sha256_update(&c, (const uint8_t *)k, key->u.text.len);
        sha256_final(&c, kbuf);
    } else {
        memcpy(kbuf, k, key->u.text.len);
    }
    uint8_t ipad[64], opad[64];
    for (int i = 0; i < 64; i++) {
        ipad[i] = kbuf[i] ^ 0x36;
        opad[i] = kbuf[i] ^ 0x5c;
    }
    Sha256 inner; sha256_init(&inner);
    sha256_update(&inner, ipad, 64);
    sha256_update(&inner, (const uint8_t *)m, msg->u.text.len);
    uint8_t ih[32]; sha256_final(&inner, ih);
    Sha256 outer; sha256_init(&outer);
    sha256_update(&outer, opad, 64);
    sha256_update(&outer, ih, 32);
    uint8_t out[32]; sha256_final(&outer, out);
    return hex_of(out, 32);
}

/* -- random: splitmix64 -------------------------------------------- */
/* `random.seed` gives determinism *within* a backend (same backend +
 * same seed = same sequence); cross-backend determinism with the
 * interpreter's Mersenne Twister is NOT guaranteed (documented). */

static uint64_t rng_state = 0x9e3779b97f4a7c15ULL;

static uint64_t splitmix64(void) {
    uint64_t z = (rng_state += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

MVal *misa_random_seed(MVal *n, int line, int col) {
    if (n->tag != M_TAG_INT) {
        misa_raise("E300", "random.seed(Int) needs an int", line, col);
        return NULL;
    }
    rng_state = (uint64_t)n->u.i;
    return misa_null();
}

MVal *misa_random_int(MVal *lo, MVal *hi, int line, int col) {
    if (lo->tag != M_TAG_INT || hi->tag != M_TAG_INT) {
        misa_raise("E300", "random.int(Int, Int) needs ints", line, col);
        return NULL;
    }
    int64_t a = lo->u.i, b = hi->u.i;
    if (a > b) {
        misa_raise_fmt("E300", line, col,
                       "random.int: min (%lld) > max (%lld)",
                       (long long)a, (long long)b);
        return NULL;
    }
    uint64_t span = (uint64_t)(b - a) + 1;
    /* Rejection sampling (avoids modulo bias). */
    uint64_t limit = (~(uint64_t)0) - ((~(uint64_t)0) % span);
    uint64_t r;
    do { r = splitmix64(); } while (r >= limit);
    return misa_int(a + (int64_t)(r % span));
}

MVal *misa_random_float(void) {
    /* [0, 1): 53 bits of randomness, like CPython. */
    return misa_float((splitmix64() >> 11) * (1.0 / 9007199254740992.0));
}

MVal *misa_random_choice(MVal *xs, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "random.choice([T]) needs a list", line, col);
        return NULL;
    }
    if (xs->u.list.len == 0) {
        misa_raise("E300",
                   "random.choice: cannot choose from an empty list",
                   line, col);
        return NULL;
    }
    uint64_t r = splitmix64();
    return xs->u.list.items[r % (uint64_t)xs->u.list.len];
}

MVal *misa_random_shuffle(MVal *xs, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "random.shuffle([T]) needs a list", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < xs->u.list.len; i++)
        misa_list_push(out, xs->u.list.items[i]);
    /* Fisher-Yates. */
    for (int64_t i = out->u.list.len - 1; i > 0; i--) {
        uint64_t r = splitmix64();
        int64_t j = (int64_t)(r % (uint64_t)(i + 1));
        MVal *t = out->u.list.items[i];
        out->u.list.items[i] = out->u.list.items[j];
        out->u.list.items[j] = t;
    }
    return out;
}

/* -- time additions (offset-based zones; no tz database in v1) ----- */

MVal *misa_time_now_utc(void) {
    return misa_int((int64_t)time(NULL));
}

MVal *misa_time_to_utc(MVal *t, int line, int col) {
    /* Epoch timestamps are always UTC — identity, for clarity. */
    if (t->tag != M_TAG_INT) {
        misa_raise("E300", "time.to_utc(Int) needs an int", line, col);
        return NULL;
    }
    return t;
}

MVal *misa_time_from_parts(MVal *y, MVal *mo, MVal *d, MVal *h,
                           MVal *mi, MVal *s, int line, int col) {
    MVal *ps[6] = {y, mo, d, h, mi, s};
    for (int i = 0; i < 6; i++)
        if (ps[i]->tag != M_TAG_INT) {
            misa_raise("E300", "time.from_parts needs int args",
                       line, col);
            return NULL;
        }
    struct tm tmv;
    memset(&tmv, 0, sizeof(tmv));
    tmv.tm_year = (int)(y->u.i - 1900);
    tmv.tm_mon = (int)(mo->u.i - 1);
    tmv.tm_mday = (int)d->u.i;
    tmv.tm_hour = (int)h->u.i;
    tmv.tm_min = (int)mi->u.i;
    tmv.tm_sec = (int)s->u.i;
    time_t t = timegm(&tmv);
    if (t == (time_t)-1) {
        misa_raise("E300", "time.from_parts: invalid date", line, col);
        return NULL;
    }
    /* timegm normalizes out-of-range fields (month 13 -> next year);
     * round-trip to detect invalid input. */
    struct tm back;
    if (gmtime_r(&t, &back) == NULL) {
        misa_raise("E300", "time.from_parts: invalid date", line, col);
        return NULL;
    }
    if (back.tm_year != tmv.tm_year || back.tm_mon != tmv.tm_mon ||
        back.tm_mday != tmv.tm_mday || back.tm_hour != tmv.tm_hour ||
        back.tm_min != tmv.tm_min || back.tm_sec != tmv.tm_sec) {
        misa_raise("E300", "time.from_parts: invalid date", line, col);
        return NULL;
    }
    return misa_int((int64_t)t);
}

MVal *misa_time_add(MVal *t, MVal *secs, int line, int col) {
    if (t->tag != M_TAG_INT || secs->tag != M_TAG_INT) {
        misa_raise("E300", "time.add(Int, Int) needs ints", line, col);
        return NULL;
    }
    return misa_int(t->u.i + secs->u.i);
}

MVal *misa_time_add_hours(MVal *t, MVal *h, int line, int col) {
    if (t->tag != M_TAG_INT || h->tag != M_TAG_INT) {
        misa_raise("E300", "time.add_hours(Int, Int) needs ints",
                   line, col);
        return NULL;
    }
    return misa_int(t->u.i + h->u.i * 3600);
}

MVal *misa_time_in_tz(MVal *t, MVal *off_hours, int line, int col) {
    if (t->tag != M_TAG_INT ||
        (off_hours->tag != M_TAG_FLOAT && off_hours->tag != M_TAG_INT)) {
        misa_raise("E300", "time.in_tz(Int, Float) needs int+float",
                   line, col);
        return NULL;
    }
    double off = off_hours->tag == M_TAG_FLOAT ? off_hours->u.f
                                               : (double)off_hours->u.i;
    /* Shift the epoch so UTC formatting shows wall-clock time at the
     * given offset (fractional hours supported, e.g. +5.5). */
    return misa_int(t->u.i + (int64_t)(off * 3600.0));
}

MVal *misa_time_weekday(MVal *t, int line, int col) {
    if (t->tag != M_TAG_INT) {
        misa_raise("E300", "time.weekday(Int) needs an int", line, col);
        return NULL;
    }
    time_t tt = (time_t)t->u.i;
    struct tm tmv;
    if (gmtime_r(&tt, &tmv) == NULL) {
        misa_raise("E300", "time.weekday: bad epoch", line, col);
        return NULL;
    }
    static const char *days[] = {"Sunday", "Monday", "Tuesday",
                                 "Wednesday", "Thursday", "Friday",
                                 "Saturday"};
    return misa_text_cstr(days[tmv.tm_wday]);
}

MVal *misa_time_is_leap(MVal *y, int line, int col) {
    if (y->tag != M_TAG_INT) {
        misa_raise("E300", "time.is_leap(Int) needs an int", line, col);
        return NULL;
    }
    int64_t yr = y->u.i;
    int leap = (yr % 4 == 0 && yr % 100 != 0) || (yr % 400 == 0);
    return misa_bool(leap);
}

/* -- temp files ---------------------------------------------------- */

MVal *misa_fs_temp_file(int line, int col) {
    char tmpl[] = "/tmp/misa-XXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) {
        misa_raise_fmt("E303", line, col, "fs.temp_file failed: %s",
                       strerror(errno));
        return NULL;
    }
    close(fd);
    return misa_text_cstr(tmpl);
}

MVal *misa_fs_temp_dir(int line, int col) {
    char tmpl[] = "/tmp/misa-XXXXXX";
    if (mkdtemp(tmpl) == NULL) {
        misa_raise_fmt("E303", line, col, "fs.temp_dir failed: %s",
                       strerror(errno));
        return NULL;
    }
    return misa_text_cstr(tmpl);
}

/* -- Compression: gzip via zlib ------------------------------------
 *
 * Framing is manual (raw deflate + explicit header/trailer) so every
 * byte is deterministic: header 1f 8b 08 00 00000000 02 03
 * (mtime=0, XFL=max, OS=Unix), level 9. This is byte-identical to the
 * interpreter (`gzip.compress(..., compresslevel=9, mtime=0)`).
 * Without MISA_USE_ZLIB the entry points raise E600 (clean, like the
 * web target's other unavailable natives).
 */

MVal *misa_bytes_new(const uint8_t *data, size_t len) {
    MVal *v = val_new(M_TAG_BYTES);
    uint8_t *buf = (uint8_t *)misa_alloc(len ? len : 1);
    if (len) memcpy(buf, data, len);
    v->u.bytes.data = buf;
    v->u.bytes.len = len;
    return v;
}

#ifdef MISA_USE_ZLIB
#include <zlib.h>

/* Fixed 10-byte gzip header: magic, CM=deflate, FLG=0, MTIME=0,
 * XFL=2 (max compression), OS=3 (Unix). */
static const uint8_t GZ_HDR[10] =
    {0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x03};

static void put_u32le(uint8_t *p, uint32_t x) {
    p[0] = (uint8_t)x;         p[1] = (uint8_t)(x >> 8);
    p[2] = (uint8_t)(x >> 16); p[3] = (uint8_t)(x >> 24);
}

/* Strict UTF-8 validation (matches Python's bytes.decode('utf-8')). */
static int utf8_validate(const uint8_t *d, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint8_t c = d[i];
        uint32_t cp;
        size_t w;
        if (c < 0x80) { w = 1; }
        else if ((c & 0xE0) == 0xC0) { w = 2; }
        else if ((c & 0xF0) == 0xE0) { w = 3; }
        else if ((c & 0xF8) == 0xF0) { w = 4; }
        else return 0;
        if (i + w > n) return 0;
        for (size_t k = 1; k < w; k++)
            if ((d[i + k] & 0xC0) != 0x80) return 0;
        cp = c & (w == 1 ? 0x7F : w == 2 ? 0x1F : w == 3 ? 0x0F : 0x07);
        for (size_t k = 1; k < w; k++)
            cp = (cp << 6) | (d[i + k] & 0x3F);
        if ((w == 2 && cp < 0x80) || (w == 3 && cp < 0x800) ||
            (w == 4 && cp < 0x10000) || cp > 0x10FFFF ||
            (cp >= 0xD800 && cp <= 0xDFFF)) return 0;
        i += w;
    }
    return 1;
}

/* Streaming gzip encoder: manual header, raw deflate, CRC32+ISIZE
 * trailer. Output goes through a sink so file variants stream. */
typedef struct {
    z_stream strm;
    void *sink_ctx;
    void (*sink)(void *ctx, const uint8_t *data, size_t len);
    uint32_t crc;
    uint32_t isize;
    int line, col;
    int ok;
} GzEnc;

static void gzenc_init(GzEnc *g, void *sink_ctx,
                       void (*sink)(void *, const uint8_t *, size_t),
                       int line, int col) {
    memset(g, 0, sizeof *g);
    g->sink_ctx = sink_ctx;
    g->sink = sink;
    g->line = line; g->col = col;
    g->ok = 1;
    g->crc = crc32(0L, Z_NULL, 0);
    sink(sink_ctx, GZ_HDR, sizeof GZ_HDR);
    if (deflateInit2(&g->strm, Z_BEST_COMPRESSION, Z_DEFLATED,
                     -15, 8, Z_DEFAULT_STRATEGY) != Z_OK) {
        misa_raise("E300", "compress.gzip failed: deflate init error",
                   line, col);
        g->ok = 0;
    }
}

static void gzenc_write(GzEnc *g, const uint8_t *data, size_t len) {
    uint8_t buf[65536];
    if (!g->ok) return;
    while (len > 0) {
        uInt chunk = len > 65536 ? 65536 : (uInt)len;
        g->crc = crc32(g->crc, data, chunk);
        g->isize += chunk;
        g->strm.next_in = (Bytef *)data;
        g->strm.avail_in = chunk;
        data += chunk;
        len -= chunk;
        int ret;
        do {
            g->strm.next_out = buf;
            g->strm.avail_out = sizeof buf;
            ret = deflate(&g->strm, Z_NO_FLUSH);
            if (ret != Z_OK) {
                deflateEnd(&g->strm);
                misa_raise("E300", "compress.gzip failed: deflate error",
                           g->line, g->col);
                g->ok = 0;
                return;
            }
            g->sink(g->sink_ctx, buf, sizeof buf - g->strm.avail_out);
        } while (g->strm.avail_out == 0);
    }
}

static void gzenc_finish(GzEnc *g) {
    uint8_t buf[65536];
    uint8_t trailer[8];
    int ret;
    if (!g->ok) return;
    do {
        g->strm.next_out = buf;
        g->strm.avail_out = sizeof buf;
        ret = deflate(&g->strm, Z_FINISH);
        if (ret != Z_OK && ret != Z_STREAM_END) {
            deflateEnd(&g->strm);
            misa_raise("E300", "compress.gzip failed: deflate error",
                       g->line, g->col);
            g->ok = 0;
            return;
        }
        g->sink(g->sink_ctx, buf, sizeof buf - g->strm.avail_out);
    } while (ret != Z_STREAM_END);
    deflateEnd(&g->strm);
    put_u32le(trailer, g->crc);
    put_u32le(trailer + 4, g->isize);
    g->sink(g->sink_ctx, trailer, sizeof trailer);
}

static void str_sink(void *ctx, const uint8_t *data, size_t len) {
    str_putn((Str *)ctx, (const char *)data, len);
}

/* Inflate a gzip (or zlib) stream into `out`. Returns 1 on success. */
static int gz_inflate_all(const uint8_t *in, size_t len, Str *out,
                          const char *what, int line, int col) {
    z_stream strm;
    uint8_t buf[65536];
    size_t off = 0;
    int ret;
    memset(&strm, 0, sizeof strm);
    /* 47 = auto-detect gzip/zlib wrapper. */
    if (inflateInit2(&strm, 47) != Z_OK) {
        misa_raise("E300", "decompression failed: inflate init error",
                   line, col);
        return 0;
    }
    ret = Z_OK;
    while (ret == Z_OK) {
        if (strm.avail_in == 0 && off < len) {
            uInt chunk =
                (len - off) > 65536 ? 65536 : (uInt)(len - off);
            strm.next_in = (Bytef *)(in + off);
            strm.avail_in = chunk;
            off += chunk;
        }
        strm.next_out = buf;
        strm.avail_out = sizeof buf;
        ret = inflate(&strm, Z_NO_FLUSH);
        if (ret != Z_OK && ret != Z_STREAM_END) {
            inflateEnd(&strm);
            misa_raise_fmt("E300", line, col,
                           "%s failed: corrupt gzip data (%s)", what,
                           strm.msg ? strm.msg : "inflate error");
            return 0;
        }
        str_putn(out, (char *)buf, sizeof buf - strm.avail_out);
        if (ret == Z_STREAM_END) break;
        if (strm.avail_in == 0 && off >= len) {
            inflateEnd(&strm);
            misa_raise_fmt("E300", line, col,
                           "%s failed: truncated gzip data", what);
            return 0;
        }
    }
    inflateEnd(&strm);
    return 1;
}

MVal *misa_compress_gzip(MVal *text, int line, int col) {
    Str out = {0};
    GzEnc g;
    if (text->tag != M_TAG_TEXT) {
        misa_raise("E200", "compress.gzip(Text) needs a text arg",
                   line, col);
        return NULL;
    }
    gzenc_init(&g, &out, str_sink, line, col);
    gzenc_write(&g, (const uint8_t *)text->u.text.data,
                text->u.text.len);
    gzenc_finish(&g);
    if (!g.ok) return NULL;
    return misa_bytes_new((const uint8_t *)out.buf, out.len);
}

MVal *misa_compress_gunzip(MVal *bytes, int line, int col) {
    Str out = {0};
    if (bytes->tag != M_TAG_BYTES) {
        misa_raise("E200", "compress.gunzip(Bytes) needs a bytes arg",
                   line, col);
        return NULL;
    }
    if (!gz_inflate_all(bytes->u.bytes.data, bytes->u.bytes.len, &out,
                        "compress.gunzip", line, col))
        return NULL;
    if (!utf8_validate((const uint8_t *)out.buf, out.len)) {
        misa_raise("E300", "compress.gunzip failed: output is not UTF-8 "
                           "text (use compress.gunzip_bytes for binary "
                           "data)", line, col);
        return NULL;
    }
    return misa_text(out.buf ? out.buf : "", out.len);
}

MVal *misa_compress_gunzip_bytes(MVal *bytes, int line, int col) {
    Str out = {0};
    if (bytes->tag != M_TAG_BYTES) {
        misa_raise("E200",
                   "compress.gunzip_bytes(Bytes) needs a bytes arg",
                   line, col);
        return NULL;
    }
    if (!gz_inflate_all(bytes->u.bytes.data, bytes->u.bytes.len, &out,
                        "compress.gunzip_bytes", line, col))
        return NULL;
    return misa_bytes_new((const uint8_t *)out.buf, out.len);
}

static void file_sink(void *ctx, const uint8_t *data, size_t len) {
    fwrite(data, 1, len, (FILE *)ctx);
}

MVal *misa_compress_gzip_file(MVal *path, MVal *outpath,
                              int line, int col) {
    uint8_t buf[65536];
    size_t n;
    long total = 0;
    FILE *fin, *fout;
    GzEnc g;
    char mp[4096], mo[4096];
    if (path->tag != M_TAG_TEXT || outpath->tag != M_TAG_TEXT) {
        misa_raise("E200",
                   "compress.gzip_file(Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    if (!misa_platform_fs_path(misa_text_z(path), mp, sizeof mp) ||
        !misa_platform_fs_path(misa_text_z(outpath), mo, sizeof mo)) {
        misa_raise_fmt("E303", line, col,
                       "compress.gzip_file failed: cannot map path");
        return NULL;
    }
    fin = fopen(mp, "rb");
    if (!fin) {
        misa_raise_fmt("E303", line, col,
                       "compress.gzip_file failed: cannot open '%s'",
                       misa_text_z(path));
        return NULL;
    }
    fout = fopen(mo, "wb");
    if (!fout) {
        fclose(fin);
        misa_raise_fmt("E303", line, col,
                       "compress.gzip_file failed: cannot open '%s'",
                       misa_text_z(outpath));
        return NULL;
    }
    gzenc_init(&g, fout, file_sink, line, col);
    while (g.ok && (n = fread(buf, 1, sizeof buf, fin)) > 0)
        gzenc_write(&g, buf, n);
    if (g.ok && ferror(fin)) {
        misa_raise_fmt("E303", line, col,
                       "compress.gzip_file failed: error reading '%s'",
                       misa_text_z(path));
        g.ok = 0;
    }
    gzenc_finish(&g);
    if (g.ok && ferror(fout)) {
        misa_raise_fmt("E303", line, col,
                       "compress.gzip_file failed: error writing '%s'",
                       misa_text_z(outpath));
        g.ok = 0;
    }
    fclose(fin);
    fclose(fout);
    if (!g.ok) return NULL;
    fout = fopen(mo, "rb");
    if (fout) {
        fseek(fout, 0, SEEK_END);
        total = ftell(fout);
        fclose(fout);
    }
    return misa_int(total);
}

MVal *misa_compress_gunzip_file(MVal *path, MVal *outpath,
                                int line, int col) {
    uint8_t inbuf[65536], outbuf[65536];
    size_t n;
    long total = 0;
    FILE *fin, *fout;
    z_stream strm;
    int ret = Z_OK;
    char mp[4096], mo[4096];
    if (path->tag != M_TAG_TEXT || outpath->tag != M_TAG_TEXT) {
        misa_raise("E200",
                   "compress.gunzip_file(Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    if (!misa_platform_fs_path(misa_text_z(path), mp, sizeof mp) ||
        !misa_platform_fs_path(misa_text_z(outpath), mo, sizeof mo)) {
        misa_raise_fmt("E303", line, col,
                       "compress.gunzip_file failed: cannot map path");
        return NULL;
    }
    fin = fopen(mp, "rb");
    if (!fin) {
        misa_raise_fmt("E303", line, col,
                       "compress.gunzip_file failed: cannot open '%s'",
                       misa_text_z(path));
        return NULL;
    }
    fout = fopen(mo, "wb");
    if (!fout) {
        fclose(fin);
        misa_raise_fmt("E303", line, col,
                       "compress.gunzip_file failed: cannot open '%s'",
                       misa_text_z(outpath));
        return NULL;
    }
    memset(&strm, 0, sizeof strm);
    if (inflateInit2(&strm, 47) != Z_OK) {
        fclose(fin);
        fclose(fout);
        misa_raise("E300",
                   "compress.gunzip_file failed: inflate init error",
                   line, col);
        return NULL;
    }
    while (ret == Z_OK) {
        if (strm.avail_in == 0) {
            n = fread(inbuf, 1, sizeof inbuf, fin);
            if (ferror(fin)) {
                inflateEnd(&strm);
                fclose(fin);
                fclose(fout);
                misa_raise_fmt("E303", line, col,
                               "compress.gunzip_file failed: error "
                               "reading '%s'", misa_text_z(path));
                return NULL;
            }
            if (n == 0) break;
            strm.next_in = inbuf;
            strm.avail_in = (uInt)n;
        }
        strm.next_out = outbuf;
        strm.avail_out = sizeof outbuf;
        ret = inflate(&strm, Z_NO_FLUSH);
        if (ret != Z_OK && ret != Z_STREAM_END) {
            inflateEnd(&strm);
            fclose(fin);
            fclose(fout);
            misa_raise_fmt("E300", line, col,
                           "compress.gunzip_file failed: corrupt gzip "
                           "data (%s)",
                           strm.msg ? strm.msg : "inflate error");
            return NULL;
        }
        if (sizeof outbuf - strm.avail_out > 0) {
            fwrite(outbuf, 1, sizeof outbuf - strm.avail_out, fout);
            total += (long)(sizeof outbuf - strm.avail_out);
        }
        if (ret == Z_STREAM_END) break;
    }
    if (ret != Z_STREAM_END) {
        inflateEnd(&strm);
        fclose(fin);
        fclose(fout);
        misa_raise_fmt("E300", line, col,
                       "compress.gunzip_file failed: truncated gzip data");
        return NULL;
    }
    inflateEnd(&strm);
    if (ferror(fout)) {
        fclose(fin);
        fclose(fout);
        misa_raise_fmt("E303", line, col,
                       "compress.gunzip_file failed: error writing '%s'",
                       misa_text_z(outpath));
        return NULL;
    }
    fclose(fin);
    fclose(fout);
    return misa_int(total);
}

#else /* !MISA_USE_ZLIB */

MVal *misa_compress_gzip(MVal *t, int line, int col) {
    (void)t;
    misa_raise("E600", "compress.gzip needs zlib "
                       "(not available on this target)", line, col);
    return NULL;
}
MVal *misa_compress_gunzip(MVal *b, int line, int col) {
    (void)b;
    misa_raise("E600", "compress.gunzip needs zlib "
                       "(not available on this target)", line, col);
    return NULL;
}
MVal *misa_compress_gunzip_bytes(MVal *b, int line, int col) {
    (void)b;
    misa_raise("E600", "compress.gunzip_bytes needs zlib "
                       "(not available on this target)", line, col);
    return NULL;
}
MVal *misa_compress_gzip_file(MVal *p, MVal *o, int line, int col) {
    (void)p; (void)o;
    misa_raise("E600", "compress.gzip_file needs zlib "
                       "(not available on this target)", line, col);
    return NULL;
}
MVal *misa_compress_gunzip_file(MVal *p, MVal *o, int line, int col) {
    (void)p; (void)o;
    misa_raise("E600", "compress.gunzip_file needs zlib "
                       "(not available on this target)", line, col);
    return NULL;
}

#endif /* MISA_USE_ZLIB */

/* -- Set<T>: insertion-ordered, val_equal membership (v1: O(n)) ----- */

static MVal *set_check(MVal *v, int line, int col, const char *what) {
    if (v->tag != M_TAG_SET) {
        misa_raise("E300", what, line, col);
        return NULL;
    }
    return v;
}

static int set_find(MVal *s, MVal *x) {
    for (int64_t i = 0; i < s->u.set.len; i++)
        if (val_equal(s->u.set.items[i], x)) return (int)i;
    return -1;
}

static void set_grow(MVal *s) {
    if (s->u.set.len < s->u.set.cap) return;
    int64_t ncap = s->u.set.cap ? s->u.set.cap * 2 : 8;
    MVal **ni = (MVal **)misa_alloc(ncap * sizeof(MVal *));
    memcpy(ni, s->u.set.items, s->u.set.len * sizeof(MVal *));
    s->u.set.items = ni;
    s->u.set.cap = ncap;
}

MVal *misa_set_new(void) {
    MVal *v = val_new(M_TAG_SET);
    v->u.set.items = NULL; v->u.set.len = 0; v->u.set.cap = 0;
    return v;
}

void misa_set_add(MVal *s, MVal *x, int line, int col) {
    if (!set_check(s, line, col, "set.add needs a Set")) return;
    if (set_find(s, x) < 0) {
        set_grow(s);
        s->u.set.items[s->u.set.len++] = x;
    }
}

MVal *misa_set_contains(MVal *s, MVal *x, int line, int col) {
    if (!set_check(s, line, col, "set.contains needs a Set")) return NULL;
    return misa_bool(set_find(s, x) >= 0);
}

void misa_set_remove(MVal *s, MVal *x, int line, int col) {
    if (!set_check(s, line, col, "set.remove needs a Set")) return;
    int i = set_find(s, x);
    if (i >= 0) {
        for (int64_t j = i; j + 1 < s->u.set.len; j++)
            s->u.set.items[j] = s->u.set.items[j + 1];
        s->u.set.len--;
    }
}

MVal *misa_set_union(MVal *a, MVal *b, int line, int col) {
    if (!set_check(a, line, col, "set.union needs Sets")) return NULL;
    if (!set_check(b, line, col, "set.union needs Sets")) return NULL;
    MVal *out = misa_set_new();
    for (int64_t i = 0; i < a->u.set.len; i++)
        misa_set_add(out, a->u.set.items[i], line, col);
    for (int64_t i = 0; i < b->u.set.len; i++)
        misa_set_add(out, b->u.set.items[i], line, col);
    return out;
}

MVal *misa_set_intersect(MVal *a, MVal *b, int line, int col) {
    if (!set_check(a, line, col, "set.intersect needs Sets")) return NULL;
    if (!set_check(b, line, col, "set.intersect needs Sets")) return NULL;
    MVal *out = misa_set_new();
    for (int64_t i = 0; i < a->u.set.len; i++)
        if (set_find(b, a->u.set.items[i]) >= 0)
            misa_set_add(out, a->u.set.items[i], line, col);
    return out;
}

MVal *misa_set_diff(MVal *a, MVal *b, int line, int col) {
    if (!set_check(a, line, col, "set.diff needs Sets")) return NULL;
    if (!set_check(b, line, col, "set.diff needs Sets")) return NULL;
    MVal *out = misa_set_new();
    for (int64_t i = 0; i < a->u.set.len; i++)
        if (set_find(b, a->u.set.items[i]) < 0)
            misa_set_add(out, a->u.set.items[i], line, col);
    return out;
}

MVal *misa_set_to_list(MVal *s, int line, int col) {
    if (!set_check(s, line, col, "set.to_list needs a Set")) return NULL;
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < s->u.set.len; i++)
        misa_list_push(out, s->u.set.items[i]);
    return out;
}

int64_t misa_set_len(MVal *s, int line, int col) {
    if (!set_check(s, line, col, "set.len needs a Set")) return 0;
    return s->u.set.len;
}

/* -- cli: declarative args parsing ----------------------------------- */
/* (CliFlag/CliCmd/MisaCliParser/MisaCliArgs are defined near the top,
 * after the MVal struct, so render_into can see them.) */

static char *cli_strdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = (char *)misa_alloc(n);
    memcpy(p, s, n);
    return p;
}

static MisaCliParser *cli_parser_of(MVal *v, int line, int col,
                                    const char *what) {
    if (v->tag != M_TAG_CLIPARSER) {
        misa_raise("E300", what, line, col);
        return NULL;
    }
    return (MisaCliParser *)v->u.cliparser.p;
}

static MisaCliArgs *cli_args_of(MVal *v, int line, int col,
                                const char *what) {
    if (v->tag != M_TAG_CLIARGS) {
        misa_raise("E300", what, line, col);
        return NULL;
    }
    return (MisaCliArgs *)v->u.cliargs.a;
}

static int64_t cli_find_flag(MisaCliParser *p, const char *name) {
    for (int64_t i = 0; i < p->nflags; i++)
        if (strcmp(p->flags[i].name, name) == 0) return i;
    return -1;
}

static int64_t cli_find_short(MisaCliParser *p, char c) {
    for (int64_t i = 0; i < p->nflags; i++)
        if (p->flags[i].short_c == c) return i;
    return -1;
}

static int64_t cli_find_cmd(MisaCliParser *p, const char *name) {
    for (int64_t i = 0; i < p->ncmds; i++)
        if (strcmp(p->cmds[i].name, name) == 0) return i;
    return -1;
}

static void cli_flags_grow(MisaCliParser *p) {
    if (p->nflags < p->cflags) return;
    int64_t nc = p->cflags ? p->cflags * 2 : 8;
    CliFlag *nf = (CliFlag *)misa_alloc(nc * sizeof(CliFlag));
    if (p->flags) memcpy(nf, p->flags, p->nflags * sizeof(CliFlag));
    p->flags = nf; p->cflags = nc;
}

static void cli_cmds_grow(MisaCliParser *p) {
    if (p->ncmds < p->ccmds) return;
    int64_t nc = p->ccmds ? p->ccmds * 2 : 8;
    CliCmd *nf = (CliCmd *)misa_alloc(nc * sizeof(CliCmd));
    if (p->cmds) memcpy(nf, p->cmds, p->ncmds * sizeof(CliCmd));
    p->cmds = nf; p->ccmds = nc;
}

static void cli_pos_grow(MisaCliArgs *a) {
    if (a->npos < a->cpos) return;
    int64_t nc = a->cpos ? a->cpos * 2 : 8;
    char **nf = (char **)misa_alloc(nc * sizeof(char *));
    if (a->pos) memcpy(nf, a->pos, a->npos * sizeof(char *));
    a->pos = nf; a->cpos = nc;
}

MVal *misa_cli_parser(MVal *prog, MVal *desc, int line, int col) {
    (void)line; (void)col;
    MisaCliParser *p = (MisaCliParser *)misa_alloc(sizeof *p);
    p->prog = cli_strdup(misa_text_data(prog));
    p->desc = cli_strdup(misa_text_data(desc));
    p->flags = NULL; p->nflags = 0; p->cflags = 0;
    p->cmds = NULL; p->ncmds = 0; p->ccmds = 0;
    MVal *v = val_new(M_TAG_CLIPARSER);
    v->u.cliparser.p = p;
    return v;
}

void misa_cli_flag(MVal *pv, MVal *name, MVal *metavar, MVal *help,
                    int32_t has_def, MVal *def, int line, int col) {
    MisaCliParser *p = cli_parser_of(pv, line, col, "cli.flag needs a CliParser");
    if (!p) return;
    const char *nm = misa_text_data(name);
    if (cli_find_flag(p, nm) >= 0) {
        misa_raise_fmt("E300", line, col, "duplicate flag '%s'", nm);
        return;
    }
    cli_flags_grow(p);
    CliFlag *f = &p->flags[p->nflags++];
    f->name = cli_strdup(nm);
    f->metavar = cli_strdup(misa_text_data(metavar));
    f->help = cli_strdup(misa_text_data(help));
    f->is_bool = 0;
    f->def = (has_def && def && def->tag == M_TAG_TEXT)
             ? cli_strdup(misa_text_data(def)) : NULL;
    f->short_c = 0;
}

void misa_cli_bool_flag(MVal *pv, MVal *name, MVal *help, int line, int col) {
    MisaCliParser *p = cli_parser_of(pv, line, col,
                                     "cli.bool_flag needs a CliParser");
    if (!p) return;
    const char *nm = misa_text_data(name);
    if (cli_find_flag(p, nm) >= 0) {
        misa_raise_fmt("E300", line, col, "duplicate flag '%s'", nm);
        return;
    }
    cli_flags_grow(p);
    CliFlag *f = &p->flags[p->nflags++];
    f->name = cli_strdup(nm);
    f->metavar = NULL;
    f->help = cli_strdup(misa_text_data(help));
    f->is_bool = 1;
    f->def = NULL;
    f->short_c = 0;
}

void misa_cli_short(MVal *pv, MVal *name, MVal *sh, int line, int col) {
    MisaCliParser *p = cli_parser_of(pv, line, col, "cli.short needs a CliParser");
    if (!p) return;
    const char *nm = misa_text_data(name);
    const char *ss = misa_text_data(sh);
    int64_t fi = cli_find_flag(p, nm);
    if (fi < 0) {
        misa_raise_fmt("E300", line, col,
                       "cli.short: no such flag '%s'", nm);
        return;
    }
    if (strlen(ss) != 1) {
        misa_raise_fmt("E300", line, col,
                       "cli.short: short flag must be one character, got '%s'",
                       ss);
        return;
    }
    if (ss[0] == 'h') {
        misa_raise("E300", "cli.short: '-h' is reserved for --help", line, col);
        return;
    }
    int64_t oi = cli_find_short(p, ss[0]);
    if (oi >= 0 && oi != fi) {
        misa_raise_fmt("E300", line, col,
                       "cli.short: '-%c' is already the short flag for '--%s'",
                       ss[0], p->flags[oi].name);
        return;
    }
    p->flags[fi].short_c = ss[0];
}

void misa_cli_command(MVal *pv, MVal *name, MVal *help, int line, int col) {
    MisaCliParser *p = cli_parser_of(pv, line, col,
                                     "cli.command needs a CliParser");
    if (!p) return;
    const char *nm = misa_text_data(name);
    if (cli_find_cmd(p, nm) >= 0) {
        misa_raise_fmt("E300", line, col, "duplicate command '%s'", nm);
        return;
    }
    cli_cmds_grow(p);
    CliCmd *c = &p->cmds[p->ncmds++];
    c->name = cli_strdup(nm);
    c->help = cli_strdup(misa_text_data(help));
}

/* Build the --help text. The interpreter mirrors this function exactly;
 * keep the two in sync (byte-identical output is asserted by parity). */
static void cli_help_into(Str *s, MisaCliParser *p) {
    int64_t i, j;
    if (p->desc[0]) {
        str_puts(s, p->prog); str_puts(s, " - "); str_puts(s, p->desc);
        str_puts(s, "\n\n");
    } else {
        str_puts(s, p->prog); str_puts(s, "\n\n");
    }
    str_puts(s, "Usage: "); str_puts(s, p->prog); str_puts(s, " [flags]");
    if (p->ncmds) str_puts(s, " <command> [args]");
    else str_puts(s, " [args]");
    str_puts(s, "\n\nFlags:\n");
    /* Column width = longest flag spec ("-d, --db PATH" / "--verbose"). */
    size_t width = strlen("-h, --help");
    for (i = 0; i < p->nflags; i++) {
        size_t w = 2 + strlen(p->flags[i].name); /* "--name" */
        if (p->flags[i].short_c) w += 4;        /* "-x, " */
        if (!p->flags[i].is_bool) w += 1 + strlen(p->flags[i].metavar);
        if (w > width) width = w;
    }
    for (i = 0; i < p->nflags; i++) {
        CliFlag *f = &p->flags[i];
        char spec[256];
        if (f->short_c)
            snprintf(spec, sizeof spec, "-%c, --%s", f->short_c, f->name);
        else
            snprintf(spec, sizeof spec, "--%s", f->name);
        if (!f->is_bool) {
            strncat(spec, " ", sizeof(spec) - strlen(spec) - 1);
            strncat(spec, f->metavar, sizeof(spec) - strlen(spec) - 1);
        }
        str_puts(s, "  ");
        str_puts(s, spec);
        for (j = strlen(spec); (size_t)j < width; j++) str_puts(s, " ");
        str_puts(s, "  ");
        str_puts(s, f->help);
        if (f->def) {
            str_puts(s, " (default: ");
            str_puts(s, f->def);
            str_puts(s, ")");
        }
        str_puts(s, "\n");
    }
    str_puts(s, "  -h, --help");
    for (j = strlen("-h, --help"); (size_t)j < width; j++) str_puts(s, " ");
    str_puts(s, "  Show this help and exit\n");
    if (p->ncmds) {
        str_puts(s, "\nCommands:\n");
        size_t cwidth = 0;
        for (i = 0; i < p->ncmds; i++) {
            size_t w = strlen(p->cmds[i].name);
            if (w > cwidth) cwidth = w;
        }
        for (i = 0; i < p->ncmds; i++) {
            str_puts(s, "  ");
            str_puts(s, p->cmds[i].name);
            for (j = strlen(p->cmds[i].name); (size_t)j < cwidth; j++)
                str_puts(s, " ");
            str_puts(s, "  ");
            str_puts(s, p->cmds[i].help);
            str_puts(s, "\n");
        }
    }
}

static void cli_show_help(MisaCliParser *p) {
    Str s = {0};
    cli_help_into(&s, p);
    misa_platform_log(s.buf ? s.buf : "", s.len);
    misa_platform_flush();
}

static int cli_is_flaglike(const char *t) {
    return t[0] == '-' && t[1] != '\0' && strcmp(t, "-") != 0;
}

MVal *misa_cli_parse(MVal *pv, MVal *argv, int line, int col) {
    MisaCliParser *p = cli_parser_of(pv, line, col, "cli.parse needs a CliParser");
    if (!p) return NULL;
    if (argv->tag != M_TAG_LIST) {
        misa_raise("E300", "cli.parse needs an argv list", line, col);
        return NULL;
    }
    MisaCliArgs *a = (MisaCliArgs *)misa_alloc(sizeof *a);
    a->parser = p;
    a->values = p->nflags
        ? (char **)misa_alloc(p->nflags * sizeof(char *)) : NULL;
    a->isset = p->nflags
        ? (char *)misa_alloc(p->nflags * sizeof(char)) : NULL;
    if (a->values) memset(a->values, 0, p->nflags * sizeof(char *));
    if (a->isset) memset(a->isset, 0, p->nflags * sizeof(char));
    a->command = NULL;
    a->pos = NULL; a->npos = 0; a->cpos = 0;

    int64_t n = misa_list_len(argv);
    int dashdash = 0;
    for (int64_t i = 0; i < n; i++) {
        const char *tok = misa_text_data(misa_list_get(argv, i));
        if (dashdash) {
            cli_pos_grow(a);
            a->pos[a->npos++] = cli_strdup(tok);
            continue;
        }
        if (strcmp(tok, "--") == 0) { dashdash = 1; continue; }
        if (strcmp(tok, "-") == 0 || tok[0] != '-') {
            /* Positional: first one is the command when commands exist. */
            if (p->ncmds && !a->command) {
                if (cli_find_cmd(p, tok) < 0) {
                    Str s = {0};
                    str_puts(&s, "unknown command '");
                    str_puts(&s, tok);
                    str_puts(&s, "' (expected one of:");
                    for (int64_t k = 0; k < p->ncmds; k++) {
                        str_puts(&s, " ");
                        str_puts(&s, p->cmds[k].name);
                    }
                    str_puts(&s, ")");
                    misa_raise_fmt("E300", line, col, "%s", s.buf);
                    return NULL;
                }
                a->command = cli_strdup(tok);
            } else {
                cli_pos_grow(a);
                a->pos[a->npos++] = cli_strdup(tok);
            }
            continue;
        }
        /* Long flag: --name or --name=value. */
        if (tok[1] == '-') {
            if (strcmp(tok, "--help") == 0) {
                cli_show_help(p);
                return misa_null();
            }
            const char *body = tok + 2;
            const char *eq = strchr(body, '=');
            char namebuf[128];
            const char *val = NULL;
            if (eq) {
                size_t nl = (size_t)(eq - body);
                if (nl >= sizeof namebuf) nl = sizeof(namebuf) - 1;
                memcpy(namebuf, body, nl);
                namebuf[nl] = '\0';
                val = eq + 1;
            } else {
                strncpy(namebuf, body, sizeof(namebuf) - 1);
                namebuf[sizeof(namebuf) - 1] = '\0';
            }
            if (namebuf[0] == '\0') {
                misa_raise_fmt("E300", line, col, "malformed flag '%s'", tok);
                return NULL;
            }
            if (strcmp(namebuf, "help") == 0) {
                cli_show_help(p);
                return misa_null();
            }
            int64_t fi = cli_find_flag(p, namebuf);
            if (fi < 0) {
                misa_raise_fmt("E300", line, col,
                               "unknown flag '--%s' (run '%s --help' for usage)",
                               namebuf, p->prog);
                return NULL;
            }
            CliFlag *f = &p->flags[fi];
            if (f->is_bool) {
                if (val) {
                    misa_raise_fmt("E300", line, col,
                                   "flag '--%s' takes no value (it's a bool flag)",
                                   namebuf);
                    return NULL;
                }
            } else if (!val) {
                if (i + 1 >= n || cli_is_flaglike(misa_text_data(misa_list_get(argv, i + 1)))) {
                    misa_raise_fmt("E300", line, col,
                                   "flag '--%s' needs a value", namebuf);
                    return NULL;
                }
                val = misa_text_data(misa_list_get(argv, ++i));
            }
            a->values[fi] = cli_strdup(val ? val : "");
            a->isset[fi] = 1;
            continue;
        }
        /* Short flag(s): -x, -h, -d PATH, -d=PATH. */
        if (strcmp(tok, "-h") == 0) {
            cli_show_help(p);
            return misa_null();
        }
        const char *rest = tok + 1;
        const char *eq = strchr(rest, '=');
        if (eq && eq != rest && eq[1] != '\0' && (eq - rest) == 1) {
            /* -d=PATH form. */
            int64_t fi = cli_find_short(p, rest[0]);
            if (fi < 0) {
                misa_raise_fmt("E300", line, col,
                               "unknown flag '-%c' (run '%s --help' for usage)",
                               rest[0], p->prog);
                return NULL;
            }
            CliFlag *f = &p->flags[fi];
            if (f->is_bool) {
                misa_raise_fmt("E300", line, col,
                               "flag '-%c' takes no value (it's a bool flag)",
                               rest[0]);
                return NULL;
            }
            a->values[fi] = cli_strdup(eq + 1);
            a->isset[fi] = 1;
            continue;
        }
        if (strlen(rest) != 1) {
            misa_raise_fmt("E300", line, col,
                           "combined short flags like '%s' are not supported "
                           "(use -a -b -c)", tok);
            return NULL;
        }
        int64_t fi = cli_find_short(p, rest[0]);
        if (fi < 0) {
            misa_raise_fmt("E300", line, col,
                           "unknown flag '-%c' (run '%s --help' for usage)",
                           rest[0], p->prog);
            return NULL;
        }
        CliFlag *f = &p->flags[fi];
        const char *val = NULL;
        if (!f->is_bool) {
            if (i + 1 >= n || cli_is_flaglike(misa_text_data(misa_list_get(argv, i + 1)))) {
                misa_raise_fmt("E300", line, col,
                               "flag '-%c' needs a value", rest[0]);
                return NULL;
            }
            val = misa_text_data(misa_list_get(argv, ++i));
        }
        a->values[fi] = cli_strdup(val ? val : "");
        a->isset[fi] = 1;
    }
    MVal *v = val_new(M_TAG_CLIARGS);
    v->u.cliargs.a = a;
    return v;
}

static int64_t cli_get_index(MVal *av, MVal *name, int line, int col,
                             const char *what, int want_bool) {
    MisaCliArgs *a = cli_args_of(av, line, col, what);
    if (!a) return -2;
    const char *nm = misa_text_data(name);
    int64_t fi = cli_find_flag(a->parser, nm);
    if (fi < 0) {
        misa_raise_fmt("E300", line, col,
                       "unknown flag '%s' (not defined on this parser)", nm);
        return -2;
    }
    CliFlag *f = &a->parser->flags[fi];
    if (want_bool && !f->is_bool) {
        misa_raise_fmt("E300", line, col,
                       "flag '%s' takes a value; use get, not get_bool", nm);
        return -2;
    }
    if (!want_bool && f->is_bool) {
        misa_raise_fmt("E300", line, col,
                       "flag '%s' is a bool flag; use get_bool, not get", nm);
        return -2;
    }
    return fi;
}

MVal *misa_cli_get(MVal *av, MVal *name, int line, int col) {
    if (av->tag == M_TAG_NULL) {
        misa_raise("E300", "no parsed arguments (parse returned nil, likely --help)",
                   line, col);
        return NULL;
    }
    int64_t fi = cli_get_index(av, name, line, col,
                               "cli.get needs CliArgs", 0);
    if (fi < 0) return NULL;
    MisaCliArgs *a = (MisaCliArgs *)av->u.cliargs.a;
    CliFlag *f = &a->parser->flags[fi];
    if (a->isset[fi]) return misa_text_cstr(a->values[fi]);
    if (f->def) return misa_text_cstr(f->def);
    misa_raise_fmt("E300", line, col,
                   "flag '--%s' was not provided and has no default",
                   f->name);
    return NULL;
}

MVal *misa_cli_get_bool(MVal *av, MVal *name, int line, int col) {
    if (av->tag == M_TAG_NULL) {
        misa_raise("E300", "no parsed arguments (parse returned nil, likely --help)",
                   line, col);
        return NULL;
    }
    int64_t fi = cli_get_index(av, name, line, col,
                               "cli.get_bool needs CliArgs", 1);
    if (fi < 0) return NULL;
    MisaCliArgs *a = (MisaCliArgs *)av->u.cliargs.a;
    return misa_bool(a->isset[fi] ? 1 : 0);
}

MVal *misa_cli_get_command(MVal *av, int line, int col) {
    (void)line; (void)col;
    if (av->tag == M_TAG_NULL) return misa_null();
    MisaCliArgs *a = (MisaCliArgs *)av->u.cliargs.a;
    if (!a->command) return misa_null();
    return misa_text_cstr(a->command);
}

MVal *misa_cli_positionals(MVal *av, int line, int col) {
    (void)line; (void)col;
    MVal *out = misa_list_new();
    if (av->tag == M_TAG_NULL) return out;
    MisaCliArgs *a = (MisaCliArgs *)av->u.cliargs.a;
    for (int64_t i = 0; i < a->npos; i++)
        misa_list_push(out, misa_text_cstr(a->pos[i]));
    return out;
}

/* -- calling a Misa closure value from C ---------------------------- */
/* Closures are records {__closure_fn: Int(addr), __closure_env}.
 * The compiled function has signature MVal*(MVal* env, MVal* args). */

static MVal *closure_invoke(MVal *clos, MVal **args, int nargs,
                            int line, int col) {
    MVal *fnv = misa_record_get(clos, "__closure_fn", line, col);
    if (!fnv) return NULL;
    MVal *env = misa_record_get(clos, "__closure_env", line, col);
    if (!env) return NULL;
    MVal *arglist = misa_list_new();
    for (int i = 0; i < nargs; i++) misa_list_push(arglist, args[i]);
    MVal *(*fn)(MVal *, MVal *) =
        (MVal *(*)(MVal *, MVal *))(intptr_t)misa_as_int(fnv);
    MVal *r = fn(env, arglist);
    if (misa_pending()) return NULL;
    return r;
}

/* Strict Bool check (mirrors the interpreter's `is True`). */
static int is_true(MVal *v) {
    return v->tag == M_TAG_BOOL && v->u.b;
}

MVal *misa_list_map(MVal *xs, MVal *f, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "map needs a list", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *r = closure_invoke(f, &xs->u.list.items[i], 1, line, col);
        if (!r) return NULL;
        misa_list_push(out, r);
    }
    return out;
}

MVal *misa_list_filter(MVal *xs, MVal *f, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "filter needs a list", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *r = closure_invoke(f, &xs->u.list.items[i], 1, line, col);
        if (!r) return NULL;
        if (is_true(r)) misa_list_push(out, xs->u.list.items[i]);
    }
    return out;
}

MVal *misa_list_fold(MVal *xs, MVal *init, MVal *f, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "fold needs a list", line, col);
        return NULL;
    }
    MVal *acc = init;
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *args[2] = {acc, xs->u.list.items[i]};
        acc = closure_invoke(f, args, 2, line, col);
        if (!acc) return NULL;
    }
    return acc;
}

/* Stable mergesort driven by a `less(a, b) -> Bool` comparator. */
static int sort_less(MVal *f, MVal *a, MVal *b, int line, int col,
                     int *err) {
    MVal *args[2] = {a, b};
    MVal *r = closure_invoke(f, args, 2, line, col);
    if (!r) { *err = 1; return 0; }
    return is_true(r);
}

static void merge_sort(MVal **arr, MVal **tmp, int64_t lo, int64_t hi,
                       MVal *f, int line, int col, int *err) {
    if (hi - lo < 2 || *err) return;
    int64_t mid = lo + (hi - lo) / 2;
    merge_sort(arr, tmp, lo, mid, f, line, col, err);
    merge_sort(arr, tmp, mid, hi, f, line, col, err);
    if (*err) return;
    int64_t i = lo, j = mid, k = lo;
    while (i < mid && j < hi) {
        /* stable: take from the left run when NOT (right < left) */
        if (sort_less(f, arr[j], arr[i], line, col, err))
            tmp[k++] = arr[j++];
        else
            tmp[k++] = arr[i++];
        if (*err) return;
    }
    while (i < mid) tmp[k++] = arr[i++];
    while (j < hi) tmp[k++] = arr[j++];
    for (i = lo; i < hi; i++) arr[i] = tmp[i];
}

MVal *misa_list_sort_by(MVal *xs, MVal *f, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "sort_by needs a list", line, col);
        return NULL;
    }
    int64_t n = xs->u.list.len;
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < n; i++)
        misa_list_push(out, xs->u.list.items[i]);
    if (n > 1) {
        MVal **tmp = (MVal **)misa_alloc(n * sizeof(MVal *));
        int err = 0;
        merge_sort(out->u.list.items, tmp, 0, n, f, line, col, &err);
        if (err) return NULL;
    }
    return out;
}

MVal *misa_list_flat(MVal *xss, int line, int col) {
    if (xss->tag != M_TAG_LIST) {
        misa_raise("E300", "list.flat needs a list", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < xss->u.list.len; i++) {
        MVal *xs = xss->u.list.items[i];
        if (xs->tag != M_TAG_LIST) {
            misa_raise("E300", "list.flat needs a list of lists",
                       line, col);
            return NULL;
        }
        for (int64_t j = 0; j < xs->u.list.len; j++)
            misa_list_push(out, xs->u.list.items[j]);
    }
    return out;
}

MVal *misa_list_zip(MVal *xs, MVal *ys, int line, int col) {
    if (xs->tag != M_TAG_LIST || ys->tag != M_TAG_LIST) {
        misa_raise("E300", "list.zip needs two lists", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    int64_t n = xs->u.list.len < ys->u.list.len ? xs->u.list.len
                                                : ys->u.list.len;
    for (int64_t i = 0; i < n; i++) {
        MVal *pair = misa_list_new();
        misa_list_push(pair, xs->u.list.items[i]);
        misa_list_push(pair, ys->u.list.items[i]);
        misa_list_push(out, pair);
    }
    return out;
}

MVal *misa_list_unique(MVal *xs, int line, int col) {
    if (xs->tag != M_TAG_LIST) {
        misa_raise("E300", "list.unique needs a list", line, col);
        return NULL;
    }
    MVal *out = misa_list_new();
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *x = xs->u.list.items[i];
        int seen = 0;
        for (int64_t j = 0; j < out->u.list.len; j++)
            if (val_equal(out->u.list.items[j], x)) { seen = 1; break; }
        if (!seen) misa_list_push(out, x);
    }
    return out;
}

/* -- str extras (Wave 3) -------------------------------------------- */

MVal *misa_str_join(MVal *xs, MVal *sep, int line, int col) {
    if (xs->tag != M_TAG_LIST || sep->tag != M_TAG_TEXT) {
        misa_raise("E300", "str.join([Text], Text) needs text args",
                   line, col);
        return NULL;
    }
    size_t total = 0;
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *x = xs->u.list.items[i];
        if (x->tag != M_TAG_TEXT) {
            misa_raise("E300", "str.join([Text], Text) needs text args",
                       line, col);
            return NULL;
        }
        total += x->u.text.len;
        if (i) total += sep->u.text.len;
    }
    char *buf = (char *)misa_alloc(total + 1);
    size_t off = 0;
    for (int64_t i = 0; i < xs->u.list.len; i++) {
        MVal *x = xs->u.list.items[i];
        if (i) {
            memcpy(buf + off, sep->u.text.data, sep->u.text.len);
            off += sep->u.text.len;
        }
        memcpy(buf + off, x->u.text.data, x->u.text.len);
        off += x->u.text.len;
    }
    buf[total] = '\0';
    MVal *v = misa_text(buf, total);
    return v;
}

MVal *misa_str_replace(MVal *s, MVal *old, MVal *nw, int line, int col) {
    if (s->tag != M_TAG_TEXT || old->tag != M_TAG_TEXT ||
        nw->tag != M_TAG_TEXT) {
        misa_raise("E300",
                   "str.replace(Text, Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    if (old->u.text.len == 0) {
        misa_raise("E300", "str.replace: search string is empty",
                   line, col);
        return NULL;
    }
    const char *data = s->u.text.data, *sp = old->u.text.data,
               *rp = nw->u.text.data;
    size_t len = s->u.text.len, slen = old->u.text.len,
           rlen = nw->u.text.len;
    /* Count occurrences to size the buffer. */
    size_t count = 0;
    for (size_t k = 0; k + slen <= len; k++) {
        if (memcmp(data + k, sp, slen) == 0) { count++; k += slen - 1; }
    }
    size_t total = len + count * (rlen > slen ? rlen - slen : 0);
    /* (shrinking still fits: total >= final size) */
    if (rlen < slen) total = len;
    char *buf = (char *)misa_alloc(total + 1);
    size_t off = 0, k = 0;
    while (k < len) {
        if (k + slen <= len && memcmp(data + k, sp, slen) == 0) {
            memcpy(buf + off, rp, rlen);
            off += rlen; k += slen;
        } else {
            buf[off++] = data[k++];
        }
    }
    buf[off] = '\0';
    return misa_text(buf, off);
}

MVal *misa_str_to_upper(MVal *s, int line, int col) {
    const char *t = as_text(s, line, col, "str.to_upper(Text) needs text");
    if (!t) return NULL;
    /* v1: ASCII only (documented); full Unicode casefold is future. */
    size_t len = s->u.text.len;
    char *buf = (char *)misa_alloc(len + 1);
    for (size_t i = 0; i < len; i++) {
        char c = t[i];
        buf[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
    }
    buf[len] = '\0';
    return misa_text(buf, len);
}

MVal *misa_str_to_lower(MVal *s, int line, int col) {
    const char *t = as_text(s, line, col, "str.to_lower(Text) needs text");
    if (!t) return NULL;
    size_t len = s->u.text.len;
    char *buf = (char *)misa_alloc(len + 1);
    for (size_t i = 0; i < len; i++) {
        char c = t[i];
        buf[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    buf[len] = '\0';
    return misa_text(buf, len);
}

MVal *misa_str_ends_with(MVal *s, MVal *sfx, int line, int col) {
    if (s->tag != M_TAG_TEXT || sfx->tag != M_TAG_TEXT) {
        misa_raise("E300",
                   "str.ends_with(Text, Text) needs text args",
                   line, col);
        return NULL;
    }
    size_t len = s->u.text.len, slen = sfx->u.text.len;
    if (slen > len) return misa_bool(0);
    return misa_bool(memcmp(s->u.text.data + len - slen,
                            sfx->u.text.data, slen) == 0);
}

/* Pad with the first char of `pad` (code-point aware). */
static MVal *str_pad(MVal *s, int64_t width, MVal *pad, int left,
                     int line, int col) {
    const char *t = as_text(s, line, col, "str.pad needs text");
    if (!t) return NULL;
    if (pad->tag != M_TAG_TEXT || pad->u.text.len == 0) {
        misa_raise("E300", "str.pad: pad string is empty", line, col);
        return NULL;
    }
    Decoded d = decode_text(t, s->u.text.len);
    if ((int64_t)d.n >= width) return s;
    /* First code point of pad, as bytes. */
    const unsigned char *pp = (const unsigned char *)pad->u.text.data;
    const unsigned char *start = pp;
    utf8_decode_one(&pp);
    size_t padlen = (size_t)(pp - start);
    int64_t need = width - (int64_t)d.n;
    size_t total = s->u.text.len + (size_t)need * padlen;
    char *buf = (char *)misa_alloc(total + 1);
    size_t off = 0;
    if (left)
        for (int64_t i = 0; i < need; i++) {
            memcpy(buf + off, start, padlen); off += padlen;
        }
    memcpy(buf + off, t, s->u.text.len);
    off += s->u.text.len;
    if (!left)
        for (int64_t i = 0; i < need; i++) {
            memcpy(buf + off, start, padlen); off += padlen;
        }
    buf[off] = '\0';
    return misa_text(buf, off);
}

MVal *misa_str_pad_left(MVal *s, MVal *w, MVal *pad, int line, int col) {
    if (w->tag != M_TAG_INT) {
        misa_raise("E300", "str.pad_left(Text, Int, Text) needs int",
                   line, col);
        return NULL;
    }
    return str_pad(s, w->u.i, pad, 1, line, col);
}

MVal *misa_str_pad_right(MVal *s, MVal *w, MVal *pad, int line, int col) {
    if (w->tag != M_TAG_INT) {
        misa_raise("E300", "str.pad_right(Text, Int, Text) needs int",
                   line, col);
        return NULL;
    }
    return str_pad(s, w->u.i, pad, 0, line, col);
}

// Optimized list access with unboxed int64 index (avoids index boxing)
MVal *misa_list_get_raw(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return NULL;
    // Unboxed bool list: box the result
    if (list->tag == M_TAG_LIST_BOOL) {
        int b = misa_list_bool_get(list, idx, line, col);
        if (misa_pending()) return NULL;
        return misa_bool(b);
    }
    // Unboxed int list: box the result
    if (list->tag == M_TAG_LIST_INT) {
        int64_t v = misa_list_int_get(list, idx, line, col);
        if (misa_pending()) return NULL;
        return misa_int(v);
    }
    if (list->tag != M_TAG_LIST) {
        misa_raise("E200", "index on non-list", line, col);
        return NULL;
    }
    if (idx < 0 || idx >= list->u.list.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return NULL;
    }
    return list->u.list.items[idx];
}

int64_t misa_list_get_int_raw(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return 0;
    if (!list) {
        misa_raise("E200", "index on non-list", line, col);
        return 0;
    }
    if (list->tag == M_TAG_LIST_INT) {
        if (idx < 0 || idx >= list->u.list_int.len) {
            misa_raise("E300", "index out of range", line, col);
            return 0;
        }
        return list->u.list_int.items[idx];
    }
    if (list->tag != M_TAG_LIST) {
        misa_raise("E200", "expected [Int] list", line, col);
        return 0;
    }
    if (idx < 0 || idx >= list->u.list.len) {
        misa_raise("E300", "index out of range", line, col);
        return 0;
    }
    MVal *v = list->u.list.items[idx];
    if (v && v->tag == M_TAG_INT) return v->u.i;
    misa_raise("E200", "expected Int element in [Int] list", line, col);
    return 0;
}

double misa_list_get_float_raw(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return 0.0;
    if (!list) {
        misa_raise("E200", "index on non-list", line, col);
        return 0.0;
    }
    if (list->tag == M_TAG_LIST_FLOAT) {
        if (idx < 0 || idx >= list->u.list_float.len) {
            misa_raise("E300", "index out of range", line, col);
            return 0.0;
        }
        return list->u.list_float.items[idx];
    }
    if (list->tag != M_TAG_LIST) {
        misa_raise("E200", "expected [Float] list", line, col);
        return 0.0;
    }
    if (idx < 0 || idx >= list->u.list.len) {
        misa_raise("E300", "index out of range", line, col);
        return 0.0;
    }
    MVal *v = list->u.list.items[idx];
    if (v && v->tag == M_TAG_FLOAT) return v->u.f;
    if (v && v->tag == M_TAG_INT) return (double)v->u.i;
    misa_raise("E200", "expected numeric element in [Float] list",
               line, col);
    return 0.0;
}

void misa_list_set_raw(MVal *list, int64_t idx, MVal *val, int line, int col) {
    if (misa_pending()) return;
    // Dispatch to unboxed storage if applicable
    if (list->tag == M_TAG_LIST_BOOL) {
        if (val->tag != M_TAG_BOOL) {
            misa_raise("E200", "type mismatch in [Bool] list", line, col);
            return;
        }
        misa_list_bool_set(list, idx, val->u.b, line, col);
        return;
    }
    if (list->tag == M_TAG_LIST_INT) {
        if (val->tag != M_TAG_INT) {
            misa_raise("E200", "type mismatch in [Int] list", line, col);
            return;
        }
        misa_list_int_set(list, idx, val->u.i, line, col);
        return;
    }
    if (list->tag != M_TAG_LIST) {
        misa_raise("E200", "index-assign on non-list", line, col);
        return;
    }
    if (idx < 0 || idx >= list->u.list.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return;
    }
    list->u.list.items[idx] = val;
}

// Returns raw bool (i1) for [Bool] list element — avoids MVal* unboxing
int misa_list_get_bool_raw(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return 0;
    if (list->tag != M_TAG_LIST) {
        misa_raise("E200", "index on non-list", line, col);
        return 0;
    }
    if (idx < 0 || idx >= list->u.list.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return 0;
    }
    MVal *v = list->u.list.items[idx];
    if (v->tag == M_TAG_BOOL) return v->u.b;
    // fallback: truthy
    return misa_truthy(v);
}

// Unboxed typed list constructors
MVal *misa_list_bool_new(void) {
    MVal *l = val_new(M_TAG_LIST_BOOL);
    l->u.list_bool.items = NULL;
    l->u.list_bool.len = 0;
    l->u.list_bool.cap = 0;
    return l;
}

MVal *misa_list_int_new(void) {
    MVal *l = val_new(M_TAG_LIST_INT);
    l->u.list_int.items = NULL;
    l->u.list_int.len = 0;
    l->u.list_int.cap = 0;
    return l;
}

MVal *misa_list_float_new(void) {
    MVal *l = val_new(M_TAG_LIST_FLOAT);
    l->u.list_float.items = NULL;
    l->u.list_float.len = 0;
    l->u.list_float.cap = 0;
    return l;
}

void misa_list_bool_push(MVal *list, int b) {
    list->u.list_bool.items = (uint8_t*)grow_array(
        list->u.list_bool.items, &list->u.list_bool.cap,
        list->u.list_bool.len + 1, sizeof(uint8_t));
    list->u.list_bool.items[list->u.list_bool.len++] = b ? 1 : 0;
}

void misa_list_int_push(MVal *list, int64_t v) {
    list->u.list_int.items = (int64_t*)grow_array(
        list->u.list_int.items, &list->u.list_int.cap,
        list->u.list_int.len + 1, sizeof(int64_t));
    list->u.list_int.items[list->u.list_int.len++] = v;
}

static void misa_list_float_push(MVal *list, double v) {
    list->u.list_float.items = (double*)grow_array(
        list->u.list_float.items, &list->u.list_float.cap,
        list->u.list_float.len + 1, sizeof(double));
    list->u.list_float.items[list->u.list_float.len++] = v;
}

void misa_list_reserve(MVal *list, int64_t capacity, int line, int col) {
    if (misa_pending()) return;
    if (!list || capacity < 0) {
        misa_raise("E300", "list.reserve needs a list and non-negative capacity",
                   line, col);
        return;
    }
    switch (list->tag) {
    case M_TAG_LIST:
        if ((uint64_t)capacity > SIZE_MAX / sizeof(MVal *)) goto too_large;
        list->u.list.items = (MVal **)grow_array(
            list->u.list.items, &list->u.list.cap,
            capacity > list->u.list.len ? capacity : list->u.list.len,
            sizeof(MVal *));
        return;
    case M_TAG_LIST_BOOL:
        if ((uint64_t)capacity > SIZE_MAX / sizeof(uint8_t)) goto too_large;
        list->u.list_bool.items = (uint8_t *)grow_array(
            list->u.list_bool.items, &list->u.list_bool.cap,
            capacity > list->u.list_bool.len ? capacity : list->u.list_bool.len,
            sizeof(uint8_t));
        return;
    case M_TAG_LIST_INT:
        if ((uint64_t)capacity > SIZE_MAX / sizeof(int64_t)) goto too_large;
        list->u.list_int.items = (int64_t *)grow_array(
            list->u.list_int.items, &list->u.list_int.cap,
            capacity > list->u.list_int.len ? capacity : list->u.list_int.len,
            sizeof(int64_t));
        return;
    case M_TAG_LIST_FLOAT:
        if ((uint64_t)capacity > SIZE_MAX / sizeof(double)) goto too_large;
        list->u.list_float.items = (double *)grow_array(
            list->u.list_float.items, &list->u.list_float.cap,
            capacity > list->u.list_float.len ? capacity : list->u.list_float.len,
            sizeof(double));
        return;
    default:
        misa_raise("E300", "list.reserve needs a list", line, col);
        return;
    }
too_large:
    misa_raise("E300", "list.reserve capacity is too large", line, col);
}

void misa_list_append(MVal *list, MVal *value, int line, int col) {
    if (!list || !value) {
        misa_raise("E300", "list.push needs a list and a value", line, col);
        return;
    }
    switch (list->tag) {
    case M_TAG_LIST:
        misa_list_push(list, value);
        return;
    case M_TAG_LIST_BOOL:
        if (value->tag != M_TAG_BOOL) {
            misa_raise("E300", "list.push value type mismatch", line, col);
            return;
        }
        misa_list_bool_push(list, value->u.b);
        return;
    case M_TAG_LIST_INT:
        if (value->tag != M_TAG_INT) {
            misa_raise("E300", "list.push value type mismatch", line, col);
            return;
        }
        misa_list_int_push(list, value->u.i);
        return;
    case M_TAG_LIST_FLOAT:
        if (value->tag == M_TAG_INT) {
            misa_list_float_push(list, (double)value->u.i);
            return;
        }
        if (value->tag == M_TAG_FLOAT) {
            misa_list_float_push(list, value->u.f);
            return;
        }
        misa_raise("E300", "list.push value type mismatch", line, col);
        return;
    default:
        misa_raise("E300", "list.push needs a list", line, col);
        return;
    }
}

// Direct unboxed access (no MVal* boxing)
int misa_list_bool_get(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return 0;
    if (idx < 0 || idx >= list->u.list_bool.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return 0;
    }
    return list->u.list_bool.items[idx];
}

void misa_list_bool_set(MVal *list, int64_t idx, int b, int line, int col) {
    if (misa_pending()) return;
    if (idx < 0 || idx >= list->u.list_bool.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return;
    }
    list->u.list_bool.items[idx] = b ? 1 : 0;
}

int64_t misa_list_int_get(MVal *list, int64_t idx, int line, int col) {
    if (misa_pending()) return 0;
    if (idx < 0 || idx >= list->u.list_int.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return 0;
    }
    return list->u.list_int.items[idx];
}

void misa_list_int_set(MVal *list, int64_t idx, int64_t v, int line, int col) {
    if (misa_pending()) return;
    if (idx < 0 || idx >= list->u.list_int.len) {
        misa_raise_fmt("E304", line, col, "list index out of range");
        return;
    }
    list->u.list_int.items[idx] = v;
}

// Create a [Bool] list of size n, initialized to false
MVal *misa_bool_list_new(int64_t n, int line, int col) {
    if (misa_pending()) return NULL;
    if (n < 0) {
        misa_raise("E200", "negative list size", line, col);
        return NULL;
    }
    MVal *l = misa_list_bool_new();
    l->u.list_bool.items = (uint8_t*)misa_alloc((size_t)n);
    memset(l->u.list_bool.items, 0, (size_t)n);
    l->u.list_bool.len = n;
    l->u.list_bool.cap = n;
    return l;
}
