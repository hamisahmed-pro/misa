/* Phase 5 (DB): SQLite bindings for the Misa AOT runtime.
 *
 * Implements misa_db_open/exec/query/close on top of the SQLite
 * amalgamation. Db handles are small integers indexing a static
 * registry (opaque to Misa code; the `Db` type is check-time only).
 * Compiled only when the program uses db.* (see build.py).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sqlite3.h"
#include "misa_rt.h"
#include "misa_platform.h"

#define MISA_DB_MAX 64
/* P-3: prepared-statement cache (perf gap P-3). Each connection caches
 * up to MISA_DB_CACHE prepared statements keyed by SQL text, avoiding
 * a sqlite3_prepare_v2 on every exec. Entries are finalized on
 * db.close. If the schema changes under a cached statement,
 * sqlite3_step returns SQLITE_SCHEMA and we re-prepare transparently.
 */
#define MISA_DB_CACHE 16

typedef struct {
    char *sql;              /* owned copy of the SQL text (key) */
    sqlite3_stmt *stmt;     /* prepared statement */
} DbCacheEntry;

static sqlite3 *g_db_handles[MISA_DB_MAX];
static int g_db_used[MISA_DB_MAX];
/* Phase 5 DB2: explicit-transaction nesting depth per handle (0 =
 * autocommit; 1 = BEGIN..COMMIT; N > 1 = SAVEPOINT misa_sp_{N-1}). */
static int g_db_txn_depth[MISA_DB_MAX];
static DbCacheEntry g_db_cache[MISA_DB_MAX][MISA_DB_CACHE];

static sqlite3 *db_lookup(int64_t h, int line, int col, const char *op) {
    if (h < 1 || h > MISA_DB_MAX || !g_db_used[h - 1]) {
        misa_raise_fmt("E300", line, col,
                       "%s: invalid Db handle (was it closed?)", op);
        return NULL;
    }
    return g_db_handles[h - 1];
}

/* Fetch a cached prepared statement for (db, sql), preparing and
 * caching on miss. The returned stmt is reset and has no bindings;
 * the caller must bind, step, and reset (not finalize) it. */
static sqlite3_stmt *db_prepare_cached(sqlite3 *db, int64_t slot,
                                       const char *zsql,
                                       int line, int col, const char *op) {
    DbCacheEntry *cache = g_db_cache[slot];
    for (int i = 0; i < MISA_DB_CACHE; i++) {
        if (cache[i].sql && strcmp(cache[i].sql, zsql) == 0) {
            sqlite3_stmt *st = cache[i].stmt;
            sqlite3_reset(st);
            sqlite3_clear_bindings(st);
            return st;
        }
    }
    /* Miss: prepare and insert (evict slot 0 on overflow, FIFO). */
    sqlite3_stmt *st = NULL;
    int rc = sqlite3_prepare_v2(db, zsql, -1, &st, NULL);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "%s failed: %s", op,
                       sqlite3_errmsg(db));
        return NULL;
    }
    int idx = 0;
    for (int i = 0; i < MISA_DB_CACHE; i++) {
        if (!cache[i].sql) { idx = i; break; }
        idx = 0;  /* full: evict oldest */
    }
    if (cache[idx].sql) {
        sqlite3_finalize(cache[idx].stmt);
        free(cache[idx].sql);
    }
    cache[idx].sql = strdup(zsql);
    cache[idx].stmt = st;
    return st;
}

