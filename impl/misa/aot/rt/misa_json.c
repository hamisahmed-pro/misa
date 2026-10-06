/* Phase 5 (stdlib2): JSON parse/stringify for the Misa AOT runtime.
 *
 * Byte-parity target: Python's json module as used by the interpreter:
 *   json.loads(s)
 *   json.dumps(v, separators=(",", ":"), ensure_ascii=False,
 *              allow_nan=False)
 *
 * Numbers: Int when there is no fraction/exponent and the value fits
 * int64, else Float. Floats print byte-identically to Python str(float)
 * via misa_fmt_float. Strings escape exactly like CPython's ensure_ascii=False:
 * '"', '\\', \b \f \n \r \t, other C0 controls as \u00xx; all other
 * bytes (including UTF-8) pass through raw.
 *
 * Uses only the public misa_rt.h API (MVal is opaque here).
 */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "misa_rt.h"

/* ---------------- growable buffer ---------------- */

typedef struct {
    char *buf;
    size_t len, cap;
} JBuf;

static void jb_reserve(JBuf *b, size_t n) {
    if (b->len + n <= b->cap)
        return;
    size_t cap = b->cap ? b->cap : 64;
    while (cap < b->len + n)
        cap *= 2;
    char *nb = (char *)misa_alloc(cap);
    if (b->buf)
        memcpy(nb, b->buf, b->len);
    b->buf = nb;
    b->cap = cap;
}

static void jb_putc(JBuf *b, char c) {
    jb_reserve(b, 1);
    b->buf[b->len++] = c;
}

static void jb_putn(JBuf *b, const char *s, size_t n) {
    jb_reserve(b, n);
    memcpy(b->buf + b->len, s, n);
    b->len += n;
}

static void jb_puts(JBuf *b, const char *s) {
    jb_putn(b, s, strlen(s));
}

/* ---------------- parser ---------------- */

typedef struct {
    const char *p;
    const char *end;
    const char *err;
} JP;

static void jp_ws(JP *j) {
    while (j->p < j->end && (*j->p == ' ' || *j->p == '\t' ||
                             *j->p == '\n' || *j->p == '\r'))
        j->p++;
}

static int jp_hex4(JP *j, unsigned *out) {
    if ((size_t)(j->end - j->p) < 4)
        return 0;
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        char c = j->p[i];
        v <<= 4;
        if (c >= '0' && c <= '9')
            v |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f')
            v |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F')
            v |= (unsigned)(c - 'A' + 10);
        else
            return 0;
    }
    j->p += 4;
    *out = v;
    return 1;
}

static int utf8_enc(unsigned cp, char *out) {
    if (cp < 0x80) {
        out[0] = (char)cp;
        return 1;
    }
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

static MVal *jp_value(JP *j);

static MVal *jp_string(JP *j) {
    /* j->p at opening quote */
    j->p++;
    JBuf b = {0};
    while (j->p < j->end) {
        char c = *j->p;
        if (c == '"') {
            j->p++;
            return misa_text(b.buf ? b.buf : "", b.len);
        }
        if (c == '\\') {
            j->p++;
            if (j->p >= j->end) {
                j->err = "bad escape at end of string";
                return NULL;
            }
            char e = *j->p++;
            switch (e) {
            case '"': jb_putc(&b, '"'); break;
            case '\\': jb_putc(&b, '\\'); break;
            case '/': jb_putc(&b, '/'); break;
            case 'b': jb_putc(&b, '\b'); break;
            case 'f': jb_putc(&b, '\f'); break;
            case 'n': jb_putc(&b, '\n'); break;
            case 'r': jb_putc(&b, '\r'); break;
            case 't': jb_putc(&b, '\t'); break;
            case 'u': {
                unsigned cp;
                if (!jp_hex4(j, &cp)) {
                    j->err = "bad \\u escape";
                    return NULL;
                }
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    if ((size_t)(j->end - j->p) < 6 || j->p[0] != '\\' ||
                        j->p[1] != 'u') {
                        j->err = "lone surrogate";
                        return NULL;
                    }
                    j->p += 2;
                    unsigned lo;
                    if (!jp_hex4(j, &lo) || lo < 0xDC00 ||
                        lo > 0xDFFF) {
                        j->err = "lone surrogate";
                        return NULL;
                    }
                    cp = 0x10000 + ((cp - 0xD800) << 10) +
                         (lo - 0xDC00);
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    j->err = "lone surrogate";
                    return NULL;
                }
                char tmp[4];
                jb_putn(&b, tmp, (size_t)utf8_enc(cp, tmp));
                break;
            }
            default:
                j->err = "bad escape";
                return NULL;
            }
            continue;
        }
        if ((unsigned char)c < 0x20) {
            j->err = "unescaped control character in string";
            return NULL;
        }
        jb_putc(&b, c);
        j->p++;
    }
    j->err = "unterminated string";
    return NULL;
}

