# Misa Benchmark Findings — combined weakness list (benchmarks 1 + 2)

**Status:** private. No publication without Hamis's explicit approval.
**Method:** `spec/BENCHMARK.md` (limitations L1–L7 apply to both) +
`bench/specs/crossplatform.md`. Raw data: `bench/results/`.

Severity scale:
- **BLOCKS** — the thousand-mile claim cannot be honestly made until fixed.
- **HURTS** — measurably weakens the claim; fix soon.
- **MINOR** — papercut; fix opportunistically.
- **CONTEXT** — not a weakness, but constrains how results may be cited.

---

## Benchmark 1 recap (CLI task manager, 3+3 trials, zero human help)

| | Misa | Python |
|---|---|---|
| Completion | 100% (3/3) | 100% (3/3) |
| Wall (mean) | 229 s | 49 s (**4.7× faster**) |
| Correction loops (mean) | 4.3 | 0 |
| Artifact | 1.87 MB binary | 6 KB source |
| Startup | 7.7 ms (**~8× faster**) | 64 ms |

Proved: writability (agents converged from docs alone); the structured
error loop (all 13 Misa loops were compile-time, file:line:col, fixed in
one iteration each; zero runtime debugging). Did NOT prove: reduced
iteration cycles for CLI apps — Python's training-data advantage dominated.

## Benchmark 2 recap (cross-platform: CLI + Android APK, one codebase, 3+3 trials)

| | Misa | Python |
|---|---|---|
| CLI completion | 100% (27/27) | 100% (27/27) |
| APK completion (build+sign+structure) | 100% (6/6) | 100% (6/6) |
| Wall (mean) | 300 s (258/313/329) | 1029 s (2070/702/314) |
| CLI loops (mean) | 2.7 | 0 |
| APK loops (mean) | **0, 0, 0** | 4.3 (7/2/4) |
| LOC (mean) | 235 (one file) | 486 (multi-file + Java) |
| Shared code (mean) | 82% | 75% |
| Host artifact | 1.88 MB / ~8 ms startup | .py / 64 ms startup |
| APK size (mean) | **4.15 MB** | **18.0 MB** (4.3×) |
| APK build (final) | 13–56 s, one command | 9–11 s assembly + toolchain ordeal |
| APK route | `scripts/build_apk.py` (official) | hand-assembled around Chaquopy 17.0.0 |

Critical context: Python's standard route (buildozer/Gradle) was
environmentally blocked (Gradle daemon killed by a sandbox confirmation
gate; 0 bytes in 280s). Trial 1 burned 2070 s discovering this. Trials 2–3
were given the environment lesson and told to prefer the Chaquopy-manual
route. APK on-device RUNTIME is unverified for both languages (no
emulator/device on this box) — `apk_completed` = builds, signs, and
structurally contains the required app.

---

## The weakness list (Misa only — this is the fix list)

### HURTS

**W-1. First-contact language-learning friction.**
B1: 4.3 loops, 4.7× wall vs Python. B2: 2.7 CLI loops (APK leg: 0).
The same papercuts recur across trials: `{` inside strings always opens
interpolation (E001 hit 2/3 B2 trials + B1 — the audit documented it as
M-1, trials prove it bites); no `else if` in the grammar (hit B1 and B2);
`any`/`value` rejected as annotations (E208 confusion); strict
return-path analysis (E211). Root cause: grammar choices that differ from
every mainstream language, multiplied by zero training-data presence (L5).
Mitigating truth: every loop was a fast compile-time error with a
position — zero runtime-debugging loops in 6 Misa trials. This amortizes
toward zero for an agent that knows Misa, but the *first* contact is where
adoption is won or lost.

**W-2. No tree-shaking — binary size.**
B1: 1.87 MB binary vs 6 KB Python source. B2 host: 1.88 MB. The whole
runtime links into every binary. For phone-sized apps this is fine (and
Misa's 4.15 MB APK beats Python's 18 MB embarrassingly), but for small
CLI utilities it is a genuine, visible weakness — the first number any
skeptic will quote.

