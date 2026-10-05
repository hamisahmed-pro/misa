# Misa on Android (Phase 4, M6)

One `.misa` codebase → a debuggable APK with **native Android Views**
(`LinearLayout`, `TextView`, `Button`, `EditText`, `ImageView` …) —
no WebView, no cross-platform renderer. The same program also builds
to the host binary and the web target unchanged.

## What was built (M6, 2026-10-05)

| Piece | Location | Notes |
|---|---|---|
| NDK toolchain | `~/workspace/android-ndk/android-ndk-r27d` | pinned r27d (LTS) |
| SDK tools | `~/workspace/android-sdk/` | platform-tools (adb), build-tools 34 (aapt/d8/apksigner/zipalign), android-35 platform |
| Platform layer | `impl/misa/aot/rt/platform_android.c` | logcat, JNI config, sandbox FS, `HttpURLConnection` transport |
| Views driver | `impl/misa/aot/rt/ui_android.c` | `misa_ui.h` via JNI → `MisaWidgets.java` |
| JNI entry | `impl/misa/aot/rt/jni_android.c` | `JNI_OnLoad` + `MisaNative.*` native methods → emitted `misa_ui_boot()` |
| App template | `android/` | `AndroidManifest.xml` + `java/com/misa/app/*.java` |
| APK builder | `scripts/build_apk.py` | one command: `.misa` → signed debuggable APK |
| Demo app | `examples/android.misa` | Heading/Text/Banner/Row/List/TextInput + Button → navigate |

## Build the APK

```bash
cd ~/workspace/misa
python3 scripts/build_apk.py --misa examples/android.misa \
    --out /tmp/m6/misa.apk            # default out
# --abis arm64,armv7   (default: both; arm64 = arm64-v8a, armv7 = armeabi-v7a)
```

Verify on this box (no device needed):

```bash
BT=~/workspace/android-sdk/build-tools/android-14
$BT/aapt dump badging /tmp/m6/misa.apk
JAVA_HOME=~/workspace/tools/jdk-21 bash $BT/apksigner verify \
    --print-certs /tmp/m6/misa.apk
```

## Install & run on your phone (Hamis)

1. On the phone: enable **Developer options → USB debugging**
   (tap Build number 7×), plug into a machine with the APK.
2. Install:
   ```bash
   ~/workspace/android-sdk/platform-tools/adb install -r /tmp/m6/misa.apk
   ```
   (or copy the APK to the phone and tap it — allow "install unknown apps".)
3. Launch **MisaApp**. The demo screen renders: heading, banner,
   list, a name field, and a Greet button → second screen.

Watch it work:
```bash
adb logcat -s MISA
```
`print(...)` in Misa goes to logcat; handler errors show as toasts.

### Passing provider keys (P3 — keys are never baked into the APK)

The app reads **intent extras** as config before boot:

```bash
adb shell am start -n com.misa.app/.MisaActivity \
    --es OPENAI_KEY sk-your-key-here
```

Without keys, network model calls degrade to the catchable `E401`
(the program keeps running; only the model call fails).

## Threading model (why it won't ANR)

- `MisaActivity` owns a `HandlerThread("Misa")`. **Every** native
  entry — boot, button taps, camera results — is posted to it.
- View work from the Misa thread is marshalled to the UI thread
  inside `MisaWidgets` (`runOnUiThread` + latch when a result is
  needed; direct call when already on the UI thread — no deadlock).
- Network (`HttpURLConnection`) therefore always runs off the UI
  thread: no `NetworkOnMainThreadException`, no ANR during model
  calls.

## Platform behaviors worth knowing

- **P2 fatal** (e.g. out of memory): logcat `FATAL` + toast + the
  activity finishes. The process is **never** `exit()`ed on mobile;
  the faulting thread parks instead.
- **P4 files**: app-relative paths map under the app's private
  `files/misa_fs/` (no permissions needed, uninstalled with the app).
- **CameraButton**: launches the system camera (`ACTION_IMAGE_CAPTURE`)
  into the MediaStore; the photo's `content://` URI becomes the
  `Photo` handle for `onCapture`. Needs a device with a camera app.
