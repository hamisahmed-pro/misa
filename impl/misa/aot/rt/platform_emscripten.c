/* Misa AOT runtime — Emscripten platform implementation (Phase 4, M3).
 *
 *   P1 log      -> console.log / console.error
 *   P2 fatal    -> console + overlay banner (never kills the page;
 *                  killing the process is meaningless on the web)
 *   P3 getenv   -> window.MISA_ENV, a JS object populated by the
 *                  shell page (e.g. from ?env_KEY= query params).
 *                  Provider keys are NEVER baked into the wasm.
 *   P4 fs root  -> the Emscripten virtual FS. Absolute paths address
 *                  it directly (preloaded assets, MISA_MOCK_FILE);
 *                  app-relative paths map under /misa_fs, which the
 *                  shell mounts as IDBFS for persistent writes.
 *
 * HTTP: GET and POST via emscripten_fetch, blocked on with Asyncify
 * (emscripten_sleep poll, since emscripten_fetch_wait is deprecated and
 * non-functional since 3.1.62), so misa_http_get and the network model
 * calls keep their blocking shape (M5).
 */
#include "misa_platform.h"

#include <emscripten.h>
#include <emscripten/fetch.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

/* ------------------------------------------------------------------ */
/* P1: logging                                                        */
/* ------------------------------------------------------------------ */

EM_JS(void, misa_js_log, (const char *s, int len), {
    console.log(UTF8ToString(s, len));
});

EM_JS(void, misa_js_log_err, (const char *s, int len), {
    console.error(UTF8ToString(s, len));
});

void misa_platform_log(const char *msg, size_t len) {
    if (len) misa_js_log(msg, (int)len);
}

void misa_platform_log_err(const char *msg, size_t len) {
    if (len) misa_js_log_err(msg, (int)len);
}

void misa_platform_flush(void) {
    /* console output needs no flush */
}

/* ------------------------------------------------------------------ */
/* P2: fatal errors (never kills the page)                            */
/* ------------------------------------------------------------------ */

EM_JS(void, misa_js_fatal, (const char *code, const char *msg), {
    var text = 'Misa fatal ' + UTF8ToString(code) + ': ' +
               UTF8ToString(msg);
    console.error(text);
    var banner = document.createElement('div');
    banner.className = 'misa-banner misa-banner-error';
    banner.textContent = text;
    var root = document.getElementById('misa-root');
    (root || document.body).appendChild(banner);
});

void misa_platform_fatal(const char *code, const char *msg) {
    misa_js_fatal(code ? code : "E300", msg ? msg : "error");
    /* Never returns. abort() traps the wasm instance; the overlay
     * above is the user-visible part. */
    abort();
}

/* ------------------------------------------------------------------ */
/* P3: environment (JS-supplied config table)                          */
/* ------------------------------------------------------------------ */

EM_JS(char *, misa_js_getenv, (const char *name), {
    var v = (window.MISA_ENV || {})[UTF8ToString(name)];
    if (v === undefined || v === null) return 0;
    var s = String(v);
    var len = lengthBytesUTF8(s) + 1;
    var p = _malloc(len);
    stringToUTF8(s, p, len);
    return p;
});

EM_JS(void, misa_js_free, (void *p), { _free(p); });

const char *misa_platform_getenv(const char *name) {
    /* getenv-like semantics: a static buffer overwritten per call,
     * no per-call leak. Callers use the value immediately. */
    static char buf[4096];
    char *p = misa_js_getenv(name);
    if (!p) return NULL;
    strncpy(buf, p, sizeof buf - 1);
    buf[sizeof buf - 1] = '\0';
    misa_js_free(p);
    return buf;
}

/* ------------------------------------------------------------------ */
/* P4: filesystem root                                                */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* P4: filesystem root + IDBFS bootstrap                              */
/* ------------------------------------------------------------------ */

