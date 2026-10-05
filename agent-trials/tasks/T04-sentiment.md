# Task T04 — Sentiment (Tier 2: model call + eval)

## Goal
Write a Misa program that classifies sentiment via a typed model call,
with an eval proving the behavior.

## Requirements
- `provider mock { key: env("MOCK_KEY") }`
- `models { cls: mock("cls") }`
- `type Sentiment { label: Text, confidence: Float }`
- `model classify(text: Text) -> Sentiment with mock.cls`,
  `budget { tokens: 500 }`, `prompt v1` mentioning `{text}` and
  "Respond with structured data only."
- `eval sentiment_eval { model: classify,`
  `given: { text: "I love this" },`
  `expect: result.confidence > 0.5, runs: 2 }`
- Top level: `let s = classify("terrible service")`, print `s.label`.

## Mock scripts (write to mocks.json next to the program)
{"cls": {"label": "positive", "confidence": 0.9}}

## Acceptance
- `misa check program.misa` is clean.
- `misa test program.misa --mock mocks.json` reports 1/1 evals passed.
- `misa run program.misa --mock mocks.json` prints `positive`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