- **HTTP**: real `HttpURLConnection` backend (GET + POST, connect/
  read timeouts from the call's latency budget). Any Java-side
  exception becomes a transport failure → catchable `E401`/`E304`.

## Reproducing the toolchain (download recipe)

Pinned versions (2026-10-05): **NDK r27d** (LTS), build-tools 34,
platform android-35, platform-tools latest. Layout:

```
~/workspace/android-ndk/android-ndk-r27d/
~/workspace/android-sdk/{platform-tools,build-tools/android-14,
                         platforms/android-35/android.jar,debug.keystore}
~/workspace/tools/jdk-21/          (javac/d8 need a JDK)
```

```bash
DL=~/workspace/android-ndk/dl && mkdir -p $DL && cd $DL
# NDK r27d (664 MB) — plain curl stalls on this egress, so the repo
# ships scripts/get_parts.sh: 8 parallel ranged chunks + resume.
# usage: get_parts.sh <url> <total-bytes> <nparts> <prefix>
unzip -q ndk.bin -d ~/workspace/android-ndk   # -> android-ndk-r27d/
# SDK pieces (dl.google.com/android/repository/):
#   platform-tools-latest-linux.zip -> ~/workspace/android-sdk/
#   build-tools_r34-linux.zip       -> ~/workspace/android-sdk/build-tools/android-14/
#   platform-35_r02.zip             -> android.jar to
#                                      ~/workspace/android-sdk/platforms/android-35/
```

## GC decision (per data, 2026-10-05)

**Decision: Boehm GC, built for Android — it worked.** Per the task
("decide PER DATA"), the experiment was run rather than assumed:

- bdwgc 8.2.8 (vendored `~/workspace/deps/bdwgc.tar.gz`) configures
  and builds cleanly under NDK r27d clang for **both** ABIs, with one
  extra step the data demanded: cross-compiling skips bdwgc's
  compiler-intrinsics probe, so `configure` hard-requires
  libatomic_ops — fetched v7.8.2 and unpacked it into the source
  tree per upstream's own recipe.
- The static lib must be built with `-fPIC` (else `ld.lld` refuses
  with `R_AARCH64_ADR_PREL_PG_HI21 ... recompile with -fPIC` when
  linking `libmisa.so`).
- Outputs: `~/workspace/deps/gc-android-aarch64/lib/libgc.a`
  (409 KB), `~/workspace/deps/gc-android-armv7/lib/libgc.a`
  (284 KB).
- `runtime_flags()` auto-detects them (`include/gc.h` present) and
  adds `-DMISA_USE_GC`; with no GC dir it silently takes the malloc
  fallback. Reproduce: `bash scripts/build_bdwgc_android.sh
  [aarch64|armv7]`.

So the M6 APK links the real collector (`GC_malloc` for every
`MVal`), same memory semantics as the host binary — no malloc
fallback, no behavior fork between targets.

## What is NOT yet verified on a device

This box has **no Android device and no emulator**, so the following
were verified by construction only (they compile, package, and pass
every static check available here):

1. The app actually launching and rendering on a phone.
2. Button tap → handler → `navigate()` re-render on device.
3. `TextInput` readback (`.text`) on device.
4. Camera capture → `onCapture` photo handle on device.
5. `HttpURLConnection` model calls on a real network (timeouts,
   TLS, large bodies).
6. `misa_platform_fatal` toast + activity finish on device.

What **was** verified here (see the M6 report):
`misa build --target aarch64-linux-android` emits a valid ELF64
AArch64 object; the full runtime compiles under NDK clang with
`-Wall -Werror` (zero warnings); the APK assembles and passes
`aapt dump badging` + `apksigner verify`; host suites stay green
(interpreter 74/74, AOT parity 12/12).

## Known M6-slice deviations from spec/PHASE4.md §4.4

- `List` → vertical `LinearLayout` of `TextView` rows, **not**
  `RecyclerView` (avoids the androidx aar dependency cascade for the
  first slice; identical single-text-row shape, recycling is a later
  optimization).
- `CameraButton` → `Button` + `ACTION_IMAGE_CAPTURE` intent with a
  MediaStore `content://` URI (no `ActivityResultContracts` — that
  needs androidx.activity).
