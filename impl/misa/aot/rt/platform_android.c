/* Misa AOT runtime — Android platform implementation (Phase 4, M6).
 *
 *   P1 log   -> __android_log_print (logcat, tag "MISA"); long writes
 *               are chunked at 1 KB (logcat truncates long lines).
 *   P2 fatal -> logcat + toast + activity.finish() via the Java side.
 *               NEVER exit(): killing the process is wrong on mobile.
 *               The faulting (Misa) thread parks forever afterwards so
 *               the "never returns" contract holds while the activity
 *               tears down gracefully.
 *   P3 getenv -> app-supplied config table, set through JNI by
 *               MisaActivity BEFORE boot (intent extras /
 *               SharedPreferences — see docs/android.md). Provider keys
 *               are NEVER baked into the native library.
 *   P4 fs root -> the app sandbox: relative paths map under
 *               <filesDir>/misa_fs (created on demand); absolute paths
 *               pass through (preloaded assets, camera captures).
 *
 * HTTP: blocking GET/POST via JNI -> java.net.HttpURLConnection
 * (doc §1.4: the Android backend; OkHttp is a possible later swap).
 * Runs on the Misa thread — never the UI thread — so there is no
 * NetworkOnMainThreadException. Any Java exception becomes a
 * transport failure (err set), which the model layer turns into the
 * graceful catchable E401/E304 path. Response bodies are plain
 * malloc'd buffers, freed by misa_platform_http_free (never GC
 * pointers: the body outlives no GC root discipline).
 */
#include "misa_platform.h"

#include "jni_android.h"
#include "misa_rt.h"   /* MVal/MModelMeta for misa_tflite_infer */

#include <android/log.h>
#include <jni.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define LOG_TAG "MISA"

/* ------------------------------------------------------------------ */
/* P1: logging                                                        */
/* ------------------------------------------------------------------ */

void misa_platform_log(const char *msg, size_t len) {
    while (len > 0) {
        size_t n = len > 1024 ? 1024 : len;
        __android_log_print(ANDROID_LOG_INFO, LOG_TAG, "%.*s",
                            (int)n, msg);
        msg += n;
        len -= n;
    }
}

void misa_platform_log_err(const char *msg, size_t len) {
    while (len > 0) {
        size_t n = len > 1024 ? 1024 : len;
        __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, "%.*s",
                            (int)n, msg);
        msg += n;
        len -= n;
    }
}

void misa_platform_flush(void) {
    /* logcat needs no flush */
}

/* ------------------------------------------------------------------ */
/* P2: fatal errors (never kills the process)                         */
/* ------------------------------------------------------------------ */

void misa_platform_fatal(const char *code, const char *msg) {
    char buf[1024];
    snprintf(buf, sizeof buf, "Misa fatal %s: %s",
             code ? code : "E300", msg ? msg : "error");
    __android_log_print(ANDROID_LOG_FATAL, LOG_TAG, "%s", buf);
    /* Toast + activity.finish() on the UI thread (Java side). */
    misa_android_fatal_ui(buf);
    /* The "never returns" contract without exit(): the activity is
     * finishing; park this thread forever. */
    for (;;)
        sleep(3600);
}

/* ------------------------------------------------------------------ */
/* P3: environment (app-supplied config via JNI)                      */
/* ------------------------------------------------------------------ */

const char *misa_platform_getenv(const char *name) {
    return misa_android_config_get(name);
}

/* ------------------------------------------------------------------ */
/* P4: filesystem root (app sandbox)                                  */
/* ------------------------------------------------------------------ */

static void mkdir_p(const char *path) {
    /* best-effort mkdir -p over the parent chain */
    char tmp[4096];
    size_t n = strlen(path);
    if (n == 0 || n >= sizeof tmp) return;
    strcpy(tmp, path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, 0777); /* EEXIST is fine */
            *p = '/';
        }
    }
    mkdir(tmp, 0777);
}

