# On-device AI — `model` calls without the cloud (Phase 5)

The fourth flywheel: **identical `model` syntax for cloud and on-device**.
A program written against a cloud provider runs unchanged against a
local model file — only the `provider` block changes.

## 1. Syntax

```misa
provider edge {
    model: file("./models/plantdoc.onnx")
}

model classify(pixels: [Float]) -> [Float]
    with edge.local
    budget { latency: 2s }
    prompt "classify the leaf"
```

The runtime is selected by the model file's extension — one spelling,
no kind annotations:

| Extension | Runtime | Platforms |
|---|---|---|
| `.onnx` | ONNX Runtime | everywhere (Linux/macOS/Windows/Android/iOS) |
| `.mlpackage` / `.mlmodelc` | CoreML | iOS/macOS |
| `.tflite` | TFLite (NNAPI delegate) | Android |

`key: env(...)` is not required for on-device providers (no key, no
network). `budget { tokens }` is ignored with a warning; `latency` is
enforced as an inference-time cap.

## 2. Type ↔ tensor mapping (v1)

Positional binding: the Nth declared param binds to the Nth model
input; the declared return type binds to the first model output.

Supported v1 mappings:
- `[Float]` ↔ 1-D float32 tensor (shape taken from the model; a
  length mismatch is `E501` with expected/got)
- `Float` ↔ scalar float32 tensor
- `String` ↔ string tensor (tokenized outside the model; v1 supports
  only models with string inputs where the runtime handles them
  natively, e.g. via ORT string tensors)

Anything else is `E502` (unsupported on-device signature) at first
call, with the concrete mismatch described. Images: v1 takes
`[Float]` pixel buffers (the app flattens); a native `Image` type is a
later milestone, not this one.

## 3. Provider architecture

```
model call → Provider.dispatch(meta, args)
               ├─ openai/gemini/mock   (existing, HTTP)
               └─ ondevice
                    ├─ ext .onnx    → OnnxRuntime   (all platforms)
                    ├─ ext .mlpackage/.mlmodelc → CoreML (Apple only; E503 elsewhere)
                    └─ ext .tflite  → TFLite/NNAPI  (Android only; E503 elsewhere)
```

Interpreter (`impl/misa/providers.py`): `OnDeviceProvider` with
per-extension backends. ONNX via the `onnxruntime` package; CoreML via
`coremltools` (macOS only); TFLite via `ai-edge-litert` (where
available). Model files are loaded lazily on first call and cached per
process.

AOT (`impl/misa/aot/rt/misa_model.c`): same dispatch. ONNX via the
ONNX Runtime C API (vendored per platform); CoreML via
`platform_apple.m`; TFLite via the Android platform layer. The model
file ships inside the app bundle (Android assets / iOS resources /
next to the binary for host builds).

`misa build` must fail with `E504` when the target platform cannot run
the model file's runtime (e.g. `.mlpackage` → Android target), at
build time, not on the device.

## 4. Offline guarantee

An on-device `model` call makes **zero network requests**. The
interpreter asserts this in tests by blocking sockets during the call
(allow-list: none). `misa serve` refuses to serve apps whose active
provider is on-device? No — serve works fine (the model runs on the
server). The guarantee is about the *device* build.

## 5. Phase 5 exit: fully offline app

`examples/plantdoc_edge.misa`: the PlantDoc app with `provider edge`
pointing at a tiny real ONNX classifier (shipped under
`examples/models/`), `misa build` to a host binary, runs with
networking disabled and still diagnoses. That is the exit proof.

## 6. Milestones

- **M1 (done):** syntax + `E50x` codes, interpreter ONNX backend,
  real tiny ONNX model + tests (incl. the socket-blocked offline
  test), `plantdoc_edge.misa` running offline on Linux.
- **M2 (done):** AOT ONNX Runtime C wiring for the Linux host —
  `misa build` compiles on-device `.onnx` programs to native binaries;
  the model file and `libonnxruntime` are staged next to the binary
  (relocatable via exe-dir resolution); E501/E502/E503 surface from
  compiled binaries; E504 remains for unwired runtimes/targets.
  Verified: `plantdoc_edge` binary diagnoses with no network namespace,
  probabilities bit-identical to the interpreter.
- **M3 (implemented, device-unverified):** CoreML backend in
  `platform_apple.m` (`misa_coreml_infer`, declared in `misa_rt.h`):
  `.mlpackage`/`.mlmodelc` load via `+[MLModel modelWithContentsOfURL:]`,
  lazy per-process cache, v1 type mapping (Float→scalar incl. native
  double/int64 scalar inputs, [Float]→1-D float32 multi-array,
  Text→string), input binding by declared param name with fallback to
  positional over sorted input names, first output → JSON, E501/E502
  surface with exact codes, load/inference failure → E400. The Xcode
  template links CoreML.framework. `misa build --target
  aarch64-apple-ios-simulator` (and the other Apple targets) no longer
  E504 on CoreML models; the .mlpackage ships as a bundle resource
  (see docs/ios-cross.md). Status: compile-verified by the macOS CI
  job; **no device run yet** — verification steps below.
- **M4:** TFLite/NNAPI on Android.

### Device verification for M3 (pending hardware)

1. On a Mac: convert `examples/models/tiny_classifier.onnx` (or train
   any 4→3 classifier) to CoreML with coremltools
   (`ct.convert(..., convert_to="mlprogram")` → `plantdoc.mlpackage`).
2. `python3 impl/bin/misa build examples/plantdoc_edge_coreml.misa
   -o ios/MisaApp/program/prog_arm64.o --target
   aarch64-apple-ios-simulator` (provider block points at the
   .mlpackage; build must succeed — E504 is lifted for Apple targets).
3. In Xcode: add the `.mlpackage` to the MisaApp target's Copy Bundle
   Resources (or `ios/MisaApp/model/` + target membership).
4. Run on the simulator: the app must print the same probabilities as
   the interpreter's offline run (bit-identical doubles, modulo the
   `%.17g` rendering note in M2).
5. Negative paths on device: wrong-length `[Float]` → E501;
   non-numeric model → E502; missing .mlpackage → E400 with the
   resolved path in the message.

## 7. Non-goals (Phase 5)

- Model downloading/hosting, quantization tooling, tokenizers
  (string models are pass-through), GPU delegate tuning, `Image`
  type, streaming from local models.
