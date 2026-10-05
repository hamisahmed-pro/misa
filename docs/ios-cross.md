# Misa on iOS — cross-compile path (M7)

**Status: design + machine-verified cross objects. NOT a finished iOS build.**
This document is the M7 milestone of Phase 4 (`spec/PHASE4.md` §3 M7,
§7, §4.4). It makes the Mac final mile mechanical for whoever runs it
on Apple hardware.

**Read this first:** everything that can be proven on a Linux box is
proven in §2 below, with exact commands and bytes. Everything after
§3 was written on Linux against the real runtime headers
(`impl/misa/aot/rt/misa_platform.h`, `misa_ui.h`) — it has **not** been
compiled, linked, or run on any Apple device. §10 lists exactly what
still needs a Mac. Do not mistake this document for a shipped build.

Conventions: lines marked **[VERIFY ON THE MAC]** are claims I could
not test from Linux (no Xcode, no Apple SDK, no signing). They are
flagged rather than asserted.

---

## 1. The split: what happens here vs. what happens on a Mac

The Misa AOT pipeline splits cleanly at the object file:

| Step | Where | Done |
|------|-------|------|
| `.misa` → LLVM IR → relocatable object (`.o`) for an Apple triple | This Linux box (`misa build --target …`) | ✅ proven §2 |
| Compile the C runtime for `arm64-apple-ios` | Mac only (needs the iOS SDK) | ❌ |
| Link object + runtime, code-sign | Mac only (needs Xcode + Apple keys) | ❌ |
| Install on a phone (direct device install / TestFlight) | Mac + Hamis's Apple ID/team | ❌ |

Why the split is safe: the Phase 4 portability audit (§1 of
PHASE4.md) found the emitter produces portable IR (opaque `i8*`
value model, no inline asm, no target intrinsics, no C/LLVM struct
layout shared across the boundary). Retargeting is a driver +
runtime-linking change, not an emitter redesign. The two Apple
triples resolve to real LLVM targets inside llvmlite and emit real
Mach-O objects — verified below.

The iOS entry contract (from `spec/PHASE4.md` §1.5 B6 and §7):
the IR defines both `main()` and `misa_main()`; the iOS app's
`main.m` calls `misa_rt_init()` + `misa_main()` inside a
`UIApplicationMain` app — the emitted `main()` is unused on iOS
(see §5 for the exact mechanics).

---

## 2. Machine-verified evidence (this Linux box, 2026-10-05)

Environment: Linux x86_64, `llvmlite==0.50.0` (bundled LLVM 22.1),
`misa` at `impl/bin/misa`, program = `examples/hello.misa`.

### 2.1 `aarch64-apple-darwin` — IR

```
$ misa build --target aarch64-apple-darwin --emit-llvm arm64.ir examples/hello.misa
built hello.o
$ grep -E 'target triple|^define ' arm64.ir
target triple = "aarch64-apple-darwin"
define i8* @"misa_main"()
define i32 @"main"()
```

The module carries the Apple triple. `misa_main()` — the function
the iOS `main.m` will call — is defined. (Note: `--emit-llvm` dumps
the IR text *before* the driver attaches the data layout, so the
dumped `.ll` shows `target datalayout = ""`. This is a cosmetic
driver quirk, **not** an object bug — §2.3.)

### 2.2 `aarch64-apple-darwin` — Mach-O object

```
$ misa build --target aarch64-apple-darwin -o misa_arm64.o examples/hello.misa
built /tmp/misa_arm64.o
$ od -A x -t x1 -N 4 /tmp/misa_arm64.o
000000 cf fa ed fe
$ file /tmp/misa_arm64.o
/tmp/misa_arm64.o: Mach-O 64-bit arm64 object, flags:<|SUBSECTIONS_VIA_SYMBOLS>
```

First four bytes `cf fa ed fe` = `0xfeedfacf` (`MH_MAGIC_64`) stored
little-endian — a genuine 64-bit Mach-O relocatable object, and
`file(1)` identifies it as **arm64**.

### 2.3 `x86_64-apple-darwin` — IR + Mach-O object

