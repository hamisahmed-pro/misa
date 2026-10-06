# Misa Agent-Velocity Benchmark — Methodology

**Status:** private. Results stay in `bench/results/` (repo-local). No
publication without Hamis's explicit approval.

## Claim under test (agreed framing — do not overclaim)

"Misa reduces failure modes and iteration cycles for AI-native
cross-platform apps." NOT "Misa makes agents smarter." Python remains
stronger for scripting, data science, and existing ecosystems. This
benchmark measures the *loop*, not the *mind*.

## Design

- **Same agent:** each trial is a subagent with the identical base model
  and an identically-structured task prompt. The only difference is the
  language section (Misa toolchain + docs vs Python stdlib).
- **Same spec:** `bench/specs/taskman.md` (frozen, versioned). A CLI task
  manager with SQLite + JSON export/import — the kind of app Misa is for.
  Driven by environment variables (`TASKMAN_CMD`/`TASKMAN_ARG`/`TASKMAN_DB`)
  because Misa has no argv; both languages use the identical interface so
  the comparison is about the build loop, not CLI parsing.
- **Same judge:** `bench/verify_taskman.sh "<run-cmd>" <workdir>` —
  27 language-agnostic checks. Completion = `RESULT: PASS 27/27`.
- **Zero human help:** the trial prompt forbids asking for help; the
  orchestrator only reads the final report. Non-convergence is data.
- **Trials:** ≥3 per language (Misa, Python). More trials smooth
  LLM-latency variance.

## Resources given to each trial

- Misa: the CLI at `impl/bin/misa`, plus `spec/SYNTAX.md`,
  `spec/SEMANTICS.md`, `spec/STDLIB.md`, `spec/ERRORS.md`,
  `spec/PHASE5-DB.md`, `spec/PHASE5-TYPES.md`. The trial MUST NOT read
  `examples/`, `agent-trials/`, `selfhost/`, or `impl/` (blinding by
  instruction, same as `agent-trials/PROTOCOL.md`).
- Python: Python 3.12 standard library only (`sqlite3`, `json`, `os`,
  `sys`, `datetime`). No pip, no network.
- Both: the frozen spec, the verifier, an empty workdir. Both may run
  the verifier as often as they like.

## Deliverables per trial

- Misa: AOT binary at `<workdir>/taskman`
  (`misa build taskman.misa -o taskman`). Verified as `./taskman`.
- Python: `<workdir>/taskman.py`. Verified as `python3 taskman.py`.

## Metrics per trial

| Metric | Source |
|---|---|
| `completed` (bool) | verifier `RESULT: PASS` (re-run by orchestrator, not just self-reported) |
| `wall_seconds` | orchestrator-measured spawn → final report |
| `correction_loops` | self-reported count of failed attempts (check/build/verifier failures), with a failure log |
| `loc` | lines of the delivered source file |
| `artifact_bytes` | AOT binary size vs `.py` file size |
| `startup_ms` | min of 15 runs of a no-op invocation (`list` on empty DB), measured by orchestrator |
| `tokens_in/out` | NOT observable from the subagent runtime — see limitation L2 |

## Orchestration

`bench/run.py` generates trial prompts (`gen-prompts`) and collects
results (`collect`). Spawning and wall-clock measurement are done by the
orchestrating agent (a subagent cannot be spawned from a script); each
trial writes `<workdir>/report.json`, the orchestrator writes
`<workdir>/meta.json` (spawn/complete timestamps), and `collect` merges
them into `bench/results/<timestamp>.json` plus a summary table.

## Runtime benchmarks (deterministic, no agent)

`bench/runtime_bench.py` measures, for hello-world and the showcase apps
(`tasks.misa`, `diary.misa`): AOT binary size, AOT startup latency
(min of 15), interpreter startup latency, vs equivalent Python
(`print("hello")`, and the best Python trial's `taskman.py`).

## Known limitations (read before citing any number)

- **L1 — Temperature/seed:** not settable on subagents. Variance is
  smoothed with ≥3 trials per language, not eliminated.
- **L2 — Tokens:** the runtime does not expose per-agent token counts.
  Wall-clock time is the cost proxy; it conflates model latency with
  reasoning. Correction loops are the cleaner efficiency signal.
- **L3 — Correction loops are self-reported.** The prompt requires an
  honest count with a failure log; the orchestrator spot-checks against
  the transcript where feasible.
- **L4 — Blinding is by instruction.** Trial agents inherit the
  coordinator's context; they are told not to read `examples/` etc.,
  but isolation is not enforced mechanically.
- **L5 — Asymmetric background knowledge.** The base model has seen far
  more Python than Misa in training. This favors Python; any Misa edge
  appears *despite* it, not because of a level field.
- **L6 — Small-n.** This is a pilot (3+3 trials, one app). It detects
  large effects, not subtle ones. Do not generalize to "all apps."
- **L7 — The env-var CLI** neutralizes Python's argparse ergonomics by
  design, to keep the interface identical. A real-world CLI comparison
  would look different.
