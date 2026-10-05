# Task T05 — Translator (Tier 2: multi-param model, prompt interpolation)

## Goal
Write a Misa program that translates text via a two-parameter model call.

## Requirements
- `provider mock { key: env("MOCK_KEY") }`
- `models { tr: mock("tr") }`
- `type Translation { text: Text }`
- `model translate(text: Text, lang: Text) -> Translation with mock.tr`,
  `budget { tokens: 500, latency: 10s }`,
  `prompt v1` must interpolate BOTH `{text}` and `{lang}` and say
  "Respond with structured data only."
- Top level: print the `.text` of `translate("Hello", "French")`.

## Mock scripts (write to mocks.json next to the program)
{"tr": {"text": "Bonjour"}}

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa --mock mocks.json` prints `Bonjour`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
