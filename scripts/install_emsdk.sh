#!/usr/bin/env bash
# Install the Emscripten SDK (pinned version) for Misa Phase 4 M3.
#
# Usage: scripts/install_emsdk.sh [version]
# Clones emsdk into ~/workspace/emsdk, installs + activates the pinned
# toolchain. Idempotent: re-running with the same version is a no-op
# past the already-installed state.
set -euo pipefail

VERSION="${1:-4.0.10}"
EMSDK_DIR="${EMSDK_DIR:-$HOME/workspace/emsdk}"

if [ ! -d "$EMSDK_DIR/.git" ]; then
  echo "cloning emsdk into $EMSDK_DIR ..."
  git clone https://github.com/emscripten-core/emsdk.git "$EMSDK_DIR"
fi

cd "$EMSDK_DIR"
git fetch --tags --quiet
git checkout -q "$VERSION" 2>/dev/null || git checkout -q "tags/$VERSION" 2>/dev/null || {
  echo "warning: tag $VERSION not found; staying on current checkout" >&2
}

./emsdk install "$VERSION"
./emsdk activate "$VERSION"

echo "emsdk $VERSION installed at $EMSDK_DIR"
echo "activate with: source $EMSDK_DIR/emsdk_env.sh"
