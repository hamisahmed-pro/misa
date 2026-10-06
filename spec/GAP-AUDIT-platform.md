# Platform Support Map — Adversarial Audit (Workstream 4)

**Date:** 2026-10-06 · **Auditor:** subagent (depth 2) · **Repo:** `~/workspace/misa`
**Scope:** namespace × target support matrix. No fixes applied; `selfhost/` untouched.

**Targets:** interp (`misa run`) · host AOT (`misa build` → native binary) ·
Android (APK via `scripts/build_apk.py`) · iOS (via `.github/workflows/ios.yml` + `rt/platform_apple.m`) ·
web (wasm via `wasm32-unknown-emscripten`).

**Evidence legend:** `[T]` = actually executed on this machine · `[I]` = code-inspected only ·
`[U]` = unreached (could not execute; reason given). "Links clean" ≠ "runs correctly"
— on-device execution was impossible for Android/iOS here.

---

## 1. The matrix

| Namespace | interp (`misa run`) | host AOT | Android (APK) | iOS | web (emscripten) |
|---|---|---|---|---|---|
| `fs` | ✅ works `[T]` | ✅ works `[T]` | ✅ impl present (P4 sandbox `<filesDir>/misa_fs`) `[I]`; links `[T]` | ✅ impl present (Documents sandbox, escape-reject) `[I]` | code present (vFS + IDBFS) `[I]` — **target broken, §2.1** |
| `http.get` | ✅ works `[T]` | ✅ works `[T]` | ✅ JNI→HttpURLConnection `[I]`; symbol in shipped `.so` `[T]` | ✅ NSURLSession `[I]` | code present (emscripten_fetch) `[I]` — target broken |
| `http.post` / `post_json` | ✅ works, correct Content-Types `[T]` | ✅ works, byte-identical, correct Content-Types `[T]` | ✅ JNI, prepends caller Content-Type `[I]` | ❌ **ABI mismatch → crash** `[I]` §2.3 | code present `[I]` — target broken |
| `str` (all 6) | ✅ works `[T]` | ✅ works, byte-identical `[T]` | ✅ generic `misa_rt.c` `[I]` | ✅ generic `[I]` | generic `[I]` — target broken |
| `process.run` | ✅ works `[T]` | ⚠️ **rejects with E101, not E600** `[T]` §2.4 | ⚠️ same E101 `[I]` (no impl anywhere) | ⚠️ same E101 `[I]` | ⚠️ same E101 `[I]` |
| `db.*` | ✅ works `[T]` | ✅ works, byte-identical `[T]` | ✅ amalgamation linked when used — **266 `sqlite3_*` syms + `misa_db_open` in shipped `libmisa.so`** `[T]` | 🚫 clean E600 `"db.* is not supported … (host and Android only)"` `[T]` | 🚫 clean E600 `[T]` (moot — target broken) |
| `json` | ✅ works `[T]` | ✅ works, byte-identical `[T]` | ✅ `misa_json.c` in APK sources `[I]` | ✅ generic `[I]` | generic `[I]` — target broken |
| `time.now` / `now_ms` / `format` | ✅ works `[T]` | ✅ works `[T]` | ✅ generic `[I]`; NDK `-Wall -Werror` clean `[T]` | ✅ generic (`__APPLE__` branch) `[I]` | generic `[I]` — target broken |
| `time.parse` | ✅ works `[T]` | ❌ **SIGSEGV (exit 139)** `[T]` §2.2 | ✅ impl OK under Bionic `[I]` | ✅ impl OK `[I]` | ❌ won't compile (same root cause as §2.1) `[T]` |
| `math` (all 6) | ✅ works `[T]` | ✅ works, byte-identical `[T]` | ✅ generic, `-lm` at link `[I]` | ✅ generic `[I]` | generic `[I]` — target broken |
| `extern` (FFI) | ✅ works (`c`, `m`) `[T]` | ✅ works (`c`, `m`) `[T]` | ✅ `c`/`m` link (ffi APK built) `[T]`; other libs → **raw NDK link failure, no E-code** `[I]` §2.7 | ⚠️ `c` fine `[I]`; other libs → raw Xcode link failure, no E-code `[I]` §2.8 | ⚠️ `c`/`m` resolve via musl `[I]`; other libs → E600 with raw linker text `[I]` |
| `model` (cloud) | ✅ works (mock) `[T]` | ✅ works (mock), byte-identical `[T]` | ✅ JNI HTTP transport (`MISA_USE_JNI_HTTP`) `[I]` | ✅ NSURLSession transport `[I]` | code present `[I]` — target broken |
| on-device `.onnx` | ⚠️ **raw Python traceback** w/o `onnxruntime` pkg `[T]` §2.5 | ✅ **build + run, real inference** (ORT `.so` staged, bit-correct) `[T]` | 🚫 clean E504 at build `[T]` | 🚫 clean E504 at build `[T]` | 🚫 clean E504 at build `[T]` |
| on-device `.tflite` | 🚫 clean E503 (+pip hint) `[T]` | 🚫 clean E504 at build `[T]` | ✅ `MisaTflite` (NNAPI/CPU) + asset staging `[I]`; `.tflite` accepted at build `[T]` | 🚫 clean E504 at build `[T]` | 🚫 clean E504 `[T]` |
| on-device `.mlpackage` | 🚫 E503 per `providers.py` `[I]` | 🚫 clean E504 at build `[T]` | 🚫 clean E504 at build `[T]` | ✅ `misa_coreml_infer` impl `[I]`; E504 lifted for Apple targets (object builds) `[T]` | 🚫 clean E504 `[T]` |
| screens / UI | ✅ works (text driver) `[T]` | ✅ works, byte-identical `[T]` | ✅ `ui_android.c` Views driver `[I]` | ✅ `ui_uikit.m` + CI sim smoke test `[I]` | ✅ `ui_dom.c` `[I]` — target broken |

