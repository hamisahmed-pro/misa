#!/usr/bin/env bash
# Profile a Misa AOT executable. Prefer Linux perf sampling, with Callgrind as
# a deterministic fallback for hosted runners that deny perf_event access.
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

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required by the Misa launcher." >&2
  exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/misa-perf.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/misa-profile-target"

cd "$ROOT"
python3 impl/bin/misa build "$SOURCE" -o "$BIN" --keep-symbols
# Keep the exact executable and symbol table so address-only samples can be
# resolved after the temporary directory has been removed.
cp "$BIN" "$OUT/misa-profile-target"
if command -v nm >/dev/null 2>&1; then
  nm -n "$BIN" > "$OUT/symbols.txt" 2> "$OUT/nm-stderr.txt" || true
fi
if [[ ! -s "$OUT/symbols.txt" ]]; then
  echo "warning: native symbol table is empty; hot-symbol attribution may be incomplete" >&2
fi
{
  echo "source=$SOURCE"
  echo "revision=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "platform=$(uname -a)"
  echo "perf_event_paranoid=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo unavailable)"
  echo "command=$BIN $*"
} > "$OUT/metadata.txt"

# Start clean: repeated runs must not leave stale profiler data that could be
# mistaken for output from this invocation.
rm -f "$OUT/perf.data" "$OUT/callgrind.out" "$OUT/hot-symbols.txt" \
  "$OUT/stdout.txt" "$OUT/perf-stderr.txt" "$OUT/callgrind-stderr.txt"
# Try sampling first. A perf executable alone does not imply kernel access.
# Only record the selected profiler after it successfully produces a profile.
if command -v perf >/dev/null 2>&1; then
  if perf record --call-graph dwarf -o "$OUT/perf.data" -- "$BIN" "$@" > "$OUT/stdout.txt" 2> "$OUT/perf-stderr.txt"; then
    if perf report --stdio --no-children --sort symbol -i "$OUT/perf.data" > "$OUT/hot-symbols.txt"; then
      echo "profiler=perf" >> "$OUT/metadata.txt"
      echo "profile_mode=perf-sampling" >> "$OUT/metadata.txt"
      echo "Profile written using perf sampling to $OUT"
      exit 0
    fi
  fi
  echo "perf sampling unavailable; attempting Callgrind fallback." >&2
  cat "$OUT/perf-stderr.txt" >&2 || true
  rm -f "$OUT/perf.data"
fi

if command -v valgrind >/dev/null 2>&1 && command -v callgrind_annotate >/dev/null 2>&1; then
  if valgrind --tool=callgrind --callgrind-out-file="$OUT/callgrind.out" --collect-jumps=yes -- "$BIN" "$@" > "$OUT/stdout.txt" 2> "$OUT/callgrind-stderr.txt"; then
    if callgrind_annotate --inclusive=yes --auto=yes "$OUT/callgrind.out" > "$OUT/hot-symbols.txt"; then
      echo "profiler=valgrind-callgrind" >> "$OUT/metadata.txt"
      echo "profile_mode=callgrind-instrumentation" >> "$OUT/metadata.txt"
      echo "Profile written using Callgrind instrumentation to $OUT"
      exit 0
    fi
  fi
  echo "Callgrind profiling failed; see $OUT/callgrind-stderr.txt" >&2
  exit 2
fi

echo "Neither perf sampling nor Callgrind profiling is available." >&2
exit 2