int misa_platform_fs_path(const char *path, char *out, size_t outcap) {
    char tmp[4096];
    if (!path || !out || outcap == 0) return 0;
    if (path[0] == '/') {
        snprintf(tmp, sizeof tmp, "%s", path);
    } else {
        const char *fd = misa_android_files_dir();
        if (!fd || !fd[0]) return 0; /* no sandbox yet: fail closed */
        snprintf(tmp, sizeof tmp, "%s/misa_fs/%s", fd, path);
        mkdir_p(tmp); /* ensure the parent chain exists */
        /* mkdir_p created the leaf too when the leaf has no trailing
         * component issue — harmless for files (mkdir on an existing
         * file path fails silently, fopen still works). */
    }
    if (strlen(tmp) + 1 > outcap) return 0;
    strcpy(out, tmp);
    return 1;
}

void misa_platform_fs_after_write(const char *mapped_path) {
    (void)mapped_path;
    /* App-sandbox files persist; nothing to sync. */
}

/* ------------------------------------------------------------------ */
/* HTTP transport: JNI -> java.net.HttpURLConnection                 */
/* ------------------------------------------------------------------ */

/* Drain a pending Java exception into out->err and clear it.
 * Returns 1 when an exception was pending. */
static int take_java_exc(JNIEnv *env, MisaHttpResp *out,
                         const char *what) {
    jthrowable t = (*env)->ExceptionOccurred(env);
    if (!t) return 0;
    (*env)->ExceptionClear(env);
    char detail[200] = "";
    jclass tc = (*env)->GetObjectClass(env, t);
    jmethodID getMessage = (*env)->GetMethodID(
        env, tc, "getMessage", "()Ljava/lang/String;");
    if (getMessage) {
        jstring jm = (jstring)(*env)->CallObjectMethod(env, t, getMessage);
        if (jm && !(*env)->ExceptionOccurred(env)) {
            const char *c = (*env)->GetStringUTFChars(env, jm, NULL);
            if (c) {
                strncpy(detail, c, sizeof detail - 1);
                (*env)->ReleaseStringUTFChars(env, jm, c);
            }
            (*env)->DeleteLocalRef(env, jm);
        }
        (*env)->ExceptionClear(env);
    }
    (*env)->DeleteLocalRef(env, tc);
    (*env)->DeleteLocalRef(env, t);
    snprintf(out->err, sizeof out->err, "%s: %s", what,
             detail[0] ? detail : "Java exception");
    return 1;
}

/* Read an InputStream fully into a malloc'd buffer. Returns NULL on
 * failure (err set via take_java_exc by the caller). */
static char *read_all(JNIEnv *env, jobject stream, size_t *len_out,
                      MisaHttpResp *out) {
    jclass isc = (*env)->FindClass(env, "java/io/InputStream");
    jmethodID read = (*env)->GetMethodID(env, isc, "read", "([B)I");
    jmethodID close = (*env)->GetMethodID(env, isc, "close", "()V");
    (*env)->DeleteLocalRef(env, isc);
    if (!read || !close) {
        snprintf(out->err, sizeof out->err,
                 "http: cannot resolve InputStream methods");
        return NULL;
    }
    jbyteArray chunk = (*env)->NewByteArray(env, 8192);
    if (!chunk) {
        snprintf(out->err, sizeof out->err, "http: out of memory");
        return NULL;
    }
    size_t cap = 8192, len = 0;
    char *buf = (char *)malloc(cap ? cap : 1);
    if (!buf) {
        (*env)->DeleteLocalRef(env, chunk);
        snprintf(out->err, sizeof out->err, "http: out of memory");
        return NULL;
    }
    for (;;) {
        jint n = (*env)->CallIntMethod(env, stream, read, chunk);
        if (take_java_exc(env, out, "http: read failed")) {
            free(buf);
            (*env)->DeleteLocalRef(env, chunk);
            return NULL;
        }
        if (n <= 0) break; /* 0 = no data (non-blocking n/a), -1 = EOF */
        if (len + (size_t)n > cap) {
            size_t ncap = cap * 2;
            while (ncap < len + (size_t)n) ncap *= 2;
            char *nb = (char *)realloc(buf, ncap);
            if (!nb) {
                snprintf(out->err, sizeof out->err,
                         "http: out of memory");
                free(buf);
                (*env)->DeleteLocalRef(env, chunk);
                return NULL;
            }
            buf = nb;
            cap = ncap;
        }
        (*env)->GetByteArrayRegion(env, chunk, 0, n, (jbyte *)(buf + len));
        len += (size_t)n;
    }
    (*env)->CallVoidMethod(env, stream, close);
    (*env)->ExceptionClear(env); /* close() failure is non-fatal here */
    (*env)->DeleteLocalRef(env, chunk);
    *len_out = len;
    return buf;
}

