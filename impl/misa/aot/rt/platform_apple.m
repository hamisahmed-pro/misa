/* Misa AOT runtime — iOS platform implementation (Phase 4, M7).
 *
 * Implements rt/misa_platform.h on iOS:
 *   P1  logging ......... os_log (unified log; visible in Console.app)
 *   P2  fatal ........... os_log_fault + best-effort alert, then abort
 *                         (unrecoverable by definition; the alert is the
 *                         "do not kill blindly" part)
 *   P3  environment ..... NSUserDefaults / Info.plist table (first ship);
 *                         move provider keys to the Keychain before any
 *                         TestFlight / App Store build. Keys are NEVER
 *                         baked into the binary.
 *   P4  filesystem ...... app-sandbox Documents directory; escapes
 *                         above the sandbox root are rejected
 *   HTTP transport ...... NSURLSession, blocking shape identical to
 *                         platform_linux.c's curl backend (semaphore +
 *                         watchdog). misa_model.c's retry + budget logic
 *                         runs unchanged on top.
 *
 * Compile (on the Mac): see docs/ios-cross.md §4.2 (-fobjc-arc).
 */
#import "misa_platform.h"
#include "misa_rt.h"

#import <Foundation/Foundation.h>
#import <os/log.h>
#import <UIKit/UIKit.h>   /* only for the fatal-alert path */
#import <CoreML/CoreML.h> /* on-device inference (Phase 5, M3) */

#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

/* ---------------- P1: logging ---------------- */

void misa_platform_log(const char *msg, size_t len) {
    os_log_info(OS_LOG_DEFAULT, "misa: %{public}.*s", (int)len, msg);
}

void misa_platform_log_err(const char *msg, size_t len) {
    os_log_error(OS_LOG_DEFAULT, "misa err: %{public}.*s", (int)len, msg);
}

void misa_platform_flush(void) { /* os_log is unbuffered */ }

/* ---------------- P2: fatal ---------------- */
/* Never returns. Presents (best effort) instead of killing blindly. */

void misa_platform_fatal(const char *code, const char *msg) {
    os_log_fault(OS_LOG_DEFAULT, "misa fatal %{public}s: %{public}s",
                 code ? code : "?", msg ? msg : "?");
    /* Present a visible failure on the main thread, then abort.
     * Alert presentation requires a visible view controller; the
     * UIKit driver records the key window at boot. */
    dispatch_async(dispatch_get_main_queue(), ^{
        UIAlertController *a = [UIAlertController
            alertControllerWithTitle:@"Misa runtime error"
            message:[NSString stringWithFormat:@"%s: %s",
                     code ? code : "?", msg ? msg : "?"]
            preferredStyle:UIAlertControllerStyleAlert];
        [a addAction:[UIAlertAction actionWithTitle:@"Close"
                     style:UIAlertActionStyleDefault handler:nil]];
        UIWindow *w = [UIApplication sharedApplication].keyWindow;
        [w.rootViewController presentViewController:a
            animated:YES completion:nil];
    });
    abort();
}

/* ---------------- P3: environment ---------------- */
/* First phone ship: read from the app's Info.plist / NSUserDefaults
 * table populated at install time (e.g. a Settings bundle or a
 * first-run config screen). Production: move provider keys to the
 * Keychain. */

const char *misa_platform_getenv(const char *name) {
    static char buf[4096];
    NSString *k = [NSString stringWithUTF8String:name];
    NSString *v = [[NSUserDefaults standardUserDefaults] stringForKey:k];
    if (!v) v = [[NSBundle mainBundle].infoDictionary objectForKey:k];
    if (!v) return NULL;
    const char *c = [v UTF8String];
    size_t n = strlen(c);
    if (n >= sizeof buf) return NULL;
    memcpy(buf, c, n + 1);
    return buf;
}

/* ---------------- P4: filesystem root ---------------- */
/* App-sandbox mapping: "app-relative" paths land in Documents. */

int misa_platform_fs_path(const char *path, char *out, size_t outcap) {
    NSArray *dirs = NSSearchPathForDirectoriesInDomains(
        NSDocumentDirectory, NSUserDomainMask, YES);
    NSString *root = dirs.firstObject;
    NSString *full = [root stringByAppendingPathComponent:
        [NSString stringWithUTF8String:path]];
    /* Reject escapes above the sandbox root. */
    NSString *std = [full stringByStandardizingPath];
    if (![std hasPrefix:[root stringByStandardizingPath]]) return 0;
    const char *c = [std fileSystemRepresentation];
    size_t n = strlen(c) + 1;
    if (n > outcap || outcap == 0) return 0;
    memcpy(out, c, n);
    return 1;
}

