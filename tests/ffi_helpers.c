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
