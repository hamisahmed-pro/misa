# Task T07 — Budget (Tier 2: budget that must NOT trip)

## Goal
Write a Misa program whose model call carries a realistic budget that
the (mock) provider stays inside.

## Requirements
- `provider mock { key: env("MOCK_KEY") }`
- `models { m: mock("m") }`
- `type R { ok: Bool }`
- `model ping() -> R with mock.m`,
  `budget { tokens: 500, latency: 30s, cost: $1.00 }`,
  `prompt v1`.
- Top level: `let r = ping()`, `assert(r.ok, "not ok")`,
  print `budget ok`.

## Mock scripts (write to mocks.json next to the program)
{"m": {"ok": true}}

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa --mock mocks.json` prints `budget ok`
  (no E500).

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
