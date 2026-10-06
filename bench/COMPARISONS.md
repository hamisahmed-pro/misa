# Misa Head-to-Head Comparisons

**Status:** private. No publication without Hamis's explicit approval.
**Method honesty:** every number below was measured by the author with the
exact command recorded. Anything estimated or reasoned is labeled
**ANALYSIS**, not measurement. The Python numbers are reused from
`bench/FINDINGS.md` (benchmarks 1+2, 6+6 agent trials) — not re-run.
Comparisons were run 2026-10-06 on this Linux x86_64 box.

Comparison artifacts live in `bench/compare/`:
`tasklist.misa` (Misa screens task list), `jsoncli.misa` (Misa JSON CLI),
`go/hello.go` + `go/jsoncli.go`, `rust/` (cargo project, serde_json),
`java/` (hand-assembled Android APK sources). The React app was built in
`/tmp/reactcmp/taskapp` (Vite scaffold; ephemeral).

---

## 1. Misa vs Python

**Source:** `bench/FINDINGS.md` (benchmark 1: CLI task manager, 3+3 agent
trials; benchmark 2: CLI + Android APK, 3+3 trials). Zero human help,
same model, frozen specs, independent re-verification.

| | Misa | Python |
|---|---|---|
| Agent completion (CLI) | 100% (6/6 trials) | 100% (6/6) |
| CLI wall (B1 mean) | 229 s | 49 s (**4.7× faster**) |
| CLI correction loops (B1) | 4.3 | 0 |
| APK loops (B2) | **0, 0, 0** | 4.3 (7/2/4) |
| APK size | **4.15 MB** (native) | 18.0 MB (embedded CPython) |
| Binary startup | **7.7 ms (~8× faster)** | 64 ms |
| Binary size (CLI) | 172 KB (post-tree-shaking) | 6 KB source |
| Codebase (B2) | one 235-line file | ~486 lines, multi-file + Java |

**Verdict.** Python wins the CLI decisively on wall-time and iteration
count — the model's Python training data dominates from-scratch coding.
Misa wins startup latency (~8×), the APK leg decisively (zero loops,
one command, 4× smaller APKs), and the structural prize: one file vs a
project. The honest headline: **Python is the better language for writing
a CLI today; Misa is the better language for shipping that CLI to a
phone tomorrow.** Misa's binary-size weakness vs a 6 KB script is real
but narrowed (1.87 MB → 172 KB by tree-shaking).

---

## 2. Misa vs Rust

**Compared:** systems-style tasks — a hello-world binary and a small
JSON-parsing CLI. Rust via rustup 1.99.0 (`~/.cargo`, minimal profile).
Misa via `impl/bin/misa build` (host AOT, post-tree-shaking).

The JSON programs: Rust reads stdin, deserializes with `serde_json`
into a typed struct, prints `"{name} is {age}"` (19 lines + 8-line
Cargo.toml). Misa reads `/tmp/input.json` (Misa has no stdin/argv —
documented adaptation), `json.parse`, prints via interpolation (4 lines).

| | Misa | Rust |
|---|---|---|
| hello LOC | 1 (`print("hello")`) | 1 (`fn main…`) |
| hello binary (stripped) | **172,216 B** | 336,752 B |
| json LOC | 4 | 19 + 8 (Cargo.toml) |
| json binary (stripped) | **176,336 B** | 440,456 B |
| clean build, hello | 2,368 ms (clang C runtime) | 134 ms (`rustc -O`, no deps) |
| clean build, json | 2,368 ms (same runtime) | 17,419 ms (cargo release, serde compiled) |
| incremental rebuild | ~321 ms (full warm rebuild) | 1,736 ms (cargo release) |
| startup | 6–14 ms | 2–4 ms |

Commands: `rustc -O rhello.rs -o rhello`; `cargo build --release`
(timed, cold target dir); `strip` on both; `python3 impl/bin/misa
build …` (timed, runtime cache cleared for the clean figure).

