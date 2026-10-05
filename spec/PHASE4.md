# Misa Phase 4 — Mobile & web (design)

**Status:** M1 + M2 implemented 2026-10-05 (see "Implementation notes"
at the end); M3–M7 still design. Roadmap exit criterion: *one codebase
ships to a phone and the web.*

Phase 4 takes the v0.3 LLVM AOT pipeline — which today produces
host-Linux binaries — and turns it into a genuine multi-target
compiler: iOS/Android toolchains, UI lowered to native widgets per
platform, and a WASM+DOM web target. Streaming in AOT and further LLVM
optimization are explicitly **out** of Phase 4 (streaming stays a
build-time `E402`).

Everything below was verified against the actual sources on
2026-10-05 (`impl/misa/aot/emit.py`, `build.py`, `rt/misa_rt.h`,
`rt/misa_rt.c`, `rt/misa_model.c`) and against llvmlite 0.50.0 /
LLVM 22.1 installed fresh on this machine. Claims about target
support were tested, not assumed.

---

## 1. Portability audit: every host-Linux assumption in v0.3

### 1.1 `impl/misa/aot/build.py` — the driver (all fixable, small)

| # | Assumption | Location | Fix |
|---|-----------|----------|-----|
| B1 | Only the native LLVM target is initialized: `initialize_native_target()` / `initialize_native_asmprinter()`, then `Target.from_default_triple()`. Cross triples fail. | `build()` | When `--target` is given: `initialize_all_targets()` + `initialize_all_asmprinters()`, `Target.from_triple(triple)`, `create_target_machine()`. |
| B2 | No data layout is ever set on the module. Host verify passes by luck of the default. Cross targets need it. | `build()` | `mod.triple = triple; mod.data_layout = str(tm.target_data)` (`TargetMachine.target_data` exists in llvmlite 0.50). |
| B3 | `gcc` is hardcoded for compiling the C runtime and for linking. | `ensure_runtime()`, `build()` | Per-target C compiler: `emcc` (web), NDK `clang` (Android), `xcrun clang` (Apple). Select via a target table. |
| B4 | `_find_lib("libcurl.so*")` searches only x86-64 Linux dirs (`/lib/x86_64-linux-gnu`, …). | `runtime_flags()` | Per-target library resolution; on targets without curl the transport is a platform shim (§4), not libcurl. |
| B5 | Runtime cache key hashes only defines/cflags/libs. Two targets would collide. | `_rt_key()` | Include target triple + C compiler id in the key. |
| B6 | Entry point is always `int main()`. | `emit_entry_main()` (emit.py) | Target-specific entry: keep `main` for hosted targets; exported `misa_ui_boot()` for Emscripten UI apps; JNI `JNI_OnLoad` + native methods for Android; ObjC `main.m` calling `misa_main()` for iOS. The emitter must select the entry module by target. |

### 1.2 `impl/misa/aot/emit.py` — the IR generator (no host assumptions)

Verified clean. The entire value model is an opaque `i8*`; only
`i1`/`i8`/`i32`/`i64`/`double` appear in signatures. No inline asm,
no target intrinsics, no target-specific constants, no struct layout
shared with C (the `MModelMeta` struct exists in the header but the
emitter uses the `misa_model_call_meta` **text** path, so there is no
C/LLVM ABI coupling). Integer literals are `i64`, floats are
IEEE-754 doubles — identical on every Phase 4 target. **The IR the
emitter produces today is already portable; only the driver (B1–B6)
is host-bound.** This is the single most important audit finding:
retargeting is a driver + runtime-linking change, not an emitter
redesign.

One emitter change is still required for widgets: event-handler
lambdas are currently never compiled (see §4.2).

### 1.3 `impl/misa/aot/rt/misa_rt.h` / `misa_rt.c` — portable with a platform layer

- `struct MVal` (`uint8_t tag; uint8_t pad[7];` + union) is
  self-consistent within one C compilation and opaque to LLVM. No
  cross-language layout to preserve. **Portable as-is.**
- No direct syscalls anywhere: no `mmap`, `ioctl`, `fork`, `dlopen`,
  `system`. Only libc: stdio, stdlib, string, math, ctype, stdarg,
  `time.h`, `getenv`.
- `_Thread_local Pending` (misa_rt.c:79) — C11; fine on Linux,
  Android (bionic), Apple, Emscripten, wasi-libc single-threaded.
  (MSVC would need `__declspec(thread)`; Windows is not in the
  Phase 4 matrix.)
- `clock_gettime(CLOCK_MONOTONIC, …)` in `misa_model.c:now_ms` —
  POSIX; present on Linux, bionic, Apple (10.12+), Emscripten,
  wasi-libc. No change for Phase 4 targets.
- `long` appears in a few spots (`long status`, timeout casts) —
  LP64 on every Phase 4 target. Fine.
- The following need a **platform abstraction** (`misa_platform.h`,
  one implementation per target; Linux keeps current behavior):

| # | Current behavior | Why it must abstract |
|---|-----------------|----------------------|
| P1 | `printf`/`fprintf(stderr)` for `print`, screen preview, uncaught-error report | Android needs `__android_log_print` (logcat); iOS needs `os_log`; web needs `console.log`. |
| P2 | `exit(1)` on OOM (`misa_alloc`) and `return 1` from `main` on uncaught error | Killing the process is wrong on mobile and meaningless in a web page. Needs a platform abort hook: `misa_platform_fatal(code, msg)` → Android: log + finish activity; iOS: log + present; web: console + overlay. |
| P3 | `getenv()` for provider keys, `MISA_MOCK_FILE`, `MISA_*_BASE_URL` | No environment on web/mobile. Web: JS-supplied config table at boot (`misa_platform_getenv` reads it; keys are passed by the page, never baked into the wasm). Mobile: app-supplied config (BuildConfig / Info.plist → JNI/ObjC bridge). |
| P4 | `fopen` on arbitrary paths for `fs.read`/`fs.write` | WASI: preopened dirs only. Emscripten: MEMFS/IDBFS virtual FS (`--preload-file` for bundled assets). Android/iOS: app-sandbox paths. Semantics stay ("app-relative paths"); the platform layer maps the root. |
| P5 | `misa_screen_*` (7 functions) print a text tree to stdout | Replaced by the UI driver interface (§5). The text driver is *kept* as the default for headless targets (server/desktop CLI) — driver selected at link time. |

### 1.4 `impl/misa/aot/rt/misa_model.c` — the network boundary

- The entire HTTP layer is `#ifdef MISA_USE_CURL` (libcurl). There
  is **no libcurl** on Android (bionic), WASM (no sockets), and iOS
  (buildable, but unidiomatic — `NSURLSession` is correct).