/* Step a cached statement, re-preparing once on SQLITE_SCHEMA. */
static int db_step_cached(sqlite3 *db, int64_t slot, const char *zsql,
                          sqlite3_stmt **stp,
                          int line, int col, const char *op) {
    int rc = sqlite3_step(*stp);
    if (rc == SQLITE_SCHEMA) {
        /* Schema changed: drop the cached entry and re-prepare. */
        DbCacheEntry *cache = g_db_cache[slot];
        for (int i = 0; i < MISA_DB_CACHE; i++) {
            if (cache[i].sql && strcmp(cache[i].sql, zsql) == 0) {
                sqlite3_finalize(cache[i].stmt);
                free(cache[i].sql);
                cache[i].sql = NULL;
                cache[i].stmt = NULL;
                break;
            }
        }
        *stp = db_prepare_cached(db, slot, zsql, line, col, op);
        if (!*stp) return SQLITE_ERROR;
        rc = sqlite3_step(*stp);
    }
    return rc;
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
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.exec");
    if (!db) return NULL;
    const char *zsql = misa_text_z(sql);
    sqlite3_stmt *st = db_prepare_cached(db, slot - 1, zsql,
                                         line, col, "db.exec");
    if (!st) return NULL;
    db_bind_args(st, args, line, col, "db.exec");
    int rc = db_step_cached(db, slot - 1, zsql, &st,
                            line, col, "db.exec");
    int changes = 0;
    if (rc == SQLITE_DONE) {
        changes = sqlite3_changes(db);
    } else {
        misa_raise_fmt("E303", line, col, "db.exec failed: %s",
                       sqlite3_errmsg(db));
        return NULL;
    }
    sqlite3_reset(st);
    return misa_int((int64_t)changes);
}

/* P-3: bulk exec. `rows` is a list of lists of scalar args. The
 * statement is prepared once and re-bound per row. Returns the total
 * number of rows changed. */
MVal *misa_db_exec_many(MVal *h, MVal *sql, MVal *rows,
                        int line, int col) {
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.exec_many");
    if (!db) return NULL;
    const char *zsql = misa_text_z(sql);
    sqlite3_stmt *st = db_prepare_cached(db, slot - 1, zsql,
                                         line, col, "db.exec_many");
    if (!st) return NULL;
    int64_t n = misa_list_len(rows);
    int64_t total = 0;
    for (int64_t i = 0; i < n; i++) {
        MVal *row = misa_list_get(rows, i);
        if (misa_tag(row) != M_TAG_LIST) {
            misa_raise_fmt("E200", line, col,
                           "db.exec_many: row %d must be a list", i);
            return NULL;
        }
        sqlite3_reset(st);
        sqlite3_clear_bindings(st);
        db_bind_args(st, row, line, col, "db.exec_many");
        int rc = db_step_cached(db, slot - 1, zsql, &st,
                                line, col, "db.exec_many");
        if (rc != SQLITE_DONE) {
            misa_raise_fmt("E303", line, col,
                           "db.exec_many failed at row %d: %s", i,
                           sqlite3_errmsg(db));
            return NULL;
        }
        total += sqlite3_changes(db);
    }
    sqlite3_reset(st);
    return misa_int(total);
}

/* P-3: explicit transaction control. */
static MVal *db_exec_simple(int64_t slot, const char *zsql,
                            const char *op, int line, int col) {
    sqlite3 *db = db_lookup(slot, line, col, op);
    if (!db) return NULL;
    char *errmsg = NULL;
    int rc = sqlite3_exec(db, zsql, NULL, NULL, &errmsg);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "%s failed: %s", op,
                       errmsg ? errmsg : sqlite3_errmsg(db));
        sqlite3_free(errmsg);
        return NULL;
    }
    return misa_null();
}

/* Phase 5 DB2: nested transactions via savepoints. Depth 0->1 issues
 * BEGIN; deeper levels use SAVEPOINT misa_sp_{depth}. */
MVal *misa_db_begin(MVal *h, int line, int col) {
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.begin");
    if (!db) return NULL;
    int depth = g_db_txn_depth[slot - 1];
    char sql[64];
    if (depth == 0)
        snprintf(sql, sizeof sql, "BEGIN");
    else
        snprintf(sql, sizeof sql, "SAVEPOINT misa_sp_%d", depth);
    MVal *r = db_exec_simple(slot, sql, "db.begin", line, col);
    if (r) g_db_txn_depth[slot - 1] = depth + 1;
    return r;
}

MVal *misa_db_commit(MVal *h, int line, int col) {
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.commit");
    if (!db) return NULL;
    int depth = g_db_txn_depth[slot - 1];
    if (depth == 0) return misa_null();  /* no-op, matches interpreter */
    char sql[64];
    if (depth == 1)
        snprintf(sql, sizeof sql, "COMMIT");
    else
        snprintf(sql, sizeof sql, "RELEASE misa_sp_%d", depth - 1);
    MVal *r = db_exec_simple(slot, sql, "db.commit", line, col);
    if (r) g_db_txn_depth[slot - 1] = depth - 1;
    return r;
}