**Verdict.** Rust wins raw performance, memory control (no GC), and the
mature crates ecosystem by miles — for systems programming there is no
contest, and Misa does not pretend otherwise. But on the measured
systems-task numbers, Misa's tree-shaken binaries are **smaller than
both Rust and Go** (172 KB vs 337 KB vs 2.2 MB), its clean build is
faster than cargo-with-dependencies, and the JSON task took 4 lines vs
19. Where Misa genuinely wins: **agent writability** — benchmark 1
showed agents with zero Misa knowledge converging 6/6 from docs alone
with every error caught at compile time; no equivalent evidence exists
for agents writing borrow-checker-clean Rust unsupervised (ANALYSIS, not
measured — a Rust agent trial is the honest follow-up). Misa also wins
anything involving a phone or a model call, which Rust simply doesn't do.

---

## 3. Misa vs Go

**Compared:** the same hello-world + JSON CLI. Go 1.24.7 from go.dev
tarball (`~/workspace/tools/go`). Go JSON program uses
`encoding/json` with a typed struct (28 lines). Same Misa programs as §2.

| | Misa | Go |
|---|---|---|
| hello LOC | 1 | 7 |
| hello binary | **172,216 B** | 2,209,295 B (2.1 MB) |
| json LOC | 4 | 28 |
| json binary | **176,336 B** | 2,765,685 B (2.6 MB) |
| clean build, hello | 2,368 ms | 10,584 ms (cold GOCACHE) |
| incremental rebuild | ~321 ms (warm) | 133 ms |
| startup | 6–14 ms | 4–7 ms |

Commands: `go build -o hello hello.go` (timed, GOCACHE cleared for the
clean figure); `go build` after `touch` for incremental.

