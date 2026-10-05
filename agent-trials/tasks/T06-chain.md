# Task T06 — Chain (Tier 2: composing two model calls)

## Goal
Write a Misa program that chains two model calls, feeding one's typed
output into the next.

## Requirements
- `provider mock { key: env("MOCK_KEY") }`
- `models { a: mock("a"), b: mock("b") }`
- `type Label { label: Text }`
- `type Expl { reason: Text }`
- `model classify(text: Text) -> Label with mock.a`, `prompt v1`.
- `model explain(text: Text, label: Text) -> Expl with mock.b`,
  `prompt v1` interpolating `{text}` and `{label}`.
- Top level: `let l = classify("great")`,
  `let e = explain("great", l.label)`,
  print `"{l.label}: {e.reason}"`.

## Mock scripts (write to mocks.json next to the program)
{"a": {"label": "positive"}, "b": {"reason": "upbeat words"}}

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa --mock mocks.json` prints
  `positive: upbeat words`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