void misa_platform_fs_after_write(const char *mapped_path) {
    (void)mapped_path; /* writes are durable in Documents */
}

/* ---------------- HTTP transport (NSURLSession) ---------------- */

static int misa_http_run(NSURLRequest *req, double timeout_s,
                         MisaHttpResp *out) {
    memset(out, 0, sizeof *out);
    NSURLSessionConfiguration *cfg =
        [NSURLSessionConfiguration defaultSessionConfiguration];
    cfg.timeoutIntervalForRequest = timeout_s > 0 ? timeout_s : 60.0;
    cfg.timeoutIntervalForResource = cfg.timeoutIntervalForRequest * 3;
    NSURLSession *sess = [NSURLSession sessionWithConfiguration:cfg];

    __block NSData *rdata = nil;
    __block NSHTTPURLResponse *rresp = nil;
    __block NSError *rerr = nil;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    NSURLSessionDataTask *task =
        [sess dataTaskWithRequest:req
               completionHandler:^(NSData *d, NSURLResponse *r, NSError *e) {
            rdata = d; rresp = (NSHTTPURLResponse *)r; rerr = e;
            dispatch_semaphore_signal(sem);
        }];
    [task resume];

    /* Watchdog: never block the runtime past ~3x the budget. */
    dispatch_time_t deadline = dispatch_time(DISPATCH_TIME_NOW,
        (int64_t)((cfg.timeoutIntervalForResource + 5.0) * NSEC_PER_SEC));
    long w = dispatch_semaphore_wait(sem, deadline);
    [sess invalidateAndCancel];
    if (w != 0 || rerr) {
        snprintf(out->err, sizeof out->err, "network error: %s",
                 rerr ? [[rerr localizedDescription] UTF8String]
                      : "timeout waiting for response");
        return 0;
    }
    out->status = (long)rresp.statusCode;
    size_t n = rdata.length;
    char *buf = n ? (char *)malloc(n + 1) : NULL;
    if (n && !buf) {
        snprintf(out->err, sizeof out->err, "out of memory");
        return 0;
    }
    if (n) { memcpy(buf, rdata.bytes, n); buf[n] = '\0'; }
    out->body = buf;   /* may be NULL when the body was empty */
    out->len = n;
    out->err[0] = '\0';
    return 1;
}

int misa_platform_http_post(const char *url, const char *content_type,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    NSMutableURLRequest *req = [NSMutableURLRequest
        requestWithURL:[NSURL URLWithString:
            [NSString stringWithUTF8String:url]]];
    req.HTTPMethod = @"POST";
    /* C-4: use the caller's content_type (was hardcoded to
     * application/json, and the param was missing entirely -> ABI
     * mismatch with misa_platform.h's 7-param declaration). */
    [req setValue:[NSString stringWithUTF8String:content_type]
        forHTTPHeaderField:@"Content-Type"];
    [req setValue:@"misa/0.3" forHTTPHeaderField:@"User-Agent"];
    for (int i = 0; i < nheaders; i++) {
        NSString *h = [NSString stringWithUTF8String:headers[i]];
        NSRange c = [h rangeOfString:@":"];
        if (c.location != NSNotFound) {
            NSString *k = [[h substringToIndex:c.location]
                stringByTrimmingCharactersInSet:
                [NSCharacterSet whitespaceCharacterSet]];
            NSString *v = [[h substringFromIndex:c.location + 1]
                stringByTrimmingCharactersInSet:
                [NSCharacterSet whitespaceCharacterSet]];
            [req setValue:v forHTTPHeaderField:k];
        }
    }
    const char *b = body ? body : "";
    req.HTTPBody = [NSData dataWithBytes:b length:strlen(b)];
    return misa_http_run(req, timeout_s, out);
}

int misa_platform_http_get(const char *url, double timeout_s,
                           MisaHttpResp *out) {
    NSMutableURLRequest *req = [NSMutableURLRequest
        requestWithURL:[NSURL URLWithString:
            [NSString stringWithUTF8String:url]]];
    req.HTTPMethod = @"GET";
    [req setValue:@"misa/0.3" forHTTPHeaderField:@"User-Agent"];
    return misa_http_run(req, timeout_s, out);
}

