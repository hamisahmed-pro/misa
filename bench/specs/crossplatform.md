# xplat — frozen cross-platform benchmark spec v1 (2026-10-06)

One codebase, TWO artifacts. This spec is FROZEN. Do not change it
between trials or languages. The verifiers are the sole judges of
completion.

## The app

A task manager with SQLite persistence and JSON export/import.

### Artifact 1 — CLI (host)

Identical to the benchmark-1 taskman spec (`bench/specs/taskman.md`):
driven ENTIRELY by environment variables `TASKMAN_CMD` / `TASKMAN_ARG` /
`TASKMAN_DB`, same schema, same 8 commands, same output lines, same exit
codes. Judged by `bench/verify_taskman.sh` — completion is
`RESULT: PASS 27/27`. Read `bench/specs/taskman.md` for the full detail;
everything there applies unchanged.

### Artifact 2 — Android APK

A working Android app built from the SAME codebase:

- **Misa:** your single `.misa` file must additionally declare an `app`
  block and `screen`s. Build with
  `python3 scripts/build_apk.py --misa <file> --out <workdir>/taskman.apk`
  (repo-rooted script; SDK/NDK already installed on this box).
  On Android `TASKMAN_CMD` is unset — your program must `navigate()`
  to its home screen in that case.
- **Python:** build an APK from the same codebase via the most viable
  Python-on-Android route (recommended: buildozer + Kivy; alternatives:
  Briefcase, Chaquopy — your choice, document why). `pip` and network
  are allowed FOR THE APK LEG ONLY. Timebox: if Android toolchain setup
  (SDK downloads, first builds) exceeds ~45 minutes without producing
  an APK, STOP and report honestly — non-completion is valid data, not
  failure. Check `~/.buildozer` first: a previous trial may have cached
  the toolchain — reuse it, do not re-download blindly.

The APK's UI must contain (verified structurally from your sources,
since this box has no emulator or device):
1. A task **list** driven by the SQLite database (same schema as the CLI).
2. An **add-task** affordance: a text input plus a button (or equivalent)
   that inserts the new task into the database.
3. Both must share the database logic with the CLI (no separate
   reimplementation of storage — one shared code path).

### Completion criteria

- `cli_completed`: `bench/verify_taskman.sh` prints `RESULT: PASS 27/27`
  against your host artifact (re-run by the orchestrator, not just you).
- `apk_completed`: your APK file builds successfully AND passes
  `bench/verify_apk.sh` (package badging present, signature verifies,
  required UI components present in your sources).
- **Honesty note (applies to both languages):** this box has no Android
  emulator and no physical device, so on-device RUNTIME behavior is
  UNVERIFIED for every trial. `apk_completed` means "builds, signs, and
  structurally contains the required app" — not "tested on a phone."

### Metrics (per trial)

`cli_completed`, `apk_completed`, `wall_seconds` (spawn → final report),
`correction_loops` (split as `cli_loops` / `apk_loops` in your report),
`loc` (all delivered source lines), `shared_code_pct` (computed by the
orchestrator: lines serving both artifacts ÷ total lines),
host `artifact_bytes` + `startup_ms`, `apk_bytes`, `apk_build_seconds`.

### Rules (same as benchmark 1, plus)

- Zero human help. Work inside your workdir. Iterate freely; count EVERY
  failed attempt with a one-line failure log.
- Misa: toolchain at `impl/bin/misa` (via `python3`), docs
  `spec/SYNTAX.md`, `spec/SEMANTICS.md`, `spec/STDLIB.md`,
  `spec/ERRORS.md`, `spec/PHASE5-DB.md`, `spec/PHASE5-TYPES.md`,
  `docs/targets.md`. Do NOT read `examples/`, `agent-trials/`,
  `selfhost/`, `impl/`, or `bench/work/`.
- Python CLI: stdlib only (`sqlite3`, `json`, `os`, `sys`, `datetime`).
  APK leg: pip + network allowed.
- Misa deliverables: `<workdir>/taskman.misa` → `<workdir>/taskman`
  (host binary, `misa build`) and `<workdir>/taskman.apk`
  (`scripts/build_apk.py`). Python deliverables: `<workdir>/taskman.py`
  (+ any shared modules) and `<workdir>/taskman.apk`.
- When both artifacts are done (or you judge further effort will not
  converge), write `<workdir>/report.json`:
  `{"trial": <id>, "language": <lang>, "cli_completed": bool,
    "apk_completed": bool, "cli_loops": int, "apk_loops": int,
    "failure_log": [...], "apk_build_seconds": int|null,
    "apk_route": "<e.g. buildozer+kivy>" (python only),
    "notes": "..."}`
  Then stop; your final message just says done.
