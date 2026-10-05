/* Misa AOT runtime — blocking model calls.
 *
 * Mirrors the interpreter's call_model(): prompt rendering, 3 attempts
 * (initial + 2 repair retries), structured-output validation against the
 * declared return type, and token/latency/cost budgets (E500).
 *
 * Providers: openai (OpenAI-compatible POST /chat/completions),
 * gemini (POST :generateContent), mock (MISA_MOCK_FILE scripts).
 * Streaming is not supported in AOT v0.3 (compile-time E402).
 */
#include "misa_rt.h"
#include "misa_platform.h" /* P3 getenv; HTTP transport */

#include <ctype.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* ------------------------------------------------------------------ */
/* minimal JSON parser                                                */
/* ------------------------------------------------------------------ */

typedef enum { J_NULL, J_BOOL, J_INT, J_FLOAT, J_STR, J_ARR, J_OBJ } JTag;

typedef struct JVal {
    JTag tag;
    union {
        int b;
        int64_t i;
        double f;
        struct { char *data; size_t len; } str;
        struct { struct JVal **items; size_t len, cap; } arr;
        struct { char **keys; struct JVal **vals; size_t len, cap; } obj;
    } u;
} JVal;

typedef struct {
    const char *p;
    const char *end;
    int depth;
    char err[256];
} JParser;

static JVal *jval_new(JTag t) {
    JVal *v = (JVal *)misa_alloc(sizeof(JVal));
    memset(v, 0, sizeof(JVal));
    v->tag = t;
    return v;
}

static void jskip_ws(JParser *j) {
    while (j->p < j->end && (*j->p == ' ' || *j->p == '\t' ||
                             *j->p == '\n' || *j->p == '\r'))
        j->p++;
}

static JVal *jparse_val(JParser *j);

static int jparse_hex4(JParser *j, unsigned *out) {
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        if (j->p >= j->end) return 0;
        char c = *j->p++;
        v <<= 4;
        if (c >= '0' && c <= '9') v |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') v |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') v |= (unsigned)(c - 'A' + 10);
        else return 0;
    }
    *out = v;
    return 1;
}

/* append a unicode code point as UTF-8 */
static void jput_utf8(char **buf, size_t *len, size_t *cap, unsigned cp) {
    char tmp[4];
    int n = 0;
    if (cp < 0x80) { tmp[0] = (char)cp; n = 1; }
    else if (cp < 0x800) {
        tmp[0] = (char)(0xC0 | (cp >> 6)); tmp[1] = (char)(0x80 | (cp & 63)); n = 2;
    } else if (cp < 0x10000) {
        tmp[0] = (char)(0xE0 | (cp >> 12)); tmp[1] = (char)(0x80 | ((cp >> 6) & 63));
        tmp[2] = (char)(0x80 | (cp & 63)); n = 3;
    } else {
        tmp[0] = (char)(0xF0 | (cp >> 18)); tmp[1] = (char)(0x80 | ((cp >> 12) & 63));
        tmp[2] = (char)(0x80 | ((cp >> 6) & 63)); tmp[3] = (char)(0x80 | (cp & 63)); n = 4;
    }
    if (*len + (size_t)n + 1 > *cap) {
        *cap = *cap ? *cap * 2 : 32;
        while (*cap < *len + (size_t)n + 1) *cap *= 2;
        char *nb = (char *)misa_alloc(*cap);
        if (*buf) memcpy(nb, *buf, *len);
        *buf = nb;
    }
    memcpy(*buf + *len, tmp, (size_t)n);
    *len += (size_t)n;
}

static JVal *jparse_str(JParser *j) {
    j->p++; /* opening quote */
    char *buf = NULL;
    size_t len = 0, cap = 0;
    while (j->p < j->end) {
        char c = *j->p++;
        if (c == '"') break;
        if (c == '\\') {
            if (j->p >= j->end) goto bad;
            char e = *j->p++;
            switch (e) {
            case '"': case '\\': case '/':
                jput_utf8(&buf, &len, &cap, (unsigned char)e); break;
            case 'b': jput_utf8(&buf, &len, &cap, '\b'); break;
            case 'f': jput_utf8(&buf, &len, &cap, '\f'); break;
            case 'n': jput_utf8(&buf, &len, &cap, '\n'); break;
            case 'r': jput_utf8(&buf, &len, &cap, '\r'); break;
            case 't': jput_utf8(&buf, &len, &cap, '\t'); break;
            case 'u': {
                unsigned cp;
                if (!jparse_hex4(j, &cp)) goto bad;
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    /* surrogate pair */
                    if (j->p + 1 < j->end && j->p[0] == '\\' && j->p[1] == 'u') {
                        unsigned lo;
                        j->p += 2;
                        if (!jparse_hex4(j, &lo)) goto bad;
                        if (lo >= 0xDC00 && lo <= 0xDFFF)
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        else goto bad;
                    } else goto bad;
                }
                jput_utf8(&buf, &len, &cap, cp);
                break;
            }
            default: goto bad;
            }
        } else {
            jput_utf8(&buf, &len, &cap, (unsigned char)c);
        }
    }
    jput_utf8(&buf, &len, &cap, 0); /* NUL (len excludes it) */
    len--; /* don't count the NUL */
    {
        JVal *v = jval_new(J_STR);
        v->u.str.data = buf ? buf : (char *)misa_alloc(1);
        v->u.str.len = len;
        return v;
    }
bad:
    snprintf(j->err, sizeof j->err, "bad string escape");
    return NULL;
}

static JVal *jparse_num(JParser *j) {
    const char *start = j->p;
    if (j->p < j->end && *j->p == '-') j->p++;
    while (j->p < j->end && isdigit((unsigned char)*j->p)) j->p++;
    int is_float = 0;
    if (j->p < j->end && *j->p == '.') {
        is_float = 1; j->p++;
        while (j->p < j->end && isdigit((unsigned char)*j->p)) j->p++;
    }
    if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
        is_float = 1; j->p++;
        if (j->p < j->end && (*j->p == '+' || *j->p == '-')) j->p++;
        while (j->p < j->end && isdigit((unsigned char)*j->p)) j->p++;
    }
    size_t n = (size_t)(j->p - start);
    char *tmp = (char *)misa_alloc(n + 1);
    memcpy(tmp, start, n);
    tmp[n] = '\0';
    if (is_float) {
        JVal *v = jval_new(J_FLOAT);
        v->u.f = strtod(tmp, NULL);
        return v;
    }
    JVal *v = jval_new(J_INT);
    v->u.i = strtoll(tmp, NULL, 10);
    return v;
}

static JVal *jparse_arr(JParser *j) {
    j->p++; /* [ */
    JVal *v = jval_new(J_ARR);
    jskip_ws(j);
    if (j->p < j->end && *j->p == ']') { j->p++; return v; }
    while (1) {
        JVal *it = jparse_val(j);
        if (!it) return NULL;
        if (v->u.arr.len + 1 > v->u.arr.cap) {
            size_t nc = v->u.arr.cap ? v->u.arr.cap * 2 : 8;
            JVal **nb = (JVal **)misa_alloc(nc * sizeof(JVal *));
            if (v->u.arr.items)
                memcpy(nb, v->u.arr.items, v->u.arr.len * sizeof(JVal *));
            v->u.arr.items = nb;
            v->u.arr.cap = nc;
        }
        v->u.arr.items[v->u.arr.len++] = it;
        jskip_ws(j);
        if (j->p < j->end && *j->p == ',') { j->p++; jskip_ws(j); continue; }
        if (j->p < j->end && *j->p == ']') { j->p++; return v; }
        snprintf(j->err, sizeof j->err, "expected ',' or ']' in array");
        return NULL;
    }
}

static JVal *jparse_obj(JParser *j) {
    j->p++; /* { */
    JVal *v = jval_new(J_OBJ);
    jskip_ws(j);
    if (j->p < j->end && *j->p == '}') { j->p++; return v; }
    while (1) {
        jskip_ws(j);
        if (j->p >= j->end || *j->p != '"') {
            snprintf(j->err, sizeof j->err, "expected string key");
            return NULL;
        }
        JVal *ks = jparse_str(j);
        if (!ks) return NULL;
        jskip_ws(j);
        if (j->p >= j->end || *j->p != ':') {
            snprintf(j->err, sizeof j->err, "expected ':' in object");
            return NULL;
        }
        j->p++;
        jskip_ws(j);
        JVal *val = jparse_val(j);
        if (!val) return NULL;
        if (v->u.obj.len + 1 > v->u.obj.cap) {
            size_t nc = v->u.obj.cap ? v->u.obj.cap * 2 : 8;
            char **nk = (char **)misa_alloc(nc * sizeof(char *));
            JVal **nv = (JVal **)misa_alloc(nc * sizeof(JVal *));
            if (v->u.obj.keys) {
                memcpy(nk, v->u.obj.keys, v->u.obj.len * sizeof(char *));
                memcpy(nv, v->u.obj.vals, v->u.obj.len * sizeof(JVal *));
            }
            v->u.obj.keys = nk;
            v->u.obj.vals = nv;
            v->u.obj.cap = nc;
        }
        v->u.obj.keys[v->u.obj.len] = ks->u.str.data;
        v->u.obj.vals[v->u.obj.len] = val;
        v->u.obj.len++;
        jskip_ws(j);
        if (j->p < j->end && *j->p == ',') { j->p++; continue; }
        if (j->p < j->end && *j->p == '}') { j->p++; return v; }
        snprintf(j->err, sizeof j->err, "expected ',' or '}' in object");
        return NULL;
    }
}

