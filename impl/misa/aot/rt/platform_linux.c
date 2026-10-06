/* Misa AOT runtime — Linux platform implementation (Phase 4, M2).
 *
 * P1-P5 with today's (v0.3) behavior:
 *   P1 log  -> stdio (log) / stderr (log_err)
 *   P2 fatal -> stderr + exit(1)
 *   P3 getenv -> getenv()
 *   P4 fs root -> cwd (identity mapping)
 *   P5 UI   -> text driver (rt/ui_text.c, selected at link time)
 *
 * HTTP transport: libcurl when built with -DMISA_USE_CURL (moved
 * verbatim from misa_model.c's http_post_json); otherwise a stub that
 * reports the transport as unavailable, preserving the v0.3
 * graceful-degradation shape (model calls -> E401).
 */
#include "misa_platform.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef MISA_USE_CURL
#include <curl/curl.h>
#endif

/* ------------------------------------------------------------------ */
/* P1: logging                                                        */
/* ------------------------------------------------------------------ */

void misa_platform_log(const char *msg, size_t len) {
    if (len) fwrite(msg, 1, len, stdout);
}

void misa_platform_log_err(const char *msg, size_t len) {
    if (len) fwrite(msg, 1, len, stderr);
}

void misa_platform_flush(void) {
    fflush(stdout);
    fflush(stderr);
}

/* ------------------------------------------------------------------ */
/* P2: fatal errors                                                   */
/* ------------------------------------------------------------------ */

void misa_platform_fatal(const char *code, const char *msg) {
    fprintf(stderr, "<unknown>: %s: %s\n",
            code ? code : "E300", msg ? msg : "error");
    fflush(stderr);
    exit(1);
}

/* ------------------------------------------------------------------ */
/* P3: environment                                                    */
/* ------------------------------------------------------------------ */

const char *misa_platform_getenv(const char *name) {
    return getenv(name);
}

/* No persistence step on Linux (the write is already durable). */
void misa_platform_fs_after_write(const char *mapped_path) {
    (void)mapped_path;
}

/* ------------------------------------------------------------------ */
/* P4: filesystem root (identity: cwd-relative, v0.3 behavior)        */
/* ------------------------------------------------------------------ */

int misa_platform_fs_path(const char *path, char *out, size_t outcap) {
    size_t n = strlen(path) + 1;
    if (n > outcap || outcap == 0) return 0;
    memcpy(out, path, n);
    return 1;
}

/* ------------------------------------------------------------------ */
/* HTTP transport                                                     */
/* ------------------------------------------------------------------ */

#ifdef MISA_USE_CURL

typedef struct {
    char  *buf;
    size_t len, cap;
    int    oom;   /* set when growth failed; write callback aborts */
} CurlBuf;

static void curlbuf_putn(CurlBuf *b, const char *p, size_t n) {
    if (b->oom) return;
    if (b->len + n + 1 > b->cap) {
        size_t ncap = b->cap ? b->cap * 2 : 4096;
        while (ncap < b->len + n + 1) ncap *= 2;
        char *nb = (char *)realloc(b->buf, ncap);
        if (!nb) { b->oom = 1; return; }
        b->buf = nb;
        b->cap = ncap;
    }
    memcpy(b->buf + b->len, p, n);
    b->len += n;
    b->buf[b->len] = '\0';
}

static size_t curl_write(char *ptr, size_t size, size_t nmemb, void *ud) {
    CurlBuf *b = (CurlBuf *)ud;
    curlbuf_putn(b, ptr, size * nmemb);
    return b->oom ? 0 : size * nmemb; /* 0 aborts the transfer */
}