static int http_request(const char *method, const char *url,
                        const char *const *headers, int nheaders,
                        const char *body, double timeout_s,
                        MisaHttpResp *out) {
    memset(out, 0, sizeof *out);
    JNIEnv *env = misa_android_jnienv();
    if (!env) {
        snprintf(out->err, sizeof out->err, "http: no JNI env");
        return 0;
    }

    int ms = (int)(timeout_s * 1000.0);
    if (ms < 1) ms = 1;

    /* URL + connection */
    jclass urlCls = (*env)->FindClass(env, "java/net/URL");
    jmethodID urlNew = (*env)->GetMethodID(
        env, urlCls, "<init>", "(Ljava/lang/String;)V");
    jmethodID openConn = (*env)->GetMethodID(
        env, urlCls, "openConnection", "()Ljava/net/URLConnection;");
    jstring jurl = (*env)->NewStringUTF(env, url);
    jobject urlObj = (*env)->NewObject(env, urlCls, urlNew, jurl);
    (*env)->DeleteLocalRef(env, jurl);
    (*env)->DeleteLocalRef(env, urlCls);
    if (take_java_exc(env, out, "http: bad URL")) return 0;
    if (!urlObj) {
        snprintf(out->err, sizeof out->err, "http: bad URL");
        return 0;
    }
    jobject conn = (*env)->CallObjectMethod(env, urlObj, openConn);
    (*env)->DeleteLocalRef(env, urlObj);
    if (take_java_exc(env, out, "http: openConnection failed")) return 0;

    jclass connCls = (*env)->GetObjectClass(env, conn);
    /* HttpURLConnection method ids */
    jmethodID mSetMethod = (*env)->GetMethodID(
        env, connCls, "setRequestMethod", "(Ljava/lang/String;)V");
    jmethodID mSetProp = (*env)->GetMethodID(
        env, connCls, "setRequestProperty",
        "(Ljava/lang/String;Ljava/lang/String;)V");
    jmethodID mSetCT = (*env)->GetMethodID(
        env, connCls, "setConnectTimeout", "(I)V");
    jmethodID mSetRT = (*env)->GetMethodID(
        env, connCls, "setReadTimeout", "(I)V");
    jmethodID mSetDoOut = (*env)->GetMethodID(
        env, connCls, "setDoOutput", "(Z)V");
    jmethodID mSetFixed = (*env)->GetMethodID(
        env, connCls, "setFixedLengthStreamingMode", "(I)V");
    jmethodID mGetOut = (*env)->GetMethodID(
        env, connCls, "getOutputStream", "()Ljava/io/OutputStream;");
    jmethodID mGetCode = (*env)->GetMethodID(
        env, connCls, "getResponseCode", "()I");
    jmethodID mGetIn = (*env)->GetMethodID(
        env, connCls, "getInputStream", "()Ljava/io/InputStream;");
    jmethodID mGetErr = (*env)->GetMethodID(
        env, connCls, "getErrorStream", "()Ljava/io/InputStream;");
    jmethodID mDisconnect = (*env)->GetMethodID(
        env, connCls, "disconnect", "()V");
    if (!mSetMethod || !mSetProp || !mSetCT || !mSetRT || !mSetDoOut ||
        !mSetFixed || !mGetOut || !mGetCode || !mGetIn || !mGetErr ||
        !mDisconnect) {
        snprintf(out->err, sizeof out->err,
                 "http: cannot resolve HttpURLConnection methods");
        (*env)->DeleteLocalRef(env, connCls);
        (*env)->DeleteLocalRef(env, conn);
        return 0;
    }

    jstring jm = (*env)->NewStringUTF(env, method);
    (*env)->CallVoidMethod(env, conn, mSetMethod, jm);
    (*env)->DeleteLocalRef(env, jm);
    (*env)->CallVoidMethod(env, conn, mSetCT, (jint)ms);
    (*env)->CallVoidMethod(env, conn, mSetRT, (jint)ms);
    if (take_java_exc(env, out, "http: setup failed")) goto fail;

    /* Request headers ("Name: value" strings). */
    for (int i = 0; i < nheaders; i++) {
        const char *h = headers[i];
        const char *colon = strchr(h, ':');
        if (!colon) continue;
        size_t nlen = (size_t)(colon - h);
        char name[256];
        if (nlen >= sizeof name) continue;
        memcpy(name, h, nlen);
        name[nlen] = '\0';
        const char *val = colon + 1;
        while (*val == ' ') val++;
        jstring jn = (*env)->NewStringUTF(env, name);
        jstring jv = (*env)->NewStringUTF(env, val);
        (*env)->CallVoidMethod(env, conn, mSetProp, jn, jv);
        (*env)->DeleteLocalRef(env, jn);
        (*env)->DeleteLocalRef(env, jv);
        if (take_java_exc(env, out, "http: setRequestProperty failed"))
            goto fail;
    }

    int is_post = (strcmp(method, "POST") == 0);
    size_t body_len = (is_post && body) ? strlen(body) : 0;
    if (is_post) {
        (*env)->CallVoidMethod(env, conn, mSetDoOut, (jboolean)JNI_TRUE);
        (*env)->CallVoidMethod(env, conn, mSetFixed, (jint)body_len);
        if (take_java_exc(env, out, "http: POST setup failed")) goto fail;
        jobject os = (*env)->CallObjectMethod(env, conn, mGetOut);
        if (take_java_exc(env, out, "http: getOutputStream failed"))
            goto fail;
        jclass osc = (*env)->GetObjectClass(env, os);
        jmethodID mWrite = (*env)->GetMethodID(
            env, osc, "write", "([B)V");
        jmethodID mClose = (*env)->GetMethodID(
            env, osc, "close", "()V");
        (*env)->DeleteLocalRef(env, osc);
        if (body_len > 0 && mWrite) {
            jbyteArray jb = (*env)->NewByteArray(env, (jsize)body_len);
            (*env)->SetByteArrayRegion(env, jb, 0, (jsize)body_len,
                                       (const jbyte *)body);
            (*env)->CallVoidMethod(env, os, mWrite, jb);
            (*env)->DeleteLocalRef(env, jb);
        }
        if (mClose) (*env)->CallVoidMethod(env, os, mClose);
        (*env)->DeleteLocalRef(env, os);
        if (take_java_exc(env, out, "http: write failed")) goto fail;
    }

    /* Response. */
    jint code = (*env)->CallIntMethod(env, conn, mGetCode);
    if (take_java_exc(env, out, "http: getResponseCode failed")) goto fail;
    out->status = (long)code;
    jobject stream = NULL;
    if (code >= 200 && code < 400) {
        stream = (*env)->CallObjectMethod(env, conn, mGetIn);
        (*env)->ExceptionClear(env); /* null stream is fine */
    } else {
        stream = (*env)->CallObjectMethod(env, conn, mGetErr);
        (*env)->ExceptionClear(env); /* null stream is fine */
    }
    if (stream) {
        size_t rlen = 0;
        char *rbuf = read_all(env, stream, &rlen, out);
        (*env)->DeleteLocalRef(env, stream);
        if (!rbuf) goto fail; /* err already set */
        out->body = rbuf;
        out->len = rlen;
    }
    out->err[0] = '\0';
    (*env)->CallVoidMethod(env, conn, mDisconnect);
    (*env)->ExceptionClear(env);
    (*env)->DeleteLocalRef(env, connCls);
    (*env)->DeleteLocalRef(env, conn);
    return 1;

fail:
    (*env)->ExceptionClear(env);
    (*env)->CallVoidMethod(env, conn, mDisconnect);
    (*env)->ExceptionClear(env);
    (*env)->DeleteLocalRef(env, connCls);
    (*env)->DeleteLocalRef(env, conn);
    free(out->body);
    out->body = NULL;
    out->len = 0;
    return 0;
}