- The graceful-degradation path already exists (`http.get` → `E304`,
  network model calls → `E401` when curl is absent). Phase 4 keeps
  that shape and adds real transports behind one interface:

```c
/* misa_platform.h — implemented once per target */
typedef struct {
    long   status;      /* HTTP status */
    char  *body;        /* malloc'd/GC'd response bytes */
    size_t len;
    char   err[256];    /* transport error, "" on HTTP completion */
} MisaHttpResp;

/* Blocking POST of a JSON body. Returns 1 on transport success
 * (any HTTP status), 0 on transport failure (err set). */
int misa_platform_http_post(const char *url,
                            const char *const *headers, int nheaders,
                            const char *body, double timeout_s,
                            MisaHttpResp *out);
void misa_platform_http_free(MisaHttpResp *r);
```

  Backends: **curl** (Linux/desktop — today's code moves here
  unchanged), **Emscripten fetch** (web, §6.4), **NSURLSession**
  (iOS), **JNI → HttpURLConnection** (Android; OkHttp optional).
- Everything else in `misa_model.c` is portable: the JSON parser,
  schema parser/validator, base64, prompt rendering, budget math,
  `mock` provider (needs only file read → works wherever P4 works).
- `strtok_r`, `strtoll`, `strtod` — present in bionic, wasi-libc,
  musl (Emscripten), Apple libc. Fine.

### 1.5 Boehm GC (`~/workspace/deps/gc`, bdwgc 8.2.8, `libgc.a`)

- Built for **x86_64 Linux only**. Each new target needs its own
  bdwgc build (or the malloc fallback, which already exists).
- Upstream bdwgc supports Android (arm64/armv7) and Emscripten
  (conservative scan of linear memory, incl. the emscripten stack);
  WASI single-threaded builds work. iOS builds are supported
  upstream with the usual Darwin configuration.
- Risk: bdwgc-on-Emscripten is the least battle-tested combination
  here. Mitigation is already in the tree: build without
  `-DMISA_USE_GC` and the runtime uses plain `malloc` (documented
  growth caveat). Decision per target at M3/M6 time, measured, not
  guessed.

### 1.6 What is *not* a problem (checked, so nobody re-audits)

- UTF-8 handling is byte-oriented; no endianness dependence
  anywhere in the runtime.
- Float rendering (`%.17g` + `.0` rule) is IEEE-754 deterministic
  across targets.
- Integer wrap semantics are implemented with explicit unsigned
  arithmetic (`wrap_add/sub/mul`, `INT64_MIN` guards) — no UB to
  diverge across optimizers.
- The pending-error model uses no exceptions, no unwinding tables,
  no platform ABI — it works identically everywhere the C
  compiles.

---

## 2. Target matrix (verified 2026-10-05)

LLVM column: tested with llvmlite 0.50.0 (bundled LLVM 22.1) on
this machine. **After `binding.initialize_all_targets()` +
`binding.initialize_all_asmprinters()`, every triple below resolves
to a target machine, and a wasm32-unknown-wasi object was emitted
end-to-end (310 bytes, magic `0061 736d` = `\0asm`).**
Note: `binding.initialize()` is deprecated in 0.50 (raises); call
the specific init functions. `TargetMachine.target` was removed in
0.50 — use `Target.name`; `TargetMachine.target_data` exists.

Toolchain column: checked for presence/downloadability on this box
(Linux x86_64, gcc 13.3, node present, 87 GB free on /home/hatch).

| Target | LLVM via llvmlite | C toolchain here | Runtime blockers | Verdict |
|--------|-------------------|------------------|------------------|---------|
| `x86_64-linux-gnu` (server/desktop) | ✅ native (v0.3 ships) | ✅ gcc | none | **Done (v0.3)** |
| `wasm32` web via **Emscripten** | ✅ object emission proven | ❌ not installed — **installable**: emsdk clones from github.com (reachable, HTTP 200); needs node (present) + python3 (present) + ~1.2 GB disk (87 GB free) | P1–P5 platform layer; curl→fetch transport; bdwgc-under-emscripten risk (malloc fallback ready); entry → `misa_ui_boot` export | **Feasible now — FIRST backend** |
| `wasm32-unknown-wasi` (headless) | ✅ object emission proven | ❌ not installed — wasi-sdk downloadable | no DOM by design; P2/P3/P4 needed; wasi-libc file access = preopens | Feasible; deprioritized (Emscripten covers "the web") |
| `aarch64-linux-android` (ARM64) | ✅ triple resolves | ❌ not installed — **installable**: NDK r27d/r28c zips return HTTP 200 from dl.google.com | P1–P5; curl→JNI transport; bdwgc Android build; JNI entry; APK packaging (needs Android SDK platform-tools — also downloadable) | **Feasible now (not yet installed)** |
| `armv7-linux-androideabi` | ✅ triple resolves | same NDK | same as above | Feasible; low priority (arm64 covers modern phones) |
| `aarch64-apple-darwin` / `x86_64-apple-darwin` (iOS/macOS) | ✅ triple resolves (IR/object emission works **here**) | ❌ **environment-blocked**: linking needs the Apple SDK and code signing needs Xcode + Apple hardware | NSURLSession transport; UIKit/SwiftUI driver (design §5/§7); signing & provisioning (Hamis holds keys) | **Blocked on Apple hardware.** Cross-compile path documented in §7; IR/object emission verifiable on this box, final link+sign on a Mac. |
| Desktop macOS/Windows | LLVM fine | same Apple block for macOS; Windows out of matrix | — | macOS rides the iOS path; Windows not in roadmap |

**Bottom line:** everything except Apple linking/signing can be
built and tested on this Linux box. iOS gets a documented
cross-compile path with machine-verifiable IR/object output here
and a Mac-only final mile.

---

## 3. Phased milestones

**M1 — Cross-target plumbing in `misa build`** (driver only, no
behavior change on host)
- Add `--target <triple>` (+ `--list-targets`). With `--target`:
  `initialize_all_targets()`/`initialize_all_asmprinters()`,
  `Target.from_triple`, `create_target_machine()`,
  `mod.triple` + `mod.data_layout` from the target machine.
- Runtime cache key includes triple + compiler id (fix B5).
- Per-target C compiler selection table (fix B3); `_find_lib`
  becomes per-target (fix B4).
- Pin `llvmlite==0.50.0`; adapt to the 0.50 API (`initialize()`
  deprecated, `TargetMachine.target` removed).