static MVal *jp_number(JP *j) {
    const char *start = j->p;
    if (j->p < j->end && *j->p == '-')
        j->p++;
    if (j->p >= j->end) {
        j->err = "bad number";
        return NULL;
    }
    if (*j->p == '0') {
        j->p++;
    } else if (*j->p >= '1' && *j->p <= '9') {
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    } else {
        j->err = "bad number";
        return NULL;
    }
    int is_float = 0;
    if (j->p < j->end && *j->p == '.') {
        is_float = 1;
        j->p++;
        if (j->p >= j->end || *j->p < '0' || *j->p > '9') {
            j->err = "bad number";
            return NULL;
        }
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    }
    if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
        is_float = 1;
        j->p++;
        if (j->p < j->end && (*j->p == '+' || *j->p == '-'))
            j->p++;
        if (j->p >= j->end || *j->p < '0' || *j->p > '9') {
            j->err = "bad number";
            return NULL;
        }
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    }
    size_t n = (size_t)(j->p - start);
    if (!is_float) {
        if (n >= 32) {
            /* M-6: STDLIB.md §3e — out-of-int64 integers become Float. */
            char tmp[64];
            size_t m = n < sizeof tmp - 1 ? n : sizeof tmp - 1;
            memcpy(tmp, start, m);
            tmp[m] = '\0';
            return misa_float(strtod(tmp, NULL));
        }
        char tmp[32];
        memcpy(tmp, start, n);
        tmp[n] = '\0';
        errno = 0;
        char *e2;
        long long v = strtoll(tmp, &e2, 10);
        if (errno == ERANGE || *e2 != '\0') {
            /* M-6: STDLIB.md §3e — out-of-int64 integers become Float. */
            return misa_float(strtod(tmp, NULL));
        }
        return misa_int((int64_t)v);
    }
    if (n >= 64) {
        j->err = "bad number";
        return NULL;
    }
    {
        char tmp[64];
        memcpy(tmp, start, n);
        tmp[n] = '\0';
        return misa_float(strtod(tmp, NULL));
    }
}

static int jp_lit(JP *j, const char *word) {
    size_t n = strlen(word);
    if ((size_t)(j->end - j->p) < n || memcmp(j->p, word, n) != 0)
        return 0;
    j->p += n;
    return 1;
}

static MVal *jp_array(JP *j) {
    j->p++; /* [ */
    MVal *arr = misa_list_new();
    jp_ws(j);
    if (j->p < j->end && *j->p == ']') {
        j->p++;
        return arr;
    }
    for (;;) {
        jp_ws(j);
        MVal *v = jp_value(j);
        if (!v)
            return NULL;
        misa_list_push(arr, v);
        jp_ws(j);
        if (j->p >= j->end) {
            j->err = "unterminated array";
            return NULL;
        }
        if (*j->p == ']') {
            j->p++;
            return arr;
        }
        if (*j->p != ',') {
            j->err = "expected ',' or ']' in array";
            return NULL;
        }
        j->p++;
    }
}

static MVal *jp_object(JP *j) {
    j->p++; /* { */
    MVal *rec = misa_record_new();
    jp_ws(j);
    if (j->p < j->end && *j->p == '}') {
        j->p++;
        return rec;
    }
    for (;;) {
        jp_ws(j);
        if (j->p >= j->end || *j->p != '"') {
            j->err = "expected string key in object";
            return NULL;
        }
        MVal *k = jp_string(j);
        if (!k)
            return NULL;
        size_t klen = misa_text_len(k);
        char *kcopy = (char *)misa_alloc(klen + 1);
        memcpy(kcopy, misa_text_data(k), klen);
        kcopy[klen] = '\0';
        jp_ws(j);
        if (j->p >= j->end || *j->p != ':') {
            j->err = "expected ':' in object";
            return NULL;
        }
        j->p++;
        jp_ws(j);
        MVal *v = jp_value(j);
        if (!v)
            return NULL;
        misa_record_set_cstr(rec, kcopy, v);
        jp_ws(j);
        if (j->p >= j->end) {
            j->err = "unterminated object";
            return NULL;
        }
        if (*j->p == '}') {
            j->p++;
            return rec;
        }
        if (*j->p != ',') {
            j->err = "expected ',' or '}' in object";
            return NULL;
        }
        j->p++;
    }
}

static MVal *jp_value(JP *j) {
    jp_ws(j);
    if (j->p >= j->end) {
        j->err = "unexpected end of input";
        return NULL;
    }
    char c = *j->p;
    if (c == '{')
        return jp_object(j);
    if (c == '[')
        return jp_array(j);
    if (c == '"')
        return jp_string(j);
    if (c == '-' || (c >= '0' && c <= '9'))
        return jp_number(j);
    if (jp_lit(j, "true"))
        return misa_bool(1);
    if (jp_lit(j, "false"))
        return misa_bool(0);
    if (jp_lit(j, "null"))
        return misa_null();
    j->err = "unexpected character";
    return NULL;
}

