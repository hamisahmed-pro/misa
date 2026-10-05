#!/usr/bin/env python3
"""Build a debuggable APK from a .misa program (Phase 4, M6).

Pipeline (all on this Linux box, no device needed):
  1. `misa build --target <triple>` -> prog.o  (llvmlite, PIC)
  2. build.py ensure_runtime(<triple>) -> Android runtime .o files
     (NDK clang, -fPIC -Wall -Werror)
  3. NDK clang -shared -> lib/<abi>/libmisa.so
  4. javac the android/java sources -> d8 -> classes.dex
  5. aapt package + add -> zipalign -> apksigner (debug key)

Usage:
  python3 scripts/build_apk.py --misa examples/android.misa \
      [--out /tmp/m6/misa.apk] [--abis arm64,armv7 | arm64]

The SDK/NDK layout (~/.workspace side):
  ~/workspace/android-ndk/android-ndk-r27d/
  ~/workspace/android-sdk/{platform-tools,build-tools,platforms,debug.keystore}

Verification on this box: `aapt dump badging` + `apksigner verify`.
Install/run on a real device: see docs/android.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

MISA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPL_DIR = os.path.join(MISA_DIR, "impl")
sys.path.insert(0, IMPL_DIR)

from misa.aot.build import (  # noqa: E402
    build, canonical_target, ensure_runtime, runtime_flags,
    NDK_DIR, NDK_LLVM_BIN, _ndk_clang,
)

HOME = os.path.expanduser("~")
SDK_DIR = os.path.join(HOME, "workspace", "android-sdk")
ANDROID_JAR = os.path.join(SDK_DIR, "platforms", "android-35", "android.jar")
JAVA_HOME = os.path.join(HOME, "workspace", "tools", "jdk-21")

# ABI table: llvmlite/LLVM triple -> (APK abi dir, NDK clang triple prefix)
ABIS = {
    "arm64": ("aarch64-linux-android", "arm64-v8a"),
    "armv7": ("armv7-linux-androideabi", "armeabi-v7a"),
    # x86_64 is emulator-only (CI boot test); never shipped to phones.
    "x64": ("x86_64-linux-android", "x86_64"),
}


def _tool(name: str) -> str:
    p = os.path.join(SDK_DIR, "build-tools", "android-14", name)
    if not os.path.exists(p):
        raise RuntimeError(f"missing SDK build-tool: {p}")
    return p


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def _check_sdk():
    missing = []
    for p in [NDK_DIR, _tool("aapt"), _tool("d8"), _tool("apksigner"),
              _tool("zipalign"), ANDROID_JAR,
              os.path.join(JAVA_HOME, "bin", "javac")]:
        if not os.path.exists(p):
            missing.append(p)
    if missing:
        raise RuntimeError("missing SDK/NDK components:\n  " +
                           "\n  ".join(missing) +
                           "\nsee docs/android.md for the download recipe")


def _debug_keystore() -> str:
    ks = os.path.join(SDK_DIR, "debug.keystore")
    if not os.path.exists(ks):
        keytool = os.path.join(JAVA_HOME, "bin", "keytool")
        _run([keytool, "-genkeypair",
              "-keystore", ks, "-alias", "androiddebugkey",
              "-storepass", "android", "-keypass", "android",
              "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
              "-dname", "CN=Android Debug,O=Misa,C=NG"])
        print(f"generated debug keystore: {ks}")
    return ks


def _build_native(workdir: str, abi_key: str):
    """Compile prog.o + runtime, link libmisa.so for one ABI."""
    triple, abi_dir = ABIS[abi_key]
    triple = canonical_target(triple)
    cc = _ndk_clang(triple)
    abi_work = os.path.join(workdir, abi_key)
    os.makedirs(abi_work, exist_ok=True)

    prog_o = os.path.join(abi_work, "prog.o")
    ok, errors = build(args.misa, prog_o, target=triple)
    if not ok:
        raise RuntimeError(f"misa build failed for {triple}: {errors}")

    rt_objs = ensure_runtime(triple)
    _defines, _cflags, libs = runtime_flags(triple)
    lib_dir = os.path.join(workdir, "lib", abi_dir)
    os.makedirs(lib_dir, exist_ok=True)
    lib_so = os.path.join(lib_dir, "libmisa.so")
    cmd = ([cc, "-shared", "-fPIC", "-O2", "-o", lib_so, prog_o]
           + rt_objs + libs)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"native link failed ({triple}):\n{r.stderr}")
    print(f"  [{abi_key}] libmisa.so ({os.path.getsize(lib_so)} bytes)")
    return lib_dir


def _build_dex(workdir: str) -> str:
    """javac the android/java sources, d8 to classes.dex."""
    javac = os.path.join(JAVA_HOME, "bin", "javac")
    src_dir = os.path.join(MISA_DIR, "android", "java")
    classes = os.path.join(workdir, "classes")
    os.makedirs(classes, exist_ok=True)
    sources = []
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".java"):
                sources.append(os.path.join(root, f))
    env = dict(os.environ, JAVA_HOME=JAVA_HOME,
               PATH=os.path.join(JAVA_HOME, "bin") +
               os.pathsep + os.environ.get("PATH", ""))
    _run([javac, "--release", "8", "-cp", ANDROID_JAR,
          "-d", classes] + sources, env=env)
    dex_dir = os.path.join(workdir, "dex")
    os.makedirs(dex_dir, exist_ok=True)
    _run(["bash", _tool("d8"), "--lib", ANDROID_JAR,
          "--min-api", "24", "--output", dex_dir,
          os.path.join(classes, "com", "misa", "app",
                       "MisaActivity.class"),
          os.path.join(classes, "com", "misa", "app",
                       "MisaWidgets.class"),
          os.path.join(classes, "com", "misa", "app",
                       "MisaNative.class")],
         env=env)
    dex = os.path.join(dex_dir, "classes.dex")
    # The dex must live at the APK ROOT (aapt add stores the given
    # relative path), so copy it out of the d8 output dir first.
    dex_root = os.path.join(workdir, "classes.dex")
    shutil.copy(dex, dex_root)
    print(f"  classes.dex ({os.path.getsize(dex_root)} bytes)")
    return dex_root


def _package(workdir: str) -> str:
    """aapt package the manifest into an unsigned APK skeleton."""
    import re
    manifest = os.path.join(MISA_DIR, "android", "AndroidManifest.xml")
    # aapt1's XML parser chokes on comments ("not well-formed"), so
    # stage a comment-stripped copy; the source keeps its docs.
    staged = os.path.join(workdir, "AndroidManifest.xml")
    with open(manifest) as f:
        src = f.read()
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    with open(staged, "w") as f:
        f.write(src)
    unsigned = os.path.join(workdir, "unsigned.apk")
    _run([_tool("aapt"), "package", "-f",
          "-M", staged, "-I", ANDROID_JAR, "-F", unsigned])
    return unsigned


def main() -> int:
    global args
    ap = argparse.ArgumentParser(description="build a debuggable Misa APK")
    ap.add_argument("--misa", required=True, help=".misa source file")
    ap.add_argument("--out", default="/tmp/m6/misa.apk",
                    help="output APK path")
    ap.add_argument("--abis", default="arm64,armv7",
                    help="comma-separated: arm64, armv7")
    args = ap.parse_args()

    _check_sdk()
    abis = [a.strip() for a in args.abis.split(",") if a.strip()]
    for a in abis:
        if a not in ABIS:
            print(f"unknown abi {a!r} (choose from {sorted(ABIS)})",
                  file=sys.stderr)
            return 1

    workdir = tempfile.mkdtemp(prefix="misa-apk-")
    print(f"workdir: {workdir}")
    try:
        lib_dirs = [_build_native(workdir, a) for a in abis]
        dex = _build_dex(workdir)
        unsigned = _package(workdir)

        # add libs + dex with paths relative to workdir
        for lib_dir in lib_dirs:
            abi = os.path.basename(lib_dir)
            for f in sorted(os.listdir(lib_dir)):
                rel = os.path.relpath(os.path.join(lib_dir, f), workdir)
                _run([_tool("aapt"), "add", unsigned, rel], cwd=workdir)
        dex_rel = os.path.relpath(dex, workdir)
        _run([_tool("aapt"), "add", unsigned, dex_rel], cwd=workdir)

        aligned = os.path.join(workdir, "aligned.apk")
        _run([_tool("zipalign"), "-f", "4", unsigned, aligned])

        ks = _debug_keystore()
        env = dict(os.environ, JAVA_HOME=JAVA_HOME,
                   PATH=os.path.join(JAVA_HOME, "bin") +
                   os.pathsep + os.environ.get("PATH", ""))
        _run(["bash", _tool("apksigner"), "sign",
              "--ks", ks, "--ks-key-alias", "androiddebugkey",
              "--ks-pass", "pass:android", "--key-pass", "pass:android",
              "--out", args.out, aligned], env=env)

        print("APK:", args.out,
              f"({os.path.getsize(args.out)} bytes)")
        print("--- aapt dump badging ---")
        print(_run([_tool("aapt"), "dump", "badging",
                    args.out]).stdout.strip().splitlines()[0])
        print("--- apksigner verify ---")
        print(_run(["bash", _tool("apksigner"), "verify",
                    "--print-certs", args.out],
                   env=env).stdout.strip())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