- **Verify:** host `misa build` unchanged — 74/74 interpreter tests
  + 12/12 AOT parity re-run green; `misa build --target
  wasm32-unknown-wasi` and `--target aarch64-linux-android` emit
  valid objects on this box (magic bytes checked).

**M2 — Platform abstraction layer**
- New `rt/misa_platform.h` + `rt/platform_linux.c` implementing
  P1–P5 with today's behavior (log→stdio, fatal→stderr+exit,
  getenv→getenv, fs root→cwd). `misa_model.c`'s curl code moves
  behind `misa_platform_http_post`.
- The 7 `misa_screen_*` functions become the **text UI driver**,
  selected at link time; the driver interface (§5.1) is declared.
- **Verify:** host parity suite green; no observable change.

**M3 — Web first slice (Emscripten + DOM)** ← recommended first
implementation (§6)
- `scripts/install_emsdk.sh` (pinned emsdk version, under
  `~/workspace/emsdk`).
- Runtime compiled with `emcc` (`--target wasm32-unknown-emscripten`
  row in the M1 table); GC decision measured (bdwgc vs malloc).
- DOM UI driver (§5.3); HTML shell; `print`→console; env bridge;
  `misa_ui_boot()` entry.
- **Verify:** a screens example renders in headless Chromium on this
  box; button click → handler → `navigate()` re-render works
  (M4 is a prerequisite for the click half).

**M4 — Compiled event handlers + `id:` registry** (folds into M3's
verify)
- Checker records per-screen `id:` bindings (name → component
  kind); emitter compiles each handler lambda to
  `misa_handler_<n>` (reusing the `FnCtx`/pending-error machinery);
  runtime keeps a bound-component registry; `navigate()` rebuilds
  via the active driver. §5.2 is the full design.

**M5 — Model calls on web**
- `emscripten_fetch` transport + Asyncify for the blocking call
  shape; budgets via `now_ms`; keys via the JS env bridge (never
  baked into the wasm); `mock` provider via `--preload-file`.
- **Verify:** mock model call completes in headless Chromium;
  budget-exceeded paths raise catchable `E500`.