void misa_platform_http_free(MisaHttpResp *r) {
    if (r) { free(r->body); memset(r, 0, sizeof *r); }
}

/* ---------------- CoreML on-device backend (Phase 5, M3) ---------------- */
/* Implements misa_coreml_infer (declared in misa_rt.h) for `.mlpackage`
 * / `.mlmodelc` models. Mirrors the v1 type mapping of the ORT backend
 * (M2): Float -> scalar, [Float] -> 1-D float32, Text -> string; the
 * first model output is serialized to JSON so the existing schema
 * validation in misa_model_call applies unchanged.
 *
 * Input binding: by declared param name when it matches a model input
 * name, else positionally over the model's input names sorted
 * alphabetically (CoreML addresses inputs by name, so a pure index
 * would be fragile; this degrades to positional when names align).
 *
 * The model is loaded lazily on first call and cached per process.
 * A .mlpackage is compiled by CoreML on first load (one-time cost,
 * seconds); .mlmodelc loads directly.
 *
 * Model lookup order: (1) the embedded path as given, (2) the app
 * bundle's resources (drop the .mlpackage into the Xcode target's
 * "Copy Bundle Resources", or ios/MisaApp/model/ and add it to the
 * target), (3) the Documents directory, (4) as given (load then fails
 * with a clear error).
 *
 * Returns 1 on success (*out_json is misa_alloc'd JSON), 0 on an E50x
 * failure (ecode set, errbuf has the message), -1 on load/inference
 * failure (errbuf has the message; the caller raises E400).
 *
 * ARC/MRC: this file compiles both with -fobjc-arc (Xcode) and
 * without (the CI direct-clang check); the cache store below retains
 * explicitly when ARC is off. */

static MLModel *g_coreml_model = nil;
static char g_coreml_path[1024] = {0};
static NSObject *g_coreml_lock = nil;
static dispatch_once_t g_coreml_lock_once;

static NSObject *coreml_lock(void) {
    dispatch_once(&g_coreml_lock_once, ^{
        g_coreml_lock = [NSObject new];
    });
    return g_coreml_lock;
}

/* Forward: defined after misa_coreml_infer. */
static double coreml_multi_get(MLMultiArray *a, NSInteger k,
                               char *errbuf, size_t errcap,
                               char *ecode, const char *oname);

static void coreml_fail(char *ecode, const char *code,
                        char *errbuf, size_t errcap,
                        const char *fmt, ...) {
    va_list ap;
    if (ecode && code) snprintf(ecode, 8, "%s", code);
    va_start(ap, fmt);
    vsnprintf(errbuf, errcap, fmt, ap);
    va_end(ap);
}

static NSString *coreml_resolve_path(const char *given) {
    NSFileManager *fm = [NSFileManager defaultManager];
    NSString *g = [NSString stringWithUTF8String:given ? given : ""];
    if ([fm fileExistsAtPath:g]) return g;
    NSString *base = [g lastPathComponent];
    NSString *ext = [base pathExtension];
    NSString *name = [base stringByDeletingPathExtension];
    NSString *r = [[NSBundle mainBundle] pathForResource:name ofType:ext];
    if (r) return r;
    NSArray *dirs = NSSearchPathForDirectoriesInDomains(
        NSDocumentDirectory, NSUserDomainMask, YES);
    NSString *d = [[dirs firstObject]
        stringByAppendingPathComponent:base];
    if ([fm fileExistsAtPath:d]) return d;
    return g;
}

/* v1 param-type classification, mirroring ort_sig_kind: "scalar" for
 * Float, "vec" for [Float], "text" for Text, NULL when unsupported. */
static const char *coreml_sig_kind(const char *t) {
    while (t && *t == ' ') t++;
    if (!t || !*t) return NULL;
    if (strncmp(t, "[Float]", 7) == 0) return "vec";
    if (strncmp(t, "Float", 5) == 0) return "scalar";
    if (strncmp(t, "Text", 4) == 0) return "text";
    return NULL;
}

static void coreml_json_escape(NSMutableString *js, const char *s) {
    [js appendString:@"\""];
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        switch (*p) {
            case '"':  [js appendString:@"\\\""]; break;
            case '\\': [js appendString:@"\\\\"]; break;
            case '\n': [js appendString:@"\\n"]; break;
            case '\r': [js appendString:@"\\r"]; break;
            case '\t': [js appendString:@"\\t"]; break;
            default:
                if (*p < 0x20) [js appendFormat:@"\\u%04x", *p];
                else [js appendFormat:@"%c", *p];
                break;
        }
    }
    [js appendString:@"\""];
}

