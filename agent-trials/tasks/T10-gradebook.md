# Task T10 — Gradebook (Tier 3: nested conditionals)

## Goal
Write a Misa program that grades students with nested conditionals.

## Requirements
- `type Student { name: Text, score: Int }`
- `let students: [Student]` with exactly:
  `{ name: "A", score: 92 }`, `{ name: "B", score: 71 }`,
  `{ name: "C", score: 58 }`.
- For each student print `{name}: {grade}` where grade is "A" for
  score >= 90, "B" for score >= 70, else "F". (Nested if/else is fine.)

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `A: A`, `B: B`, `C: F` (one per line).

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
