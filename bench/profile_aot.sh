#!/usr/bin/env bash
# Profile a Misa AOT executable with Linux perf; keep profiling separate from timing.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 SOURCE.misa OUTPUT_DIR [PROGRAM_ARGS...]" >&2
  echo "Example: $0 bench/multilang/sieve.misa bench/results/profile-sieve 500000" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$1"
OUT="$2"
shift 2
SOURCE="$(cd "$(dirname "$SOURCE")" && pwd)/$(basename "$SOURCE")"
OUT="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"

if ! command -v perf >/dev/null 2>&1; then
  echo "Linux perf is required for symbol-level profiling; install linux-tools for this kernel." >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required by the Misa launcher." >&2
  exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/misa-perf.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/misa-profile-target"

cd "$ROOT"
python3 impl/bin/misa build "$SOURCE" -o "$BIN"
{
  echo "source=$SOURCE"
  echo "revision=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "platform=$(uname -a)"
  echo "perf=$(perf --version 2>&1)"
  echo "command=$BIN $*"
} > "$OUT/metadata.txt"

# Preserve the program output separately so correctness can be checked.
# perf may be restricted in containers/VMs; fail clearly rather than silently
# claiming a profile was collected.
perf record --call-graph dwarf -o "$OUT/perf.data" -- "$BIN" "$@" > "$OUT/stdout.txt"
perf report --stdio --no-children --sort symbol -i "$OUT/perf.data" > "$OUT/hot-symbols.txt"
echo "Profile written to $OUT"
echo "Inspect hot-symbols.txt and perf.data before changing runtime dispatch or boxing."