int misa_coreml_infer(const MModelMeta *meta, MVal **args, int nargs,
                      char **out_json, char *ecode,
                      char *errbuf, size_t errcap) {
    if (ecode) ecode[0] = '\0';
    if (!meta || !meta->model_file || !*meta->model_file) {
        coreml_fail(ecode, "E502", errbuf, errcap,
                    "on-device provider needs `model: file(\"...\")`");
        return 0;
    }

    @synchronized (coreml_lock()) {
    NSString *path = coreml_resolve_path(meta->model_file);
    const char *cpath = [path fileSystemRepresentation];
    if (!g_coreml_model || strcmp(g_coreml_path, cpath) != 0) {
        NSError *err = nil;
        MLModel *m = [MLModel modelWithContentsOfURL:
            [NSURL fileURLWithPath:path] error:&err];
        if (!m) {
            coreml_fail(NULL, NULL, errbuf, errcap,
                        "cannot load CoreML model at '%s': %s",
                        cpath, err ? [[err localizedDescription] UTF8String]
                                   : "unknown error");
            return -1;
        }
#if __has_feature(objc_arc)
        g_coreml_model = m;
#else
        [g_coreml_model release];
        g_coreml_model = [m retain];
#endif
        snprintf(g_coreml_path, sizeof g_coreml_path, "%s", cpath);
    }
    MLModel *model = g_coreml_model;

    MLModelDescription *desc = model.modelDescription;
    NSDictionary<NSString *, MLFeatureDescription *> *inDescs =
        desc.inputDescriptionsByName;
    if ((NSInteger)inDescs.count != (NSInteger)nargs) {
        coreml_fail(ecode, "E502", errbuf, errcap,
                    "model takes %lu input(s), called with %d arg(s)",
                    (unsigned long)inDescs.count, nargs);
        return 0;
    }
    NSArray<NSString *> *sortedIns =
        [[inDescs allKeys] sortedArrayUsingSelector:@selector(compare:)];
    NSArray<NSString *> *sortedOuts =
        [[desc.outputDescriptionsByName allKeys]
            sortedArrayUsingSelector:@selector(compare:)];
    if (sortedOuts.count < 1) {
        coreml_fail(ecode, "E502", errbuf, errcap,
                    "model has no outputs");
        return 0;
    }

    NSMutableDictionary<NSString *, MLFeatureValue *> *fdict =
        [NSMutableDictionary dictionary];
    for (int i = 0; i < nargs; i++) {
        const char *pname = meta->params[i].name;
        const char *ptype = meta->params[i].type;
        const char *kind = coreml_sig_kind(ptype ? ptype : "");
        MVal *av = args[i];
        NSString *key = nil;
        MLFeatureDescription *fd = nil;
        NSError *err = nil;

        if (!kind) {
            coreml_fail(ecode, "E502", errbuf, errcap,
                        "param '%s': type '%s' is not supported "
                        "on-device in v1 (Float, [Float], Text)",
                        pname, ptype ? ptype : "?");
            return 0;
        }
        if (pname) {
            NSString *pn = [NSString stringWithUTF8String:pname];
            if (inDescs[pn]) key = pn;
        }
        if (!key) key = sortedIns[i];
        fd = inDescs[key];

        if (strcmp(kind, "text") == 0) {
            const char *tz;
            if (misa_tag(av) != M_TAG_TEXT) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "param '%s': Text value required", pname);
                return 0;
            }
            if (fd.type != MLFeatureTypeString) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "param '%s': model input '%s' is not a string "
                            "(v1 supports string inputs only for Text)",
                            pname, [key UTF8String]);
                return 0;
            }
            tz = misa_text_z(av);
            fdict[key] = [MLFeatureValue featureValueWithString:
                [NSString stringWithUTF8String:tz ? tz : ""]];
            continue;
        }
        /* numeric kinds */
        if (misa_tag(av) == M_TAG_FLOAT && strcmp(kind, "scalar") == 0 &&
            (fd.type == MLFeatureTypeDouble ||
             fd.type == MLFeatureTypeInt64)) {
            /* Natural CoreML scalar inputs (coremltools often emits
             * these); the multiarray path below covers the rest. */
            double dv = misa_as_float(av);
            fdict[key] = fd.type == MLFeatureTypeDouble
                ? [MLFeatureValue featureValueWithDouble:dv]
                : [MLFeatureValue featureValueWithInt64:(int64_t)dv];
            continue;
        }
        {
            int is_vec = strcmp(kind, "vec") == 0;
            MLMultiArrayConstraint *mc = fd.multiArrayConstraint;
            NSArray<NSNumber *> *shape = mc ? mc.shape : nil;
            int64_t want = -1;
            int64_t len = 0;
            float *fp = NULL;
            MLMultiArray *arr = nil;
            if (fd.type != MLFeatureTypeMultiArray) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "param '%s': model input '%s' is not a float "
                            "multi-array (v1 supports float32 multi-array "
                            "inputs only)", pname, [key UTF8String]);
                return 0;
            }
            if (mc.dataType != MLMultiArrayDataTypeFloat32) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "param '%s': model input '%s' is not float32 "
                            "(v1 supports float32 only)",
                            pname, [key UTF8String]);
                return 0;
            }
            if (is_vec) {
                if (misa_tag(av) != M_TAG_LIST) {
                    coreml_fail(ecode, "E502", errbuf, errcap,
                                "param '%s': [Float] value required", pname);
                    return 0;
                }
                if (shape.count != 1) {
                    coreml_fail(ecode, "E502", errbuf, errcap,
                                "param '%s': model input '%s' is not 1-D "
                                "(v1 supports 1-D inputs only)",
                                pname, [key UTF8String]);
                    return 0;
                }
                len = misa_list_len(av);
                want = [shape[0] longLongValue];
                if (want >= 0 && want != len) {
                    coreml_fail(ecode, "E501", errbuf, errcap,
                                "param '%s': model input '%s' expects %lld "
                                "values, got %lld", pname, [key UTF8String],
                                (long long)want, (long long)len);
                    return 0;
                }
            } else {
                if (misa_tag(av) != M_TAG_FLOAT) {
                    coreml_fail(ecode, "E502", errbuf, errcap,
                                "param '%s': Float value required", pname);
                    return 0;
                }
                if (shape.count != 1 ||
                    [shape[0] longLongValue] != 1) {
                    coreml_fail(ecode, "E502", errbuf, errcap,
                                "param '%s': model input '%s' is not a "
                                "scalar-shaped [1] multi-array",
                                pname, [key UTF8String]);
                    return 0;
                }
                len = 1;
            }
            arr = [[MLMultiArray alloc] initWithShape:@[@(len)]
                                            dataType:MLMultiArrayDataTypeFloat32
                                               error:&err];
            if (!arr) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "param '%s': cannot allocate input array: %s",
                            pname, err ? [[err localizedDescription] UTF8String]
                                       : "unknown error");
                return 0;
            }
            fp = (float *)arr.dataPointer;
            for (int64_t k = 0; k < len; k++) {
                MVal *e = is_vec ? misa_list_get(av, k) : av;
                if (misa_tag(e) != M_TAG_FLOAT) {
                    coreml_fail(ecode, "E502", errbuf, errcap,
                                "param '%s': [Float] value required", pname);
#if !__has_feature(objc_arc)
                    [arr release];
#endif
                    return 0;
                }
                fp[k] = (float)misa_as_float(e);
            }
            fdict[key] = [MLFeatureValue featureValueWithMultiArray:arr];
