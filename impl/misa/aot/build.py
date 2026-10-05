"""`misa build`: AOT-compile a .misa program to a native binary.

Pipeline: parse -> check -> LLVM IR (llvmlite) -> native object ->
link with the C runtime -> executable.

Phase 4 (M1): cross-target plumbing. `misa build --target <triple>`
selects the LLVM target (llvmlite 0.50: initialize_all_targets() +
initialize_all_asmprinters(), Target.from_triple(); note that
binding.initialize() raises in 0.50 and TargetMachine.target was
removed — Target.name is used instead). The host triple builds and
links exactly as before; `wasm32-unknown-emscripten` links a web app
(misa.js + misa.wasm) with emcc (M3); the Android triples compile the
runtime with the NDK clang (M6) while `misa build` itself still emits
an object file for them — scripts/build_apk.py links the APK;
any other non-host triple emits a relocatable object file (.o).

The C runtime is compiled once per (target, compiler, flags, sources)
into a cache directory and reused while the cache key is fresh. GC
(Boehm) and libcurl are used when their headers are present; the
runtime degrades gracefully without them.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile

IMPL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT_DIR = os.path.join(IMPL_DIR, "aot", "rt")
CACHE_DIR = os.path.join(RT_DIR, "build")
# Vendored native deps (built from source tarballs; see deps/README).
DEPS_DIR = os.path.join(os.path.expanduser("~"), "workspace", "deps", "gc")
DEPS_INCLUDE = os.path.join(DEPS_DIR, "include")

# ----------------------------------------------------------------------
# Android NDK (Phase 4, M6). Pinned version; unpacked under
# ~/workspace/android-ndk (see scripts/build_apk.py for the SDK side).
# ----------------------------------------------------------------------
NDK_VERSION = "r27d"
NDK_DIR = os.path.join(os.path.expanduser("~"), "workspace",
                       "android-ndk", "android-ndk-" + NDK_VERSION)
NDK_LLVM_BIN = os.path.join(
    NDK_DIR, "toolchains", "llvm", "prebuilt", "linux-x86_64", "bin")
# minSdk the NDK clang targets (manifest may declare a lower minSdk;
# __ANDROID_API__ follows this).
NDK_API = "34"


def _ndk_clang(triple: str) -> str:
    """NDK clang for an android target triple (raises if NDK missing)."""
    if triple.startswith("aarch64"):
        name = f"aarch64-linux-android{NDK_API}-clang"
    elif triple.startswith("armv7"):
        name = f"armv7a-linux-androideabi{NDK_API}-clang"
    elif triple.startswith("x86_64"):
        name = f"x86_64-linux-android{NDK_API}-clang"
    else:
        raise RuntimeError(f"no NDK clang mapping for {triple}")
    cc = os.path.join(NDK_LLVM_BIN, name)
    if not os.path.exists(cc):
        raise RuntimeError(
            f"Android NDK {NDK_VERSION} not found (looked for {cc}); "
            f"see docs/android.md")
    return cc


# ----------------------------------------------------------------------
# Target table (Phase 4, M1 — fixes B3/B6 structurally).
#
# Per-target C toolchain ("cc"), a short compiler id for the runtime
# cache key ("cc_id"), and the program entry point ("entry": "main"
# for every hosted target today; misa_ui_boot / JNI_OnLoad arrive
# with M3/M6). Only the host row links in M1; every other row emits
# an LLVM object file, with the full toolchain landing per milestone.
# ----------------------------------------------------------------------
TARGETS = {
    "x86_64-linux-gnu": {
        "cc": "gcc",
        "cc_id": "gcc-host",
        "entry": "main",
        "status": "full build (host)",
        "desc": "Linux x86-64 server/desktop",
    },
    "wasm32-unknown-wasi": {
        "cc": "wasi-sdk clang",
        "cc_id": "wasi-sdk",
        "entry": "main",
        "status": "object only in M1 (runtime+link: M3)",
        "desc": "WebAssembly (headless WASI)",
    },
    "wasm32-unknown-emscripten": {
        "cc": "emcc",
        "cc_id": "emcc",
        "entry": "misa_ui_boot",
        "status": "full build (M3)",
        "desc": "Web via Emscripten (M3 first backend)",
    },
    "aarch64-linux-android": {
        "cc": "NDK r27d clang (aarch64-linux-android34-clang)",
        "cc_id": "ndk-aarch64-r27d",
        "entry": "misa_ui_boot",
        "status": "runtime: M6 (debuggable APK)",
        "desc": "Android ARM64",
    },
    "armv7-linux-androideabi": {
        "cc": "NDK r27d clang (armv7a-linux-androideabi34-clang)",
        "cc_id": "ndk-armv7-r27d",
        "entry": "misa_ui_boot",
        "status": "runtime: M6 (debuggable APK)",
        "desc": "Android ARMv7 (low priority)",
    },
    "x86_64-linux-android": {
        "cc": "NDK r27d clang (x86_64-linux-android34-clang)",
        "cc_id": "ndk-x64-r27d",
        "entry": "misa_ui_boot",
        "status": "runtime: M6 (emulator CI only)",
        "desc": "Android x86-64 (emulator only, never shipped)",
    },
    "aarch64-apple-darwin": {
        "cc": "xcrun clang (needs macOS)",
        "cc_id": "xcrun-arm64",
        "entry": "main",
        "status": "object only (link+sign on a Mac: M7 runtime in rt/*.m, template in ios/MisaApp)",
        "desc": "Apple ARM64 (iOS/macOS)",
    },
    "aarch64-apple-ios-simulator": {
        "cc": "xcrun clang (needs macOS)",
        "cc_id": "xcrun-arm64-iossim",
        "entry": "main",
        "status": "object only (link in Xcode for the iOS Simulator: M7 template ios/MisaApp)",
        "desc": "Apple ARM64 iOS Simulator",
    },
    "x86_64-apple-darwin": {
        "cc": "xcrun clang (needs macOS)",
        "cc_id": "xcrun-x86_64",
        "entry": "main",
        "status": "object only (link+sign on a Mac: M7 runtime in rt/*.m, template in ios/MisaApp)",
        "desc": "Apple x86-64 (macOS Intel)",
    },
}

# Runtime C sources, in link order. platform_linux.c is the Phase 4
# platform layer (P1-P4 + HTTP transport); ui_text.c is the default
# text UI driver behind misa_ui.h. The web target (M3) swaps in the
# Emscripten platform layer + DOM driver; the Android targets (M6)
# swap in the Android platform layer + Views driver + JNI entry.
RT_SOURCES = ("misa_rt.c", "misa_model.c", "platform_linux.c", "ui_text.c")
RT_SOURCES_BY_TARGET = {
    "wasm32-unknown-emscripten":
        ("misa_rt.c", "misa_model.c", "platform_emscripten.c", "ui_dom.c"),
    "aarch64-linux-android":
        ("misa_rt.c", "misa_model.c", "platform_android.c",
         "ui_android.c", "jni_android.c"),
    "armv7-linux-androideabi":
        ("misa_rt.c", "misa_model.c", "platform_android.c",
         "ui_android.c", "jni_android.c"),
    "x86_64-linux-android":
        ("misa_rt.c", "misa_model.c", "platform_android.c",
         "ui_android.c", "jni_android.c"),
}
RT_HEADERS = ("misa_rt.h", "misa_platform.h", "misa_ui.h", "jni_android.h")

EMSDK_DIR = (os.environ.get("EMSDK")              # set by emsdk_env.sh
             or os.environ.get("MISA_EMSDK_DIR")    # manual override
             or os.path.join(os.path.expanduser("~"), "workspace", "emsdk"))


def _rt_sources(target: str) -> tuple:
    return RT_SOURCES_BY_TARGET.get(canonical_target(target), RT_SOURCES)


def _emcc(argv: list, **kw) -> "subprocess.CompletedProcess":
    """Run emcc with the emsdk environment active.

    The SDK location is $EMSDK (set by emsdk_env.sh), $MISA_EMSDK_DIR,
    or ~/workspace/emsdk as a fallback.
    """
    env_sh = os.path.join(EMSDK_DIR, "emsdk_env.sh")
    if not os.path.exists(env_sh):
        raise RuntimeError(
            "emcc not found: install the Emscripten SDK first "
            "(scripts/install_emsdk.sh); looked for " + env_sh)
    cmd = ["bash", "-c",
           'source "$0" >/dev/null 2>&1 && exec emcc "$@"',
           env_sh] + argv
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

_HOST_TRIPLE: str | None = None


def host_triple() -> str:
    """The native LLVM triple for this machine (cached)."""
    global _HOST_TRIPLE
    if _HOST_TRIPLE is None:
        from llvmlite import binding
        binding.initialize_native_target()
        _HOST_TRIPLE = binding.Target.from_default_triple().triple
    return _HOST_TRIPLE


def canonical_target(triple: str) -> str:
    """Map accepted host spellings to the llvmlite host triple.

    llvmlite normalizes the conventional "x86_64-linux-gnu" to
    "x86_64-unknown-linux-gnu"; both spellings select the host build.
    """
    t = (triple or "").strip()
    if t in ("x86_64-linux-gnu", host_triple()):
        return host_triple()
    return t


def list_targets() -> list:
    """Rows for `misa build --list-targets`: (triple, status, cc, entry)."""
    host = host_triple()
    rows = []
    for triple, row in TARGETS.items():
        status = row["status"]
        if canonical_target(triple) == host:
            status += "  <-- this machine"
        rows.append((triple, status, row["cc"], row["entry"]))
    return rows


def _have_header(path: str) -> bool:
    return os.path.exists(path)


# Per-target system library search dirs (fix B4). Only the host row
# resolves libraries in M1; cross targets have no sysroot here yet.
def _lib_dirs(target: str) -> tuple:
    if canonical_target(target) == host_triple():
        return ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
                "/lib", "/usr/lib")
    return ()


def _find_lib(name_glob: str, target: str) -> str:
    """Find a system shared library by glob, per target (B4)."""
    import glob as _glob
    for d in _lib_dirs(target):
        hits = sorted(_glob.glob(os.path.join(d, name_glob)))
        if hits:
            return hits[-1]
    return ""


def runtime_flags(target: str = "") -> tuple:
    """(defines, cflags, libs) for the C runtime, per target.

    Uses the vendored Boehm GC + curl headers when built, else system
    headers, else graceful fallbacks (malloc; http/model-net disabled).
    Non-host targets get the portable fallback set; their real
    toolchain flags land with M3/M6.
    """
    target = canonical_target(target or host_triple())
    defines, cflags, libs = [], [], []
    if target == "wasm32-unknown-emscripten":
        # Phase 4 (M5): malloc fallback for now (MISA_USE_GC off until
        # the bdwgc-under-emscripten experiment reports); no libcurl —
        # the web target uses the emscripten_fetch transport
        # (MISA_USE_FETCH) for both http.get and network model calls.
        defines.append("-DMISA_USE_FETCH")
        return defines, cflags, libs
    if target in ("aarch64-linux-android", "armv7-linux-androideabi",
                    "x86_64-linux-android"):
        # Phase 4 (M6): Boehm GC when the vendored bdwgc Android build
        # exists (see docs/android.md for the build recipe + decision);
        # otherwise the malloc fallback. No libcurl on Android — the
        # HTTP transport is JNI -> HttpURLConnection in
        # platform_android.c. -llog/-landroid/-lm are used at .so link
        # time (scripts/build_apk.py); the runtime .o compile only
        # needs the headers from the NDK sysroot.
        # -DMISA_USE_JNI_HTTP wires the network model-call path in
        # misa_model.c to the JNI HttpURLConnection transport.
        arch = "aarch64" if target.startswith("aarch64") else "armv7"
        gc_dir = os.path.join(os.path.expanduser("~"), "workspace",
                              "deps", "gc-android-" + arch)
        gc_h = os.path.join(gc_dir, "include", "gc.h")
        if _have_header(gc_h):
            defines.append("-DMISA_USE_GC")
            cflags.append("-I" + os.path.join(gc_dir, "include"))
            libs.append(os.path.join(gc_dir, "lib", "libgc.a"))
        defines.append("-DMISA_USE_JNI_HTTP")
        libs += ["-llog", "-landroid", "-lm"]  # misa_rt.c uses <math.h>
        return defines, cflags, libs
    if target == host_triple():
        vendored_gc_h = os.path.join(DEPS_INCLUDE, "gc.h")
        if _have_header(vendored_gc_h):
            defines.append("-DMISA_USE_GC")
            cflags.append("-I" + DEPS_INCLUDE)
            libs.append(os.path.join(DEPS_DIR, "lib", "libgc.a"))
        elif _have_header("/usr/include/gc.h"):
            defines.append("-DMISA_USE_GC")
            libs.append("-lgc")
        vendored_curl_h = os.path.join(DEPS_INCLUDE, "curl", "curl.h")
        if _have_header(vendored_curl_h):
            defines.append("-DMISA_USE_CURL")
            cflags.append("-I" + DEPS_INCLUDE)
            curl_lib = _find_lib("libcurl.so*", target)
            libs.append(curl_lib if curl_lib else "-lcurl")
        elif _have_header("/usr/include/curl/curl.h"):
            defines.append("-DMISA_USE_CURL")
            libs.append("-lcurl")
    libs.append("-lm")
    return defines, cflags, libs


def _table_row(target: str) -> dict:
    """TARGETS row for a (canonical) triple; {} when unknown."""
    t = canonical_target(target)
    if t == host_triple():
        return TARGETS["x86_64-linux-gnu"]
    return TARGETS.get(t, {})


def _cc_id(target: str) -> str:
    return _table_row(target).get("cc_id", "unknown-cc")


def _rt_key(target: str = "") -> str:
    """Runtime cache key: target triple + C compiler id (fix B5) +
    flags + the full runtime source set (Phase 4 added platform_linux.c
    and ui_text.c to the hash)."""
    target = canonical_target(target or host_triple())
    defines, cflags, libs = runtime_flags(target)
    h = hashlib.sha256()
    h.update(target.encode())
    h.update(b"\x00" + _cc_id(target).encode())
    h.update(b"\x00" + (" ".join(defines + cflags + libs)).encode())
    for src in _rt_sources(target) + RT_HEADERS:
        p = os.path.join(RT_DIR, src)
        with open(p, "rb") as f:
            h.update(b"\x00" + f.read())
    return h.hexdigest()[:16]


def ensure_runtime(target: str = "") -> list:
    """Compile the C runtime if stale; return [rt.o, ...].

    Host builds use gcc; the web target (M3) compiles with emcc.
    Other cross targets still need their toolchain (M6/M7).
    """
    target = canonical_target(target or host_triple())
    if target == "wasm32-unknown-emscripten":
        return _ensure_runtime_emcc(target)
    if target in ("aarch64-linux-android", "armv7-linux-androideabi",
                    "x86_64-linux-android"):
        return _ensure_runtime_ndk(target)
    if target != host_triple():
        cc = _table_row(target).get("cc", "a cross C toolchain")
        raise RuntimeError(
            f"no C toolchain for target {target} yet "
            f"(needs: {cc}; Phase 4 M3/M6)")
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _rt_key(target)
    stamp = os.path.join(CACHE_DIR, f"stamp-{key}")
    objs = [os.path.join(CACHE_DIR,
                         f"{src[:-2]}-{key}.o") for src in _rt_sources(target)]
    if os.path.exists(stamp) and all(os.path.exists(o) for o in objs):
        return objs
    defines, cflags, _libs = runtime_flags(target)
    for src, out in zip(_rt_sources(target), objs):
        cmd = (["gcc", "-O2", "-c", os.path.join(RT_DIR, src),
                "-o", out] + defines + cflags)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"C runtime compile failed ({src}):\n{r.stderr}")
    with open(stamp, "w") as f:
        f.write("ok")
    return objs


def _ensure_runtime_emcc(target: str) -> list:
    """Compile the web runtime (platform_emscripten.c + ui_dom.c) with
    emcc; objects are cached under the same key scheme as the host."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _rt_key(target)
    stamp = os.path.join(CACHE_DIR, f"stamp-{key}")
    sources = _rt_sources(target)
    objs = [os.path.join(CACHE_DIR, f"{src[:-2]}-{key}.o")
            for src in sources]
    if os.path.exists(stamp) and all(os.path.exists(o) for o in objs):
        return objs
    defines, cflags, _libs = runtime_flags(target)
    for src, out in zip(sources, objs):
        cmd = (["-O2", "-c", os.path.join(RT_DIR, src),
                "-o", out] + defines + cflags)
        r = _emcc(cmd)
        if r.returncode != 0:
            raise RuntimeError(
                f"C runtime compile failed ({src}):\n{r.stderr}")
    with open(stamp, "w") as f:
        f.write("ok")
    return objs