static JVal *jparse_val(JParser *j) {
    jskip_ws(j);
    if (j->p >= j->end) {
        snprintf(j->err, sizeof j->err, "unexpected end of JSON");
        return NULL;
    }
    if (++j->depth > 64) {
        snprintf(j->err, sizeof j->err, "JSON too deeply nested");
        return NULL;
    }
    JVal *v = NULL;
    char c = *j->p;
    if (c == '{') v = jparse_obj(j);
    else if (c == '[') v = jparse_arr(j);
    else if (c == '"') v = jparse_str(j);
    else if (c == '-' || isdigit((unsigned char)c)) v = jparse_num(j);
    else if (j->end - j->p >= 4 && memcmp(j->p, "true", 4) == 0) {
        j->p += 4; v = jval_new(J_BOOL); v->u.b = 1;
    } else if (j->end - j->p >= 5 && memcmp(j->p, "false", 5) == 0) {
        j->p += 5; v = jval_new(J_BOOL); v->u.b = 0;
    } else if (j->end - j->p >= 4 && memcmp(j->p, "null", 4) == 0) {
        j->p += 4; v = jval_new(J_NULL);
    } else {
        snprintf(j->err, sizeof j->err, "unexpected character '%c'", c);
    }
    j->depth--;
    return v;
}

/* Parse; on success *endp advanced past trailing ws. NULL on failure. */
static JVal *json_parse(const char *text, size_t len, char *errbuf,
                        size_t errcap) {
    JParser j;
    memset(&j, 0, sizeof j);
    j.p = text;
    j.end = text + len;
    JVal *v = jparse_val(&j);
    if (v) {
        jskip_ws(&j);
        if (j.p != j.end) {
            snprintf(errbuf, errcap, "trailing characters after JSON");
            return NULL;
        }
    } else {
        snprintf(errbuf, errcap, "%s",
                 j.err[0] ? j.err : "invalid JSON");
    }
    return v;
}

static JVal *jobj_get(JVal *obj, const char *key) {
    if (!obj || obj->tag != J_OBJ) return NULL;
    for (size_t i = 0; i < obj->u.obj.len; i++) {
        if (strcmp(obj->u.obj.keys[i], key) == 0)
            return obj->u.obj.vals[i];
    }
    return NULL;
}

static JVal *jarr_get(JVal *arr, size_t i) {
    if (!arr || arr->tag != J_ARR || i >= arr->u.arr.len) return NULL;
    return arr->u.arr.items[i];
}

/* ------------------------------------------------------------------ */
/* return-type schemas (emitter-generated strings)                    */
/*   schema := type ['?']                                             */
/*   type   := Text|Int|Float|Bool | '[' schema ']'                    */
/*           | '{' name ':' schema (',' name ':' schema)* '}' | 'Map'  */
/* ------------------------------------------------------------------ */

enum { S_TEXT, S_INT, S_FLOAT, S_BOOL, S_LIST, S_RECORD, S_MAP, S_BAD };

typedef struct Schema {
    int kind;
    int optional;
    struct Schema *elem;      /* list */
    char   **fnames;
    struct Schema **fields;
    int nfields;
    char *badname;            /* S_BAD: the offending type name */
} Schema;

typedef struct {
    const char *p;
    char err[128];
} SParser;

static Schema *schema_new(int kind) {
    Schema *s = (Schema *)misa_alloc(sizeof(Schema));
    memset(s, 0, sizeof(Schema));
    s->kind = kind;
    return s;
}

static void sskip(SParser *s) {
    while (*s->p == ' ') s->p++;
}

static Schema *sparse_type(SParser *s);

static Schema *sparse_type(SParser *s) {
    sskip(s);
    Schema *t = NULL;
    if (*s->p == '[') {
        s->p++;
        t = schema_new(S_LIST);
        t->elem = sparse_type(s);
        if (!t->elem) return NULL;
        sskip(s);
        if (*s->p != ']') return NULL;
        s->p++;
    } else if (*s->p == '{') {
        s->p++;
        t = schema_new(S_RECORD);
        sskip(s);
        if (*s->p == '}') { s->p++; }
        else {
            while (1) {
                sskip(s);
                const char *ns = s->p;
                while (*s->p && (isalnum((unsigned char)*s->p) || *s->p == '_'))
                    s->p++;
                size_t nl = (size_t)(s->p - ns);
                if (!nl) return NULL;
                char *name = (char *)misa_alloc(nl + 1);
                memcpy(name, ns, nl);
                name[nl] = '\0';
                sskip(s);
                if (*s->p != ':') return NULL;
                s->p++;
                Schema *f = sparse_type(s);
                if (!f) return NULL;
                {
                    int n = t->nfields;
                    char **nn = (char **)misa_alloc(
                        sizeof(char *) * (size_t)(n + 1));
                    Schema **nf = (Schema **)misa_alloc(
                        sizeof(Schema *) * (size_t)(n + 1));
                    for (int i = 0; i < n; i++) {
                        nn[i] = t->fnames[i];
                        nf[i] = t->fields[i];
                    }
                    nn[n] = name;
                    nf[n] = f;
                    t->fnames = nn;
                    t->fields = nf;
                    t->nfields = n + 1;
                }
                sskip(s);
                if (*s->p == ',') { s->p++; continue; }
                if (*s->p == '}') { s->p++; break; }
                return NULL;
            }
        }
    } else {
        const char *ns = s->p;
        while (*s->p && isalpha((unsigned char)*s->p)) s->p++;
        size_t nl = (size_t)(s->p - ns);
        char tmp[16];
        if (nl >= sizeof tmp) return NULL;
        memcpy(tmp, ns, nl);
        tmp[nl] = '\0';
        if (strcmp(tmp, "Text") == 0) t = schema_new(S_TEXT);
        else if (strcmp(tmp, "Int") == 0) t = schema_new(S_INT);
        else if (strcmp(tmp, "Float") == 0) t = schema_new(S_FLOAT);
        else if (strcmp(tmp, "Bool") == 0) t = schema_new(S_BOOL);
        else if (strcmp(tmp, "Map") == 0) t = schema_new(S_MAP);
        else {
            /* named/media types the emitter left symbolic: reject at
             * call time exactly like the interpreter (E400). */
            t = schema_new(S_BAD);
            t->badname = (char *)misa_alloc(nl + 1);
            memcpy(t->badname, tmp, nl + 1);
        }
    }
    sskip(s);
    if (*s->p == '?') { t->optional = 1; s->p++; }
    return t;
}

/* ---------------- validation ------------------ */

#define MAX_VERRS 16

typedef struct {
    char *msgs[MAX_VERRS];
    int n;
} VErrs;

static void verr(VErrs *e, const char *path, const char *fmt, ...) {
    if (e->n >= MAX_VERRS) return;
    char buf[256], pb[320];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    snprintf(pb, sizeof pb, "%s: %s", path, buf);
    e->msgs[e->n++] = (char *)misa_text_z(misa_text_cstr(pb));
}

static const char *jtype_name(JVal *v) {
    switch (v->tag) {
    case J_NULL: return "null";
    case J_BOOL: return "boolean";
    case J_INT: return "integer";
    case J_FLOAT: return "number";
    case J_STR: return "string";
    case J_ARR: return "array";
    default: return "object";
    }
}

/* Returns MVal on success, NULL on failure (errors appended). */
static MVal *validate_against(JVal *d, Schema *s, const char *path,
                              VErrs *e) {
    char sub[384];
    if (s->optional && d->tag == J_NULL) return misa_null();
    switch (s->kind) {
    case S_TEXT:
        if (d->tag == J_STR) return misa_text(d->u.str.data, d->u.str.len);
        verr(e, path, "expected Text, got %s", jtype_name(d));
        return NULL;
    case S_INT:
        if (d->tag == J_INT) return misa_int(d->u.i);
        verr(e, path, "expected Int, got %s", jtype_name(d));
        return NULL;
    case S_FLOAT:
        if (d->tag == J_INT) return misa_float((double)d->u.i);
        if (d->tag == J_FLOAT) return misa_float(d->u.f);
        verr(e, path, "expected Float, got %s", jtype_name(d));
        return NULL;
    case S_BOOL:
        if (d->tag == J_BOOL) return misa_bool(d->u.b);
        verr(e, path, "expected Bool, got %s", jtype_name(d));
        return NULL;
    case S_LIST: {
        if (d->tag != J_ARR) {
            verr(e, path, "expected list, got %s", jtype_name(d));
            return NULL;
        }
        MVal *l = misa_list_new();
        int ok = 1;
        for (size_t i = 0; i < d->u.arr.len; i++) {
            snprintf(sub, sizeof sub, "%s[%zu]", path, i);
            MVal *it = validate_against(d->u.arr.items[i], s->elem, sub, e);
            if (!it) ok = 0;
            else misa_list_push(l, it);
        }
        return ok ? l : NULL;
    }
    case S_RECORD: {
        if (d->tag != J_OBJ) {
            verr(e, path, "expected object, got %s", jtype_name(d));
            return NULL;
        }
        MVal *r = misa_record_new();
        int ok = 1;
        for (int i = 0; i < s->nfields; i++) {
            JVal *fv = jobj_get(d, s->fnames[i]);
            snprintf(sub, sizeof sub, "%s.%s", path, s->fnames[i]);
            if (!fv) {
                if (s->fields[i]->optional) {
                    misa_record_set_cstr(r, s->fnames[i], misa_null());
                    continue;
                }
                verr(e, sub, "missing field");
                ok = 0;
                continue;
            }
            MVal *vv = validate_against(fv, s->fields[i], sub, e);
            if (!vv) ok = 0;
            else misa_record_set_cstr(r, s->fnames[i], vv);
        }
        return ok ? r : NULL;
    }
    case S_MAP:
        verr(e, path, "unknown type 'Map'");
        return NULL;
    case S_BAD:
        verr(e, path, "models cannot return %s in v0.1",
             s->badname ? s->badname : "?");
        return NULL;
    default:
        verr(e, path, "unknown type in schema");
        return NULL;
    }
}