```
$ misa build --target x86_64-apple-darwin --emit-llvm x64.ir examples/hello.misa
built hello.o
$ grep -E 'target triple|^define ' x64.ir
target triple = "x86_64-apple-darwin"
define i8* @"misa_main"()
define i32 @"main"()
$ misa build --target x86_64-apple-darwin -o misa_x64.o examples/hello.misa
built /tmp/misa_x64.o
$ od -A x -t x1 -N 4 /tmp/misa_x64.o
000000 cf fa ed fe
$ file /tmp/misa_x64.o
/tmp/misa_x64.o: Mach-O 64-bit x86_64 object, flags:<|SUBSECTIONS_VIA_SYMBOLS>
```

Same magic, identified as **x86_64**. (Sizes: 5,840 B arm64,
5,120 B x86_64 for `hello.misa`.)

### 2.4 The object was emitted with the correct data layout

Checked programmatically against llvmlite 0.50.0, the same code
path `impl/misa/aot/build.py::_emit_object` uses:

```
aarch64-apple-darwin → e-m:o-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-n32:64-S128-Fn32
x86_64-apple-darwin  → e-m:o-p270:32:32-p271:32:32-p272:64:64-i128:128-f80:128-n8:16:32:64-S128
```

`e-m:o` = little-endian Mach-O. `_emit_object` sets
`mod.triple` and `mod.data_layout = str(tm.target_data)` *before*
`binding.parse_assembly` + `bmod.verify()` + `tm.emit_object()`,
so the `.o` bytes above were produced with these layouts. (The
`--emit-llvm` text used to show an empty `target datalayout = ""`
because `build()` wrote that file before `_emit_object` ran; the
coordinator fixed this 2026-10-05 — the dump now happens after
`_emit_object`, so triple + layout match the object.)

### 2.5 Target-table row (the CLI's own contract)

```
$ misa build --list-targets
aarch64-apple-darwin   object only in M1 (link+sign on a Mac: M7) (xcrun clang (needs macOS) | main)
x86_64-apple-darwin    object only in M1 (link+sign on a Mac: M7) (xcrun clang (needs macOS) | main)
```

Unknown triples still fail cleanly (`E600`), and host behavior is
unchanged: `misa check examples/hello.misa` → OK,
`misa check examples/webslice.misa` → OK (suites untouched by M7,
which changes no code).

---

## 3. Transfer to the Mac: exactly what goes over

Ship **sources + the cross-compiled program object**. Do **not** try
to build a static library of the runtime on Linux — the runtime must
be compiled natively on the Mac against the iOS SDK (headers,
`os_log`, `NSURLSession` only exist there). The transfer package:

```
misa-ios-transfer.zip
├── prog_arm64.o                      # from §2.2: misa build --target aarch64-apple-darwin -o prog_arm64.o app.misa
├── rt/
│   ├── misa_rt.h                     # runtime API (misa_rt_init, misa_alloc, …)
│   ├── misa_rt.c                     # portable runtime (compiles as C)
│   ├── misa_model.c                  # portable model logic; curl backend OFF on iOS
│   ├── misa_platform.h               # platform interface (the contract §4 implements)
│   ├── misa_ui.h                     # UI driver interface (the contract §6 implements)
│   ├── platform_apple.m              # NEW: §4 sketch (ObjC, os_log + NSURLSession)
│   ├── ui_uikit.m                    # NEW: §6 sketch (ObjC, UIKit driver)
│   └── main.m                        # NEW: §5 sketch (UIApplicationMain + misa_rt_init + misa_main)
└── ios-cross.md                      # this document
```

Copy the `rt/` sources from `impl/misa/aot/rt/` of the Misa tree.
Do **not** include `platform_linux.c`, `platform_emscripten.c`,
`ui_text.c`, or `ui_dom.c` — the wrong platform file linked in is a
silent behavior change (curl instead of NSURLSession, stdout instead
of os_log). The build commands below name every file explicitly so
there is no glob accident.

x86_64-apple-darwin objects (§2.3) are for the iOS **simulator**
(Intel Macs) — same package, name it `prog_x64.o` if you transfer it.

---

## 4. Runtime compile on the Mac

### 4.1 SDK path

```sh
SDK=$(xcrun --sdk iphoneos --show-sdk-path)   # e.g. /Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk
```

**[VERIFY ON THE MAC]:** `xcrun` requires Xcode Command Line Tools
(or full Xcode) installed and the license accepted
(`sudo xcodebuild -license accept`).