MVal *misa_json_parse(MVal *s, int line, int col) {
    size_t len = misa_text_len(s);
    const char *data = misa_text_data(s);
    JP j = {data, data + len, NULL};
    MVal *v = jp_value(&j);
    if (!v) {
        misa_raise_fmt("E300", line, col, "json.parse: invalid JSON: %s",
                       j.err ? j.err : "parse error");
        return NULL;
    }
    jp_ws(&j);
    if (j.p != j.end) {
        /* Match CPython: "Extra data: line 1 column 41 (char 40)".
         * char = 0-based offset, column = char + 1. */
        size_t ch = (size_t)(j.p - data);
        misa_raise_fmt("E300", line, col,
                       "json.parse: invalid JSON: Extra data: line 1 column %zu (char %zu)",
                       ch + 1, ch);
        return NULL;
    }
    return v;
}

/* ---------------- stringify ---------------- */

static void js_string(JBuf *b, const char *data, size_t len) {
    static const char *escapes[32] = {
        "\\u0000", "\\u0001", "\\u0002", "\\u0003",
        "\\u0004", "\\u0005", "\\u0006", "\\u0007",
        "\\b", "\\t", "\\n", "\\u000b",
        "\\f", "\\r", "\\u000e", "\\u000f",
        "\\u0010", "\\u0011", "\\u0012", "\\u0013",
        "\\u0014", "\\u0015", "\\u0016", "\\u0017",
        "\\u0018", "\\u0019", "\\u001a", "\\u001b",
        "\\u001c", "\\u001d", "\\u001e", "\\u001f",
    };
    jb_putc(b, '"');
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c == '"') {
            jb_puts(b, "\\\"");
        } else if (c == '\\') {
            jb_puts(b, "\\\\");
        } else if (c < 0x20) {
            jb_puts(b, escapes[c]);
        } else {
            jb_putc(b, (char)c);
        }
    }
    jb_putc(b, '"');
}

/* Float formatting: byte-identical to Python str(float) via the shared
 * misa_fmt_float (H-1). Non-finite values are rejected by the caller. */
static void js_float(JBuf *b, double d) {
    char tmp[64];
    misa_fmt_float(tmp, sizeof tmp, d);
    jb_puts(b, tmp);
}

/* 0 on success, -1 if the value is not JSON-representable */
static int js_value(JBuf *b, MVal *v) {
    switch (misa_tag(v)) {
    case M_TAG_NULL:
        jb_puts(b, "null");
        return 0;
    case M_TAG_BOOL:
        jb_puts(b, misa_bool_val(v) ? "true" : "false");
        return 0;
    case M_TAG_INT: {
        char tmp[24];
        snprintf(tmp, sizeof tmp, "%lld", (long long)misa_as_int(v));
        jb_puts(b, tmp);
        return 0;
    }
    case M_TAG_FLOAT: {
        double d = misa_as_float(v);
        if (!isfinite(d))
            return -1;
        js_float(b, d);
        return 0;
    }
    case M_TAG_TEXT:
        js_string(b, misa_text_data(v), misa_text_len(v));
        return 0;
    case M_TAG_LIST: {
        int64_t n = misa_list_len(v);
        jb_putc(b, '[');
        for (int64_t i = 0; i < n; i++) {
            if (i)
                jb_putc(b, ',');
            if (js_value(b, misa_list_get(v, i)) != 0)
                return -1;
        }
        jb_putc(b, ']');
        return 0;
    }
    case M_TAG_RECORD: {
        int64_t n = misa_record_len(v);
        /* closures / fn values are records with __closure_fn; sum-type
         * values are records with __sum_disp (H-4: leaking their internals
         * as {"__tag":..,"__fields":..} is silent data corruption — the
         * interpreter raises E300, so AOT must too). */
        for (int64_t i = 0; i < n; i++) {
            const char *fn = misa_record_field_name(v, i);
            if (strcmp(fn, "__closure_fn") == 0
                    || strcmp(fn, "__sum_disp") == 0)
                return -1;
        }
        jb_putc(b, '{');
        for (int64_t i = 0; i < n; i++) {
            if (i)
                jb_putc(b, ',');
            const char *name = misa_record_field_name(v, i);
            js_string(b, name, strlen(name));
            jb_putc(b, ':');
            if (js_value(b, misa_record_field_val(v, i)) != 0)
                return -1;
        }
        jb_putc(b, '}');
        return 0;
    }
    case M_TAG_MAP: {
        int64_t n = misa_map_len(v);
        jb_putc(b, '{');
        for (int64_t i = 0; i < n; i++) {
            MVal *k = misa_map_key(v, i);
            if (misa_tag(k) != M_TAG_TEXT)
                return -1;
            if (i)
                jb_putc(b, ',');
            js_string(b, misa_text_data(k), misa_text_len(k));
            jb_putc(b, ':');
            if (js_value(b, misa_map_val(v, i)) != 0)
                return -1;
        }
        jb_putc(b, '}');
        return 0;
    }
    default:
        /* M_TAG_MEDIA, M_TAG_ITER, M_TAG_ERROR, M_TAG_PTR, ... */
        return -1;
    }
}

MVal *misa_json_stringify(MVal *v, int line, int col) {
    JBuf b = {0};
    if (js_value(&b, v) != 0) {
        misa_raise_fmt("E300", line, col,
                       "json.stringify: value is not JSON-representable");
        return NULL;
    }
    return misa_text(b.buf ? b.buf : "", b.len);
}