#if !__has_feature(objc_arc)
            [arr release];
#endif
        }
    }

    /* run: first output only (mirrors M1/M2) */
    {
        NSError *err = nil;
        MLFeatureValue *fv = nil;
        NSString *oname = sortedOuts[0];
        const char *schema = meta->schema ? meta->schema : "";
        const char *ret_kind = "vec";
        id<MLFeatureProvider> outFeatures = nil;
        NSMutableString *js = nil;
        const char *utf8 = NULL;
        size_t n = 0;
        char *out = NULL;

        while (*schema == ' ') schema++;
        if (strncmp(schema, "[Float]", 7) == 0) ret_kind = "vec";
        else if (strncmp(schema, "Float", 5) == 0) ret_kind = "scalar";
        else if (strncmp(schema, "Text", 4) == 0) ret_kind = "text";
        else {
            coreml_fail(ecode, "E502", errbuf, errcap,
                        "return type '%s' is not supported on-device in "
                        "v1 (Float, [Float], Text)", schema);
            return 0;
        }

        id<MLFeatureProvider> inProvider =
            [[MLDictionaryFeatureProvider alloc]
                initWithDictionary:fdict error:&err];
        if (!inProvider) {
            coreml_fail(NULL, NULL, errbuf, errcap,
                        "cannot build model input: %s",
                        err ? [[err localizedDescription] UTF8String]
                            : "unknown error");
            return -1;
        }
        outFeatures = [model predictionFromFeatures:inProvider error:&err];
#if !__has_feature(objc_arc)
        [inProvider release];
#endif
        if (!outFeatures) {
            coreml_fail(NULL, NULL, errbuf, errcap,
                        "model inference failed: %s",
                        err ? [[err localizedDescription] UTF8String]
                            : "unknown error");
            return -1;
        }
        fv = [outFeatures featureValueForName:oname];
        if (!fv) {
            coreml_fail(NULL, NULL, errbuf, errcap,
                        "model returned no value for output '%s'",
                        [oname UTF8String]);
            return -1;
        }

        js = [NSMutableString string];
        if (fv.type == MLFeatureTypeMultiArray) {
            MLMultiArray *a = fv.multiArrayValue;
            NSInteger count = a.count;
            if (strcmp(ret_kind, "text") == 0) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "model output is numeric but the return type "
                            "is Text");
                return 0;
            }
            if (strcmp(ret_kind, "scalar") == 0) {
                double v0 = coreml_multi_get(a, 0, errbuf, errcap,
                                            ecode, [oname UTF8String]);
                if (ecode[0]) return 0;
                [js appendFormat:@"%.17g", v0];
            } else {
                [js appendString:@"["];
                for (NSInteger k = 0; k < count; k++) {
                    double v = coreml_multi_get(a, k, errbuf, errcap,
                                               ecode, [oname UTF8String]);
                    if (ecode[0]) return 0;
                    if (k) [js appendString:@","];
                    [js appendFormat:@"%.17g", v];
                }
                [js appendString:@"]"];
            }
        } else if (fv.type == MLFeatureTypeDouble) {
            if (strcmp(ret_kind, "text") == 0) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "model output is numeric but the return type "
                            "is Text");
                return 0;
            }
            if (strcmp(ret_kind, "scalar") == 0)
                [js appendFormat:@"%.17g", fv.doubleValue];
            else
                [js appendFormat:@"[%.17g]", fv.doubleValue];
        } else if (fv.type == MLFeatureTypeInt64) {
            if (strcmp(ret_kind, "text") == 0) {
                coreml_fail(ecode, "E502", errbuf, errcap,
                            "model output is numeric but the return type "
                            "is Text");
                return 0;
            }
            if (strcmp(ret_kind, "scalar") == 0)
                [js appendFormat:@"%lld", (long long)fv.int64Value];
            else
                [js appendFormat:@"[%lld]", (long long)fv.int64Value];
        } else if (fv.type == MLFeatureTypeString) {
            const char *sv = [fv.stringValue UTF8String];
            coreml_json_escape(js, sv ? sv : "");
        } else {
            coreml_fail(ecode, "E502", errbuf, errcap,
                        "model output '%s' has unsupported type (v1: "
                        "numeric multi-array, double, int64, string)",
                        [oname UTF8String]);
            return 0;
        }

        utf8 = [js UTF8String];
        n = strlen(utf8);
        out = (char *)misa_alloc(n + 1);
        memcpy(out, utf8, n + 1);
        *out_json = out;
        return 1;
    }
    } /* @synchronized */
}

/* Read element k of a numeric multi-array as double. Sets ecode[0]
 * and returns 0.0 on unsupported data types. */
static double coreml_multi_get(MLMultiArray *a, NSInteger k,
                               char *errbuf, size_t errcap,
                               char *ecode, const char *oname) {
    switch (a.dataType) {
        case MLMultiArrayDataTypeFloat32:
            return (double)((const float *)a.dataPointer)[k];
        case MLMultiArrayDataTypeDouble:
            return ((const double *)a.dataPointer)[k];
        case MLMultiArrayDataTypeInt32:
            return (double)((const int32_t *)a.dataPointer)[k];
        default:
            break;
    }
    if (ecode) snprintf(ecode, 8, "E502");
    if (errbuf) snprintf(errbuf, errcap,
        "model output '%s' is not float32/float64/int32 (v1)", oname);
    return 0.0;
}