### 4.2 Compile the runtime (arm64 device)

```sh
# C runtime sources — plain C, no Apple SDK headers needed by these files
xcrun clang -arch arm64 -isysroot "$SDK" \
    -O2 -c rt/misa_rt.c   -o build/misa_rt.o
xcrun clang -arch arm64 -isysroot "$SDK" \
    -O2 -c rt/misa_model.c -o build/misa_model.o
```

`misa_model.c` without `-DMISA_USE_CURL` compiles to the
graceful-degradation shape: `http.get` → `E304`, network model
calls → catchable `E401` **unless** the Apple transport (§4.4)
provides `misa_platform_http_post` — which it does, so with
`platform_apple.m` linked, model calls work for real.

```sh
# ObjC platform + UI driver — ARC on, frameworks available
xcrun clang -arch arm64 -isysroot "$SDK" \
    -O2 -fobjc-arc -c rt/platform_apple.m -o build/platform_apple.o
xcrun clang -arch arm64 -isysroot "$SDK" \
    -O2 -fobjc-arc -c rt/ui_uikit.m       -o build/ui_uikit.o

# App entry
xcrun clang -arch arm64 -isysroot "$SDK" \
    -O2 -fobjc-arc -c rt/main.m -o build/main.o
```

### 4.3 Link

```sh
xcrun clang -arch arm64 -isysroot "$SDK" \
    -o build/MisaApp \
    prog_arm64.o \
    build/misa_rt.o build/misa_model.o \
    build/platform_apple.o build/ui_uikit.o build/main.o \
    -framework Foundation -framework UIKit
```

Notes:

- `Foundation` is needed by `platform_apple.m` (`NSURLSession`,
  `NSUserDefaults`, file paths). `UIKit` is needed by `ui_uikit.m`
  and `main.m` (`UIApplicationMain`, `UIStackView`, …).