/* ------------------------------------------------------------------ */
/* helpers: base64, JSON string escaping                               */
/* ------------------------------------------------------------------ */

/* base64 for image parts of the request body (provider logic, not
 * transport: the encoded body is handed to misa_platform_http_post). */
static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static char *base64_encode(const unsigned char *in, size_t n) {
    size_t out = ((n + 2) / 3) * 4;
    char *r = (char *)misa_alloc(out + 1);
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        unsigned v = (unsigned)in[i] << 16;
        if (i + 1 < n) v |= (unsigned)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        r[o++] = B64[(v >> 18) & 63];
        r[o++] = B64[(v >> 12) & 63];
        r[o++] = i + 1 < n ? B64[(v >> 6) & 63] : '=';
        r[o++] = i + 2 < n ? B64[v & 63] : '=';
    }
    r[o] = '\0';
    return r;
}

/* JSON-escape a string into a Str-like buffer (reuses misa_rt Str via render of text) */
typedef struct {
    char  *buf;
    size_t len, cap;
} Buf;

static void buf_reserve(Buf *b, size_t extra) {
    if (b->len + extra + 1 <= b->cap) return;
    size_t nc = b->cap ? b->cap * 2 : 256;
    while (nc < b->len + extra + 1) nc *= 2;
    char *nb = (char *)misa_alloc(nc);
    if (b->buf && b->len) memcpy(nb, b->buf, b->len);
    b->buf = nb;
    b->cap = nc;
}

static void buf_putn(Buf *b, const char *p, size_t n) {
    buf_reserve(b, n);
    memcpy(b->buf + b->len, p, n);
    b->len += n;
    b->buf[b->len] = '\0';
}

static void buf_puts(Buf *b, const char *p) { buf_putn(b, p, strlen(p)); }

static void buf_put_json_str(Buf *b, const char *s, size_t n) {
    buf_putn(b, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"': buf_puts(b, "\\\""); break;
        case '\\': buf_puts(b, "\\\\"); break;
        case '\n': buf_puts(b, "\\n"); break;
        case '\r': buf_puts(b, "\\r"); break;
        case '\t': buf_puts(b, "\\t"); break;
        case '\b': buf_puts(b, "\\b"); break;
        case '\f': buf_puts(b, "\\f"); break;
        default:
            if (c < 0x20) {
                char tmp[8];
                snprintf(tmp, sizeof tmp, "\\u%04x", c);
                buf_puts(b, tmp);
            } else {
                buf_putn(b, (const char *)&s[i], 1);
            }
        }
    }
    buf_putn(b, "\"", 1);
}

/* ------------------------------------------------------------------ */
/* HTTP POST helper                                                    */
/*                                                                    */
/* The transport moved to the platform layer (Phase 4, M2):           */
/* misa_platform_http_post() in rt/platform_linux.c (libcurl when     */
/* built with -DMISA_USE_CURL). misa_model.c keeps the provider       */
/* logic — request-body construction above, response parsing below.   */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* pricing (estimates — mirrors providers.py)                         */
/* ------------------------------------------------------------------ */

static double estimate_cost_usd(const char *model_id,
                                int64_t in_tok, int64_t out_tok) {
    struct { const char *prefix; double pi, po; } table[] = {
        {"gpt-5-mini", 0.00015, 0.0006},
        {"gpt-5", 0.00125, 0.01},
        {"gemini-2.5-flash", 0.000075, 0.0003},
        {"claude-haiku", 0.00025, 0.00125},
        {"deepseek", 0.00014, 0.00028},
    };
    char lower[128];
    size_t n = strlen(model_id);
    if (n >= sizeof lower) n = sizeof lower - 1;
    for (size_t i = 0; i < n; i++)
        lower[i] = (char)tolower((unsigned char)model_id[i]);
    lower[n] = '\0';
    for (size_t k = 0; k < sizeof(table) / sizeof(table[0]); k++) {
        if (strncmp(lower, table[k].prefix, strlen(table[k].prefix)) == 0)
            return in_tok / 1000.0 * table[k].pi +
                   out_tok / 1000.0 * table[k].po;
    }
    return in_tok / 1000.0 * 0.001 + out_tok / 1000.0 * 0.003;
}

/* ------------------------------------------------------------------ */
/* provider completion                                                 */
/* ------------------------------------------------------------------ */

typedef struct {
    char   *text;        /* raw model text (expected JSON) */
    int64_t in_tok;
    int64_t out_tok;
    char    ecode[8];    /* set when provider_complete returns PC_ECODE */
} Completion;

/* ------------------------------------------------------------------ */
/* on-device inference (Phase 5, M2): ONNX Runtime                      */
/* ------------------------------------------------------------------ */
/* Mirrors impl/misa/providers.py OnDeviceProvider: the model file's
 * extension selects the runtime; `.onnx` runs here via the ORT C API
 * (compiled in with -DMISA_USE_ORT). The first model output is
 * serialized to JSON in out->text so the existing schema validation
 * in misa_model_call applies unchanged, exactly like M1.
 *
 * v1 type mapping (must match _sig_kind in providers.py):
 *   Float  -> scalar float32 tensor (shape [] or [1])
 *   [Float] -> 1-D float32 tensor
 *   Text   -> string tensor
 * E501 = tensor shape mismatch, E502 = unsupported signature,
 * E503 = runtime not available on this platform. These surface via
 * PC_ECODE with the code in Completion.ecode. */

enum { PC_OK = 0, PC_MODEL_ERR = 1, PC_CONFIG_ERR = 2, PC_ECODE = 3 };

static const char *ondevice_runtime(const char *path) {
    const char *dot = strrchr(path, '.');
    char ext[16];
    size_t i;
    if (!dot) return "unknown";
    for (i = 0; i + 1 < sizeof ext && dot[i]; i++)
        ext[i] = (char)tolower((unsigned char)dot[i]);
    ext[i] = '\0';
    if (strcmp(ext, ".onnx") == 0) return "onnx";
    if (strcmp(ext, ".mlpackage") == 0 || strcmp(ext, ".mlmodelc") == 0)
        return "coreml";
    if (strcmp(ext, ".tflite") == 0) return "tflite";
    return "unknown";
}

/* Resolve the embedded model path at runtime. build.py embeds the
 * absolute source path and stages a copy next to the binary, so try:
 * (1) the path as embedded, (2) <exe-dir>/<basename> (shipped app),
 * (3) ./<basename> (current directory). */
static void ondevice_resolve_path(const char *given, char *out, size_t cap) {
    FILE *f = fopen(given, "rb");
    if (f) {
        fclose(f);
        snprintf(out, cap, "%s", given);
        return;
    }
    const char *base = strrchr(given, '/');
    base = base ? base + 1 : given;
#ifdef __linux__
    {
        char exe[1024];
        ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
        if (n > 0) {
            exe[n] = '\0';
            char *slash = strrchr(exe, '/');
            if (slash) {
                *slash = '\0';
                snprintf(out, cap, "%s/%s", exe, base);
                f = fopen(out, "rb");
                if (f) {
                    fclose(f);
                    return;
                }
            }
        }
    }
#endif
    snprintf(out, cap, "%s", given);
}

#ifdef MISA_USE_ORT
#include <onnxruntime_c_api.h>

static const OrtApi *ort_api(void) {
    static const OrtApi *api = NULL;
    if (!api) api = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    return api;
}

/* v1: one cached session per process (programs use a single model). */
static char ort_cached_path[1024] = {0};
static OrtEnv *ort_env = NULL;
static OrtSession *ort_sess = NULL;

static int ort_ensure_session(const char *path, char *errbuf, size_t errcap) {
    const OrtApi *api = ort_api();
    OrtStatus *st;
    if (ort_sess && strcmp(ort_cached_path, path) == 0) return 1;
    if (ort_sess) {
        api->ReleaseSession(ort_sess);
        ort_sess = NULL;
        ort_cached_path[0] = '\0';
    }
    if (!ort_env) {
        st = api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "misa", &ort_env);
        if (st) {
            snprintf(errbuf, errcap, "ORT env failed: %s",
                     api->GetErrorMessage(st));
            api->ReleaseStatus(st);
            return 0;
        }
    }
    {
        OrtSessionOptions *opts = NULL;
        st = api->CreateSessionOptions(&opts);
        if (st) {
            snprintf(errbuf, errcap, "ORT options failed: %s",
                     api->GetErrorMessage(st));
            api->ReleaseStatus(st);
            return 0;
        }
        st = api->CreateSession(ort_env, path, opts, &ort_sess);
        api->ReleaseSessionOptions(opts);
        if (st) {
            snprintf(errbuf, errcap, "cannot load on-device model '%s': %s",
                     path, api->GetErrorMessage(st));
            api->ReleaseStatus(st);
            return 0;
        }
    }
    snprintf(ort_cached_path, sizeof ort_cached_path, "%s", path);
    return 1;
}