MVal *misa_db_rollback(MVal *h, int line, int col) {
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.rollback");
    if (!db) return NULL;
    int depth = g_db_txn_depth[slot - 1];
    if (depth == 0) return misa_null();  /* no-op, matches interpreter */
    MVal *r;
    if (depth == 1) {
        r = db_exec_simple(slot, "ROLLBACK", "db.rollback", line, col);
    } else {
        char sql[64];
        snprintf(sql, sizeof sql, "ROLLBACK TO SAVEPOINT misa_sp_%d",
                 depth - 1);
        r = db_exec_simple(slot, sql, "db.rollback", line, col);
        if (r) {
            snprintf(sql, sizeof sql, "RELEASE misa_sp_%d", depth - 1);
            MVal *r2 = db_exec_simple(slot, sql, "db.rollback",
                                      line, col);
            if (!r2) return NULL;
        }
    }
    if (r) g_db_txn_depth[slot - 1] = depth - 1;
    return r;
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

/* Phase 5 DB2: EXPLAIN QUERY PLAN → [{id, parent, detail}]. Reuses the
 * row-materialization loop of misa_db_query. */
MVal *misa_db_explain(MVal *h, MVal *sql, MVal *args, int line, int col) {
    sqlite3 *db = db_lookup(misa_as_int(h), line, col, "db.explain");
    if (!db) return NULL;
    const char *zsql = misa_text_z(sql);
    size_t n = strlen(zsql) + 32;
    char *q = (char *)malloc(n);
    if (!q) {
        misa_raise_fmt("E300", line, col, "db.explain: out of memory");
        return NULL;
    }
    snprintf(q, n, "EXPLAIN QUERY PLAN %s", zsql);
    sqlite3_stmt *st = NULL;
    int rc = sqlite3_prepare_v2(db, q, -1, &st, NULL);
    free(q);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.explain failed: %s",
                       sqlite3_errmsg(db));
        return NULL;
    }
    db_bind_args(st, args, line, col, "db.explain");
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
                int nb = sqlite3_column_bytes(st, i);
                v = misa_text((const char *)t, (size_t)nb);
                break;
            }
            case SQLITE_NULL:
                v = misa_null();
                break;
            default:
                v = misa_null();
                break;
            }
            misa_record_set_cstr(rec, cname ? cname : "", v);
        }
        misa_list_push(out, rec);
    }
    if (rc != SQLITE_DONE) {
        misa_raise_fmt("E303", line, col, "db.explain failed: %s",
                       sqlite3_errmsg(db));
        sqlite3_finalize(st);
        return NULL;
    }
    sqlite3_finalize(st);
    return out;
}

/* Phase 5 DB2: versioned migrations. `ms` is a Misa list of
 * {version: Int, sql: Text} records. Applies pending versions in
 * ascending order, each in its own transaction; returns the count. */
