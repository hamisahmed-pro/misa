# Misa Agent Autonomy Trials — Protocol

**Question:** can an AI coding agent with no prior knowledge of Misa write
correct Misa programs using only the spec + the CLI, fixing its own
errors through the structured error loop, with no human in the loop?

**Method:** 12 tasks in 3 tiers. One autonomous worker per task. Each
worker gets ONLY:
- `~/workspace/misa/spec/SYNTAX.md`, `SEMANTICS.md`, `STDLIB.md`
- the CLI at `~/workspace/misa/impl/bin/misa`
- its task file (goal, requirements, mock scripts, acceptance criteria)

Workers MUST NOT read `~/workspace/misa/impl/` (the implementation).
Max 15 check/fix iterations per worker. All model calls use the
deterministic mock provider.

**Convergence** = every acceptance criterion in the task file passes.
Workers report honestly; non-convergence is data, not failure.

**Artifacts per trial:** `/tmp/misa-trials/<TID>/program.misa`,
`/tmp/misa-trials/<TID>/mocks.json` (if needed),
`/tmp/misa-trials/<TID>/result.json`.

**Aggregate report:** `~/workspace/misa/agent-trials/report.md` —
per-task table, convergence rate, iteration distribution, error-code
taxonomy, qualitative notes, verdict on the agent-writability thesis.

**Known limitation:** workers inherit the coordinator's context, so
blinding is by instruction, not by isolation. Reported as such.