/* Declared-type -> v1 kind. Mirrors _sig_kind: only Float/[Float]/Text. */
static const char *ort_sig_kind(const char *t) {
    char b[32];
    size_t n = strlen(t);
    if (n > 0 && t[n - 1] == '?') n--;  /* optional handled by caller */
    if (n >= sizeof b) return NULL;
    memcpy(b, t, n);
    b[n] = '\0';
    if (strcmp(b, "Float") == 0) return "scalar";
    if (strcmp(b, "[Float]") == 0) return "vec";
    if (strcmp(b, "Text") == 0) return "text";
    return NULL;
}

static int ort_infer(const MModelMeta *meta, MVal **args, int nargs,
                     Completion *out, char *errbuf, size_t errcap) {
    const OrtApi *api = ort_api();
    OrtStatus *st;
    OrtAllocator *alloc = NULL;
    OrtMemoryInfo *meminfo = NULL;
    char mpath[1024];
    size_t i, n_in = 0, n_out = 0;
    char **in_names = NULL, **out_names = NULL;
    OrtValue **in_vals = NULL;
    OrtValue *out_val = NULL;
    float **fbufs = NULL;      /* owned float buffers, freed at done */
    size_t nfbufs = 0;
    int rc = PC_ECODE;         /* default: caller raises ecode */
    Buf jb = {0};

    /* OCHK: on ORT status failure, format errbuf and jump to cleanup. */
#define OCHK(call_, ...) do { \
        OrtStatus *_st = (call_); \
        if (_st) { \
            snprintf(errbuf, errcap, __VA_ARGS__); \
            api->ReleaseStatus(_st); \
            goto done; \
        } \
    } while (0)

    ondevice_resolve_path(meta->model_file ? meta->model_file : "",
                          mpath, sizeof mpath);
    if (!ort_ensure_session(mpath, errbuf, errcap))
        return PC_MODEL_ERR;
    OCHK(api->GetAllocatorWithDefaultOptions(&alloc),
         "ORT allocator failed");
    OCHK(api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault,
                                  &meminfo),
         "ORT memory info failed");
    OCHK(api->SessionGetInputCount(ort_sess, &n_in),
         "ORT input count failed");
    OCHK(api->SessionGetOutputCount(ort_sess, &n_out),
         "ORT output count failed");
    if ((int)n_in != nargs) {
        snprintf(errbuf, errcap,
                 "E502: model takes %zu input(s), called with %d arg(s)",
                 n_in, nargs);
        goto done;
    }
    if (n_out < 1) {
        snprintf(errbuf, errcap, "E502: model has no outputs");
        goto done;
    }
    in_names = (char **)misa_alloc(sizeof(char *) * (n_in ? n_in : 1));
    memset(in_names, 0, sizeof(char *) * (n_in ? n_in : 1));
    in_vals = (OrtValue **)misa_alloc(sizeof(OrtValue *) * (n_in ? n_in : 1));
    memset(in_vals, 0, sizeof(OrtValue *) * (n_in ? n_in : 1));
    fbufs = (float **)misa_alloc(sizeof(float *) * (n_in ? n_in : 1));
    for (i = 0; i < n_in; i++) {
        const char *pname = meta->params[i].name;
        const char *ptype = meta->params[i].type;
        const char *kind = ort_sig_kind(ptype ? ptype : "");
        MVal *av = args[i];
        OrtTypeInfo *ti = NULL;
        const OrtTensorTypeAndShapeInfo *tinfo = NULL;
        ONNXTensorElementDataType et = ONNX_TENSOR_ELEMENT_DATA_TYPE_UNDEFINED;
        enum ONNXType ot = ONNX_TYPE_UNKNOWN;
        size_t ndim = 0;
        int64_t dims[8];
        char *nm = NULL;

        if (!kind) {
            snprintf(errbuf, errcap,
                     "E502: param '%s': type '%s' is not supported "
                     "on-device in v1 (Float, [Float], Text)",
                     pname, ptype ? ptype : "?");
            goto done;
        }
        OCHK(api->SessionGetInputName(ort_sess, i, alloc, &nm),
             "E502: cannot read model input %zu", i);
        in_names[i] = nm;
        OCHK(api->SessionGetInputTypeInfo(ort_sess, i, &ti),
             "E502: cannot read type of model input '%s'", nm);
        OCHK(api->GetOnnxTypeFromTypeInfo(ti, &ot),
             "E502: cannot read type of model input '%s'", nm);
        if (ot != ONNX_TYPE_TENSOR) {
            snprintf(errbuf, errcap,
                     "E502: model input '%s' is not a tensor", nm);
            api->ReleaseTypeInfo(ti);
            goto done;
        }
        OCHK(api->CastTypeInfoToTensorInfo(ti, &tinfo),
             "E502: cannot read tensor info of model input '%s'", nm);
        OCHK(api->GetTensorElementType(tinfo, &et),
             "E502: cannot read element type of model input '%s'", nm);
        OCHK(api->GetDimensionsCount(tinfo, &ndim),
             "E502: cannot read shape of model input '%s'", nm);
        if (ndim > 8) {
            snprintf(errbuf, errcap,
                     "E502: model input '%s' has %zu dims (v1: <= 8)", nm,
                     ndim);
            api->ReleaseTypeInfo(ti);
            goto done;
        }
        OCHK(api->GetDimensions(tinfo, dims, ndim),
             "E502: cannot read shape of model input '%s'", nm);

        if (strcmp(kind, "text") == 0) {
            const char *tz;
            OrtValue *v = NULL;
            const char *one[1];
            int64_t sdim[1] = {1};
            if (misa_tag(av) != M_TAG_TEXT) {
                snprintf(errbuf, errcap,
                         "E502: param '%s': Text value required", pname);
                api->ReleaseTypeInfo(ti);
                goto done;
            }
            if (et != ONNX_TENSOR_ELEMENT_DATA_TYPE_STRING) {
                snprintf(errbuf, errcap,
                         "E502: param '%s': model input '%s' is not a "
                         "string tensor", pname, nm);
                api->ReleaseTypeInfo(ti);
                goto done;
            }
            tz = misa_text_z(av);
            one[0] = tz ? tz : "";
            /* NB: CreateTensorAsOrtValue takes an OrtAllocator, not a
             * memory info (ORT >= 1.17 signature). */
            OCHK(api->CreateTensorAsOrtValue(alloc, sdim, 1,
                    ONNX_TENSOR_ELEMENT_DATA_TYPE_STRING, &v),
                 "E502: string tensor creation failed");
            OCHK(api->FillStringTensor(v, one, 1),
                 "E502: string tensor fill failed");
            api->ReleaseTypeInfo(ti);
            in_vals[i] = v;
            continue;
        }
        /* numeric kinds need float tensors */
        {
            int is_vec = strcmp(kind, "vec") == 0;
            size_t count = 0;
            float *fdata = NULL;
            OrtValue *v = NULL;
            if (et != ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT) {
                snprintf(errbuf, errcap,
                         "E502: param '%s': model input '%s' is not a "
                         "float tensor (v1 supports float tensors only)",
                         pname, nm);
                api->ReleaseTypeInfo(ti);
                goto done;
            }
            if (!is_vec) {
                double dv;
                if (misa_tag(av) != M_TAG_FLOAT) {
                    snprintf(errbuf, errcap,
                             "E502: param '%s': Float value required",
                             pname);
                    api->ReleaseTypeInfo(ti);
                    goto done;
                }
                dv = misa_as_float(av);
                if (ndim == 0) {
                    dims[0] = 1;
                    ndim = 1;
                    count = 1;
                } else if (ndim == 1 && (dims[0] == 1 || dims[0] < 0)) {
                    if (dims[0] < 0) dims[0] = 1;
                    count = 1;
                } else {
                    snprintf(errbuf, errcap,
                             "E502: param '%s': model input '%s' has "
                             "unexpected shape (v1 supports scalar/1-D "
                             "inputs only)", pname, nm);
                    api->ReleaseTypeInfo(ti);
                    goto done;
                }
                fdata = (float *)misa_alloc(sizeof(float));
                fdata[0] = (float)dv;
            } else {
                int64_t k, len;
                if (misa_tag(av) != M_TAG_LIST) {
                    snprintf(errbuf, errcap,
                             "E502: param '%s': [Float] value required",
                             pname);
                    api->ReleaseTypeInfo(ti);
                    goto done;
                }
                if (ndim != 1) {
                    snprintf(errbuf, errcap,
                             "E502: param '%s': model input '%s' is not "
                             "1-D (v1 supports 1-D inputs only)", pname,
                             nm);
                    api->ReleaseTypeInfo(ti);
                    goto done;
                }
                len = misa_list_len(av);
                if (dims[0] >= 0 && dims[0] != len) {
                    snprintf(errbuf, errcap,
                             "E501: param '%s': model input '%s' expects "
                             "%lld values, got %lld", pname, nm,
                             (long long)dims[0], (long long)len);
                    api->ReleaseTypeInfo(ti);
                    goto done;
                }
                count = (size_t)len;
                dims[0] = len;
                fdata = (float *)misa_alloc(sizeof(float) *
                                           (count ? count : 1));
                for (k = 0; k < len; k++) {
                    MVal *e = misa_list_get(av, k);
                    if (misa_tag(e) != M_TAG_FLOAT) {
                        snprintf(errbuf, errcap,
                                 "E502: param '%s': [Float] value "
                                 "required", pname);
                        api->ReleaseTypeInfo(ti);
                        goto done;
                    }
                    fdata[k] = (float)misa_as_float(e);
                }
            }
            fbufs[nfbufs++] = fdata;
            OCHK(api->CreateTensorWithDataAsOrtValue(
                    meminfo, fdata, count * sizeof(float),
                    dims, ndim, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &v),
                 "E502: tensor creation failed");
            api->ReleaseTypeInfo(ti);
            in_vals[i] = v;
        }
    }
    /* run: first output only (mirrors M1) */
    {
        char *onm = NULL;
        OCHK(api->SessionGetOutputName(ort_sess, 0, alloc, &onm),
             "E502: cannot read model output");
        out_names = (char **)misa_alloc(sizeof(char *));
        out_names[0] = onm;
        {
            OrtStatus *rst = api->Run(
                ort_sess, NULL, (const char *const *)in_names,
                (const OrtValue *const *)in_vals, n_in,
                (const char *const *)out_names, 1, &out_val);
            if (rst) {
                snprintf(errbuf, errcap, "model inference failed: %s",
                         api->GetErrorMessage(rst));
                api->ReleaseStatus(rst);
                rc = PC_MODEL_ERR;
                goto done;
            }
        }
    }
    /* first output -> JSON in out->text (existing validation applies) */
    {
        OrtTypeInfo *ti = NULL;
        const OrtTensorTypeAndShapeInfo *tinfo = NULL;
        ONNXTensorElementDataType et = ONNX_TENSOR_ELEMENT_DATA_TYPE_UNDEFINED;
        enum ONNXType ot = ONNX_TYPE_UNKNOWN;
        size_t ndim = 0, k;
        int64_t dims[8];
        size_t count = 1;
        const char *ret_kind = "vec";
        const char *schema = meta->schema ? meta->schema : "";
        while (*schema == ' ') schema++;
        if (strncmp(schema, "[Float]", 7) == 0) ret_kind = "vec";
        else if (strncmp(schema, "Float", 5) == 0) ret_kind = "scalar";
        else if (strncmp(schema, "Text", 4) == 0) ret_kind = "text";
        else {
            snprintf(errbuf, errcap,
                     "E502: return type '%s' is not supported on-device "
                     "in v1 (Float, [Float], Text)", schema);
            goto done;
        }
        OCHK(api->SessionGetOutputTypeInfo(ort_sess, 0, &ti),
             "E502: cannot read model output type");
        OCHK(api->GetOnnxTypeFromTypeInfo(ti, &ot),
             "E502: cannot read model output type");
        if (ot != ONNX_TYPE_TENSOR) {
            snprintf(errbuf, errcap,
                     "E502: model output is not a tensor (v1)");
            api->ReleaseTypeInfo(ti);
            goto done;
        }
        OCHK(api->CastTypeInfoToTensorInfo(ti, &tinfo),
             "E502: cannot read model output info");
        OCHK(api->GetTensorElementType(tinfo, &et),
             "E502: cannot read model output element type");
        OCHK(api->GetDimensionsCount(tinfo, &ndim),
             "E502: cannot read model output shape");
        if (ndim > 8) {
            snprintf(errbuf, errcap,
                     "E502: model output has %zu dims (v1: <= 8)", ndim);
            api->ReleaseTypeInfo(ti);
            goto done;
        }
        OCHK(api->GetDimensions(tinfo, dims, ndim),
             "E502: cannot read model output shape");
        for (k = 0; k < ndim; k++)
            if (dims[k] > 0) count *= (size_t)dims[k];
        if (et == ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT) {
            float *f = NULL;
            OCHK(api->GetTensorMutableData(out_val, (void **)&f),
                 "E502: cannot read model output data");
            if (strcmp(ret_kind, "scalar") == 0) {
                char tmp[64];
                snprintf(tmp, sizeof tmp, "%.17g", count ? f[0] : 0.0);
                buf_puts(&jb, tmp);
            } else {
                buf_putn(&jb, "[", 1);
                for (k = 0; k < count; k++) {
                    char tmp[64];
                    if (k) buf_putn(&jb, ",", 1);
                    snprintf(tmp, sizeof tmp, "%.17g", f[k]);
                    buf_puts(&jb, tmp);
                }
                buf_putn(&jb, "]", 1);
            }
        } else if (et == ONNX_TENSOR_ELEMENT_DATA_TYPE_STRING) {
            size_t slen = 0, nstr = count, s;
            char *sbuf;
            size_t *offs;
            OCHK(api->GetStringTensorDataLength(out_val, &slen),
                 "E502: cannot read string output");
            if (nstr == 0) {
                buf_puts(&jb,
                         strcmp(ret_kind, "scalar") == 0 ? "\"\"" : "[]");
            } else {
                sbuf = (char *)misa_alloc(slen ? slen : 1);
                offs = (size_t *)misa_alloc(sizeof(size_t) * nstr);
                OCHK(api->GetStringTensorContent(out_val, sbuf, slen,
                                                offs, nstr),
                     "E502: cannot read string output");
                if (strcmp(ret_kind, "scalar") == 0) {
                    size_t e0 = nstr > 1 ? offs[1] : slen;
                    buf_putn(&jb, "\"", 1);
                    buf_put_json_str(&jb, sbuf + offs[0], e0 - offs[0]);
                    buf_putn(&jb, "\"", 1);
                } else {
                    buf_putn(&jb, "[", 1);
                    for (s = 0; s < nstr; s++) {
                        size_t e0 = s + 1 < nstr ? offs[s + 1] : slen;
                        if (s) buf_putn(&jb, ",", 1);
                        buf_putn(&jb, "\"", 1);
                        buf_put_json_str(&jb, sbuf + offs[s],
                                         e0 - offs[s]);
                        buf_putn(&jb, "\"", 1);
                    }
                    buf_putn(&jb, "]", 1);
                }
            }
        } else {
            snprintf(errbuf, errcap,
                     "E502: model output is not float/string (v1)");
            api->ReleaseTypeInfo(ti);
            goto done;
        }
        api->ReleaseTypeInfo(ti);
    }
    out->text = jb.buf ? jb.buf : (char *)misa_alloc(1);
    if (!jb.buf) out->text[0] = '\0';
    jb.buf = NULL;  /* ownership transferred */
    rc = PC_OK;

