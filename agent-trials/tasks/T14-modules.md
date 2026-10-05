# Task T14 — Modules (v0.2: import)

## Goal
Write a TWO-FILE Misa program using imports.

## Requirements
- Create BOTH files in your work directory:
  - `math.misa`: `fn square(n: Int) -> Int { return n * n }`
  - `main.misa`: `import "math.misa"`, then print the result of
    `square(9)`.
- The import path is relative to the importing file.

## Acceptance
- `misa check main.misa` is clean.
- `misa run main.misa` prints `81`.

## Rules
- Max 15 check/fix iterations. Read the v0.2 spec files first
  (SYNTAX.md, SEMANTICS.md, STDLIB.md).
- Use `misa check --json` for machine-readable errors.
- The deliverable program is `main.misa` (+ `math.misa`).