MVal *misa_db_migrate(MVal *h, MVal *ms, int line, int col) {
    int64_t slot = misa_as_int(h);
    sqlite3 *db = db_lookup(slot, line, col, "db.migrate");
    if (!db) return NULL;
    int64_t nm = misa_list_len(ms);
    if (nm > 1024) {
        misa_raise_fmt("E303", line, col,
                       "db.migrate: too many migrations (%lld)", nm);
        return NULL;
    }
    /* Collect (version, sql) pairs, validating shape. */
    typedef struct { int64_t ver; const char *sql; MVal *rec; } Mig;
    Mig *migs = (Mig *)calloc((size_t)(nm ? nm : 1), sizeof(Mig));
    if (!migs) {
        misa_raise_fmt("E300", line, col, "db.migrate: out of memory");
        return NULL;
    }
    for (int64_t i = 0; i < nm; i++) {
        MVal *rec = misa_list_get(ms, i);
        MVal *vv = misa_record_get_opt(rec, "version");
        MVal *ss = misa_record_get_opt(rec, "sql");
        if (!vv || !ss) {
            misa_raise_fmt("E303", line, col,
                           "db.migrate: migration %lld must be "
                           "{version: Int, sql: Text}", i);
            free(migs);
            return NULL;
        }
        int64_t ver = misa_as_int(vv);
        if (ver <= 0) {
            misa_raise_fmt("E303", line, col,
                           "db.migrate: bad version %lld (positive Int "
                           "required)", ver);
            free(migs);
            return NULL;
        }
        for (int64_t j = 0; j < i; j++) {
            if (migs[j].ver == ver) {
                misa_raise_fmt("E303", line, col,
                               "db.migrate: duplicate version %lld", ver);
                free(migs);
                return NULL;
            }
        }
        migs[i].ver = ver;
        migs[i].sql = misa_text_z(ss);
        migs[i].rec = rec;
    }
    /* Sort by version (insertion sort; n is small). */
    for (int64_t i = 1; i < nm; i++) {
        Mig t = migs[i];
        int64_t j = i;
        while (j > 0 && migs[j - 1].ver > t.ver) {
            migs[j] = migs[j - 1];
            j--;
        }
        migs[j] = t;
    }
    char *errmsg = NULL;
    int rc = sqlite3_exec(db,
        "CREATE TABLE IF NOT EXISTS _misa_migrations "
        "(version INTEGER PRIMARY KEY)", NULL, NULL, &errmsg);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.migrate failed: %s",
                       errmsg ? errmsg : sqlite3_errmsg(db));
        sqlite3_free(errmsg);
        free(migs);
        return NULL;
    }
    int applied = 0;
    sqlite3_stmt *chk = NULL;
    rc = sqlite3_prepare_v2(db,
        "SELECT 1 FROM _misa_migrations WHERE version = ?1",
        -1, &chk, NULL);
    if (rc != SQLITE_OK) {
        misa_raise_fmt("E303", line, col, "db.migrate failed: %s",
                       sqlite3_errmsg(db));
        free(migs);
        return NULL;
    }
    for (int64_t i = 0; i < nm; i++) {
        sqlite3_bind_int64(chk, 1, migs[i].ver);
        rc = sqlite3_step(chk);
        int done = (rc == SQLITE_ROW);
        sqlite3_reset(chk);
        sqlite3_clear_bindings(chk);
        if (done) continue;
        /* Each migration runs in its own transaction. */
        char *err = NULL;
        rc = sqlite3_exec(db, "BEGIN", NULL, NULL, &err);
        if (rc != SQLITE_OK) goto mig_fail;
        rc = sqlite3_exec(db, migs[i].sql, NULL, NULL, &err);
        if (rc != SQLITE_OK) {
            sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
            goto mig_fail;
        }
        sqlite3_stmt *ins = NULL;
        rc = sqlite3_prepare_v2(db,
            "INSERT INTO _misa_migrations (version) VALUES (?1)",
            -1, &ins, NULL);
        if (rc == SQLITE_OK) {
            sqlite3_bind_int64(ins, 1, migs[i].ver);
            rc = sqlite3_step(ins);
            sqlite3_finalize(ins);
            /* a successful INSERT step returns SQLITE_DONE, not OK */
            if (rc != SQLITE_DONE) rc = SQLITE_ERROR;
            else rc = SQLITE_OK;
        }
        if (rc != SQLITE_OK) {
            sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
            goto mig_fail;
        }
        rc = sqlite3_exec(db, "COMMIT", NULL, NULL, &err);
        if (rc != SQLITE_OK) goto mig_fail;
        applied++;
        continue;
    mig_fail:
        misa_raise_fmt("E303", line, col,
                       "db.migrate: version %lld failed: %s", migs[i].ver,
                       err ? err : sqlite3_errmsg(db));
        sqlite3_free(err);
        sqlite3_finalize(chk);
        free(migs);
        return NULL;
    }
    sqlite3_finalize(chk);
    free(migs);
    return misa_int(applied);
}

MVal *misa_db_close(MVal *h, int line, int col) {    sqlite3 *db = db_lookup(misa_as_int(h), line, col, "db.close");
    if (!db) return NULL;
    int64_t slot = misa_as_int(h);
    /* P-3: finalize cached statements for this connection. */
    DbCacheEntry *cache = g_db_cache[slot - 1];
    for (int i = 0; i < MISA_DB_CACHE; i++) {
        if (cache[i].sql) {
            sqlite3_finalize(cache[i].stmt);
            free(cache[i].sql);
            cache[i].sql = NULL;
            cache[i].stmt = NULL;
        }
    }
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