`wasm32-unknown-wasi`: object-only per the target table (honest); `misa build --target
wasm32-unknown-wasi` emitted an object `[T]`. Not in the required matrix.

---

## 2. Findings (severity-rated)

### CRITICAL

**2.1 Web target cannot compile ANY program** `[T]`
`misa build --target wasm32-unknown-emscripten` fails on *every* program — even one that
never touches `time` — because the shared C runtime doesn't compile under emcc:

```
misa: error: C runtime compile failed (misa_rt.c):
misa_rt.c:1317:9: error: use of undeclared identifier 't'
```

Root cause (`impl/misa/aot/rt/misa_rt.c`, `misa_time_parse`): the `timegm` selection is
`#if defined(__linux__) || defined(__APPLE__) || defined(__FreeBSD__)` / `#else
{ …portable fallback… return …; }` / `#endif`, followed by unconditional code using `t`.
emcc defines `__EMSCRIPTEN__` but **not** `__linux__` (verified via `emcc -dM -E`), so the
`#else` branch is taken, returns early, and the trailing `if (t == (time_t)-1)` references
an undeclared identifier → hard error. The target table claims `wasm32-unknown-emscripten`
is `"full build (M3)"`. Repro: `misa build t_web.misa --target wasm32-unknown-emscripten`
with `t_web.misa` = five `print` lines (no time usage). Two compounding problems:
(a) the failure carries **no E-code** — it surfaces via `die("C runtime compile failed…")`
as a raw string, not the E600 the error contract promises for build failures
(`spec/ERRORS.md`: E600 = "AOT/build failure"); (b) `misa serve` (which builds to wasm
internally) dies with an **unhandled Python traceback** (`RuntimeError` escaping
`cmd_serve`) instead of even the `die()` path.

**2.2 Host AOT segfaults on `time.parse`; interpreter is fine** `[T]` — interp/AOT divergence.
```misa
print(time.parse("2024-01-15", "%Y-%m-%d"))
```
interp prints `1705276800`; the AOT binary prints `before` then dies with **SIGSEGV
(exit 139)**. Root cause: `strptime` is **implicitly declared** in the host compile —
no `_XOPEN_SOURCE`/`_DEFAULT_SOURCE` is defined when building `misa_rt.c`
(verified: `gcc -c misa_rt.c -Wall` → `warning: implicit declaration of function
'strptime'`). Implicit declaration assumes `int` return; the 64-bit pointer is truncated
and `*r == '\0'` dereferences garbage. Characterization: **host-only**. The Android NDK
build compiles the same file with `-Wall -Werror` and succeeded (Bionic declares
`strptime`), and the iOS path takes the `__APPLE__` branch where the declaration is
visible. So the bug is in the host build flags / missing feature-test macro, not the C
logic — but on this machine every host binary carrying `time.parse` crashes.