done:
    if (rc == PC_ECODE) {
        /* Every E50x site prefixes its message with the code; extract
         * it for the raise, then strip it so misa_raise doesn't print
         * it twice ("E501: E501: ..."). */
        if (sscanf(errbuf, "E%3[0-9]: ", out->ecode + 1) == 1) {
            out->ecode[0] = 'E';
            out->ecode[4] = '\0';
            memmove(errbuf, errbuf + 6, strlen(errbuf + 6) + 1);
        } else if (!out->ecode[0]) {
            memcpy(out->ecode, "E502", 5);
        }
    }
    if (out_val) api->ReleaseValue(out_val);
    if (in_vals) {
        for (i = 0; i < n_in; i++)
            if (in_vals[i]) api->ReleaseValue(in_vals[i]);
    }
    if (in_names) {
        for (i = 0; i < n_in; i++)
            if (in_names[i]) alloc->Free(alloc, in_names[i]);
    }
    if (out_names && out_names[0]) alloc->Free(alloc, out_names[0]);
    if (meminfo) api->ReleaseMemoryInfo(meminfo);
    if (jb.buf) misa_free(jb.buf);
    return rc;
#undef OCHK
}

#else
/* No ORT linked: on-device .onnx calls fail with E503. */
static int ort_infer(const MModelMeta *meta, MVal **args, int nargs,
                     Completion *out, char *errbuf, size_t errcap) {
    (void)meta;
    (void)args;
    (void)nargs;
    (void)out;
    snprintf(out->ecode, sizeof out->ecode, "E503");
    snprintf(errbuf, errcap,
             "ONNX Runtime is not linked into this binary "
             "(rebuild with the vendored ORT; see spec/ONDEVICE.md)");
    return PC_ECODE;
}
#endif