int misa_platform_http_post(const char *url, const char *content_type,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    /* Phase 5 (stdlib2): prepend the caller's Content-Type. */
    char ct[128];
    snprintf(ct, sizeof ct, "Content-Type: %s",
             content_type ? content_type : "application/json");
    const char *hdrs[17];
    int n = 0;
    hdrs[n++] = ct;
    for (int i = 0; i < nheaders && n < 17; i++)
        hdrs[n++] = headers[i];
    return http_request("POST", url, hdrs, n, body,
                        timeout_s, out);
}

int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out) {
    return http_request("GET", url, NULL, 0, NULL, timeout_s, out);
}

void misa_platform_http_free(MisaHttpResp *r) {
    if (r) {
        free(r->body); /* plain malloc'd, never a GC pointer */
        memset(r, 0, sizeof *r);
    }
}

/* ------------------------------------------------------------------ */
/* TFLite on-device backend (Phase 5, M4)                             */
/*                                                                    */
/* Inference itself runs in Java (org.tensorflow.lite.Interpreter     */
/* with the NNAPI delegate when available, CPU fallback otherwise) —  */
/* that is the officially supported Android TFLite path. This layer   */
/* validates the declared Misa types, marshals args to JSON, and       */
/* forwards to com.misa.app.MisaTflite.infer(assetName, inputJson).    */
/*                                                                    */
/* Java raises RuntimeException("E501: ...") / ("E502: ...") for      */
/* model-compat failures; anything else becomes E400.                 */
/* ------------------------------------------------------------------ */

