/* Misa AOT runtime — platform abstraction layer (Phase 4, M2).
 *
 * Every host-OS assumption in the runtime funnels through this
 * interface. One implementation per target:
 *
 *   platform_linux.c       Linux / hosted server & desktop (P1-P5 =
 *                          today's behavior: log->stdio, fatal->stderr
 *                          +exit, getenv->getenv, fs root->cwd,
 *                          HTTP via libcurl when MISA_USE_CURL)
 *   platform_emscripten.c  (M3) console.log, JS env bridge, fetch
 *   platform_android.c     (M6) logcat, JNI env, HttpURLConnection
 *   platform_apple.m       (M7) os_log, NSURLSession
 *
 * The platform file is selected at link time by build.py's target
 * table; the rest of the runtime (misa_rt.c, misa_model.c, ui_*.c)
 * never touches the OS directly.
 */
#ifndef MISA_PLATFORM_H
#define MISA_PLATFORM_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* P1: logging                                                        */
/* ------------------------------------------------------------------ */

/* Write exactly len bytes to the platform log. Linux: stdout.
 * (print, screen preview.) */
void misa_platform_log(const char *msg, size_t len);

/* Write exactly len bytes to the platform error stream. Linux: stderr.
 * (uncaught-error report.) */
void misa_platform_log_err(const char *msg, size_t len);

/* Flush pending log output. */
void misa_platform_flush(void);

/* ------------------------------------------------------------------ */
/* P2: fatal errors                                                   */
/* ------------------------------------------------------------------ */

/* Unrecoverable runtime failure (out-of-memory today). Never returns.
 * Linux: "<unknown>: <code>: <msg>\n" on stderr, then exit(1).
 * Mobile/web implementations must NOT kill the process blindly:
 * Android logs and finishes the activity, iOS logs and presents,
 * web logs to the console and shows an overlay. */
void misa_platform_fatal(const char *code, const char *msg);

/* ------------------------------------------------------------------ */
/* P3: environment                                                    */
/* ------------------------------------------------------------------ */

/* Provider keys, MISA_MOCK_FILE, MISA_*_BASE_URL overrides.
 * Returns NULL when unset. Linux: getenv(name). Web/mobile read an
 * app-supplied config table instead (keys are never baked into the
 * binary). */
const char *misa_platform_getenv(const char *name);

/* ------------------------------------------------------------------ */
/* P4: filesystem root                                                */
/* ------------------------------------------------------------------ */

/* Map an app-relative path to a platform path for fopen().
 * Returns 1 and writes the NUL-terminated mapped path into out on
 * success, 0 when the path cannot be mapped (out untouched).
 * Linux: identity mapping (cwd-relative, today's behavior).
 * WASI: preopened dirs only. Emscripten: MEMFS/IDBFS virtual FS.
 * Android/iOS: app-sandbox paths. */
int misa_platform_fs_path(const char *path, char *out, size_t outcap);

/* Called after a successful fs.write with the mapped path (lets the
 * platform persist async — Emscripten syncs MEMFS -> IDBFS). */
void misa_platform_fs_after_write(const char *mapped_path);

/* Web-only (platform_emscripten.c): mount /misa_fs on IDBFS and
 * populate it from the browser's IndexedDB. Blocking (Asyncify).
 * Exported to JS; the shell calls it before misa_ui_boot. */
void misa_platform_fs_bootstrap(void);

/* ------------------------------------------------------------------ */
/* HTTP transport (blocking POST of a JSON body)                      */
/* ------------------------------------------------------------------ */

typedef struct {
    long   status;      /* HTTP status */
    char  *body;        /* response bytes (owned by the struct) */
    size_t len;
    char   err[256];    /* transport error, "" on HTTP completion */
} MisaHttpResp;

/* Blocking POST. Returns 1 on transport success (any HTTP status),
 * 0 on transport failure (err set). The response body is valid until
 * misa_platform_http_free(). content_type is sent as the Content-Type
 * header (replacing any caller-supplied one). */
int misa_platform_http_post(const char *url, const char *content_type,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out);

/* Blocking GET. Returns 1 on HTTP completion (any status), 0 on
 * transport failure (err set). Same body ownership as http_post. */
int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out);

/* Release a response (frees body). Safe on a zeroed struct. */
void misa_platform_http_free(MisaHttpResp *r);

#ifdef __cplusplus
}
#endif
#endif /* MISA_PLATFORM_H */
