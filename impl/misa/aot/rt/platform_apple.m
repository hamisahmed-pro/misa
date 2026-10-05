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

#import <Foundation/Foundation.h>
#import <os/log.h>
#import <UIKit/UIKit.h>   /* only for the fatal-alert path */

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

int misa_platform_http_post(const char *url,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out) {
    NSMutableURLRequest *req = [NSMutableURLRequest
        requestWithURL:[NSURL URLWithString:
            [NSString stringWithUTF8String:url]]];
    req.HTTPMethod = @"POST";
    [req setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
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