**W-3. TFLite is dead weight in every APK.**
`scripts/build_apk.py` unconditionally bundles the TFLite AARs (34
classes + native `.so` for 4 ABIs) whether or not the program uses ML.
The trial APKs (task managers!) carry ~1 MB+ of inference runtime they
never call. Root cause: no conditional bundling. Fix is mechanical:
bundle TFLite only when the program declares a `.tflite` model.

### MINOR

**W-4. `for x in <text>` iterates characters.**
Trial xplat-misa-1 had to probe array-ness via
`str.starts_with(json.stringify(items), "[")` because iterating a Text
yields chars. A reasonable semantic, but it forced an ugly workaround for
ordinary JSON validation. Consider `str.is_json_array` or documenting the
idiom.

**W-5. No `else if`.**
Forces nested `else { if … }`. Hit in B1 and B2 trials. Cheap grammar
addition; every agent expects it.

**W-6. Brace-interpolation asymmetry has no relief valve.**
`{{` escapes, but there is no raw-string literal for JSON-heavy code.
Trials hit E001 repeatedly. (Audit M-1 documented it; trials measure the
cost.)

**W-7. Shared-code % (82% vs 75%) understates the real win.**
The metric is close because Python trial 1 hit 99.1% with a verbatim
shared module. The real Misa advantage is structural — ONE file vs
3–5 files + Java bootstrap + manual assembly — which the percentage
doesn't capture. Not a product weakness; a measurement weakness. Keep
citing the file/project-structure difference, not just the %.

### CONTEXT (constrains citation, not product fixes)

**C-1. Nothing BLOCKS.**
No BLOCKS-severity finding in either benchmark: agents converged 6/6 on
Misa across both, and every failure mode was a fast compile-time error.
The claim's foundation held.

**C-2. Python trial 3 matched Misa on wall (314 s vs 258–329 s)** once
handed the environment lesson. Do NOT cite wall-time as a Misa advantage
for cross-platform work. The durable, citable deltas are: APK loops
0 vs 4.3, APK size 4.15 MB vs 18.0 MB, one file vs multi-file+Java, and
native Android Views vs an embedded CPython interpreter.

**C-3. The Python APKs are hand-assembled, not tool-built.**
buildozer/Gradle never ran in this environment. The Python completion is
real (6/6 structural, shared storage logic, native-widget UI in trial 1),
but "the standard Python tooling failed here" is as much an environment
finding as a language finding. A re-run on an unconstrained box with a
working buildozer is the honest follow-up.

**C-4. APK runtime unverified (both languages).**
No emulator/device exists on this box. Misa's Views-driver + db-on-Android
path in a trial-written app has never executed. The pipeline itself is
proven (Hamis installed the PlantDoc APK on his phone), but these
benchmark APKs are build-verified only.

**C-5. Misa's build enjoyed home-field advantage.**
`scripts/build_apk.py` ran against a preinstalled SDK/NDK with a warm
cache. A from-scratch machine needs the SDK/NDK download too. The
fair comparison is tool-vs-tool on equal footing, not tool-vs-hand-assembly.

## What held up (do not "fix")

- **Writability:** 6/6 Misa trials converged from docs alone, zero human
  help, zero prior Misa knowledge.
- **The structured error loop:** 21 total Misa correction loops across
  both benchmarks (13 + 8); all compile-time, all with positions, all fixed in one
  iteration. Zero runtime-debugging loops. Zero tracebacks.
- **The APK leg was frictionless:** 0 loops across 3 trials; one command;
  13–56 s builds; first-try success every time. This is the thousand-mile
  wedge working as designed.
- **Startup latency:** ~8× faster than CPython on the same app, both
  benchmarks.
- **APK size:** 4.15 MB native vs 18 MB embedded-interpreter. Even with
  the TFLite dead weight (W-3), less than a quarter the size.