int misa_platform_fs_path(const char *path, char *out, size_t outcap) {
    char tmp[4096];
    if (path[0] == '/') {
        /* absolute: the virtual FS directly (preloaded assets,
         * MISA_MOCK_FILE, staged uploads) */
        snprintf(tmp, sizeof tmp, "%s", path);
    } else {
        /* app-relative: /misa_fs (IDBFS-backed when the shell mounts
         * it there; plain MEMFS otherwise) */
        mkdir("/misa_fs", 0777); /* best effort; EEXIST is fine */
        snprintf(tmp, sizeof tmp, "/misa_fs/%s", path);
    }
    if (strlen(tmp) + 1 > outcap) return 0;
    strcpy(out, tmp);
    return 1;
}

/* Mount /misa_fs on IDBFS and populate it from the browser's
 * IndexedDB. Blocking (Asyncify): the shell calls this via ccall
 * BEFORE misa_ui_boot so previously persisted files are visible.
 * Exported to JS (see build.py). */
EM_JS(void, misa_js_idbfs_mount, (void), {
    try { FS.mkdir('/misa_fs'); } catch (e) { /* EEXIST is fine */ }
    try { FS.mount(IDBFS, {}, '/misa_fs'); } catch (e) { /* ditto */ }
    window.__misa_idbfs_done = false;
    FS.syncfs(true, function(err) {
        if (err) console.error('misa: IDBFS populate failed', err);
        window.__misa_idbfs_done = true;
    });
});

EM_JS(int, misa_js_idbfs_done, (void), {
    return window.__misa_idbfs_done ? 1 : 0;
});

void misa_platform_fs_bootstrap(void) {
    misa_js_idbfs_mount();
    while (!misa_js_idbfs_done())
        emscripten_sleep(50);
}

/* Persist MEMFS -> IDBFS after a write (fire-and-forget; errors go
 * to the console). Called by misa_fs_write via the platform hook. */
EM_JS(void, misa_js_idbfs_persist, (void), {
    try {
        FS.syncfs(false, function(err) {
            if (err) console.error('misa: IDBFS persist failed', err);
        });
    } catch (e) {
        console.error('misa: IDBFS persist failed', e);
    }
});

void misa_platform_fs_after_write(const char *mapped_path) {
    (void)mapped_path;
    misa_js_idbfs_persist();
}

/* ------------------------------------------------------------------ */
/* HTTP transport                                                     */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* HTTP transport                                                     */
/* ------------------------------------------------------------------ */

/* Blocking GET via emscripten_fetch. The fetch API is callback-based;
 * with Asyncify we issue the fetch and sleep-poll until onsuccess /
 * onerror fires (or the deadline passes), so misa_http_get keeps its
 * blocking shape. (emscripten_fetch_wait is deprecated AND
 * non-functional since 3.1.62 — this is the supported pattern.) */
typedef struct {
    volatile int done;
    int          closed;   /* fetch_close issued (see fetch_close_once) */
    int          status;   /* HTTP status; -1 = transport failure */
    char         err[256];
    char        *body;     /* malloc'd, owned by the struct */
    size_t       len;
} FetchState;

/* emscripten_fetch_close on a running fetch fires onerror()
 * synchronously first; guard so the callbacks never double-close. */
static void fetch_close_once(FetchState *st, emscripten_fetch_t *f) {
    if (!st->closed) {
        st->closed = 1;
        emscripten_fetch_close(f);
    }
}

static void fetch_ok(emscripten_fetch_t *f) {
    FetchState *st = (FetchState *)f->userData;
    st->status = f->status;
    st->err[0] = '\0';
    if (f->numBytes > 0) {
        st->body = (char *)malloc(f->numBytes ? f->numBytes : 1);
        if (st->body) {
            memcpy(st->body, f->data, f->numBytes);
            st->len = f->numBytes;
        } else {
            st->status = -1;
            snprintf(st->err, sizeof st->err, "out of memory");
        }
    }
    st->done = 1;
    fetch_close_once(st, f);
}

static void fetch_err(emscripten_fetch_t *f) {
    FetchState *st = (FetchState *)f->userData;
    st->status = -1;
    snprintf(st->err, sizeof st->err, "network error: %s",
             f->statusText[0] ? f->statusText : "fetch failed");
    st->done = 1;
    fetch_close_once(st, f);
}