int misa_platform_http_post(const char *url, const char *content_type,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    memset(out, 0, sizeof *out);
    CURL *ch = curl_easy_init();
    if (!ch) {
        snprintf(out->err, sizeof out->err, "curl init failed");
        return 0;
    }
    struct curl_slist *hs = NULL;
    {
        /* Phase 5 (stdlib2): content type comes from the caller
         * (application/json for model calls, text/plain or
         * application/json for http.post). */
        char ct[128];
        snprintf(ct, sizeof ct, "Content-Type: %s",
                 content_type ? content_type : "application/json");
        hs = curl_slist_append(hs, ct);
    }
    for (int i = 0; i < nheaders; i++)
        hs = curl_slist_append(hs, headers[i]);
    CurlBuf b = {0};
    curl_easy_setopt(ch, CURLOPT_URL, url);
    curl_easy_setopt(ch, CURLOPT_POSTFIELDS, body ? body : "");
    curl_easy_setopt(ch, CURLOPT_HTTPHEADER, hs);
    curl_easy_setopt(ch, CURLOPT_WRITEFUNCTION, curl_write);
    curl_easy_setopt(ch, CURLOPT_WRITEDATA, &b);
    curl_easy_setopt(ch, CURLOPT_TIMEOUT_MS, (long)(timeout_s * 1000));
    curl_easy_setopt(ch, CURLOPT_USERAGENT, "misa/0.3");
    CURLcode rc = curl_easy_perform(ch);
    if (rc == CURLE_OK)
        curl_easy_getinfo(ch, CURLINFO_RESPONSE_CODE, &out->status);
    curl_slist_free_all(hs);
    curl_easy_cleanup(ch);
    if (rc != CURLE_OK) {
        snprintf(out->err, sizeof out->err, "network error: %s",
                 curl_easy_strerror(rc));
        free(b.buf);
        return 0;
    }
    out->body = b.buf; /* may be NULL when the body was empty */
    out->len = b.len;
    out->err[0] = '\0';
    return 1;
}

void misa_platform_http_free(MisaHttpResp *r) {
    if (r) {
        free(r->body);
        memset(r, 0, sizeof *r);
    }
}

/* Phase 4 (M3): http.get transport, moved here from misa_rt.c so the
 * web target can plug in emscripten_fetch behind the same shape. */
int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out) {
    memset(out, 0, sizeof *out);
    CURL *ch = curl_easy_init();
    if (!ch) {
        snprintf(out->err, sizeof out->err, "curl init failed");
        return 0;
    }
    CurlBuf b = {0};
    curl_easy_setopt(ch, CURLOPT_URL, url);
    curl_easy_setopt(ch, CURLOPT_WRITEFUNCTION, curl_write);
    curl_easy_setopt(ch, CURLOPT_WRITEDATA, &b);
    curl_easy_setopt(ch, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(ch, CURLOPT_TIMEOUT_MS, (long)(timeout_s * 1000));
    curl_easy_setopt(ch, CURLOPT_USERAGENT, "misa/0.3");
    CURLcode rc = curl_easy_perform(ch);
    if (rc == CURLE_OK)
        curl_easy_getinfo(ch, CURLINFO_RESPONSE_CODE, &out->status);
    curl_easy_cleanup(ch);
    if (rc != CURLE_OK || b.oom) {
        /* keep the v0.3 message shape: misa_rt.c prefixes
         * "http.get failed: " */
        snprintf(out->err, sizeof out->err, "%s",
                 b.oom ? "out of memory" : curl_easy_strerror(rc));
        free(b.buf);
        return 0;
    }
    out->body = b.buf; /* may be NULL when the body was empty */
    out->len = b.len;
    out->err[0] = '\0';
    return 1;
}

#else /* !MISA_USE_CURL: no transport on this build */

int misa_platform_http_post(const char *url, const char *content_type,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    (void)url; (void)content_type; (void)headers; (void)nheaders;
    (void)body; (void)timeout_s;
    memset(out, 0, sizeof *out);
    snprintf(out->err, sizeof out->err,
             "model calls need libcurl (rebuild runtime with -DMISA_USE_CURL)");
    return 0;
}

void misa_platform_http_free(MisaHttpResp *r) {
    if (r) memset(r, 0, sizeof *r);
}

int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out) {
    (void)url; (void)timeout_s;
    memset(out, 0, sizeof *out);
    snprintf(out->err, sizeof out->err,
             "http.get needs libcurl (rebuild runtime with -DMISA_USE_CURL)");
    return 0;
}

#endif /* MISA_USE_CURL */
