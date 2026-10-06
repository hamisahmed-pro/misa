# Misa build targets

One codebase, five targets. `misa build --list-targets` shows the full
table with per-target status.

## Host (Linux/macOS) — the primary target

```bash
misa build app.misa -o app
./app
```

Produces a small native binary (LLVM → object → link with the C
runtime). No runtime install needed on the target machine. The
interpreter and host binaries are byte-identical on all tested programs
— that's enforced by the AOT parity suite.

## Android — APK

```bash
python3 scripts/build_apk.py --misa examples/plantdoc.misa \
    --out dist/myapp.apk
```

Builds a debuggable APK (needs the Android NDK/SDK; see the script
header for setup). Install with `adb install dist/myapp.apk`.

- UI renders through native Android Views (`ui_android.c`).
- `db.*` works — SQLite is bundled into the APK.
- `extern "c"` / `"m"` link against NDK libraries; other native
  libraries fail the link with a clean `E600`.
- `.tflite` on-device models run through NNAPI with CPU fallback.

## iOS — Xcode project

The `ios/MisaApp` Xcode template links a program object built for
`aarch64-apple-ios`:

```bash
misa build app.misa -o ios/MisaApp/program/prog_arm64.o \
    --target aarch64-apple-ios
```

then build in Xcode (or via the `ios-device` CI job, which produces an
unsigned IPA). UI renders through UIKit. The iOS simulator build is
smoke-tested in CI.

> **Not yet verified:** physical-device runs. Simulator tests pass; a
> real iPhone install + CoreML inference still needs to happen.

## Web — Emscripten

```bash
misa build app.misa -o web/app.js --target wasm32-unknown-emscripten
```

Produces `app.js` + `app.wasm` (+ `.data` for preloaded files via
`--preload-file src@dst`). Needs the Emscripten SDK (`emcc`) installed.
The module exposes `misa_ui_boot()` — see `web/shell.html` for the
standard embedding:

```html
<script src="app.js"></script>
<script>
  Module.onRuntimeInitialized = () => {
    Module.ccall('misa_ui_boot', null, [], []);
  };
</script>
```

UI renders to the DOM. Verified: programs compile and link cleanly,
and the module loads under node without errors; DOM rendering is
covered by CI's headless-Chrome tests.

## What works where

| Feature | interp | host AOT | Android | iOS | web |
|---|---|---|---|---|---|
| Language core (fns, closures, sums, generics) | ✅ | ✅ | ✅ | ✅ | ✅ |
| `fs` | ✅ | ✅ | ✅ (app sandbox) | ✅ (app sandbox) | ✅ |
| `http` | ✅ | ✅ | ✅ | ✅¹ | ✅ |
| `str`, `json`, `time`, `math` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `db.*` (SQLite) | ✅ | ✅ | ✅ (bundled) | ⚠️ E600 | ⚠️ E600 |
| `process.run` | ✅ | ⚠️ E600 | ⚠️ E600 | ⚠️ E600 | ⚠️ E600 |
| `extern` (FFI) | ✅ (`c`, `m`) | ✅ (`c`, `m`) | ✅ (`c`, `m`) | ✅¹ (`c`) | ✅ (`c`, `m` via musl) |
| Cloud model calls | ✅ (mock/keys) | ✅ | ✅ | ✅¹ | ✅ |
| On-device `.onnx` | ✅² | ✅ (real inference) | ⚠️ E504 | ⚠️ E504 | ⚠️ E504 |
| On-device `.tflite` | ⚠️ E503 | ⚠️ E504 | ✅ (NNAPI/CPU) | ⚠️ E504 | ⚠️ E504 |
| On-device `.mlpackage` | ⚠️ E503 | ⚠️ E504 | ⚠️ E504 | ✅¹ (CoreML) | ⚠️ E504 |
| Screens | text driver | text driver | native Views | native UIKit | DOM |

¹ Code-complete; device run not yet verified.
² Needs the `onnxruntime` Python package, else clean `E503`.

**Reading the table:** ✅ = works. ⚠️ E600/E503/E504 = the toolchain
rejects it *cleanly* with a descriptive error — that's the contract,
not a bug. Anything outside this table is untested: assume it doesn't
work until proven otherwise.

## `misa build` options

```
misa build <file.misa> [-o output] [--emit-llvm file.ll]
           [--target triple] [--preload-file src@dst]... [--json]
misa build --list-targets
```

- `--emit-llvm` dumps the generated LLVM IR for inspection.
- `--preload-file` stages a file into the app bundle (web/Android).
- `--json` emits machine-readable diagnostics.
- `misa serve <file.misa> [--port PORT]` runs the app as a local web
  server (dev tool).

## Known limits

- Physical Android inference and physical iPhone runs are unverified
  (simulator/emulator + CI only, so far).
- `wasm32-unknown-wasi` emits objects only (no linked runtime yet).
- AOT binaries are larger than C equivalents (no unboxing yet — every
  value is a heap `MVal`); representation optimization is future work.
- The interpreter is ~1000× slower than AOT on print-heavy loops —
  use `misa run` for development, `misa build` for anything
  performance-sensitive.
