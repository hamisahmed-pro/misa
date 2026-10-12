# Comparative AI-agent benchmark protocol

The existing cold trials demonstrate that models can write some Misa programs
from the spec. They do **not** establish that Misa improves agent productivity
relative to Python, TypeScript, Go, or Rust. Any future comparative claim should
use this protocol and publish raw task-level data.

## Experimental design

- Freeze language/compiler commits, model identifiers, prompts, task suite,
  checkers, and dependency versions before the trial.
- Use at least 30 tasks across multiple strata: small algorithmic changes,
  typed data transformations, tests/bug fixes, API integration, refactoring,
  and multi-file maintenance. Include hidden tests and follow-up change requests.
- For each task, give the same task description and equivalent reference
  materials to every language condition. Randomize task/language order and
  use independent fresh sessions. Blind graders to language where feasible.
- Use the same model version and inference settings, tool access, wall-clock
  budget, retry policy, and network policy. Record any unavoidable differences.
- Run each generated solution in a clean sandbox. Acceptance must include
  compiler/type checks, visible tests, hidden tests, and a task-specific semantic
  oracle. A model saying that work is complete is not acceptance.
- Separate first-draft correctness from repaired final correctness. Preserve all
  patches, checker feedback, tool calls, timestamps, token counts, and failures.

## Primary and secondary metrics

**Primary:** total cost per successfully completed, hidden-test-passing task.

Report, at minimum:
- first-draft pass rate and final pass rate;
- median and p90 wall-clock time to accepted completion;
- number of repair iterations and tool calls;
- input/output tokens and actual provider cost;
- hidden-test pass rate and regression rate on follow-up maintenance tasks;
- compile/runtime failures, timeouts, and unsupported-feature failures.

Use paired task-level comparisons, confidence intervals, and a predeclared
analysis plan. Report the full denominator, including timeouts and failed tasks.
Do not omit a language because its result is unfavorable.

## Guardrails against misleading conclusions

- Do not compare different task sets, model versions, retry limits, or hidden
  test difficulty and call the result a language effect.
- Do not use a third-party proxy's reported cost as vendor-billed cost without
  verifying it.
- Do not count a program as successful merely because it parses or passes its
  own generated tests.
- Publish task definitions, grading code, aggregate data, and redacted raw logs
  so another person can reproduce the analysis.
- Treat a small pilot as exploratory. No general claim about agent productivity
  should be made until the sample and uncertainty support it.

The current repository's 12-task cold trial is a useful pilot. This protocol is
a future measurement gate, not a report of an experiment already run.
