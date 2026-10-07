/* Misa FFI test helpers (Overtake Wave 2): compiled on the fly by the
 * test suite into /tmp/misa_ffi_helpers.so. Uses int64_t fields so the
 * C layout matches Misa's Int->long mapping exactly. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    int64_t x;
    int64_t y;
} TestPoint;

/* int64 array element access (qsort works on raw memory; Misa's Ptr
 * is opaque, so tests fill/read through these). */
void ffi_i64_set(void *base, int64_t i, int64_t v) {
    ((int64_t *)base)[i] = v;
}
int64_t ffi_i64_get(void *base, int64_t i) {
    return ((int64_t *)base)[i];
}

/* Struct by value: scale a point, return a point. Exercises struct
 * arg + struct return across the FFI boundary. */
TestPoint ffi_scale_point(TestPoint p, double s) {
    TestPoint r;
    r.x = (int64_t)(p.x * s);
    r.y = (int64_t)(p.y * s);
    return r;
}

/* Struct with mixed field types (int64, double, int64, pointer). */
typedef struct {
    int64_t id;
    double score;
    int64_t flags;
    void *tag;
} TestMixed;

TestMixed ffi_make_mixed(int64_t id, double score, int64_t flags,
                         void *tag) {
    TestMixed m;
    m.id = id;
    m.score = score;
    m.flags = flags;
    m.tag = tag;
    return m;
}
int64_t ffi_mixed_id(TestMixed m) { return m.id; }

/* Read a NUL-terminated C string (for snprintf-based vararg tests). */
const char *ffi_cstr_at(void *p) { return (const char *)p; }

/* Overtake final wave: nested structs, unions, bitfields. */

/* Nested struct: a Point inside an Outer, with padding-sensitive layout
 * (4-byte tag forces alignment padding before the nested struct;
 * mirrors Misa Bool->int). */
typedef struct {
    int tag;
    TestPoint p;
    int64_t tail;
} TestOuter;

TestOuter ffi_outer_add(TestOuter o, int64_t k) {
    TestOuter r;
    r.tag = o.tag + (int)k;
    r.p.x = o.p.x + k;
    r.p.y = o.p.y + k;
    r.tail = o.tail + k;
    return r;
}
int64_t ffi_outer_tag(TestOuter o) { return o.tag; }

/* Union: same memory as int64 or double. */
typedef union {
    int64_t i;
    double f;
} TestValue;

double ffi_value_as_f(TestValue v) { return v.f; }
int64_t ffi_value_as_i(TestValue v) { return v.i; }
TestValue ffi_value_from_i(int64_t i) { TestValue v; v.i = i; return v; }

/* Bitfields: unsigned-long units, little-endian packing. */
struct TestBits {
    unsigned long a:3;
    unsigned long b:5;
    unsigned long c:20;
    int64_t d;
    unsigned long e:9;
};

int64_t ffi_bits_sum(struct TestBits b) {
    return (int64_t)(b.a + b.b + b.c);
}
int64_t ffi_bits_d(struct TestBits b) { return b.d; }
unsigned long ffi_bits_e(struct TestBits b) { return b.e; }
/* Raw first unit word, for byte-exact packing verification. */
unsigned long ffi_bits_word0(struct TestBits b) {
    unsigned long w;
    memcpy(&w, &b, sizeof w);
    return w;
}

/* 16-byte union through C (two-register SysV class). */
typedef union { int64_t i; TestPoint p; } TestU16;
TestU16 ffi_u16_add(TestU16 u, int64_t k) {
    TestU16 r; r.p.x = u.p.x + k; r.p.y = u.p.y + k; return r;
}
int64_t ffi_u16_x(TestU16 u) { return u.p.x; }