**M6 — Android**
- NDK toolchain row; bdwgc Android build; JNI entry
  (`JNI_OnLoad`, native methods); Android Views driver (same
  interface as §5.1); APK packaging notes (SDK platform-tools,
  debug key for Hamis's own device first).
- **Verify:** APK installs on a real device; one-screen app renders;
  host suites still green.

**M7 — iOS cross-compile path** (documented, Mac-only final mile)
- `docs/ios-cross.md`: `--target aarch64-apple-darwin` IR/object
  here → Xcode project template → `NSURLSession` transport →
  UIKit driver sketch (§7) → signing/provisioning checklist
  (Hamis's keys, his Apple ID).
- **Verify on this box:** object emission for both Apple triples.

**Exit:** the same `.misa` codebase builds to a web page (M3, on
this box) **and** an installable phone app (M6 APK; iOS via M7 on a
Mac). Phase 5 (on-device inference) is untouched.

---

## 4. Native widgets design

### 4.1 Current state

Screens are pure component trees. The emitter lowers them to a
**call stream** — `misa_screen_begin(name)`,
`misa_screen_comp_begin(name, has_args)`,
`misa_screen_comp_arg(name, value, is_first)`,
`misa_screen_comp_arg_handler(name, is_first)`,
`misa_screen_comp_head_end`, `misa_screen_comp_end`,
`misa_screen_end` — with `ScreenIf`/`ScreenFor` evaluated inline
during the stream. The runtime's 7 functions print a text tree.
`misa run` prints the identical tree. The stream is already a clean,
ordered, retained-mode description of exactly what to render; the
only thing Phase 4 changes is the *consumer* of that stream.

The closed component set (spec/STDLIB.md §6 — unchanged by this
design): `Column`, `Row`, `Spacer`, `Text(text:)`,
`Heading(text:)`, `Banner(text:, style:)`, `Button(label:, onTap:)`,
`TextInput(id:, hint:)`, `CameraButton(onCapture:)`,
`Image(photo:)`, `List(items:)` (`[Text]` in v0.1).

### 4.2 The gap: event handlers are not compiled

Today, a lambda in a component arg position is *accepted by the
checker, skipped by the emitter* (`emit_component` emits
`misa_screen_comp_arg_handler`, i.e. prints `<handler>`), and its
body is never compiled. `id:` args are filtered out at compile time
entirely. So `onCapture: |photo| { … notesInput.text … }` has no
AOT existence. Interactive widgets require closing this gap:

1. **Checker** records per screen an ordered id-binding table:
   `ScreenDecl.id_bindings: [(name, component_kind)]`, assigned
   slots 0..n-1. Duplicate `id:` is already `E207`. Ids inside
   `ScreenFor` bodies are rejected for AOT in the first slice
   (dynamic replication needs a keyed registry — later work; the
   checker raises a precise `E600` rather than miscompiling).
2. **Emitter** — in `emit_component`, each Lambda arg becomes a real
   function `misa_handler_<screen>_<n>(MVal *event_arg) -> MVal*`,
   emitted with the standard `FnCtx` (allocas, `check()` after every
   fallible call, error epilogue). Its scope is: lambda params bound
   to the incoming args, plus every screen `id:` name resolving to
   `misa_ui_prop_<kind>(misa_ui_find_bound(slot), "prop")`. The
   component-arg call becomes
   `misa_ui_set_handler(widget, "onTap", handler_id)`, where
   `handler_id` indexes a generated table
   `misa_handler_table[] = { misa_handler_… }`.
3. **Runtime** keeps a per-shown-screen registry:
   `misa_ui_find_bound(slot) -> MisaWidget` (0 = not mounted).
   Prop getters: `misa_ui_get_text(widget) -> MVal*` (TextInput),
   extensible per component kind.
4. **Dispatch:** the platform driver maps a native event to
   `misa_dispatch_handler(handler_id, event_mval)`; `onCapture`
   passes a `Photo` handle. Handler return is ignored; uncaught
   errors go through the normal pending-error path to the platform
   fatal hook (P2) — or, better for UI: the driver catches at the
   dispatch boundary and surfaces a platform-appropriate error
   (console/banner), never killing the app. Decision: dispatch
   boundary converts uncaught handler errors to a driver-level
   `misa_ui_report_error` call. Rationale: a button that throws
   must not take down the app.
5. `navigate(Name(args))` keeps its current lowering (direct call to
   `misa_screen_Name`); the screen function now builds widgets
   through the active driver and ends with `misa_ui_show_screen`.
   Screen *params* evaluate as today.

First-slice handler scope (deliberate): lambda bodies support the
statements the emitter already handles (`let`/`var`, `if`,
`for`, `try`, calls incl. model calls, `navigate`, `print`).
Closures over locals are *not* supported (handlers capture only
their params + screen `id:` bindings — matches the declared
semantics that screens are pure trees).

### 4.3 UI driver interface (one per platform, selected at link time)

```c
/* rt/misa_ui.h */
typedef struct MisaUI MisaUI;   /* driver instance, created at boot */
typedef int32_t MisaWidget;     /* driver widget handle, 0 = none  */

MisaUI    *misa_ui_create(void);
void       misa_ui_destroy(MisaUI *ui);

/* tree construction — mirrors the current misa_screen_* stream 1:1 */
MisaWidget misa_ui_begin_screen(MisaUI *ui, const char *name);
MisaWidget misa_ui_begin_comp(MisaUI *ui, MisaWidget parent,
                              const char *kind);          /* "Column", "Text", … */
void       misa_ui_set_arg(MisaUI *ui, MisaWidget w,
                           const char *arg, MVal *v);     /* label:, text:, hint:, style:, items:, photo: */
void       misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                               const char *event, int32_t handler_id); /* onTap, onCapture */
void       misa_ui_end_comp(MisaUI *ui, MisaWidget w);
void       misa_ui_bind_id(MisaUI *ui, MisaWidget w, int32_t slot);    /* id: registry */
void       misa_ui_show_screen(MisaUI *ui, MisaWidget root);           /* navigate target */

/* readback + events */
MisaWidget   misa_ui_find_bound(MisaUI *ui, int32_t slot);
MVal        *misa_ui_get_text(MisaUI *ui, MisaWidget w);   /* TextInput.text */
int32_t      misa_dispatch_handler(int32_t handler_id, MVal *event); /* driver → program */
void         misa_ui_report_error(MisaUI *ui, MVal *err);   /* handler-boundary errors */
```

The existing 7 `misa_screen_*` functions are re-implemented as the
**text driver** (`rt/ui_text.c`) preserving today's stdout tree
byte-for-byte — this keeps `misa run` parity, the AOT parity suite,
and gives every future driver a executable reference.

### 4.4 Per-platform drivers (later milestones)

- **Android Views** (M6): `Column`→`LinearLayout(vertical)`,
  `Row`→horizontal, `Text`→`TextView`, `Heading`→`TextView`
  (textAppearanceHeadline), `Banner`→`TextView`/`CardView` tinted by
  style, `Button`→`Button`, `TextInput`→`EditText`,
  `CameraButton`→`Button` + `ActivityResultContracts.TakePicture`,
  `Image`→`ImageView`, `List`→`RecyclerView` (single text row type).
  Via JNI from the C driver; an Activity hosts one `MisaUI`.
- **UIKit** (M7): `Column`→`UIStackView(vertical)`,
  `Text`→`UILabel`, `Button`→`UIButton`,
  `TextInput`→`UITextField`, `List`→`UITableView`, etc. SwiftUI is
  a possible later refinement; UIKit first (direct C interop).
- **Desktop** (future): a native desktop driver is *not* Phase 4 —
  desktop/server keep the text driver (and `main`).

### 4.5 Deliberate non-goals for Phase 4 widgets

- No reactive re-render on state change: the tree is rebuilt on
  `navigate()` only (matches today's print-the-tree semantics).
  Fine-grained reactivity is a later phase.
- No custom styling language: `Banner.style` is the only style
  knob, per the closed component set. Platform look-and-feel comes
  from the native widgets themselves — that *is* the vision
  ("not write once, look wrong everywhere").
- No new components in Phase 4. The set stays closed; drivers map
  exactly the 11.

---

## 5. First implementation slice (M3+M4, web DOM via Emscripten)

Why web-first: it is the only new target fully buildable **and**
testable on this machine (no Apple hardware, no device provisioning),
and the DOM driver exercises the entire Phase 4 seam — target
flag, platform layer, UI driver, compiled handlers, transports —
before the heavier Android/iOS work.

### 5.1 Toolchain

- `scripts/install_emsdk.sh`: clones emsdk (pinned version),
  `./emsdk install <ver> && ./emsdk activate <ver>`, into
  `~/workspace/emsdk`. Prerequisites verified on this box:
  github.com reachable, `node` present, `python3` present, 87 GB
  free.
- `misa build --target wasm32-unknown-emscripten` row in the M1
  compiler table: `emcc` compiles `misa_rt.c`, `misa_model.c`,
  `platform_emscripten.c`, `ui_dom.c`; llvmlite emits the program
  object for `wasm32-unknown-emscripten`; `emcc` links to
  `misa.js`+`misa.wasm`.
- Emscripten flags (first slice): `-s WASM=1 -s ASYNCIFY=1
  -s EXPORTED_FUNCTIONS=_misa_ui_boot,_misa_dispatch_handler
  -s MODULARIZE=1`. Asyncify is what lets the *blocking* model-call
  shape survive on the web (§5.4); measure binary size at M3 and
  revisit if it blows past ~2 MB.

### 5.2 DOM driver (`rt/ui_dom.c` + `rt/dom_bridge.js`)

Component → DOM mapping (all inside `#misa-root`):

| Misa | DOM |
|------|-----|
| `Column` / `Row` | `<div style="display:flex;flex-direction:column/row">` |
| `Spacer` | `<div style="flex:1 1 auto">` |
| `Text(text:)` | `<p>` |
| `Heading(text:)` | `<h2>` |
| `Banner(text:, style:)` | `<div class="misa-banner misa-banner-{info,warning,error}">` |
| `Button(label:, onTap:)` | `<button>` + `addEventListener('click')` → `misa_dispatch_handler(id, null)` |
| `TextInput(hint:)` | `<input type="text" placeholder="…">`; readback = `.value` |
| `CameraButton(onCapture:)` | `<button>` + hidden `<input type="file" accept="image/*">` (uses `capture="environment"` on real mobile browsers); chosen file → staged into the Emscripten FS → `Photo` handle → `misa_dispatch_handler(id, photo)` |
| `Image(photo:)` | `<img>` (object URL over the staged bytes) |
| `List(items:)` | `<ul>` / `<li>` per Text item |

`misa_ui_set_arg` renders `MVal` via the existing `misa_render`
(no new formatting rules). `misa_ui_show_screen` replaces
`#misa-root`'s children. `misa_ui_report_error` renders a dismissible
banner (never kills the page).

### 5.3 Entry, `print`, `env`

- HTML shell (`web/shell.html`): loads `misa.js`, on ready calls
  `Module.ccall('misa_ui_boot')`, which runs `misa_rt_init()` +
  `misa_main()` with the DOM driver active. The first
  `misa_ui_show_screen` paints the initial screen.
- `misa_print` → `console.log` (EM_JS bridge in
  `platform_emscripten.c`).
- `misa_platform_getenv` reads a JS-side config object populated
  by the shell page (e.g. `window.MISA_ENV = { OPENAI_KEY: … }`).
  Provider keys are **never baked into the wasm binary**.
- Uncaught top-level errors → `misa_ui_report_error` overlay + console.

### 5.4 Network on web: `emscripten_fetch` + Asyncify

- `misa_platform_http_post` (web) is implemented with
  `emscripten_fetch`; Asyncify (`-s ASYNCIFY=1`) suspends the wasm
  stack until the fetch completes, so `misa_model_call`'s blocking
  3-attempt + budget logic runs **unchanged**.
- `http.get` uses the same transport (GET variant).
- Timeouts: Emscripten fetch has no native timeout — implement with
  `emscripten_async_call` watchdog that aborts the fetch; the
  latency budget (`now_ms`, already `clock_gettime`-based, works
  under Emscripten) remains the semantic backstop.
- `mock` provider: mock JSON via `--preload-file` into MEMFS;
  `MISA_MOCK_FILE` points at the preloaded path — makes the M5
  browser test hermetic.

### 5.5 `fs` on web

`--preload-file` for read-only bundled assets; IDBFS mount for
writes that should persist (`fs.write` → IDBFS, synced). Document
that arbitrary host paths don't exist — P4 maps the app root.

### 5.6 Verification (headless, on this box)

1. `misa build --target wasm32-unknown-emscripten examples/screens.misa -o web/misa.js` succeeds.
2. Headless Chromium (present: `/opt/meta-chromium` exists on this
   box) loads `web/shell.html`: initial screen paints; DOM matches
   the text-driver tree node-for-node (mechanical comparison).
3. Scripted click on a `Button` → handler runs → `navigate()` →
   second screen paints (M4's proof).
4. `TextInput` typing → handler reads `.text` → value correct.
5. Mock model call via preloaded file returns validated data;
   forced budget breach raises catchable `E500` in-page.
6. Host suites still green (74/74, 12/12) — the web slice must not
   regress the Linux target.

---

## 6. Risks and open questions

1. **bdwgc under Emscripten** — least-tested combo. Mitigation:
   malloc fallback already in-tree; measure pause/RSS in the M3
   browser test and choose per data. (Short-lived page programs
   tolerate the fallback; long sessions want real GC.)
2. **Asyncify cost** — binary bloat and slower startup vs the
   690 KB / 6 ms host story. Mitigation: measure at M3; fallback is
   moving blocking calls into a Web Worker with a promise bridge
   (bigger redesign — only if measured bad).
3. **llvmlite API drift** — 0.50 deprecated `binding.initialize()`
   and removed `TargetMachine.target`. Mitigation: pin
   `llvmlite==0.50.0` in the toolchain; the M1 patch adapts the
   three call sites (`build.py` only).
4. **iOS final mile needs a Mac** — unavoidable (signing). The M7
   doc makes the handoff mechanical: object files + Xcode template
   + checklist. No iOS code is *designed* blind: the driver
   interface (§4.3) is platform-neutral by construction.
5. **Key custody on mobile** — provider keys ship inside apps.
   Standing rule: keys live in app-supplied config (Keychain/
   BuildConfig), never in the compiled binary; document accordingly.
6. **Play Store / App Store signing** — Hamis holds the keys and
   accounts; Phase 4 produces debuggable artifacts (APK, Xcode
   project), not store submissions.
7. **CameraButton on desktop web** degrades to a file picker by
   design (§5.2) — real capture is a mobile-browser property.
8. **Out of scope, unchanged:** streaming stays `E402`; LLVM
   optimization levels stay as-is; no new components; no reactive
   state.

---

## 7. iOS cross-compile path (M7 detail, for the record)

On this Linux box today: `misa build --target aarch64-apple-darwin
--emit-llvm` produces valid LLVM IR and (via llvmlite, proven §2)
valid Mach-O objects. The Mac-only final mile:

1. Transfer the object + `rt/*.c` (or a static lib) to the Mac.
2. `xcrun clang -arch arm64 -isysroot $(xcrun --sdk iphoneos
   --show-sdk-path)` compiles the runtime; link with
   `-framework Foundation -framework UIKit`.
3. `NSURLSession` transport implements `misa_platform_http_post`
   (ObjC file, ~150 lines).
4. UIKit driver per §4.4 behind `misa_ui.h`; `main.m` calls
   `misa_rt_init()` + `misa_main()`.
5. Signing & provisioning with Hamis's Apple ID/team; TestFlight or
   direct device install for the first phone ship.

Nothing about the Misa language, the emitter, or the driver
interface needs to change for this path — that is the point of the
audit in §1.

---

## 8. Implementation notes — M1 + M2 (2026-10-05)

**M1 — cross-target plumbing (`impl/misa/aot/build.py`, `impl/bin/misa`)**
- `misa build --target <triple>`: with `--target`,
  `binding.initialize_all_targets()` +
  `binding.initialize_all_asmprinters()` (never `binding.initialize()`,
  which raises in llvmlite 0.50), `Target.from_triple(triple)`,
  `create_target_machine()`, then `mod.triple = triple` and
  `mod.data_layout = str(tm.target_data)` (`TargetMachine.target` was
  removed in 0.50 — `Target.name` is used instead). Unknown triples
  fail cleanly with `E600`.
- The host triple builds + links exactly as v0.3 did; any other
  triple emits a relocatable object (default `<stem>.o`).
  `wasm32-unknown-wasi` and `aarch64-linux-android` objects were
  emitted on this box and magic-checked (`\0asm`, ELF).
- `misa build --list-targets` prints the per-target table: C compiler
  (`gcc` hosted; `emcc` / NDK clang / `xcrun clang` rows noted for
  M3/M6/M7), entry (`main` for every hosted target — B6), status.
- Runtime cache key (`_rt_key`) now hashes target triple + compiler id
  (B5) plus the full runtime source set (`misa_rt.c`, `misa_model.c`,
  `platform_linux.c`, `ui_text.c`, and the three headers).
- `_find_lib` resolves libraries per target (B4); non-host targets
  have no sysroot here yet.
- Toolchain env: the `misa` CLI runs on system `python3`
  (`#!/usr/bin/env python3`); `llvmlite==0.50.0` is installed there
  and pinned in `impl/requirements.txt`.

**M2 — platform abstraction layer (`impl/misa/aot/rt/`)**
- New `misa_platform.h`: P1 `misa_platform_log/_err/_flush`, P2
  `misa_platform_fatal` (never returns), P3 `misa_platform_getenv`,
  P4 `misa_platform_fs_path`, plus `MisaHttpResp` /
  `misa_platform_http_post` / `misa_platform_http_free` (§1.4).
- New `platform_linux.c`: P1→stdio/stderr, P2→stderr+exit(1),
  P3→`getenv`, P4→identity (cwd, v0.3 behavior); the libcurl POST
  backend moved here verbatim from `misa_model.c` (now behind the
  platform interface, malloc-backed with `http_free`).
- New `misa_ui.h`: the §4.3 driver interface, verbatim.
- New `ui_text.c`: the text driver implementing `misa_ui.h`, plus the
  7 `misa_screen_*` functions as shims over a process-global driver
  instance. Head-line framing (`(`/`)`/newline) closes lazily but
  byte-identically, including partial lines when an argument
  expression raises mid-stream. `misa_ui_get_text` returns NULL and
  `misa_dispatch_handler` returns -1: no live widgets or compiled
  handlers exist yet (M4).
- `misa_rt.c`: OOM→`misa_platform_fatal`, `print`→platform log,
  uncaught-error report→platform error log (piecewise writes, no
  truncation), `env()`→platform getenv, `fs.read`/`fs.write`→P4 path
  mapping; the screen functions moved to `ui_text.c`.
- `misa_model.c`: `MISA_MOCK_FILE` / key / base-URL lookups→platform
  getenv; the network branch calls `misa_platform_http_post` and
  frees with `misa_platform_http_free`; `base64_encode` and
  `jarr_get` are now unconditional (provider logic, not transport).
  The no-curl `E401` degradation path is unchanged.
- `misa_rt.h` still declares the 7 `misa_screen_*` functions (the
  emitter is untouched in M2); platform/UI headers are runtime-internal.

**Verification (2026-10-05):** interpreter suite 74/74, AOT parity
12/12, cross-target object emission magic-checked — see the M1/M2
completion report.

**M3 — Web first slice (Emscripten + DOM) — implemented 2026-10-05**
- `scripts/install_emsdk.sh`: pins emsdk 4.0.10, installs to `~/workspace/emsdk`
  (`TAR_OPTIONS=--no-same-owner` required — tar ownership fails as root).
- `impl/misa/aot/build.py`: `wasm32-unknown-emscripten` target row. `emcc`
  compiles `misa_rt.c`, `misa_model.c`, `platform_emscripten.c`, `ui_dom.c`;
  llvmlite emits the program object; `emcc` links to `misa.js`+`misa.wasm`.
  Flags: `-s WASM=1 -s ASYNCIFY=1 -s MODULARIZE=1 -s EXPORT_NAME=MisaModule`
  `-s EXPORTED_FUNCTIONS=_misa_ui_boot,_misa_dispatch_handler,_misa_dom_photo,_misa_platform_fs_bootstrap`
  `-s EXPORTED_RUNTIME_METHODS=ccall -s ALLOW_MEMORY_GROWTH=1 -s FETCH=1`
  `--js-library rt/dom_bridge.js --pre-js rt/dom_bridge_pre.js`.
  `--preload-file` plumbed through `impl/bin/misa`.
- **GC decision:** malloc fallback for web (`-DMISA_USE_GC` off). wasm is
  86KB (far under ~2MB budget). bdwgc-under-emscripten experiment deferred.
- `rt/platform_emscripten.c`: P1→console.log/error; P2→overlay+abort() (never
  kills page); P3→`window.MISA_ENV` (getenv-like static buffer); P4→`/misa_fs`
  + IDBFS mount/populate via `misa_platform_fs_bootstrap()` (Asyncify-blocking;
  shell ccalls before boot); `http_get` via `emscripten_fetch` (async callbacks
  + `emscripten_sleep` poll with deadline watchdog — `emscripten_fetch_wait`
  is deprecated/non-functional since 3.1.62); `http_post` stub → M5 (network
  model calls degrade to catchable E401).
- `rt/ui_dom.c` + `rt/dom_bridge.js`: §5.2 component→DOM mapping inside
  `#misa-root`. Button click→`Module.ccall('misa_dispatch_handler')`;
  CameraButton→hidden file input→staged to `/misa_uploads/`→Photo handle via
  exported `misa_dom_photo`; `misa_ui_report_error`→dismissible banner+console.
- **`--js-library` `$` dependency bug (fixed):** Emscripten's `$`-prefixed
  js-library dependencies do NOT resolve when the calling object file was not
  compiled by emcc (llvmlite-generated prog.o). The `$MisaDom` symbol was
  emitted literally → `ReferenceError` at boot. Fix: `rt/dom_bridge_pre.js`
  (`--pre-js`) defines `var MisaDom` as a module-scope global; `dom_bridge.js`
  references it directly (no `$`).
- `web/shell.html`: `?app=` selects app JS (default `misa.js`); `?env_KEY=`/
  `?mock=` populate `window.MISA_ENV`; IDBFS bootstrap then `misa_ui_boot`;
  boot errors via `window.misaBootError`.
- **Stack overflow bug (fixed):** `misa_model.c` had `char cur_prompt[65536]`
  as a stack local. Emscripten's default 64KB stack cannot hold it →
  wasm OOB segfault on any model call. Fixed by heap-allocating via
  `misa_alloc` (64KB leak per model call on web; acceptable for M3).
- `examples/webslice.misa` + `webslice.mock.json`: exercises Heading/Text/
  Banner/Row/Spacer/List/TextInput+id/Button+navigate/mock model/budget breach.
  Mock JSON keys are MODEL NAMES (e.g., "echo"), not provider args — matches
  existing `plantcare.misa`/`mocks.json` semantics.

**M4 — Compiled event handlers + `id:` registry — implemented 2026-10-05**
- `impl/misa/ast.py`: `id_bindings: List[Tuple[str,str]]` on `ScreenDecl`.
- `impl/misa/checker.py`: `_check_screen` records ordered `id:` table
  (name→kind, slots 0..n-1; ids inside `ScreenFor` excluded); `_check_handler_body`
  type-checks with scope=lambda params+id names; `_infer`: bare id→E201,
  `<id>.text` on TextInput→Text, else→E101; lambda >1 param→E206. Duplicate→E207.
- `impl/misa/aot/emit.py`: each handler lambda → `misa_handler_<screen>_<n>(MVal*)`
  with FnCtx; `misa_handler_table[]` + `misa_num_handlers`;
  `misa_screen_comp_arg_handler` takes handler_id; new `misa_screen_comp_bind_id(slot)`;
  `<id>.text` → `misa_ui_get_text(misa_ui_active(), misa_ui_find_bound(...))`;
  E600 for `id:` in ScreenFor and closures over locals/screen params.
  **Deviation:** kept `misa_screen_*` call-stream style (extended) instead of
  widget-handle emission — minimal diff, preserves text-driver parity.
- `rt/misa_ui.h`: `misa_ui_active()`, handler table externs. `rt/ui_text.c`:
  real `misa_dispatch_handler` (table lookup; uncaught→`misa_ui_report_error`,
  never kills); 3-arg shim; bind shim. `rt/misa_rt.h`: shims updated.
- Entry: `Emitter(merged, entry=...)`; `misa_ui_boot` for web, `main` for host.

**Verification (2026-10-05):**
- Host: interpreter 74/74, AOT parity 12/12 (a10_screen byte-identical).
- Web (headless Chromium via CDP, `file://` URLs — Chrome 152 LNA policy
  blocks `http://localhost` in this sandbox): 6/7 pass.
  - PASS: boot, DOM-vs-text-tree node-for-node, click→navigate, TextInput
    readback ("Ada"), mock model ("four"), E500 budget breach (catchable in-page).
  - FAIL (environment): http.get — Chrome LNA blocks `http://127.0.0.1` fetches
    from `file://` pages; `emscripten_fetch` plumbing is correct (code inspection),
    http.get works on host (3666 bytes for shell.html). Not a Misa bug.
- Test harness: `tests/web/cdp_web_test.js` (Node + CDP via remote-debugging-port).

**M5 — Model calls on web — implemented 2026-10-05**
- `build.py`: `wasm32-unknown-emscripten` runtime flags now add
  `-DMISA_USE_FETCH`; `misa_model.c`'s network branch is
  `#if defined(MISA_USE_CURL) || defined(MISA_USE_FETCH)` (the
  request-body construction is fully portable; only the transport
  call differs). No-transport builds keep the catchable E401.
- `rt/platform_emscripten.c`: `misa_platform_http_post` implemented
  via `emscripten_fetch` — POST method, body as `requestData`, same
  Asyncify `emscripten_sleep` poll + deadline watchdog as the GET
  path. **Two real bugs found by testing and fixed:**
  1. Emscripten's `requestHeaders` is a NULL-terminated array of
     key,value pointer PAIRS (Fetch.js steps 2 pointers) — not
     `"Key: Value"` strings. Passing `"Authorization: Bearer ..."`
     as one string silently sent NO headers. Fixed by splitting at
     the first colon into pairs (keys copied for NUL-termination).
  2. `misa_platform_getenv` on web returns a STATIC buffer
     overwritten by the next call; `provider_complete` held `key`
     across the base-URL lookup, so the Authorization header
     contained the base URL, not the key. Fixed by snapshotting the
     key into a stack buffer immediately (all other getenv callers
     audited — they use the value immediately and are safe).
- **Leak fix:** `cur_prompt` (64KB) is now released on all 7 exit
  paths of `misa_model_call`. Added `misa_free()` to the runtime API
  (`misa_rt.h`/`misa_rt.c`): raw `free()` on Boehm `GC_malloc` memory
  corrupts the collector's heap (this segfaulted a08/a09 on host
  before the fix — 10/12). `misa_free` picks `GC_free`/`free`.
  Also fixed a latent truncation bug: the repair-prompt
  `snprintf(cur_prompt, sizeof cur_prompt, ...)` used the POINTER
  size (8), not the 64KB capacity.
- New `examples/webmodel.misa` (openai provider, `?app=webmodel.js`;
  key + `MISA_OPENAI_BASE_URL` via `?env_` bridge, never in wasm),
  `tests/web/stub_server.py` (serves `web/` + OpenAI-protocol stub
  `POST /api/chat/completions`, CORS-enabled for `file://` pages),
  `tests/web/cdp_web_m5_test.js`.

**Verification (2026-10-05):**
- Host: interpreter 74/74, AOT parity 12/12 (unchanged behavior).
- Web regression: `cdp_web_test.js` 8/8 over `file://` (all M3/M4
  checks; http.get now passes too — the CORS-enabled stub lets the
  `file://` page fetch `http://localhost:8471/shell.html`).
- Web M5: `cdp_web_m5_test.js` 7/7 — network model call POSTs to the
  local stub and returns validated data (`net ok: four`; stub log
  proves method=POST, `Authorization: Bearer` header present,
  `response_format={'type':'json_object'}`); dead-port base URL →
  catchable `E400` in-page, page stays alive; missing key →
  catchable `E401`. wasm stays ~94KB.
- **Honest limit:** the sandbox browser has no external network, so a
  live HTTPS POST to a real provider was NOT proven in-page. Proven
  instead: the full transport mechanics end-to-end against a
  same-origin OpenAI-protocol stub (request construction, headers,
  body, response parsing, validation, budgets, error mapping). The
  request-body code is shared verbatim with the host libcurl path,
  which was proven against a stub in v0.3.

## 9. Implementation notes — M6, Android (2026-10-05)

**Toolchain (pinned, `~/workspace/android-ndk`, `~/workspace/android-sdk`)**
- NDK **r27d** (LTS, clang 18.0.4): `dl.google.com` throttles a plain
  curl to a hard stop at exactly 316,227,584 bytes, so the 664 MB zip
  was fetched as 8 parallel ranged chunks with explicit resume ranges
  (`-r $((S+have))-$E`; note: `curl -C -` *overrides* `-r` and
  over-downloads — never combine them). Verified byte count
  663,956,036, unzipped to `~/workspace/android-ndk/android-ndk-r27d`.
- SDK: platform-tools (adb), build-tools 34 (`aapt`/`aapt2`/`d8`/
  `apksigner`/`zipalign`), platform android-35 (`android.jar`).
  JDK 21 at `~/workspace/tools/jdk-21`.

**`impl/misa/aot/build.py`**
- TARGETS rows: `aarch64-linux-android` (cc
  `aarch64-linux-android34-clang`, cc_id `ndk-aarch64-r27d`) and
  `armv7-linux-androideabi` (`armv7a-linux-androideabi34-clang`,
  `ndk-armv7-r27d`), entry `misa_ui_boot`, status
  "runtime: M6 (debuggable APK)". `_emit_object` uses `reloc="pic"`
  for android triples (host path untouched).
- `runtime_flags()` android: `-DMISA_USE_GC` + `-I…` + `libgc.a`
  when `~/workspace/deps/gc-android-<arch>/include/gc.h` exists
  (else silent malloc fallback); always `-DMISA_USE_JNI_HTTP`;
  link libs `-llog -landroid -lm`. `_rt_key` verified
  collision-free across host/emscripten/aarch64-android/armv7-android.
- `ensure_runtime()` → `_ensure_runtime_ndk`: NDK clang
  `-O2 -fPIC -Wall -Werror -DANDROID`, cached per target+defines.

**GC experiment (decided per data): Boehm wins.**
- bdwgc 8.2.8 cross-builds cleanly for both ABIs with NDK clang.
  Two data-driven fixes: (1) cross-compile skips the intrinsics
  probe, so `configure` hard-requires libatomic_ops — fetched v7.8.2
  into the source tree per upstream's recipe; (2) the static lib
  needs `-fPIC` or `ld.lld` refuses the `.so` link
  (`R_AARCH64_ADR_PREL_PG_HI21 ... recompile with -fPIC`).
- `~/workspace/deps/gc-android-aarch64/lib/libgc.a` (409 KB),
  `gc-android-armv7` (284 KB). Recipe: `scripts/build_bdwgc_android.sh`.
- The M6 APK therefore links the real collector (every `MVal` via
  `GC_malloc`) — same memory semantics as the host binary.

**HTTP: real JNI backend (option (a), the doc's §1.4 Android choice).**
- `platform_android.c` implements `misa_platform_http_post/get`
  via JNI → `java.net.HttpURLConnection` (timeouts from the call's
  latency budget, exception draining → `MisaHttpResp.err`).
- `misa_model.c`'s network path is now guarded
  `#if defined(MISA_USE_CURL) || defined(MISA_USE_FETCH) ||
  defined(MISA_USE_JNI_HTTP)` — the request-body code is shared
  verbatim; only the transport differs. No libcurl on Android.

**JNI entry (§1.1 B6): `rt/jni_android.c` (+ `jni_android.h`).**
- `JNI_OnLoad` caches the VM + 11 `MisaWidgets` method ids;
  `nativeBoot` (global ref to widgets, files dir) → emitted
  `misa_ui_boot()` (= `misa_rt_init()` + `misa_main()` with the
  Android driver); `nativeDispatch`, `nativePhotoResult` (builds the
  Photo `MVal`), `nativeSetConfig`. All four Java↔native signatures
  verified pairwise.
- Threading (documented in `jni_android.h`): `MisaActivity` owns a
  `HandlerThread("Misa")`; every native entry is posted to it; view
  ops marshal to the UI thread inside `MisaWidgets` (`runOnUiThread`
  + latch when a result is needed; direct call when already on the UI
  thread — no deadlock); network always runs on the Misa thread.

**Views driver: `rt/ui_android.c`** implements `rt/misa_ui.h` via
JNI — Column/Row→`LinearLayout`, Text/Heading→`TextView`,
Banner→tinted `TextView`, Button→`Button` (+`OnClickListener` →
`misa_dispatch_handler`), TextInput→`EditText` (+`get_text`
readback), CameraButton→`Button` + `takePicture`, Image→`ImageView`,
List→`LinearLayout` of rows, `find_bound`/`bind_id`, `show_screen`,
`report_error`→toast. Review-fixed: C handle vs Java view id are
stored separately; `reportError`'s `(String)` call shape uses its own
helper (not the `(int, String)` one).

**`rt/platform_android.c`**: P1→`__android_log_print` (chunked at
1 KB); P2→logcat + toast + activity finish — **never `exit()`**
(the thread parks); P3→app-supplied config table via
`nativeSetConfig` (intent extras; keys never baked in); P4→
`<filesDir>/misa_fs` (private sandbox).

**App template `android/`**: `AndroidManifest.xml`
(`com.misa.app`, INTERNET, `minSdk 24`/`targetSdk 35`,
`debuggable=true`), `MisaNative.java`, `MisaWidgets.java` (11
components; camera via `ACTION_IMAGE_CAPTURE` + MediaStore
`content://` URI, no androidx), `MisaActivity.java` (intent
extras→`nativeSetConfig`, camera result forwarding).

**Deviations from §4.4 (deliberate, M6 slice):**
- `List`→vertical `LinearLayout` of `TextView` rows, not
  `RecyclerView` (avoids the androidx aar dependency cascade;
  identical single-text-row shape; recycling is a later
  optimization).
- `CameraButton`→`Button` + `ACTION_IMAGE_CAPTURE` intent
  (no `ActivityResultContracts` — needs androidx.activity).

**APK: `scripts/build_apk.py`** — `.misa` → `misa build` (PIC .o)
→ `ensure_runtime` → NDK `-shared` link → `javac --release 8` →
`d8 --min-api 24` → `aapt package`/`add` → `zipalign` →
`apksigner` (generated debug key). Two real bugs fixed during the
build: apksigner needs `--ks-pass pass:android` (not
`--ks-pass:android`); `classes.dex` must be `aapt add`ed at the APK
root (not `dex/classes.dex`); plus an aapt1 quirk — it rejects XML
*comments* in the manifest ("not well-formed"), so the script
strips them when staging.

**Verification (this box — no Android device/emulator exists here):**
- (a) `misa build --target aarch64-linux-android` → valid ELF64
  `e_machine=AArch64` object; armv7 → valid ELF32 ARM object;
  entry symbol `misa_ui_boot` present in the emitted LLVM.
- (b) Full runtime (5 objects/target) compiles under NDK clang
  `-Wall -Werror` with **zero warnings** for both ABIs (the gate
  caught and fixed two real issues: unused-function errors in
  `misa_model.c` when the network path was excluded, since fixed by
  wiring `MISA_USE_JNI_HTTP`).
- (c) `libmisa.so` links for both ABIs (arm64 306,712 B; armv7
  326,536 B): no TEXTREL, 4 JNI symbols exported, `misa_ui_boot`
  defined, NEEDED = system libs only (liblog/libandroid/libm/
  libdl/libc).
- (c) `/tmp/m6/misa.apk` (283,109 B): `aapt dump badging`
  (`com.misa.app`, sdkVersion 24, debuggable) and
  `apksigner verify --print-certs` both pass; contents =
  `AndroidManifest.xml` + both `libmisa.so` + root `classes.dex` +
  `META-INF`.
- Host suites green after all M6 changes: interpreter **74/74**,
  AOT parity **12/12** (one transient 9/12 mid-session was traced to
  /tmp filling up from download scratch — not a code issue).
- Install/run instructions + the exact device-unverified list:
  `docs/android.md`. **Not claimed: a working phone app — no
  device proof exists.**