- The runtime allocates only via `misa_alloc` (Boehm GC or malloc)
  and never holds ObjC objects, so `-fobjc-arc` on the ObjC files
  is safe — ARC manages only the ObjC side. **GC decision
  (Phase 4 §1.5):** bdwgc builds for iOS upstream ("Darwin
  configuration"), but it has not been built or measured for this
  project — default to the **malloc fallback** (build without
  `-DMISA_USE_GC`) for the first phone ship, exactly as M3 did for
  web. Measure before switching.
- The program object defines both `main()` and `misa_main()`;
  the iOS entry is `main.m`'s `main` → `UIApplicationMain`, so the
  emitted `main()` is dead code. **Do not delete it from the
  object** — leaving it is harmless and keeps the transfer
  mechanical. (Longer term, the M1 target table should gain an
  iOS entry mode that emits only `misa_main()`; out of M7 scope.)

### 4.4 `platform_apple.m` — the NSURLSession transport sketch

Implements the `misa_platform.h` contract (§2 of the header:
P1 log, P2 fatal, P3 getenv, P4 fs, HTTP transport). Blocking
semantics are preserved — the caller's `misa_model_call`
3-attempt + budget logic runs **unchanged** — by waiting on a
dispatch semaphore. The budget backstop (`now_ms`,
`clock_gettime`-based) is untouched.

```objc
// rt/platform_apple.m — iOS platform implementation (M7 sketch).
// Implements misa_platform.h. Compile: see §4.2 (-fobjc-arc).
// [VERIFY ON THE MAC]: semaphore timeout arithmetic, os_log format
// specifiers, NSUserDefaults vs Keychain choice for P3 (see notes).

#import "misa_platform.h"
#import <Foundation/Foundation.h>
#import <os/log.h>
#import <UIKit/UIKit.h>   // only for the fatal-alert path

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
                 code ? code : "?", msg ? msg : "");
    // Present a visible failure on the main thread, then abort.
    // Alert presentation requires a visible view controller; the
    // UIKit driver (§6) records the key window at boot.
    dispatch_async(dispatch_get_main_queue(), ^{
        UIAlertController *a = [UIAlertController
            alertControllerWithTitle:@"Misa runtime error"
            message:[NSString stringWithFormat:@"%s: %s",
                     code ? code : "?", msg ? msg : ""]
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
/* Keys are NEVER baked into the binary. First phone ship: read from
 * the app's Info.plist / NSUserDefaults table populated at install
 * time (e.g. a Settings bundle or a first-run config screen).
 * Production: move provider keys to the Keychain. */

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
    // Reject escapes above the sandbox root.
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

    // Watchdog: never block the runtime past ~3x the budget.
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
    out->body = buf;   // may be NULL when the body was empty
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
```

Notes for the Mac operator:

- The blocking shape matches `platform_linux.c`'s curl backend
  (which this mirrors 1:1) and satisfies the `misa_platform.h`
  contract: return 1 on HTTP completion at *any* status, 0 + `err`
  on transport failure. HTTP error statuses are *not* transport
  failures — `misa_model.c` maps them to catchable Misa errors
  itself.
- Body ownership: valid until `misa_platform_http_free`, exactly
  like the Linux backend.
- **Timeouts:** `timeout_s` comes from the Misa budget logic; the
  watchdog adds a hard backstop. Never remove the watchdog — a
  hung semaphore would freeze the runtime forever.
- **P3 decision to make on the Mac:** `NSUserDefaults` above is
  the first-ship convenience. Provider keys are secrets — move to
  the **Keychain** (`SecItemAdd`/`SecItemCopyMatching`) before any
  TestFlight/App Store build. Keys in `Info.plist` ship inside the
  signed bundle = baked into the binary in effect. Standing rule
  (Phase 4 §6.5): keys live in app-supplied config, never in the
  compiled binary.

---

## 5. `main.m` — the iOS entry

The M1 target table says Apple rows use entry `main` (the same
hosted entry as Linux). On iOS the real entry is
`UIApplicationMain`; the emitted `main()` is ignored and
`misa_main()` is invoked after the UIKit driver is created:

```objc
// rt/main.m — iOS app entry (M7 sketch).
// [VERIFY ON THE MAC]: UIApplicationSceneManifest / scene-delegate
// vs classic AppDelegate choice for the Xcode template (§7).

#import <UIKit/UIKit.h>
#import "misa_rt.h"   // misa_rt_init()
#import "misa_ui.h"   // misa_ui_create(), misa_main is emitted

// misa_main is defined in the cross-compiled program object.
extern MVal *misa_main(void);

@interface MisaAppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end

@implementation MisaAppDelegate

- (BOOL)application:(UIApplication *)app
        didFinishLaunchingWithOptions:(NSDictionary *)opts {
    (void)app; (void)opts;
    self.window = [[UIWindow alloc]
        initWithFrame:[UIScreen mainScreen].bounds];
    UIViewController *root = [UIViewController new];
    root.view.backgroundColor = [UIColor systemBackgroundColor];
    self.window.rootViewController = root;
    [self.window makeKeyAndVisible];

    // Create the UIKit driver BEFORE any Misa code runs: generated
    // screen/handler code reaches the driver via misa_ui_active().
    MisaUI *ui = misa_ui_create();
    misa_ui_set_root_view(ui, (__bridge void *)root.view); // §6 helper

    misa_rt_init();   // once, before any Misa call
    misa_main();      // builds the first screen through the driver
    return YES;
}

@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil,
                                 NSStringFromClass([MisaAppDelegate class]));
    }
}
```

`misa_ui_set_root_view` is a two-line addition to the UIKit driver
(§6) so `misa_ui_show_screen` has a container to attach to.

---

## 6. UIKit driver sketch (`ui_uikit.m`)

Implements `misa_ui.h` behind the closed component set
(`spec/PHASE4.md` §4.4 — UIKit first, SwiftUI a later refinement;
UIKit chosen for direct C interop). Kinds arrive as their source
names (`"Column"`, `"Text"`, …); the driver switches on them.

### 6.1 Component mapping

| Misa component | UIKit |
|---|---|
| `Column` | `UIStackView`, `axis = UILayoutConstraintAxisVertical` |
| `Row` | `UIStackView`, `axis = UILayoutConstraintAxisHorizontal` |
| `Spacer` | `UIView` with low hugging priority / flexible spacer inside the stack (`setContentHuggingPriority:forAxis:`) |
| `Text(text:)` | `UILabel`, `numberOfLines = 0` |
| `Heading(text:)` | `UILabel`, headline font (`UIFontTextStyleHeadline`, bold) |
| `Banner(text:, style:)` | `UILabel` (or `UIView` + label) tinted by style: `info` → system blue tint, `warning` → system yellow, `error` → system red; corner radius for the banner look |
| `Button(label:, onTap:)` | `UIButton` (`UIButtonTypeSystem`), `setTitle:forState:`; `UIControlEventTouchUpInside` → `misa_dispatch_handler(handler_id, NULL)` |
| `TextInput(id:, hint:)` | `UITextField`, `placeholder = hint`; readback = `.text` |
| `CameraButton(onCapture:)` | `UIButton` + `UIImagePickerController` (`sourceType = Camera`) presented from the root view controller; captured `UIImage` → JPEG bytes → `Photo` handle → `misa_dispatch_handler(handler_id, photo)` |
| `Image(photo:)` | `UIImageView`, `contentMode = ScaleAspectFit` |
| `List(items:)` | `UITableView`, single text row type (`UITableViewCell` with `textLabel`) |

Args arrive via `misa_ui_set_arg(ui, w, arg, v)` where `v` is an
`MVal*` — render with the existing `misa_render` and convert to
`NSString`. Handlers arrive via `misa_ui_set_handler(ui, w, event,
handler_id)` (`event` is `"onTap"` / `"onCapture"`); the driver's
`misa_dispatch_handler` looks the id up in the emitter-generated
`misa_handler_table[]` (see `misa_ui.h`), runs it, and routes
uncaught errors to `misa_ui_report_error` — which presents a
dismissible banner/alert, **never** killing the app (per
`spec/PHASE4.md` §4.2 decision).

### 6.2 Driver skeleton

```objc
// rt/ui_uikit.m — UIKit driver skeleton (M7 sketch, not complete).
// Implements misa_ui.h. Widget handles are 1-based indices into an
// internal NSMutableArray of UIView*; handle 0 = none.
// ALL UIKit calls must run on the main thread: wrap the mutating
// entry points in dispatch_async(dispatch_get_main_queue(), …) where
// the caller may be off-main (model calls run on worker threads).
// [VERIFY ON THE MAC]: main-thread discipline for dispatch_handler,
// UITableView data-source wiring for List, CameraButton permissions
// (NSCameraUsageDescription in Info.plist).

#import "misa_ui.h"
#import <UIKit/UIKit.h>

struct MisaUI {
    UIView *rootView;          // set by misa_ui_set_root_view (§5)
    NSMutableArray *widgets;   // index 0 unused; UIView* per handle
    NSMutableArray *idSlots;   // slot -> MisaWidget (NSNumber)
    UIViewController *hostVc;  // for presenting picker/alerts
};

MisaUI *misa_ui_create(void) {
    MisaUI *ui = calloc(1, sizeof *ui);
    ui->widgets = [NSMutableArray arrayWithObject:[NSNull null]];
    ui->idSlots = [NSMutableArray array];
    return ui;
}

static MisaWidget pushWidget(MisaUI *ui, UIView *v) {
    [ui->widgets addObject:v ? v : [NSNull null]];
    return (MisaWidget)(ui->widgets.count - 1);
}
static UIView *getWidget(MisaUI *ui, MisaWidget w) {
    id o = (w > 0 && w < ui->widgets.count) ? ui->widgets[w] : nil;
    return (o == [NSNull null]) ? nil : o;
}

/* kind -> UIView factory (mapping table §6.1) */
MisaWidget misa_ui_begin_comp(MisaUI *ui, MisaWidget parent,
                              const char *kind) {
    NSString *k = [NSString stringWithUTF8String:kind];
    UIView *v = nil;
    if ([k isEqualToString:@"Column"] || [k isEqualToString:@"Row"]) {
        UIStackView *s = [UIStackView new];
        s.axis = [k isEqualToString:@"Column"]
            ? UILayoutConstraintAxisVertical
            : UILayoutConstraintAxisHorizontal;
        v = s;
    } else if ([k isEqualToString:@"Text"] ||
               [k isEqualToString:@"Heading"] ||
               [k isEqualToString:@"Banner"]) {
        v = [UILabel new];
    } else if ([k isEqualToString:@"Button"] ||
               [k isEqualToString:@"CameraButton"]) {
        v = [UIButton buttonWithType:UIButtonTypeSystem];
    } else if ([k isEqualToString:@"TextInput"]) {
        v = [UITextField new];
    } else if ([k isEqualToString:@"Image"]) {
        v = [UIImageView new];
    } else if ([k isEqualToString:@"List"]) {
        v = [[UITableView alloc] initWithFrame:CGRectZero
                                         style:UITableViewStylePlain];
    } else if ([k isEqualToString:@"Spacer"]) {
        v = [UIView new];
    }
    MisaWidget w = pushWidget(ui, v);
    UIView *p = getWidget(ui, parent);
    if (p && v) {
        if ([p isKindOfClass:[UIStackView class]])
            [(UIStackView *)p addArrangedSubview:v];
        else
            [p addSubview:v];
    }
    return w;
}

void misa_ui_set_arg(MisaUI *ui, MisaWidget w,
                     const char *arg, MVal *val) {
    // Render the MVal with the existing misa_render(), convert to
    // NSString, and apply per (kind, arg): label:/text:/hint:/style:
    // /items:/photo:. [Sketch: full arg application is Mac-side work.]
}

void misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                         const char *event, int32_t handler_id) {
    // Button.onTap / CameraButton.onCapture: attach a target-action
    // (or delegate) that calls misa_dispatch_handler(handler_id, NULL).
    // [Sketch: target-action trampoline is Mac-side work.]
}

void misa_ui_show_screen(MisaUI *ui, MisaWidget root) {
    // Remove previous screen subviews from ui->rootView, add the new
    // root UIView full-bleed with Auto Layout constraints.
}

MisaWidget misa_ui_find_bound(MisaUI *ui, int32_t slot) {
    // Look up ui->idSlots[slot]; 0 when not mounted.
    return 0; // [Sketch]
}

MVal *misa_ui_get_text(MisaUI *ui, MisaWidget w) {
    // UITextField.text -> misa_text_cstr. [Sketch]
    return NULL;
}

int32_t misa_dispatch_handler(int32_t handler_id, MVal *event) {
    // Table lookup in misa_handler_table[] (emitted by misa build);
    // run handler; uncaught error -> misa_ui_report_error, never crash.
    // [Sketch: mirrors ui_text.c's real implementation.]
    return -1;
}

void misa_ui_report_error(MisaUI *ui, MVal *err) {
    // Dismissible alert/banner on the main thread. [Sketch]
}

MisaUI *misa_ui_active(void) { /* current instance, set at boot */ return NULL; }
```

`ui_text.c` is the executable reference for every function above —
the Mac implementer should keep its semantics (especially
`misa_dispatch_handler`'s never-kill behavior and the lazy
head-line framing, which do not apply to UIKit but document the
intent).

---

## 7. Xcode project template (`ios/MisaApp`)

The template is a real, openable Xcode project — the Mac operator
drops the transfer package's `prog_arm64.o` into `ios/MisaApp/program/`
and presses Run. No Misa knowledge required. Regenerate with
`ios/MisaApp/gen_xcodeproj.py` if the file list changes (it verifies
every object id is defined).

What the template encodes (so nobody re-derives it):

1. **No storyboard** — the UI is 100% programmatic via the UIKit
   driver (`rt/main.m` builds the window + root view controller).
   `Info.plist` carries no `UIMainStoryboardFile` /
   `UILaunchStoryboardName` keys and uses the `UILaunchScreen` stub.
2. **Sources:** `rt/main.m`, `rt/platform_apple.m`, `rt/ui_uikit.m`,
   `rt/misa_rt.c`, `rt/misa_model.c` (Compile Sources, ARC on).
   Headers (`misa_rt.h`, `misa_ui.h`, `misa_platform.h`) are referenced
   for navigation; everything is C/ObjC so no bridging header and no
   Swift in the first ship.
3. **The program object** `program/prog_arm64.o` is in *Link Binary
   With Libraries* — Xcode links raw `.o` files added to the target.
   **[VERIFY ON THE MAC]:** confirm the current Xcode accepts the `.o`
   in Link Binary With Libraries vs. Compile Sources.
4. **Build Settings:** `ARCHS = arm64` (device); `SDKROOT = iphoneos`;
   iOS Deployment Target 15.0 (`os_log %{public}.*s` and the
   `UIStackView`/`UIAlertController` APIs used all predate it —
   **[VERIFY ON THE MAC]** against the Xcode of the day). The `x86_64`
   slice is simulator-only: swap in `prog_x64.o` (`misa build --target
   x86_64-apple-darwin`) and build against the simulator SDK.
5. **Info.plist:** `NSCameraUsageDescription` is present (required if
   any screen uses `CameraButton` — iOS kills the app at capture time
   without it).
6. **Signing:** automatic (`CODE_SIGN_STYLE = Automatic`); the Mac
   operator sets their Development Team once (§8 checklist).

Status 2026-10-05: `rt/platform_apple.m`, `rt/main.m`, `rt/ui_uikit.m`
are implemented (were §4.4/§5/§6 sketches). Linux-side verification:
delimiter balance, `@interface`/`@implementation`/`@end` pairing, and
every `misa_*` call cross-checked against `misa_rt.h` /
`misa_platform.h` / `misa_ui.h` (script: `/tmp/check_objc.py` —
this box has no Apple SDK and GCC cannot parse ObjC blocks, so a full
compile is Mac-side only). All Foundation/UIKit API usage is
transcribed from the reviewed sketches above; runtime behavior is
Mac-side verification. The `.xcodeproj` is generated, not
hand-edited: `project.pbxproj` balances and every referenced object id
resolves (checked by the generator).

---

## 8. Signing & provisioning checklist (Hamis / Mac operator)

Hamis holds his own Apple ID and any team keys — nobody else's
credentials are involved. Exact handoff:

- [ ] **Mac with Xcode** (App Store download, free) + Command
      Line Tools; run `sudo xcodebuild -license accept` once.
- [ ] **Apple ID signed into Xcode** (Xcode → Settings →
      Accounts → +). Two tiers:
  - **Free Apple ID** — enough for the *first phone ship* via
    direct device install: Xcode creates a personal development
    certificate + provisioning profile automatically ("Automatically
    manage signing"). Limits: the app signature expires after
    ~7 days (reinstall weekly), no TestFlight, no App Store.
  - **Apple Developer Program** (~$99/year, paid by Hamis if/when
    he wants it) — required for **TestFlight** and App Store
    distribution; certificates last a year; TestFlight builds
    don't expire weekly.
- [ ] **Device registration:** plug the iPhone in (or use
      network device pairing), select it as the run destination;
      Xcode registers its UDID in the provisioning profile
      automatically under automatic signing.
- [ ] **Bundle identifier:** set once (e.g.
      `com.cvyon.misaapp` — **[VERIFY]**: Hamis picks the real
      reverse-DNS name); must be unique if he later enrolls in
      the paid program.
- [ ] **Capabilities:** none needed for the first ship (no push,
      no iCloud, no in-app purchase).
- [ ] **First run on device:** iOS may require trusting the
      developer certificate: Settings → General → VPN & Device
      Management → trust. Then launch.

### TestFlight vs direct device install — first phone ship

| | Direct device install | TestFlight |
|---|---|---|
| Cost | Free (Apple ID only) | Paid Developer Program |
| How | Xcode → Run on plugged-in iPhone | Archive → upload → App Store Connect → invite |
| Cert lifetime | ~7 days, then reinstall | ~90 days per build |
| Best for | **First phone ship: prove it runs** | Sharing with testers / iterating |

**Recommendation:** direct device install first (minutes, free).
TestFlight only when Hamis decides the app leaves his own phone.

---

## 9. Simulator check (optional, no phone needed)

The `x86_64-apple-darwin` object (§2.3) is for Intel-Mac
simulators; Apple-silicon Macs simulate iOS on **arm64**, so prefer
`prog_arm64.o` rebuilt for the simulator SDK:

```sh
SIMSDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
# runtime + program object for the simulator triple, then link with
# -isysroot "$SIMSDK" and run in Simulator from Xcode.
```

**[VERIFY ON THE MAC]:** whether the `aarch64-apple-darwin`
object links cleanly against the simulator SDK (Apple uses the
same arch with a different platform — `xcrun clang` handles it via
`-isysroot`, but confirm no `-target` flag is needed).

---

## 10. Not yet done / needs a Mac (honest list)

Nothing in §4–§9 has been compiled or run. Concretely outstanding:

1. `platform_apple.m` — sketch only; needs compile + runtime
   testing of `misa_platform_http_post/get` against a real
   endpoint, the `NSUserDefaults`→Keychain P3 migration, and the
   fatal-alert presentation path.
2. `ui_uikit.m` — skeleton only; `misa_ui_set_arg` (all 11
   components × args), `misa_ui_set_handler` trampolines,
   `misa_ui_show_screen` layout, `List`'s `UITableViewDataSource`,
   `CameraButton`'s picker → `Photo` handle, `misa_ui_get_text`
   readback, main-thread discipline — all Mac-side work.
3. `main.m` — sketch only; AppDelegate-vs-SceneDelegate match
   with the Xcode template (§7.5).
4. Link + sign + install on a real iPhone; first screenshot.
5. GC decision for iOS: bdwgc Darwin build vs malloc fallback —
   unmeasured (default: malloc fallback, §4.3).
6. `misa build` target-table entry mode for iOS (emit
   `misa_main` as the sole entry vs the current `main`): the
   object works either way today since `main.m` bypasses the
   emitted `main()`, but the table should say so explicitly.
7. App Store submission, privacy manifest, Keychain key custody —
   all after the first device install proves the path.

When 1–4 are done, M7's exit proof is: **the same `.misa`
codebase that builds to a web page (M3) installs and runs on an
iPhone.**

---

## Appendix A. One-page Mac runbook (copy-paste order)

```sh
# 0. Prereqs: Xcode installed, license accepted, Apple ID in Xcode.
unzip misa-ios-transfer.zip && cd misa-ios-transfer
SDK=$(xcrun --sdk iphoneos --show-sdk-path)

# 1. Compile the runtime (see §4.2 for the exact commands)
mkdir -p build
xcrun clang -arch arm64 -isysroot "$SDK" -O2 -c rt/misa_rt.c    -o build/misa_rt.o
xcrun clang -arch arm64 -isysroot "$SDK" -O2 -c rt/misa_model.c  -o build/misa_model.o
xcrun clang -arch arm64 -isysroot "$SDK" -O2 -fobjc-arc -c rt/platform_apple.m -o build/platform_apple.o
xcrun clang -arch arm64 -isysroot "$SDK" -O2 -fobjc-arc -c rt/ui_uikit.m       -o build/ui_uikit.o
xcrun clang -arch arm64 -isysroot "$SDK" -O2 -fobjc-arc -c rt/main.m            -o build/main.o

# 2. Link (see §4.3)
xcrun clang -arch arm64 -isysroot "$SDK" -o build/MisaApp \
    prog_arm64.o build/misa_rt.o build/misa_model.o \
    build/platform_apple.o build/ui_uikit.o build/main.o \
    -framework Foundation -framework UIKit

# 3. Or do steps 1-2 inside the Xcode template (§7) and press Run.
```

## Appendix B. Evidence index (Linux-side, reproducible)

| Artifact | Command | Result |
|---|---|---|
| arm64 IR | `misa build --target aarch64-apple-darwin --emit-llvm arm64.ir examples/hello.misa` | `target triple = "aarch64-apple-darwin"`, `misa_main` defined |
| arm64 object | `misa build --target aarch64-apple-darwin -o misa_arm64.o examples/hello.misa` | `cf fa ed fe`, `file` → Mach-O 64-bit arm64, 5,840 B |
| x86_64 IR | `misa build --target x86_64-apple-darwin --emit-llvm x64.ir examples/hello.misa` | `target triple = "x86_64-apple-darwin"`, `misa_main` defined |
| x86_64 object | `misa build --target x86_64-apple-darwin -o misa_x64.o examples/hello.misa` | `cf fa ed fe`, `file` → Mach-O 64-bit x86_64, 5,120 B |
| Data layouts | llvmlite 0.50.0 `Target.from_triple(...).create_target_machine().target_data` | `e-m:o-…` Mach-O layouts for both triples (§2.4) |
| Target table | `misa build --list-targets` | Apple rows: "object only in M1 (link+sign on a Mac: M7)" |
| Suite sanity | `misa check examples/hello.misa`, `misa check examples/webslice.misa` | OK / OK (M7 changes no code) |

*Document version: M7 doc task, 2026-10-05. Machine evidence from
Linux x86_64, llvmlite 0.50.0 (LLVM 22.1). Mac-side sections are
design sketches — see §10.*