static const char *tflite_sig_kind(const char *type) {
    /* mirrors the v1 mapping: Float -> scalar, [Float] -> vec,
     * Text -> string; anything else is E502. */
    if (!type) return NULL;
    while (*type == ' ') type++;
    if (strcmp(type, "Float") == 0) return "scalar";
    if (strcmp(type, "[Float]") == 0) return "vec";
    if (strcmp(type, "Text") == 0) return "text";
    return NULL;
}

/* Minimal growable buffer (local; misa_model.c's Buf is file-static). */
typedef struct { char *buf; size_t len, cap; } TBuf;

static void tbuf_reserve(TBuf *b, size_t n) {
    if (b->len + n + 1 > b->cap) {
        size_t nc = b->cap ? b->cap * 2 : 256;
        while (nc < b->len + n + 1) nc *= 2;
        char *nb = (char *)realloc(b->buf, nc);
        if (!nb) return; /* OOM: callers check via misa_platform_fatal */
        b->buf = nb;
        b->cap = nc;
    }
}

static void tbuf_putn(TBuf *b, const char *p, size_t n) {
    tbuf_reserve(b, n);
    if (!b->buf) misa_platform_fatal("E300", "out of memory");
    memcpy(b->buf + b->len, p, n);
    b->len += n;
    b->buf[b->len] = '\0';
}

static void tbuf_puts(TBuf *b, const char *p) { tbuf_putn(b, p, strlen(p)); }

static void tbuf_put_json_str(TBuf *b, const char *s, size_t n) {
    tbuf_putn(b, "\"", 1);
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"': tbuf_puts(b, "\\\""); break;
        case '\\': tbuf_puts(b, "\\\\"); break;
        case '\n': tbuf_puts(b, "\\n"); break;
        case '\r': tbuf_puts(b, "\\r"); break;
        case '\t': tbuf_puts(b, "\\t"); break;
        default:
            if (c < 0x20) {
                char tmp[8];
                snprintf(tmp, sizeof tmp, "\\u%04x", c);
                tbuf_puts(b, tmp);
            } else {
                tbuf_putn(b, (const char *)&s[i], 1);
            }
        }
    }
    tbuf_putn(b, "\"", 1);
}

