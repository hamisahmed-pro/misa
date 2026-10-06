/* Misa AOT runtime — core values, errors, arithmetic, collections.
 *
 * Memory: every MVal comes from misa_alloc(), which is GC_malloc() when
 * the runtime is built with -DMISA_USE_GC (Boehm), plain malloc()
 * otherwise. Boehm is conservative: all MVal* held in generated-code
 * allocas, the C stack, and registers are visible roots.
 */
#include "misa_rt.h"
#include "misa_platform.h" /* P1-P4: logging, fatal, env, fs root */

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

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
    } u;
};

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
}

/* ------------------------------------------------------------------ */
/* errors                                                             */
/* ------------------------------------------------------------------ */

typedef struct {
    MVal  *err;   /* T_ERROR value */
    int    line;
    int    col;
} Pending;

static _Thread_local Pending tl_pending = {0};

MVal *misa_error_new(const char *code, const char *msg) {
    MVal *e = val_new(M_TAG_ERROR);
    e->u.error.code = misa_text_cstr(code ? code : "E300");
    e->u.error.message = misa_text_cstr(msg ? msg : "error");
    return e;
}

void misa_raise(const char *code, const char *msg, int line, int col) {
    if (!tl_pending.err) {
        tl_pending.err = misa_error_new(code, msg);
        tl_pending.line = line;
        tl_pending.col = col;
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

MVal *misa_pending(void) { return tl_pending.err; }

MVal *misa_take_pending(void) {
    MVal *e = tl_pending.err;
    tl_pending.err = NULL;
    tl_pending.line = 0;
    tl_pending.col = 0;
    return e;
}

/* `throw e`: e must be an Error value (checked by generated code via
 * misa_do_throw_check); here we just install it as pending. */
void misa_do_throw(MVal *err, int line, int col) {
    if (!tl_pending.err) {
        tl_pending.err = err;
        tl_pending.line = line;
        tl_pending.col = col;
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
    if (tl_pending.line > 0)
        snprintf(head, sizeof head, "%d:%d: ",
                 tl_pending.line, tl_pending.col);
    else
        snprintf(head, sizeof head, "<unknown>: ");
    misa_platform_log_err(head, strlen(head));
    misa_platform_log_err(code, strlen(code));
    misa_platform_log_err(": ", 2);
    misa_platform_log_err(msg, strlen(msg));
    misa_platform_log_err("\n", 1);
    misa_platform_flush();
}

/* ------------------------------------------------------------------ */
/* constructors                                                       */
/* ------------------------------------------------------------------ */

MVal *misa_int(int64_t v)   { MVal *x = val_new(M_TAG_INT); x->u.i = v; return x; }
MVal *misa_float(double v)  { MVal *x = val_new(M_TAG_FLOAT); x->u.f = v; return x; }
MVal *misa_bool(int v)      { MVal *x = val_new(M_TAG_BOOL); x->u.b = v ? 1 : 0; return x; }
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
    case M_TAG_MAP:   return v->u.map.len > 0;
    case M_TAG_RECORD:return v->u.record.len > 0;
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

/* Format a double exactly like Python str(float): shortest round-trip,
 * with ".0" for integral values. */
static void str_put_float(Str *s, double f) {
    char tmp[64];
    snprintf(tmp, sizeof tmp, "%.17g", f);
    str_puts(s, tmp);
    /* append ".0" when the text looks like a plain integer */
    int i = 0, is_intlike = 1;
    if (tmp[0] == '-' || tmp[0] == '+') i = 1;
    if (!tmp[i]) is_intlike = 0;
    for (; tmp[i] && is_intlike; i++) {
        if (tmp[i] < '0' || tmp[i] > '9') is_intlike = 0;
    }
    if (is_intlike) str_puts(s, ".0");
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
    default: str_puts(s, "<value>"); break;
    }
}

MVal *misa_render(MVal *v) {
    Str s = {0};
    render_into(&s, v);
    return misa_text(s.buf ? s.buf : "", s.len);
}

MVal *misa_text_concat(MVal *a, MVal *b) {
    Str s = {0};
    MVal *ra = misa_render(a), *rb = misa_render(b);
    /* render() of Text is identity content-wise; use raw bytes */
    str_putn(&s, ra->u.text.data, ra->u.text.len);
    str_putn(&s, rb->u.text.data, rb->u.text.len);
    return misa_text(s.buf ? s.buf : "", s.len);
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
    misa_raise("E200", "cannot multiply these values", line, col);
    return NULL;
}

MVal *misa_div(MVal *a, MVal *b, int line, int col) {
    /* Misa `/` is always float division (interpreter parity). */
    if (!both_num(a, b)) {
        misa_raise("E200", "cannot divide these values", line, col);
        return NULL;
    }
    double d = to_f(b);
    if (d == 0.0) { misa_raise("E300", "division by zero", line, col); return NULL; }
    return misa_float(to_f(a) / d);
}

/* Python-style floored modulo. */
MVal *misa_mod(MVal *a, MVal *b, int line, int col) {
    if (both_int(a, b)) {
        int64_t x = a->u.i, y = b->u.i;
        if (y == 0) { misa_raise("E300", "integer modulo by zero", line, col); return NULL; }
        if (y == -1) return misa_int(0); /* x % -1 == 0; also dodges INT64_MIN/-1 UB */
        int64_t r = x % y; /* truncated; safe now */
        if (r != 0 && ((r < 0) != (y < 0))) r += y; /* floor it, Python-style */
        return misa_int(r);
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
    case M_TAG_MAP:    return v->u.map.len;
    case M_TAG_RECORD: return v->u.record.len;
    default:
        misa_raise("E300", "len() needs text, list, map, or record", line, col);
        return 0;
    }
}

int64_t misa_list_len(MVal *l) { return l->u.list.len; }
MVal *misa_list_get(MVal *l, int64_t i) { return l->u.list.items[i]; }

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
        char *end = NULL;
        /* Python int(str) strips whitespace and needs the full string */
        while (*s && isspace((unsigned char)*s)) s++;
        if (!*s) goto bad;
        long long r = strtoll(s, &end, 10);
        while (*end && isspace((unsigned char)*end)) end++;
        if (*end) goto bad;
        (void)r;
        return misa_int((int64_t)r);
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

MVal *misa_builtin_env(const char *name, int line, int col) {
    /* P3: web/mobile have no process environment; the platform reads
     * an app-supplied config table instead. */
    const char *v = misa_platform_getenv(name);
    if (!v) {
        misa_raise_fmt("E300", line, col, "env var '%s' is not set", name);
        return NULL;
    }
    return misa_text_cstr(v);
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
 * unchanged (E304, interpreter parity). */

MVal *misa_http_get(MVal *url, int line, int col) {
    const char *u = misa_text_z(url);
    if (strncmp(u, "http://", 7) != 0 && strncmp(u, "https://", 8) != 0) {
        misa_raise_fmt("E304", line, col,
                       "http.get needs an http(s) URL, got '%s'", u);
        return NULL;
    }
    MisaHttpResp r;
    memset(&r, 0, sizeof r);
    if (!misa_platform_http_get(u, 30.0, &r)) {
        misa_raise_fmt("E304", line, col, "http.get failed: %s", r.err);
        misa_platform_http_free(&r);
        return NULL;
    }
    long status = r.status;
    if (status < 200 || status >= 300) {
        misa_raise_fmt("E304", line, col, "http.get %s -> HTTP %d", u,
                       (int)status);
        misa_platform_http_free(&r);
        return NULL;
    }
    /* body must decode as UTF-8 (interpreter parity) */
    const unsigned char *d = (const unsigned char *)(r.body ? r.body : "");
    size_t len = r.len;
    for (size_t i = 0; i < len;) {
        unsigned char c = d[i];
        size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
        if (!w || i + w > len) {
            misa_raise_fmt("E304", line, col,
                           "http.get %s: body is not UTF-8 text", u);
            misa_platform_http_free(&r);
            return NULL;
        }
        i += w;
    }
    MVal *t = misa_text((const char *)d, len);
    misa_platform_http_free(&r);
    return t;
}

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
#if defined(_XOPEN_SOURCE) || defined(__linux__) || defined(__APPLE__)
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
     * portable fallback via mktime + timezone correction. */
#if defined(__linux__) || defined(__APPLE__) || defined(__FreeBSD__)
    time_t t = timegm(&tmv);
#else
    {
        /* Portable: interpret as UTC by round-tripping through mktime
         * in a forced-UTC environment is racy; instead compute the
         * civil-days formula directly. */
        int y = tmv.tm_year + 1900, m = tmv.tm_mon + 1, d = tmv.tm_mday;
        y -= m <= 2;
        int era = (y >= 0 ? y : y - 399) / 400;
        unsigned yoe = (unsigned)(y - era * 400);
        unsigned doy =
            (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + (unsigned)(d - 1);
        unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;
        time_t t2 = (time_t)(days * 86400 + tmv.tm_hour * 3600 +
                             tmv.tm_min * 60 + tmv.tm_sec);
        return misa_int((int64_t)t2);
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