def _ensure_runtime_ndk(target: str) -> list:
    """Compile the Android runtime (platform_android.c + ui_android.c +
    jni_android.c) with the NDK clang (Phase 4, M6). Objects are
    cached under the same key scheme as the host; compiled -fPIC with
    -Wall -Werror (zero warnings tolerated)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _rt_key(target)
    stamp = os.path.join(CACHE_DIR, f"stamp-{key}")
    sources = _rt_sources(target)
    objs = [os.path.join(CACHE_DIR, f"{src[:-2]}-{key}.o")
            for src in sources]
    if os.path.exists(stamp) and all(os.path.exists(o) for o in objs):
        return objs
    cc = _ndk_clang(canonical_target(target))
    defines, cflags, _libs = runtime_flags(target)
    for src, out in zip(sources, objs):
        cmd = ([cc, "-O2", "-fPIC", "-Wall", "-Werror",
                "-DANDROID", "-c", os.path.join(RT_DIR, src),
                "-o", out] + defines + cflags)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"C runtime compile failed ({src}, {target}):\n{r.stderr}")
    with open(stamp, "w") as f:
        f.write("ok")
    return objs


def _frontend(path: str):
    """Load + check. Returns (merged_prog, errors)."""
    sys.path.insert(0, IMPL_DIR)
    from misa.loader import load as load_with_imports
    from misa.checker import Checker
    merged, load_errors, _order, file_of = load_with_imports(path)
    if merged is None:
        return None, load_errors
    errors = list(load_errors) + Checker().check(merged, file_of=file_of)
    if errors:
        return None, errors
    return merged, []


def _err(code: str, message: str):
    return [{"code": code, "message": message, "line": 0, "col": 0,
             "hint": ""}]


def _emit_object(mod, triple: str) -> bytes:
    """Lower the module to a native object for `triple` (llvmlite 0.50).

    Host path is byte-for-byte the v0.3 behavior; the cross path
    initializes all LLVM targets, resolves the triple, and sets the
    module triple + data layout (fixes B1/B2).
    """
    from llvmlite import binding
    if triple == host_triple():
        binding.initialize_native_target()
        binding.initialize_native_asmprinter()
        target = binding.Target.from_default_triple()
        eff_triple = target.triple
    else:
        # NB: binding.initialize() raises in llvmlite 0.50 — the
        # specific init functions are required.
        binding.initialize_all_targets()
        binding.initialize_all_asmprinters()
        try:
            target = binding.Target.from_triple(triple)
        except Exception as e:
            raise ValueError(f"unknown target triple {triple!r}: {e}")
        eff_triple = triple
    tm = target.create_target_machine(
        # Phase 4 (M6): Android links the program object into a shared
        # library (libmisa.so inside the APK) — it must be PIC. Host
        # behavior is unchanged.
        reloc="pic" if "android" in eff_triple else "default")
    mod.triple = eff_triple
    # TargetMachine.target_data exists in 0.50 (TargetMachine.target
    # was removed — use Target.name, e.g. for diagnostics).
    mod.data_layout = str(tm.target_data)
    bmod = binding.parse_assembly(str(mod))
    bmod.verify()
    return tm.emit_object(bmod)


def build(path: str, out_path: str, *, emit_llvm: str = "",
          target: str = "", verbose: bool = False, preload_files=()):
    """Compile path -> out_path. Returns (ok: bool, errors: list).

    `target` is an LLVM triple (see --list-targets). Empty (or the
    host triple) builds + links a native binary exactly as v0.3 did;
    `wasm32-unknown-emscripten` links a web app (misa.js + misa.wasm)
    with emcc (Phase 4 M3); any other triple emits a relocatable
    object file instead — linking for those arrives in Phase 4 M6/M7.

    `preload_files` (web only): host paths bundled into the virtual
    FS, as "src" or "src@dst" (emcc --preload-file).
    """
    sys.path.insert(0, IMPL_DIR)
    merged, errors = _frontend(path)
    if errors:
        return False, [e.to_dict() if hasattr(e, "to_dict") else e
                       for e in errors]

    from misa.aot.emit import Emitter, AOTError
    from misa.ast import ProviderDecl
    eff_target = canonical_target(target or host_triple())

    # --- on-device models: interpreter-only in M1 --------------------
    # The AOT runtime has no local-inference transport yet (milestone
    # M2), so compiling a program with an on-device provider fails here
    # with E504 instead of breaking on the device.
    for item in merged.items:
        if isinstance(item, ProviderDecl) and item.model_file:
            return False, [{
                "code": "E504",
                "message": f"on-device model '{item.model_file}' cannot "
                           f"be compiled for target '{eff_target}' "
                           f"(interpreter only in M1; AOT on-device "
                           f"support is milestone M2)",
                "line": item.line, "col": item.col, "hint": ""}]

    entry = _table_row(eff_target).get("entry", "main")
    try:
        em = Emitter(merged, entry=entry)
        mod = em.emit()
    except AOTError as e:
        return False, [{"code": e.code, "message": e.message,
                        "line": e.line, "col": e.col, "hint": ""}]

    # --- LLVM IR -> object -------------------------------------------
    try:
        obj_bytes = _emit_object(mod, eff_target)
    except ValueError as e:
        return False, _err("E600", str(e))

    if emit_llvm:
        # Dump AFTER _emit_object: it sets mod.triple + mod.data_layout
        # for the effective target, so the .ll text matches the object.
        with open(emit_llvm, "w") as f:
            f.write(str(mod))

    # --- web target: emcc link to misa.js + misa.wasm (M3) ------------
    if eff_target == "wasm32-unknown-emscripten":
        return _link_emcc(obj_bytes, out_path, eff_target,
                          preload_files, verbose)

    # --- cross target: object only (M1) -------------------------------
    if eff_target != host_triple():
        with open(out_path, "wb") as f:
            f.write(obj_bytes)
        if verbose:
            row = TARGETS.get(eff_target, {})
            print(f"target {eff_target}: wrote object {out_path} "
                  f"({row.get('status', 'no runtime/toolchain yet')})",
                  file=sys.stderr)
        return True, []

    # --- host: link ----------------------------------------------------
    _defines, _cflags, libs = runtime_flags(eff_target)
    rt_objs = ensure_runtime(eff_target)
    with tempfile.TemporaryDirectory() as td:
        prog_o = os.path.join(td, "prog.o")
        with open(prog_o, "wb") as f:
            f.write(obj_bytes)
        cmd = (["gcc", prog_o] + rt_objs +
               ["-o", out_path] + libs)
        if verbose:
            print("link:", " ".join(cmd), file=sys.stderr)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, _err(
                "E600", f"link failed:\n{r.stderr}")
    os.chmod(out_path, 0o755)
    return True, []


# ----------------------------------------------------------------------
# Web target (Phase 4, M3): emcc link to misa.js + misa.wasm.
# ----------------------------------------------------------------------

# Emscripten link flags (first slice, per spec/PHASE4.md §5.1):
# WASM output, Asyncify (lets the blocking http.get/model-call shape
# survive on the web), MODULARIZE (page gets a MisaModule() factory),
# and the two program entry points exported to JS.
EMCC_LINK_FLAGS = [
    "-s", "WASM=1",
    "-s", "ASYNCIFY=1",
    "-s", "MODULARIZE=1",
    "-s", "EXPORT_NAME=MisaModule",
    "-s", "EXPORTED_FUNCTIONS=_misa_ui_boot,_misa_dispatch_handler,_misa_dom_photo,_misa_platform_fs_bootstrap",
    "-s", "EXPORTED_RUNTIME_METHODS=ccall",
    "-s", "ALLOW_MEMORY_GROWTH=1",
    "-s", "FETCH=1",  # link libfetch.js (emscripten_fetch for http.get)
    "--js-library", os.path.join(RT_DIR, "dom_bridge.js"),
    "--pre-js", os.path.join(RT_DIR, "dom_bridge_pre.js"),
]


def _link_emcc(obj_bytes: bytes, out_path: str, target: str,
               preload_files, verbose: bool):
    """Link the llvmlite program object + emcc runtime objects into
    `<out>.js` + `<out>.wasm`. Returns (ok, errors)."""
    rt_objs = ensure_runtime(target)
    with tempfile.TemporaryDirectory() as td:
        prog_o = os.path.join(td, "prog.o")
        with open(prog_o, "wb") as f:
            f.write(obj_bytes)
        cmd = (["-O2", prog_o] + rt_objs + ["-o", out_path]
               + EMCC_LINK_FLAGS)
        for pf in preload_files:
            cmd += ["--preload-file", pf]
        if verbose:
            print("emcc link:", " ".join(cmd), file=sys.stderr)
        r = _emcc(cmd)
        if r.returncode != 0:
            return False, _err("E600", f"emcc link failed:\n{r.stderr}")
    return True, []