**2.3 iOS `http.post`: silent ABI mismatch → crash on any POST** `[I]` (no Mac to execute;
would crash on device).
`impl/misa/aot/rt/misa_platform.h:99` declares
`misa_platform_http_post(url, content_type, headers, nheaders, body, timeout_s, out)`
(7 params) and both callers (`misa_rt.c:1189`, `misa_model.c:1529`) pass 7 args — but
`platform_apple.m:159` **defines it with 6 params** (no `content_type`), hardcoding
`Content-Type: application/json`. Because `misa_rt.h` does not include `misa_platform.h`,
no prototype is visible at the definition → **zero compile-time diagnostic**; the linker
only sees a symbol. On arm64 the arguments shift by one slot: the callee's `out` receives
the bit pattern of `30.0` (the caller's `timeout_s`) as a pointer, and `misa_http_run`
`memset`s a `MisaHttpResp` through it → segfault/memory corruption. Blast radius: every
`http.post`, `http.post_json`, and network model call on iOS. (Even absent the crash, the
hardcoded header is wrong: `http.post` must send `text/plain`.) This is the "no E600 and
no implementation = silent wrong behavior" case — except it's a latent ABI break, not a
missing stub. `http.get` is unaffected (signatures match).

### HIGH

**2.4 `process.run`: type-checks, then AOT rejects with E101 instead of E600** `[T]`
`process.run` is in the checker's namespace table (`checker.py:46`) and documented in
`spec/STDLIB.md` with **no AOT caveat** — but `emit_builtin` has no `process.run` case,
so `misa build` fails with `E101: undefined function 'process.run'`. E101 means
"undefined name" (a *user* type error); the contract for "unsupported construct for the
target" is **E600** (`spec/ERRORS.md`). There is no `misa_process_*` anywhere in the C
runtime — zero AOT implementation on *any* target — so the mislabeled E101 is the only
thing standing between the program and a link failure. Compounding: **no test program
anywhere exercises `process.run`** (interp or AOT; `grep -rn "process\.run(" tests/
--include="*.misa"` is empty), so the suite cannot catch the divergence. The honest
matrix cell is "interp-only, cleanly rejected on AOT" — today it is "interp-only,
rejected with the wrong code."

**2.5 Interp on-device without optional deps → raw Python traceback, not E503** `[T]`
`misa run examples/plantdoc_edge.misa` on a machine without the `onnxruntime` pip
package dies with an unhandled `ModuleNotFoundError` traceback through the `misa` CLI
(`providers.py:321`, bare `import onnxruntime as ort` in `_ort_session`). The error
contract has E503 = "runtime not available on this platform", and the `.tflite` path
demonstrates the intended UX — a clean `E503: TFLite models need the 'ai-edge-litert'
package (pip install ai-edge-litert)`. The `.onnx` path should do the same.

**2.6 Build failures escape without E-codes in two places** `[T]`
(a) Web runtime compile failure → `die("C runtime compile failed…")`, raw string, no
E600 (see §2.1). (b) `misa serve` → unhandled `RuntimeError` traceback (see §2.1). The
CLI's `cmd_build` wraps `build()` in `try/except RuntimeError → die(...)`; `cmd_serve`
has no such guard. Every build failure should surface as a Misa E-code (E600).

### MEDIUM

**2.7 Android `extern` for non-libc/libm libraries → raw NDK link failure** `[I]` + partial `[T]`
`build()` appends `-l<lib>` for `extern` decls **only on the host link path**
(`build.py:836-841`); `scripts/build_apk.py` links `libmisa.so` with a fixed
`["-llog", "-landroid", "-lm"]`. Verified `[T]`: an APK using `extern "c"` (strlen/strcmp)
+ `extern "m"` (cos/pow) builds, links, signs, and verifies cleanly — so the
"Android: `extern` works via NDK libraries (at least `libc` math)" box in
`spec/PHASE7-FFI.md` is satisfiable at link level. But `extern "sqlite3"` (or any third
lib) would fail at `.so` link with a raw `RuntimeError: native link failed` — no Misa
E-code, no per-target E600/E504-style routing. (Runtime behavior of FFI on Android is
`[U]` — no device/emulator; `adb devices` is empty.)

**2.8 iOS `extern` for non-libc libraries → raw Xcode link failure** `[I]`
`misa build --target aarch64-apple-ios-simulator` emits only an object file; the Xcode
project (`ios/MisaApp`) performs the final link with no per-program `-l<lib>` injection
for `extern` decls. `extern "c"` resolves against libSystem; anything else fails at the
Xcode link step outside any Misa E-code. `[U]` — no macOS to execute.

**2.9 `misa serve --help` unhandled** `[T]` (minor)
Prints `misa serve: unknown argument: --help`. No help text exists for serve; every other
subcommand documents itself via the top-level usage.

### Accurate rejections (contract working — positive control)
- `db.*` → iOS Simulator / web: clean `E600: db.* is not supported for target '…'
  (host and Android only)` `[T]`.
- On-device cross pairs (`.tflite`→host, `.mlpackage`→host, `.onnx`→web, `.tflite`→iOS…):
  clean **E504 at build time** with a per-pair reason `[T]`; E504 correctly *lifted* for
  `.mlpackage`→Apple targets (object builds) and `.tflite`→Android (object builds) `[T]`.
- Interp `.tflite` without the package: clean **E503** with install hint `[T]`.

---

## 3. What was actually executed vs inspected vs unreached

**Executed `[T]`** (this machine, 2026-10-06):
Host interp + host AOT build-and-run for: `fs`, `http.get`, `http.post`, `http.post_json`
(against a local echo server — Content-Types verified), `str` (6 fns), `process.run`,
`db` (open/exec/query/close), `json`, `time` (4 fns — `time.parse` segfault isolated to a
3-line repro), `math` (6 fns), `extern` (`c`+`m`), cloud `model` (mock provider), screens
(text driver), on-device `.onnx` (build + run, real inference, ORT `.so` staged),
on-device `.tflite`/`.mlpackage` build-time E504s, `db`→iOS/web E600s, `mlpackage`→iOS-sim
and `tflite`→Android object acceptance, `wasi` object emission. Android: **two full APKs
built end-to-end** (`t_db.apk`, `t_ffi.apk` — NDK compile incl. `-Wall -Werror`, dex,
aapt package, zipalign, apksigner sign+verify); sqlite/TFLite symbols confirmed in the
shipped `libmisa.so` (266 `sqlite3_*` dynamic symbols, `misa_db_open`,
`misa_platform_http_get/post`, `misa_time_parse` exported). Web: build attempted —
fails at C runtime compile (§2.1). Tooling: `misa lsp` (starts, "listening on stdio"),
`misa pkg list` (works), `misa serve` (broken, §2.1/2.6), `misa build --list-targets`.

**Inspected `[I]`** (read, not executed): `platform_android.c` (P1–P4, JNI HTTP, TFLite
marshal), `platform_apple.m` (P1–P4, NSURLSession, CoreML backend, §2.3 mismatch),
`platform_emscripten.c` (log/fatal/env/vFS+IDBFS/fetch HTTP), `build_apk.py` (full
pipeline read before running), `ios.yml` (CI flow), `ui_*.c`/`ui_*.m` drivers (existence
+ entry points only), `providers.py` on-device dispatch.

**Unreached `[U]`**: no Android device/emulator (`adb devices` empty; the x86_64-android
"emulator CI only" target can't boot here) → **Android runtime behavior never executed**;
no macOS → **iOS never executed** (repo's own `ios.yml` CI is the execution path);
web execution impossible (target doesn't compile); `wasm32-unknown-wasi` runtime
(object-only by design).

---

## 4. Target-table honesty check (`misa build --list-targets`)

| Claimed | Reality |
|---|---|
| `x86_64-linux-gnu`: "full build (host)" | ✅ True — with the `time.parse` segfault caveat (§2.2) and `process.run` E101 caveat (§2.4) |
| `wasm32-unknown-wasi`: "object only in M1" | ✅ True (and honest) |
| `wasm32-unknown-emscripten`: "full build (M3)" | ❌ **False** — cannot compile any program (§2.1) |
| Android triples: "runtime: M6 (debuggable APK)" | ⚠️ Half-true — APK pipeline works end-to-end (proven), but runtime behavior is unverified here and FFI-lib routing has no E-code (§2.7) |
| Apple triples: "object only (link+sign on a Mac…)" | ⚠️ Object emission works; but `http.post` will crash at runtime (§2.3) — "object only" undersells the risk |

## 5. Suggested follow-ups (for the parent orchestrator, not this audit)
1. `misa_rt.c`: add `-D_XOPEN_SOURCE=700` (or `_DEFAULT_SOURCE`) to host runtime defines **and**
   fix the `#else`-branch/`t` scoping so non-glibc targets compile (§2.1, §2.2 — likely one
   root fix area; Android/iOS unaffected).
2. Teach `platform_apple.m` the 7-arg `misa_platform_http_post` (or route Apple through the
   header signature) — currently a guaranteed crash on any iOS POST (§2.3).
3. `emit_builtin`: map `process.run` → `AOTError("E600", …)` (or implement it); add a
   `process.run` parity test program (§2.4).
4. Wrap the `onnxruntime` import in `_ort_session` → E503 with install hint (§2.5).
5. Give `ensure_runtime` failures and `cmd_serve` the E600 path instead of `die()`/traceback
   (§2.1, §2.6).
6. Route `extern` `-l<lib>` flags into `build_apk.py`'s `.so` link and surface a clean
   E-code when a lib is unavailable for Android/iOS (§2.7, §2.8).
