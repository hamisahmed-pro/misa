#!/bin/bash
# Build Boehm GC (bdwgc 8.2.8, vendored at ~/workspace/deps/bdwgc.tar.gz)
# for Android with the NDK clang (Phase 4, M6 GC experiment).
#
# Usage: build_bdwgc_android.sh [aarch64|armv7]
# Installs to ~/workspace/deps/gc-android-<arch>/ (picked up by
# impl/misa/aot/build.py runtime_flags when include/gc.h exists).
set -euo pipefail

ARCH="${1:-aarch64}"
NDK="$HOME/workspace/android-ndk/android-ndk-r27d"
TC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
SRC="$HOME/workspace/deps/bdwgc.tar.gz"
WORK="/tmp/bdwgc-android-$ARCH"
DEST="$HOME/workspace/deps/gc-android-$ARCH"

if [ ! -x "$TC/aarch64-linux-android34-clang" ]; then
  echo "NDK not found at $NDK" >&2; exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "missing $SRC" >&2; exit 1
fi

case "$ARCH" in
  aarch64) TRIPLE="aarch64-linux-android";  CC="aarch64-linux-android34-clang";;
  armv7)   TRIPLE="arm-linux-androideabi";  CC="armv7a-linux-androideabi34-clang";;
  *) echo "unknown arch $ARCH" >&2; exit 1;;
esac

rm -rf "$WORK"; mkdir -p "$WORK"
tar xzf "$SRC" -C "$WORK"
tar xzf /tmp/libatomic_ops.tar.gz -C "$WORK"
mv "$WORK"/libatomic_ops-7.8.2 "$WORK"/gc-8.2.8/libatomic_ops
cd "$WORK"/gc-8.2.8

export PATH="$TC:$PATH"
export CC="$TC/$CC"
# -fPIC: libgc.a links into libmisa.so (a shared library); without it
# ld errors on non-PIC relocations (R_AARCH64_ADR_PREL_PG_HI21 etc.).
export CFLAGS="-fPIC -O2"
export CXXFLAGS="-fPIC -O2"
# -DANDROID is implied by the clang target; keep the build static.
./configure --host="$TRIPLE" --prefix="$DEST" \
  --disable-shared --enable-static \
  --disable-gcj-support --disable-tests 2>&1 | tail -5

make -j"$(nproc)" 2>&1 | tail -5
make install 2>&1 | tail -3

echo "installed: $DEST"
ls -la "$DEST/lib/libgc.a" "$DEST/include/gc.h"