/* Append one MVal arg as JSON. Returns 0 with ecode set on E502. */
static int tflite_arg_json(TBuf *b, MVal *av, const char *kind,
                           const char *pname, char *ecode,
                           char *errbuf, size_t errcap) {
    if (strcmp(kind, "text") == 0) {
        if (misa_tag(av) != M_TAG_TEXT) {
            snprintf(errbuf, errcap,
                     "E502: param '%s': Text value required", pname);
            snprintf(ecode, 8, "E502");
            return 0;
        }
        const char *tz = misa_text_z(av);
        tbuf_put_json_str(b, tz ? tz : "", tz ? strlen(tz) : 0);
        return 1;
    }
    if (strcmp(kind, "scalar") == 0) {
        if (misa_tag(av) != M_TAG_FLOAT) {
            snprintf(errbuf, errcap,
                     "E502: param '%s': Float value required", pname);
            snprintf(ecode, 8, "E502");
            return 0;
        }
        char tmp[64];
        snprintf(tmp, sizeof tmp, "%.17g", misa_as_float(av));
        tbuf_puts(b, tmp);
        return 1;
    }
    /* vec */
    if (misa_tag(av) != M_TAG_LIST) {
        snprintf(errbuf, errcap,
                 "E502: param '%s': [Float] value required", pname);
        snprintf(ecode, 8, "E502");
        return 0;
    }
    int64_t len = misa_list_len(av), k;
    tbuf_putn(b, "[", 1);
    for (k = 0; k < len; k++) {
        MVal *e = misa_list_get(av, k);
        if (misa_tag(e) != M_TAG_FLOAT) {
            snprintf(errbuf, errcap,
                     "E502: param '%s': [Float] value required", pname);
            snprintf(ecode, 8, "E502");
            return 0;
        }
        char tmp[64];
        if (k) tbuf_putn(b, ",", 1);
        snprintf(tmp, sizeof tmp, "%.17g", misa_as_float(e));
        tbuf_puts(b, tmp);
    }
    tbuf_putn(b, "]", 1);
    return 1;
}

