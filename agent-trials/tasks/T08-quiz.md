# Task T08 — Quiz (Tier 3: lists from a model + eval over them)

## Goal
Write a Misa program where a model returns a list, and an eval reasons
about the list contents.

## Requirements
- `provider mock { key: env("MOCK_KEY") }`
- `models { q: mock("q") }`
- `type Question { question: Text, options: [Text], answer: Int }`
- `model ask(topic: Text) -> Question with mock.q`,
  `budget { tokens: 800 }`, `prompt v1` interpolating `{topic}`.
- `eval quiz_eval { model: ask, given: { topic: "math" },`
  `expect: result.answer >= 0 and result.answer < len(result.options),`
  `runs: 2 }`
- Top level: `let q = ask("math")`, print `q.question`,
  print `q.options[q.answer]`.

## Mock scripts (write to mocks.json next to the program)
{"q": {"question": "2+2?", "options": ["3", "4", "5"], "answer": 1}}

## Acceptance
- `misa check program.misa` is clean.
- `misa test program.misa --mock mocks.json` reports 1/1 evals passed.
- `misa run program.misa --mock mocks.json` prints `2+2?` and `4`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