static int provider_complete_ondevice(const MModelMeta *meta, MVal **args,
                                      int nargs, Completion *out,
                                      char *errbuf, size_t errcap) {
    const char *rt;
    const char *mfile = meta->model_file ? meta->model_file : "";
    if (!*mfile) {
        snprintf(errbuf, errcap,
                 "on-device provider needs `model: file(\"...\")`");
        return PC_CONFIG_ERR;
    }
    rt = ondevice_runtime(mfile);
    if (strcmp(rt, "coreml") == 0) {
        snprintf(out->ecode, sizeof out->ecode, "E503");
        snprintf(errbuf, errcap,
                 "CoreML models (.mlpackage/.mlmodelc) need Apple "
                 "hardware (milestone M3)");
        return PC_ECODE;
    }
    if (strcmp(rt, "tflite") == 0) {
        snprintf(out->ecode, sizeof out->ecode, "E503");
        snprintf(errbuf, errcap,
                 "TFLite models (.tflite) need Android (milestone M4)");
        return PC_ECODE;
    }
    if (strcmp(rt, "unknown") == 0) {
        snprintf(errbuf, errcap,
                 "unknown on-device model extension for '%s' "
                 "(want .onnx, .mlpackage, .mlmodelc, .tflite)",
                 mfile);
        return PC_CONFIG_ERR;
    }
    return ort_infer(meta, args, nargs, out, errbuf, errcap);
}

/* Read a whole file into a Buf. Returns 1 on success. */
static int read_file_buf(const char *path, Buf *b) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    char tmp[8192];
    size_t n;
    while ((n = fread(tmp, 1, sizeof tmp, f)) > 0) buf_putn(b, tmp, n);
    int ok = !ferror(f);
    fclose(f);
    return ok;
}

static const char *guess_mime(const char *path) {
    const char *dot = strrchr(path, '.');
    if (!dot) return "application/octet-stream";
    if (strcmp(dot, ".png") == 0) return "image/png";
    if (strcmp(dot, ".jpg") == 0 || strcmp(dot, ".jpeg") == 0)
        return "image/jpeg";
    if (strcmp(dot, ".gif") == 0) return "image/gif";
    if (strcmp(dot, ".webp") == 0) return "image/webp";
    return "application/octet-stream";
}

/* Image part: path + mime, collected during prompt rendering. */
typedef struct {
    char *path;
    const char *mime;
} Image;

#define MAX_IMAGES 8

