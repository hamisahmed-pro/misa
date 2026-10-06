/* Phase 5 (DB): SQLite bindings for the Misa AOT runtime.
 *
 * Implements misa_db_open/exec/query/close on top of the SQLite
 * amalgamation. Db handles are small integers indexing a static
 * registry (opaque to Misa code; the `Db` type is check-time only).
 * Compiled only when the program uses db.* (see build.py).
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "sqlite3.h"
#include "misa_rt.h"
#include "misa_platform.h"

#define MISA_DB_MAX 64

static sqlite3 *g_db_handles[MISA_DB_MAX];
static int g_db_used[MISA_DB_MAX];

static sqlite3 *db_lookup(int64_t h, int line, int col, const char *op) {
    if (h < 1 || h > MISA_DB_MAX || !g_db_used[h - 1]) {
        misa_raise_fmt("E300", line, col,
                       "%s: invalid Db handle (was it closed?)", op);
        return NULL;
    }
    return g_db_handles[h - 1];
}

/* Bind a Misa list of scalar args as statement parameters.
 * H-3: the interpreter binds natively via sqlite3 (Int/Float/Text all
 * work), so AOT must too — a program passing `misa check` and running on
 * the interpreter must not fail on AOT. */
static void db_bind_args(sqlite3_stmt *st, MVal *args, int line, int col,
                         const char *op) {
    int64_t n = misa_list_len(args);
    for (int64_t i = 0; i < n; i++) {
        MVal *a = misa_list_get(args, i);
        int rc = SQLITE_OK;
        int idx = (int)(i + 1);
        switch (misa_tag(a)) {
        case M_TAG_INT:
            rc = sqlite3_bind_int64(st, idx, misa_as_int(a));
            break;
        case M_TAG_FLOAT:
            rc = sqlite3_bind_double(st, idx, misa_as_float(a));
            break;
        case M_TAG_BOOL:
            rc = sqlite3_bind_int(st, idx, misa_bool_val(a));
            break;
        case M_TAG_TEXT: {
            const char *z = misa_text_z(a);
            rc = sqlite3_bind_text(st, idx, z, -1, SQLITE_TRANSIENT);
            break;
        }
        case M_TAG_NULL:
            rc = sqlite3_bind_null(st, idx);
            break;
        default:
            misa_raise_fmt("E200", line, col,
                           "%s: bind arg %d must be Int, Float, Bool, "
                           "Text, or nil", op, idx);
            return;
        }
        if (rc != SQLITE_OK) {
            misa_raise_fmt("E303", line, col, "%s: bind failed", op);
            return;
        }
    }
}

MVal *misa_db_open(MVal *path, int line, int col) {
    const char *p = misa_text_z(path);
    char mp[4096];
    if (!misa_platform_fs_path(p, mp, sizeof mp)) {
        misa_raise_fmt("E303", line, col,
                       "db.open failed: cannot map path '%s'", p);
        return NULL;
    }
    int slot = -1;
    for (int i = 0; i < MISA_DB_MAX; i++) {
        if (!g_db_used[i]) { slot = i; break; }
    }
    if (slot < 0) {
        misa_raise_fmt("E300", line, col,
                       "db.open failed: too many open databases");
        return NULL;
    }
    sqlite3 *db = NULL;
    int rc = sqlite3_open(mp, &db);
    if (rc != SQLITE_OK) {
        const char *msg = db ? sqlite3_errmsg(db) : "out of memory";
        if (db) sqlite3_close(db);
        misa_raise_fmt("E303", line, col, "db.open failed: %s", msg);
        return NULL;
    }
    g_db_handles[slot] = db;
    g_db_used[slot] = 1;
    return misa_int((int64_t)(slot + 1));
}

MVal *misa_db_exec(MVal *h, MVal *sql, MVal *args, int line, int col) {
    sqlite3 *db = db_lookup(misa_as_int(h), line, col, "db.exec");
    if (!db) return NULL;
    const char *zsql = misa_text_z(sql);
    sqlite3_stmt *st = NULL;
    int rc = sqlite3_prepare_v2(db, zsql, -1, &st, NULL);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.exec failed: %s",
                       sqlite3_errmsg(db));
        return NULL;
    }
    db_bind_args(st, args, line, col, "db.exec");
    rc = sqlite3_step(st);
    int changes = 0;
    if (rc == SQLITE_DONE) {
        changes = sqlite3_changes(db);
    } else {
        misa_raise_fmt("E303", line, col, "db.exec failed: %s",
                       sqlite3_errmsg(db));
        sqlite3_finalize(st);
        return NULL;
    }
    sqlite3_finalize(st);
    return misa_int((int64_t)changes);
}

MVal *misa_db_query(MVal *h, MVal *sql, MVal *args, int line, int col) {
    sqlite3 *db = db_lookup(misa_as_int(h), line, col, "db.query");
    if (!db) return NULL;
    const char *zsql = misa_text_z(sql);
    sqlite3_stmt *st = NULL;
    int rc = sqlite3_prepare_v2(db, zsql, -1, &st, NULL);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.query failed: %s",
                       sqlite3_errmsg(db));
        return NULL;
    }
    db_bind_args(st, args, line, col, "db.query");
    MVal *out = misa_list_new();
    int ncol = sqlite3_column_count(st);
    while ((rc = sqlite3_step(st)) == SQLITE_ROW) {
        MVal *rec = misa_record_new();
        for (int i = 0; i < ncol; i++) {
            const char *cname = sqlite3_column_name(st, i);
            MVal *v = NULL;
            switch (sqlite3_column_type(st, i)) {
            case SQLITE_INTEGER:
                v = misa_int(sqlite3_column_int64(st, i));
                break;
            case SQLITE_FLOAT:
                v = misa_float(sqlite3_column_double(st, i));
                break;
            case SQLITE_TEXT: {
                const unsigned char *t = sqlite3_column_text(st, i);
                int n = sqlite3_column_bytes(st, i);
                v = misa_text((const char *)t, (size_t)n);
                break;
            }
            case SQLITE_NULL:
                v = misa_null();
                break;
            case SQLITE_BLOB:
            default:
                misa_raise_fmt("E303", line, col,
                               "db.query: BLOB columns are not "
                               "supported in v1");
                sqlite3_finalize(st);
                return NULL;
            }
            misa_record_set_cstr(rec, cname ? cname : "", v);
        }
        misa_list_push(out, rec);
    }
    if (rc != SQLITE_DONE) {
        misa_raise_fmt("E303", line, col, "db.query failed: %s",
                       sqlite3_errmsg(db));
        sqlite3_finalize(st);
        return NULL;
    }
    sqlite3_finalize(st);
    return out;
}

MVal *misa_db_close(MVal *h, int line, int col) {
    sqlite3 *db = db_lookup(misa_as_int(h), line, col, "db.close");
    if (!db) return NULL;
    int64_t slot = misa_as_int(h);
    int rc = sqlite3_close(db);
    g_db_used[slot - 1] = 0;
    g_db_handles[slot - 1] = NULL;
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.close failed: %s",
                       sqlite3_errmsg(db));
        return NULL;
    }
    return misa_null();
}