int misa_tflite_infer(const MModelMeta *meta, MVal **args, int nargs,
                      char **out_json, char *ecode,
                      char *errbuf, size_t errcap) {
    if (ecode) ecode[0] = '\0';
    if (!meta || !meta->model_file || !*meta->model_file) {
        if (ecode) snprintf(ecode, 8, "E502");
        snprintf(errbuf, errcap,
                 "E502: on-device provider needs `model: file(\"...\")`");
        return 0;
    }
    /* declared return type must be a v1 on-device type */
    {
        const char *schema = meta->schema ? meta->schema : "";
        while (*schema == ' ') schema++;
        if (!(strncmp(schema, "[Float]", 7) == 0 ||
              strncmp(schema, "Float", 5) == 0 ||
              strncmp(schema, "Text", 4) == 0)) {
            if (ecode) snprintf(ecode, 8, "E502");
            snprintf(errbuf, errcap,
                     "E502: return type '%s' is not supported on-device "
                     "in v1 (Float, [Float], Text)", schema);
            return 0;
        }
    }
    if (nargs != meta->nparams) {
        if (ecode) snprintf(ecode, 8, "E502");
        snprintf(errbuf, errcap,
                 "E502: model takes %d param(s), called with %d arg(s)",
                 meta->nparams, nargs);
        return 0;
    }

    /* marshal args -> JSON array */
    TBuf jb = {NULL, 0, 0};
    tbuf_putn(&jb, "[", 1);
    for (int i = 0; i < nargs; i++) {
        const char *pname = meta->params[i].name;
        const char *kind = tflite_sig_kind(meta->params[i].type);
        char lecode[8] = "";
        if (!kind) {
            if (ecode) snprintf(ecode, 8, "E502");
            snprintf(errbuf, errcap,
                     "E502: param '%s': type '%s' is not supported "
                     "on-device in v1 (Float, [Float], Text)",
                     pname, meta->params[i].type
                         ? meta->params[i].type : "?");
            free(jb.buf);
            return 0;
        }
        if (i) tbuf_putn(&jb, ",", 1);
        if (!tflite_arg_json(&jb, args[i], kind, pname, lecode,
                             errbuf, errcap)) {
            if (ecode) snprintf(ecode, 8, "%s", lecode);
            free(jb.buf);
            return 0;
        }
    }
    tbuf_putn(&jb, "]", 1);

    /* asset name: basename of the embedded model path */
    const char *asset = strrchr(meta->model_file, '/');
    asset = asset ? asset + 1 : meta->model_file;

    JNIEnv *env = misa_android_jnienv();
    if (!env) {
        snprintf(errbuf, errcap, "TFLite: no JNI environment");
        free(jb.buf);
        return -1;
    }
    jclass cls = (*env)->FindClass(env, "com/misa/app/MisaTflite");
    if (!cls) {
        (*env)->ExceptionClear(env);
        snprintf(errbuf, errcap,
                 "TFLite: com.misa.app.MisaTflite not found in APK "
                 "(tensorflow-lite AAR missing?)");
        free(jb.buf);
        return -1;
    }
    jmethodID infer = (*env)->GetStaticMethodID(
        env, cls, "infer",
        "(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)"
        "Ljava/lang/String;");
    if (!infer) {
        (*env)->ExceptionClear(env);
        (*env)->DeleteLocalRef(env, cls);
        snprintf(errbuf, errcap, "TFLite: MisaTflite.infer not found");
        free(jb.buf);
        return -1;
    }
    /* declared param names, for E501 messages */
    TBuf nb = {NULL, 0, 0};
    tbuf_putn(&nb, "[", 1);
    for (int i = 0; i < nargs; i++) {
        if (i) tbuf_putn(&nb, ",", 1);
        tbuf_put_json_str(&nb, meta->params[i].name,
                          strlen(meta->params[i].name));
    }
    tbuf_putn(&nb, "]", 1);
    jstring jasset = (*env)->NewStringUTF(env, asset);
    jstring jinput = (*env)->NewStringUTF(env, jb.buf ? jb.buf : "[]");
    jstring jnames = (*env)->NewStringUTF(env, nb.buf ? nb.buf : "[]");
    free(jb.buf);
    free(nb.buf);
    jstring jout = NULL;
    if (jasset && jinput && jnames) {
        jout = (jstring)(*env)->CallStaticObjectMethod(env, cls, infer,
                                                       jasset, jinput,
                                                       jnames);
    }
    (*env)->DeleteLocalRef(env, jasset);
    (*env)->DeleteLocalRef(env, jinput);
    (*env)->DeleteLocalRef(env, jnames);
    (*env)->DeleteLocalRef(env, cls);

    /* Java exceptions: "E50x: ..." -> E-code; anything else -> E400 */
    jthrowable t = (*env)->ExceptionOccurred(env);
    if (t) {
        (*env)->ExceptionClear(env);
        char detail[512] = "";
        jclass tc = (*env)->GetObjectClass(env, t);
        jmethodID getMessage = (*env)->GetMethodID(
            env, tc, "getMessage", "()Ljava/lang/String;");
        if (getMessage) {
            jstring jm = (jstring)(*env)->CallObjectMethod(env, t,
                                                           getMessage);
            if (jm && !(*env)->ExceptionOccurred(env)) {
                const char *c = (*env)->GetStringUTFChars(env, jm, NULL);
                if (c) {
                    strncpy(detail, c, sizeof detail - 1);
                    (*env)->ReleaseStringUTFChars(env, jm, c);
                }
                (*env)->DeleteLocalRef(env, jm);
            }
            (*env)->ExceptionClear(env);
        }
        (*env)->DeleteLocalRef(env, tc);
        (*env)->DeleteLocalRef(env, t);
        if (!detail[0]) snprintf(detail, sizeof detail, "Java exception");
        if (detail[0] == 'E' && detail[1] == '5' && detail[2] == '0' &&
            detail[3] >= '1' && detail[3] <= '4' && detail[4] == ':') {
            if (ecode) { memcpy(ecode, detail, 4); ecode[4] = '\0'; }
            snprintf(errbuf, errcap, "%s", detail);
            return 0;
        }
        snprintf(errbuf, errcap, "TFLite inference failed: %s", detail);
        return -1;
    }
    if (!jout) {
        snprintf(errbuf, errcap,
                 "TFLite: MisaTflite.infer returned null");
        return -1;
    }
    const char *c = (*env)->GetStringUTFChars(env, jout, NULL);
    *out_json = strdup(c ? c : "");
    if (c) (*env)->ReleaseStringUTFChars(env, jout, c);
    (*env)->DeleteLocalRef(env, jout);
    if (!*out_json) {
        snprintf(errbuf, errcap, "TFLite: out of memory");
        return -1;
    }
    return 1;
}