static int provider_complete(const MModelMeta *meta, const char *prompt,
                             Image *images, int nimages, double timeout_s,
                             Completion *out, char *errbuf, size_t errcap,
                             MVal **args, int nargs) {
    memset(out, 0, sizeof *out);
    if (strcmp(meta->provider_kind, "ondevice") == 0) {
        /* Local inference: no prompt/images needed; the declared
         * params are bound positionally to the model inputs. */
        return provider_complete_ondevice(meta, args, nargs, out,
                                          errbuf, errcap);
    }
    if (strcmp(meta->provider_kind, "mock") == 0) {
        const char *mp = misa_platform_getenv("MISA_MOCK_FILE");
        if (!mp || !*mp) {
            snprintf(errbuf, errcap,
                     "mock provider needs MISA_MOCK_FILE set");
            return PC_CONFIG_ERR;
        }
        Buf b = {0};
        if (!read_file_buf(mp, &b)) {
            snprintf(errbuf, errcap,
                     "mock provider cannot read MISA_MOCK_FILE");
            return PC_CONFIG_ERR;
        }
        char je[256];
        JVal *root = json_parse(b.buf ? b.buf : "", b.len, je, sizeof je);
        if (!root || root->tag != J_OBJ) {
            snprintf(errbuf, errcap, "mock file is not a JSON object: %s",
                     je);
            return PC_CONFIG_ERR;
        }
        JVal *script = jobj_get(root, meta->alias);
        if (!script) {
            snprintf(errbuf, errcap,
                     "mock provider has no script for alias '%s'",
                     meta->alias);
            return PC_CONFIG_ERR;
        }
        JVal *e = jobj_get(script, "_error");
        if (e && e->tag == J_STR) {
            snprintf(errbuf, errcap, "mock error: %s", e->u.str.data);
            return PC_MODEL_ERR;
        }
        JVal *resp = script;
        JVal *u = jobj_get(script, "_usage");
        if (u) {
            JVal *it = jobj_get(u, "input_tokens");
            JVal *ot = jobj_get(u, "output_tokens");
            if (it && it->tag == J_INT) out->in_tok = it->u.i;
            if (ot && ot->tag == J_INT) out->out_tok = ot->u.i;
            resp = jobj_get(script, "response");
            if (!resp) resp = script;
        } else {
            out->in_tok = 120;
            out->out_tok = 60;
        }
        /* re-serialize the response JSON as the model text */
        Buf tb = {0};
        /* minimal re-serialization: reuse raw text slice is complex;
         * instead serialize via a small writer */
        {
            /* forward declaration needed */
            extern void json_write_buf(Buf *, JVal *);
            json_write_buf(&tb, resp);
        }
        out->text = tb.buf ? tb.buf : (char *)misa_alloc(1);
        return PC_OK;
    }

/* Network providers need a platform HTTP transport: libcurl on host
 * (MISA_USE_CURL), emscripten_fetch on web (MISA_USE_FETCH, M5), or
 * JNI -> HttpURLConnection on Android (MISA_USE_JNI_HTTP, M6).
 * The request-body construction below is fully portable; only the
 * transport call at the bottom differs per platform. Without a
 * transport, network model calls degrade to catchable E401. */
#if defined(MISA_USE_CURL) || defined(MISA_USE_FETCH) || \
    defined(MISA_USE_JNI_HTTP)
    /* NOTE: misa_platform_getenv may return a static buffer that is
     * overwritten by the next call (web P3). Snapshot the key NOW,
     * before the base-URL lookup below reuses that buffer. */
    char keybuf[1024];
    {
        const char *k0 =
            misa_platform_getenv(meta->key_env ? meta->key_env : "");
        snprintf(keybuf, sizeof keybuf, "%s", k0 ? k0 : "");
    }
    const char *key = keybuf;
    if ((!key || !*key) && strcmp(meta->provider_kind, "mock") != 0) {
        snprintf(errbuf, errcap,
                 "provider '%s' needs a key (set %s)",
                 meta->provider_kind, meta->key_env);
        return PC_CONFIG_ERR;
    }

    Buf body = {0};
    char url[1024];
    const char *auth_hdr = NULL;
    char auth_buf[2048];
    if (strcmp(meta->provider_kind, "openai") == 0) {
        const char *base = misa_platform_getenv(
            meta->base_url_env && *meta->base_url_env ? meta->base_url_env
                                                     : "MISA_OPENAI_BASE_URL");
        if (!base || !*base) base = "https://api.openai.com/v1";
        /* strip trailing slash */
        char bs[512];
        snprintf(bs, sizeof bs, "%s", base);
        size_t bl = strlen(bs);
        while (bl > 0 && bs[bl - 1] == '/') bs[--bl] = '\0';
        snprintf(url, sizeof url, "%s/chat/completions", bs);
        snprintf(auth_buf, sizeof auth_buf, "Authorization: Bearer %s", key);
        auth_hdr = auth_buf;
        buf_puts(&body, "{\"model\":");
        buf_put_json_str(&body, meta->model_id, strlen(meta->model_id));
        buf_puts(&body, ",\"messages\":[{\"role\":\"user\",\"content\":");
        if (nimages == 0) {
            buf_put_json_str(&body, prompt, strlen(prompt));
        } else {
            buf_puts(&body, "[{\"type\":\"text\",\"text\":");
            buf_put_json_str(&body, prompt, strlen(prompt));
            buf_puts(&body, "}");
            for (int i = 0; i < nimages; i++) {
                Buf fb = {0};
                if (!read_file_buf(images[i].path, &fb)) {
                    snprintf(errbuf, errcap,
                             "cannot read image '%s'", images[i].path);
                    return PC_MODEL_ERR;
                }
                char *b64 = base64_encode((const unsigned char *)fb.buf,
                                          fb.len);
                buf_puts(&body,
                         ",{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:");
                buf_puts(&body, images[i].mime);
                buf_puts(&body, ";base64,");
                buf_puts(&body, b64);
                buf_puts(&body, "\"}}");
            }
            buf_puts(&body, "]");
        }
        buf_puts(&body,
                 "}],\"response_format\":{\"type\":\"json_object\"},"
                 "\"temperature\":0}");
    } else if (strcmp(meta->provider_kind, "gemini") == 0) {
        const char *base = misa_platform_getenv(
            meta->base_url_env && *meta->base_url_env ? meta->base_url_env
                                                     : "MISA_GEMINI_BASE_URL");
        if (!base || !*base)
            base = "https://generativelanguage.googleapis.com";
        char bs[512];
        snprintf(bs, sizeof bs, "%s", base);
        size_t bl = strlen(bs);
        while (bl > 0 && bs[bl - 1] == '/') bs[--bl] = '\0';
        /* URL-encode the key minimally */
        char kq[1024];
        size_t ko = 0;
        for (const char *k = key; *k && ko + 4 < sizeof kq; k++) {
            if (isalnum((unsigned char)*k) || *k == '-' || *k == '_' ||
                *k == '.' || *k == '~')
                kq[ko++] = *k;
            else {
                snprintf(kq + ko, sizeof(kq) - ko, "%%%02X",
                         (unsigned char)*k);
                ko += 3;
            }
        }
        kq[ko] = '\0';
        snprintf(url, sizeof url,
                 "%s/v1beta/models/%s:generateContent?key=%s",
                 bs, meta->model_id, kq);
        buf_puts(&body, "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":");
        {
            /* gemini appends "\nRespond with JSON only." */
            size_t pl = strlen(prompt);
            char *p2 = (char *)misa_alloc(pl + 32);
            memcpy(p2, prompt, pl);
            memcpy(p2 + pl, "\nRespond with JSON only.", 23);
            buf_put_json_str(&body, p2, pl + 23);
        }
        for (int i = 0; i < nimages; i++) {
            Buf fb = {0};
            if (!read_file_buf(images[i].path, &fb)) {
                snprintf(errbuf, errcap,
                         "cannot read image '%s'", images[i].path);
                return PC_MODEL_ERR;
            }
            char *b64 = base64_encode((const unsigned char *)fb.buf,
                                      fb.len);
            buf_puts(&body, ",{\"inlineData\":{\"mimeType\":");
            buf_put_json_str(&body, images[i].mime,
                             strlen(images[i].mime));
            buf_puts(&body, ",\"data\":");
            buf_put_json_str(&body, b64, strlen(b64));
            buf_puts(&body, "}}");
        }
        buf_puts(&body,
                 "]} }],\"generationConfig\":{\"responseMimeType\":"
                 "\"application/json\",\"temperature\":0}}");
    } else {
        snprintf(errbuf, errcap, "unknown provider kind '%s'",
                 meta->provider_kind);
        return PC_CONFIG_ERR;
    }

    /* Always send Content-Type: without it, OpenAI-compatible
     * gateways don't parse the JSON body and the model field arrives
     * empty (seen live: hcnsec 403 "no access to model "). */
    const char *hdrs[2];
    int nhdrs = 0;
    hdrs[nhdrs++] = "Content-Type: application/json";
    if (auth_hdr) hdrs[nhdrs++] = auth_hdr;
    /* Phase 4 (M2): the transport is the platform layer. The curl
     * backend lives in rt/platform_linux.c when built with
     * -DMISA_USE_CURL; the JNI HttpURLConnection backend lives in
     * rt/platform_android.c (-DMISA_USE_JNI_HTTP); other targets
     * plug in their own. */
    MisaHttpResp presp;
    memset(&presp, 0, sizeof presp);
    if (!misa_platform_http_post(url, hdrs, nhdrs,
                                 body.buf ? body.buf : "",
                                 timeout_s, &presp)) {
        snprintf(errbuf, errcap, "provider network error: %s", presp.err);
        misa_platform_http_free(&presp);
        return PC_MODEL_ERR;
    }
    if (presp.status == 401 || presp.status == 403) {
        snprintf(errbuf, errcap,
                 "provider rejected credentials (HTTP %ld)", presp.status);
        misa_platform_http_free(&presp);
        return PC_CONFIG_ERR;
    }
    if (presp.status < 200 || presp.status >= 300) {
        snprintf(errbuf, errcap, "provider HTTP %ld: %.200s", presp.status,
                 presp.body ? presp.body : "");
        misa_platform_http_free(&presp);
        return PC_MODEL_ERR;
    }
    char je[256];
    JVal *root = json_parse(presp.body ? presp.body : "",
                            presp.len, je, sizeof je);
    if (!root) {
        snprintf(errbuf, errcap, "malformed provider response: %s", je);
        misa_platform_http_free(&presp);
        return PC_MODEL_ERR;
    }
    const char *text = "";
    if (strcmp(meta->provider_kind, "openai") == 0) {
        JVal *ch0 = jarr_get(jobj_get(root, "choices"), 0);
        JVal *c = jobj_get(jobj_get(ch0, "message"), "content");
        JVal *u = jobj_get(root, "usage");
        if (!c || c->tag != J_STR) {
            snprintf(errbuf, errcap, "malformed provider response");
            return PC_MODEL_ERR;
        }
        text = c->u.str.data;
        if (u) {
            JVal *it = jobj_get(u, "prompt_tokens");
            JVal *ot = jobj_get(u, "completion_tokens");
            if (it && it->tag == J_INT) out->in_tok = it->u.i;
            if (ot && ot->tag == J_INT) out->out_tok = ot->u.i;
        }
    } else {
        JVal *c0 = jarr_get(jobj_get(root, "candidates"), 0);
        JVal *t = jobj_get(jarr_get(jobj_get(jobj_get(c0, "content"), "parts"), 0),
                           "text");
        JVal *u = jobj_get(root, "usageMetadata");
        if (!t || t->tag != J_STR) {
            snprintf(errbuf, errcap, "malformed provider response");
            return PC_MODEL_ERR;
        }
        text = t->u.str.data;
        if (u) {
            JVal *it = jobj_get(u, "promptTokenCount");
            JVal *ot = jobj_get(u, "candidatesTokenCount");
            if (it && it->tag == J_INT) out->in_tok = it->u.i;
            if (ot && ot->tag == J_INT) out->out_tok = ot->u.i;
        }
    }
    {
        size_t tl = strlen(text);
        out->text = (char *)misa_alloc(tl + 1);
        memcpy(out->text, text, tl + 1);
    }
    misa_platform_http_free(&presp);
    return PC_OK;
#else
    snprintf(errbuf, errcap,
             "model calls need a network transport "
             "(rebuild the runtime with one)");
    return PC_CONFIG_ERR;
#endif
}

/* minimal JSON re-serializer (for mock provider responses) */
void json_write_buf(Buf *b, JVal *v) {
    char tmp[64];
    switch (v->tag) {
    case J_NULL: buf_puts(b, "null"); break;
    case J_BOOL: buf_puts(b, v->u.b ? "true" : "false"); break;
    case J_INT:
        snprintf(tmp, sizeof tmp, "%lld", (long long)v->u.i);
        buf_puts(b, tmp);
        break;
    case J_FLOAT:
        snprintf(tmp, sizeof tmp, "%.17g", v->u.f);
        buf_puts(b, tmp);
        break;
    case J_STR: buf_put_json_str(b, v->u.str.data, v->u.str.len); break;
    case J_ARR:
        buf_putn(b, "[", 1);
        for (size_t i = 0; i < v->u.arr.len; i++) {
            if (i) buf_putn(b, ",", 1);
            json_write_buf(b, v->u.arr.items[i]);
        }
        buf_putn(b, "]", 1);
        break;
    case J_OBJ:
        buf_putn(b, "{", 1);
        for (size_t i = 0; i < v->u.obj.len; i++) {
            if (i) buf_putn(b, ",", 1);
            buf_put_json_str(b, v->u.obj.keys[i],
                             strlen(v->u.obj.keys[i]));
            buf_putn(b, ":", 1);
            json_write_buf(b, v->u.obj.vals[i]);
        }
        buf_putn(b, "}", 1);
        break;
    }
}

/* ------------------------------------------------------------------ */
/* misa_model_call                                                     */
/* ------------------------------------------------------------------ */

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

MVal *misa_model_call(const MModelMeta *meta, MVal **args, int nargs,
                      int line, int col) {
    char errbuf[512];
    /* --- render prompt --- */
    Buf prompt = {0};
    Image images[MAX_IMAGES];
    int nimages = 0;
    for (int i = 0; i < meta->nprompt; i++) {
        const MPromptPart *pp = &meta->prompt[i];
        if (pp->is_lit) {
            buf_puts(&prompt, pp->text);
        } else {
            int pi = pp->param_idx;
            MVal *av = (pi >= 0 && pi < nargs) ? args[pi] : misa_null();
            if (misa_tag(av) == M_TAG_MEDIA &&
                misa_media_kind(av) == M_MEDIA_PHOTO &&
                meta->params[pi].kind &&
                strcmp(meta->params[pi].kind, "Photo") == 0) {
                FILE *f = fopen(misa_media_path(av), "rb");
                if (f) {
                    fclose(f);
                    if (nimages < MAX_IMAGES) {
                        images[nimages].path = (char *)misa_media_path(av);
                        images[nimages].mime =
                            guess_mime(misa_media_path(av));
                        nimages++;
                    }
                    continue;
                }
            }
            MVal *t = misa_render(av);
            const char *tz = misa_text_z(t);
            buf_putn(&prompt, tz, misa_text_len(t));
        }
    }

    /* --- schema --- */
    SParser sp;
    sp.p = meta->schema;
    sp.err[0] = '\0';
    Schema *schema = sparse_type(&sp);
    if (!schema) {
        misa_raise_fmt("E400", line, col,
                       "bad model schema for '%s'", meta->alias);
        return NULL;
    }

    double timeout_s = 120.0;
    if (meta->has_latency && meta->budget_latency_ms > 0) {
        double t = meta->budget_latency_ms / 1000.0;
        if (t < timeout_s) timeout_s = t;
    }

    /* --- 3 attempts --- */
    int64_t in_tok = 0, out_tok = 0;
    double t0 = now_ms();
    MVal *value = NULL;
    VErrs verrs;
    /* 64KB prompt buffer: heap-allocated, not stack — Emscripten's
     * default stack (64KB) cannot hold this as a local; the stack
     * overflow caused a wasm OOB segfault on web (M3). Explicitly
     * released via misa_free() on every exit path below (M5): on the
     * malloc fallback a missing free is a real 64KB leak per model
     * call, and raw free() on Boehm memory corrupts the collector —
     * misa_free() picks GC_free/free correctly. */
    char *cur_prompt = (char *)misa_alloc(65536);
    snprintf(cur_prompt, 65536, "%s",
             prompt.buf ? prompt.buf : "");
    for (int attempt = 0; attempt < 3; attempt++) {
        Completion comp;
        int rc = provider_complete(meta, cur_prompt, images, nimages,
                                   timeout_s, &comp,
                                   errbuf, sizeof errbuf, args, nargs);
        if (rc == PC_ECODE) {
            /* on-device E501/E502/E503: exact code in comp.ecode */
            misa_raise(comp.ecode[0] ? comp.ecode : "E502", errbuf,
                       line, col);
            misa_free(cur_prompt);
            return NULL;
        }
        if (rc == PC_CONFIG_ERR) {
            misa_raise("E401", errbuf, line, col);
            misa_free(cur_prompt);
            return NULL;
        }
        if (rc == PC_MODEL_ERR) {
            misa_raise("E400", errbuf, line, col);
            misa_free(cur_prompt);
            return NULL;
        }
        in_tok += comp.in_tok;
        out_tok += comp.out_tok;
        memset(&verrs, 0, sizeof verrs);
        char je[256];
        JVal *data = json_parse(comp.text, strlen(comp.text),
                                je, sizeof je);
        if (!data) {
            verr(&verrs, "$", "response was not valid JSON");
        } else {
            value = validate_against(data, schema, "$", &verrs);
        }
        if (verrs.n == 0 && value) break;
        value = NULL;
        /* repair prompt */
        {
            Buf rp = {0};
            buf_puts(&rp, prompt.buf ? prompt.buf : "");
            buf_puts(&rp,
                     "\n\nYour previous response failed validation:\n");
            for (int i = 0; i < verrs.n; i++) {
                buf_puts(&rp, "- ");
                buf_puts(&rp, verrs.msgs[i]);
                buf_putn(&rp, "\n", 1);
            }
            buf_puts(&rp, "Respond with structured data only.");
            /* NOTE: sizeof(cur_prompt) is the pointer size (8), NOT the
             * buffer — the repair prompt must use the real capacity. */
            snprintf(cur_prompt, 65536, "%s",
                     rp.buf ? rp.buf : "");
        }
    }
    if (!value) {
        char msg[1024];
        snprintf(msg, sizeof msg,
                 "model '%s' returned invalid data after 3 attempts",
                 meta->alias);
        misa_raise("E400", msg, line, col);
        misa_free(cur_prompt);
        return NULL;
    }

    /* --- budgets --- */
    int64_t total = in_tok + out_tok;
    double latency = now_ms() - t0;
    if (meta->has_tokens && total > meta->budget_tokens) {
        misa_raise_fmt("E500", line, col,
                       "token budget exceeded: used %lld, budget %lld",
                       (long long)total, (long long)meta->budget_tokens);
        misa_free(cur_prompt);
        return NULL;
    }
    if (meta->has_latency && latency > (double)meta->budget_latency_ms) {
        misa_raise_fmt("E500", line, col,
                       "latency budget exceeded: took %.0fms, budget %lldms",
                       latency, (long long)meta->budget_latency_ms);
        misa_free(cur_prompt);
        return NULL;
    }
    if (meta->has_cost) {
        double cost = estimate_cost_usd(meta->model_id, in_tok, out_tok);
        if (cost > meta->budget_cost_usd) {
            misa_raise_fmt("E500", line, col,
                           "cost budget exceeded: ~$%.4f, budget $%.4f (estimated)",
                           cost, meta->budget_cost_usd);
            misa_free(cur_prompt);
            return NULL;
        }
    }
    misa_free(cur_prompt);
    return value;
}

/* ------------------------------------------------------------------ */
/* emitter-generated metadata text block                              */
/*                                                                    */
/*   MISA-MODEL v1                                                    */
/*   alias: <alias>                                                   */
/*   kind: <openai|gemini|mock>                                       */
/*   model: <model_id>                                                */
/*   key_env: <env>                                                   */
/*   base_env: <env or empty>                                         */
/*   tokens: <int, -1 = none>                                         */
/*   latency_ms: <int, -1 = none>                                     */
/*   cost_usd: <float, -1 = none>                                     */
/*   schema: <schema string>                                          */
/*   model_file: <path>  (on-device only)                             */
/*   param: <name>\t<kind>\t<type>                                    */
/*        (kind empty unless Photo; type is the declared type string)  */
/*   prompt: L\t<escaped literal>                                     */
/*   prompt: V\t<param_idx>                                            */
/*                                                                    */
/* Literal escaping: \\ -> backslash, \n -> newline, \t -> tab.       */
/* ------------------------------------------------------------------ */

static char *meta_unescape(const char *s) {
    size_t n = strlen(s);
    char *r = (char *)misa_alloc(n + 1);
    size_t o = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] == '\\' && i + 1 < n) {
            char e = s[++i];
            if (e == 'n') r[o++] = '\n';
            else if (e == 't') r[o++] = '\t';
            else if (e == '\\') r[o++] = '\\';
            else { r[o++] = '\\'; r[o++] = e; }
        } else {
            r[o++] = s[i];
        }
    }
    r[o] = '\0';
    return r;
}