int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out) {
    memset(out, 0, sizeof *out);
    FetchState st;
    memset(&st, 0, sizeof st);
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.userData = &st;
    attr.onsuccess = fetch_ok;
    attr.onerror = fetch_err;
    emscripten_fetch_t *f = emscripten_fetch(&attr, url);
    /* Emscripten fetch has no native timeout: the sleep-poll doubles
     * as the watchdog (doc §5.4). */
    double deadline = emscripten_get_now() + timeout_s * 1000.0;
    while (!st.done) {
        if (emscripten_get_now() >= deadline) {
            /* abort: close() on a running fetch fires onerror first
             * (synchronously), which marks the state done */
            fetch_close_once(&st, f);
            snprintf(st.err, sizeof st.err, "network error: timed out");
            st.status = -1;
            st.done = 1;
            break;
        }
        emscripten_sleep(50);
    }
    if (st.status == -1) {
        snprintf(out->err, sizeof out->err, "%s", st.err);
        free(st.body);
        return 0;
    }
    out->status = st.status;
    out->body = st.body; /* may be NULL when the body was empty */
    out->len = st.len;
    out->err[0] = '\0';
    return 1;
}

int misa_platform_http_post(const char *url,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    /* Phase 4 (M5): POST via emscripten_fetch, same Asyncify
     * sleep-poll pattern as the GET path above. NOTE: Emscripten's
     * requestHeaders is a NULL-terminated array of key,value POINTER
     * PAIRS (Fetch.js reads key at [0], value at [1], stepping 2
     * pointers) — NOT "Key: Value" strings. The caller's counted
     * "Key: Value" list is split into pairs here; keys are copied so
     * they can be NUL-terminated for UTF8ToString. */
    memset(out, 0, sizeof *out);
    FetchState st;
    memset(&st, 0, sizeof st);
    const char *kv[33]; /* <=16 headers -> 32 slots + NULL key */
    char *keycopy[16];
    int nkeys = 0, nkv = 0;
    int nh = nheaders < 16 ? nheaders : 16;
    for (int i = 0; i < nh; i++) {
        const char *h = headers[i] ? headers[i] : "";
        const char *colon = strchr(h, ':');
        if (!colon) continue; /* malformed header: skip */
        size_t klen = (size_t)(colon - h);
        char *kc = (char *)malloc(klen + 1);
        if (!kc) continue;
        memcpy(kc, h, klen);
        kc[klen] = '\0';
        const char *v = colon + 1;
        while (*v == ' ') v++;
        keycopy[nkeys++] = kc;
        kv[nkv++] = kc;
        kv[nkv++] = v;
    }
    kv[nkv] = NULL;
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    strcpy(attr.requestMethod, "POST");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.requestHeaders = kv;
    attr.requestData = body ? body : "";
    attr.requestDataSize = body ? strlen(body) : 0;
    attr.userData = &st;
    attr.onsuccess = fetch_ok;
    attr.onerror = fetch_err;
    emscripten_fetch_t *f = emscripten_fetch(&attr, url);
    /* Emscripten fetch has no native timeout: the sleep-poll doubles
     * as the watchdog (doc §5.4). */
    double deadline = emscripten_get_now() + timeout_s * 1000.0;
    while (!st.done) {
        if (emscripten_get_now() >= deadline) {
            /* abort: close() on a running fetch fires onerror first
             * (synchronously), which marks the state done */
            fetch_close_once(&st, f);
            snprintf(st.err, sizeof st.err, "network error: timed out");
            st.status = -1;
            st.done = 1;
            break;
        }
        emscripten_sleep(50);
    }
    for (int i = 0; i < nkeys; i++) free(keycopy[i]);
    if (st.status == -1) {
        snprintf(out->err, sizeof out->err, "%s", st.err);
        free(st.body);
        return 0;
    }
    out->status = st.status;
    out->body = st.body; /* may be NULL when the body was empty */
    out->len = st.len;
    out->err[0] = '\0';
    return 1;
}

void misa_platform_http_free(MisaHttpResp *r) {
    if (r) {
        free(r->body);
        memset(r, 0, sizeof *r);
    }
}