**Verdict.** Go wins build iteration speed (133 ms incremental is superb),
goroutine concurrency (Misa has none for user code), and a standard
library + ecosystem that Misa can't touch. Misa wins binary size by an
order of magnitude (Go statically links ~2 MB of runtime into hello;
Misa's tree-shaken runtime is 172 KB), source brevity on the JSON task
(4 vs 28 lines — `json.parse` + interpolation vs struct tags + error
plumbing), and everything Go doesn't do: Android/iOS targets and typed
model calls. For a network service, take Go. For a small tool you ship
to a phone, Misa's numbers are better.

---

## 4. Misa vs React

**Compared:** the same UI — a task list screen (list + add box). Misa:
`bench/compare/tasklist.misa` using `screen` declarations (SQLite-backed
state, the supported AOT pattern — see note below). React: a Vite +
React app (`/tmp/reactcmp/taskapp`), `App.jsx` with `useState`, built
with `vite build` (verified working build).

| | Misa | React (web) |
|---|---|---|
| App code | **1 file, 44 lines** | 3 files, 52 lines (App.jsx 29, main.jsx 10, index.html 13) |
| Scaffolding | none | package.json, vite.config.js, css files, 22 top-level npm packages (60 MB node_modules) |
| Production JS | n/a (native) | 220 KB bundle (68.8 KB gzip), built in 823 ms |
| `misa check` / build | `misa check` clean; `misa run` renders; `misa build` → native binary | `vite build` clean |
| Second platform | **same file**: `scripts/build_apk.py` → Android APK (one command) | **new project**: React Native — different primitives (`View`/`FlatList` vs `div`/`ul`), Metro bundler, native SDKs (ANALYSIS) |

**A real Misa limitation found during this comparison:** the first
version of `tasklist.misa` kept task state in a screen parameter and
captured it in the Add button's `onTap` handler. The interpreter ran it,
but AOT rejected it: `E600: handler cannot capture 'items': event
handlers see only their params and screen id: bindings`. A top-level
`var` was also invisible to screens (`E101`). The supported pattern —
the one the benchmark-2 trials used — is **SQLite-backed state**: the
handler writes to the db and re-navigates to refresh. It works and it
built to APK, but it is honestly clunkier than React's `useState` for
ephemeral UI state. Ephemeral screen state without a database round-trip
is a genuine gap (filed as a follow-up, not fixed in this pass).

**Verdict.** React wins the web UI ecosystem by miles — component
libraries, devtools, hiring, documentation — and `useState` is a better
ephemeral-state story than Misa's db-backed screens today. Misa wins
one-codebase native reach: the same 44-line file targets Android, iOS,
and web with no second framework, no bridge, no native toolchain on the
developer's machine. A LOC comparison does not settle this; the
ecosystem gap is real and the reach gap is real. They are different bets.

---

## 5. Misa vs Java (Android)

**Compared:** the same task-list app as a native Android APK. Java: a
hand-assembled minimal app — `MainActivity.java` (ListView +
ArrayAdapter + EditText + Button), `activity_main.xml` layout,
`AndroidManifest.xml` — built with `javac` (JDK 21) + `aapt2` + `d8` +
`zipalign` + `apksigner` (build-tools 34.0.0, android-35 platform), 5
manual steps, signed and `apksigner verify` clean. Misa: the same
`bench/compare/tasklist.misa` via `scripts/build_apk.py` (one command).

*Note: Gradle was environmentally blocked in this sandbox (same as
benchmark 2), so the Java build is hand-assembled, not Gradle-built —
the standard professional path. This flatters Java on build-time and
hides Gradle's real-world cost; it does not flatter the file count.*

| | Misa | Java (hand-assembled) |
|---|---|---|
| Source | **1 file, 44 lines** | 3 files, 71 lines (Activity 35, layout 22, manifest 14) + generated R.java |
| Build | **1 command** (`scripts/build_apk.py`) | 5 manual steps (aapt2 ×2, javac, d8, zipalign+sign) |
| APK size | **1,425,964 B** (1.36 MB; includes SQLite — the app is db-backed; a non-db Misa APK is ~143 KB per the tree-shaking sprint) | **12,870 B** (dex 3.5 KB; no native libs; in-memory state only) |
| Build time | 65 s (one command; NDK compile) | ~5.9 s total (aapt2 355 ms, javac 2.2 s, d8 1.9 s) |
| UI framework | Misa screens → native Views driver | Android Views directly |

**Verdict.** Java/Kotlin wins the entire Android ecosystem — libraries,
documentation, tooling, hiring, Play Store pipeline — by an
insurmountable margin today. A minimal Java APK is also far smaller than
anything carrying a language runtime (12.9 KB vs 1.36 MB for the
db-backed Misa app; ~143 KB for a non-db Misa APK). Misa wins the
authoring experience for cross-platform work: one 44-line file that is
*also* the CLI and *also* the iOS and web app, built with one command,
vs Java + XML + manifest that is Android-only forever. For an
Android-only app with a team, Java/Kotlin. For one codebase everywhere,
Misa.

---

## Cross-cutting verdicts

**Strongest Misa result:** the cross-platform APK leg (benchmark 2,
§1): 0 correction loops across 3 trials, one command, 4.15 MB native
APKs vs 18 MB embedded-interpreter APKs. Nothing else in this document
is as lopsided, and it is the thousand-mile wedge working as designed.

**Weakest Misa result:** from-scratch CLI wall-time vs Python (§1):
4.7× slower with 4.3 correction loops. Training data dominates, and no
honest framing avoids it. The mitigations are real (docs now exist,
`else if` + raw strings + better errors landed, first-contact friction
amortizes), but today, for a CLI-only task, Python wins.

**Where Misa is genuinely competitive on systems numbers:** binary size
— post-tree-shaking Misa binaries (172 KB) undercut both Rust (337 KB)
and Go (2.1 MB) on hello-world. That was not true a week ago (1.87 MB);
it is true now, measured.

**Honest boundaries of this document:**
- APK *runtime* is unverified for Misa and Java here (no emulator);
  both APKs are build+sign+structure verified only.
- No agent-writability trials were run for Rust/Go/React/Java — the
  writability claim rests on the 6 Misa Python-comparison trials.
- React Native and Gradle builds were not executed (heavy native
  toolchains); those sections are scoped comparisons + analysis.
- The Misa screen-state limitation (no handler captures, no top-level
  `var` in screens; db-backed state required) is documented in §4 and
  stands as an open gap.