MVal *misa_model_call_meta(const char *meta_text, MVal **args, int nargs,
                           int line, int col) {
    MModelMeta meta;
    memset(&meta, 0, sizeof meta);
    MModelParam *params = NULL;
    int nparams = 0, pcap = 0;
    MPromptPart *prompt = NULL;
    int nprompt = 0, prcap = 0;

    char *copy = (char *)misa_alloc(strlen(meta_text) + 1);
    strcpy(copy, meta_text);
    char *save = NULL;
    for (char *ln = strtok_r(copy, "\n", &save); ln;
         ln = strtok_r(NULL, "\n", &save)) {
        if (strncmp(ln, "alias: ", 7) == 0) meta.alias = ln + 7;
        else if (strncmp(ln, "kind: ", 6) == 0) meta.provider_kind = ln + 6;
        else if (strncmp(ln, "model: ", 7) == 0) meta.model_id = ln + 7;
        else if (strncmp(ln, "key_env: ", 9) == 0) meta.key_env = ln + 9;
        else if (strncmp(ln, "base_env: ", 10) == 0)
            meta.base_url_env = ln + 10;
        else if (strncmp(ln, "tokens: ", 8) == 0) {
            long long v = atoll(ln + 8);
            if (v >= 0) {
                meta.has_tokens = 1;
                meta.budget_tokens = (int64_t)v;
            }
        } else if (strncmp(ln, "latency_ms: ", 12) == 0) {
            long long v = atoll(ln + 12);
            if (v >= 0) {
                meta.has_latency = 1;
                meta.budget_latency_ms = (int64_t)v;
            }
        } else if (strncmp(ln, "cost_usd: ", 10) == 0) {
            double v = atof(ln + 10);
            if (v >= 0) {
                meta.has_cost = 1;
                meta.budget_cost_usd = v;
            }
        } else if (strncmp(ln, "schema: ", 8) == 0) {
            meta.schema = ln + 8;
        } else if (strncmp(ln, "model_file: ", 12) == 0) {
            meta.model_file = ln + 12;
        } else if (strncmp(ln, "param: ", 7) == 0) {
            char *tab = strchr(ln + 7, '\t');
            char *tab2;
            if (nparams + 1 > pcap) {
                pcap = pcap ? pcap * 2 : 8;
                MModelParam *np = (MModelParam *)misa_alloc(
                    sizeof(MModelParam) * (size_t)pcap);
                if (params)
                    memcpy(np, params,
                           sizeof(MModelParam) * (size_t)nparams);
                params = np;
            }
            params[nparams].name = ln + 7;
            params[nparams].kind = "";
            params[nparams].type = "";
            if (tab) {
                *tab = '\0';
                params[nparams].kind = tab + 1;
                /* v1 meta blocks append the declared type as a third
                 * field: param: <name>\t<kind>\t<type> */
                tab2 = strchr(tab + 1, '\t');
                if (tab2) {
                    *tab2 = '\0';
                    params[nparams].type = tab2 + 1;
                }
            }
            nparams++;
        } else if (strncmp(ln, "prompt: ", 8) == 0) {
            if (nprompt + 1 > prcap) {
                prcap = prcap ? prcap * 2 : 16;
                MPromptPart *np = (MPromptPart *)misa_alloc(
                    sizeof(MPromptPart) * (size_t)prcap);
                if (prompt)
                    memcpy(np, prompt,
                           sizeof(MPromptPart) * (size_t)nprompt);
                prompt = np;
            }
            const char *body = ln + 8;
            if (body[0] == 'L' && body[1] == '\t') {
                prompt[nprompt].is_lit = 1;
                prompt[nprompt].text = meta_unescape(body + 2);
                prompt[nprompt].param_idx = -1;
            } else if (body[0] == 'V' && body[1] == '\t') {
                prompt[nprompt].is_lit = 0;
                prompt[nprompt].text = "";
                prompt[nprompt].param_idx = atoi(body + 2);
            }
            nprompt++;
        }
    }
    meta.nparams = nparams;
    meta.params = params;
    meta.nprompt = nprompt;
    meta.prompt = prompt;
    if (!meta.alias || !meta.provider_kind || !meta.model_id ||
        !meta.schema) {
        misa_raise("E400", "bad model metadata", line, col);
        return NULL;
    }
    if (strcmp(meta.provider_kind, "ondevice") == 0 && !meta.model_file) {
        misa_raise("E400", "bad model metadata: on-device model needs "
                           "model_file", line, col);
        return NULL;
    }
    return misa_model_call(&meta, args, nargs, line, col);
}
