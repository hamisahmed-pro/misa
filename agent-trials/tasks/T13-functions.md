# Task T13 — Functions (v0.2: fn, recursion, Void)

## Goal
Write a Misa program using user-defined functions.

## Requirements
- `fn fib(n: Int) -> Int` with the classic recursive definition
  (return n if n < 2, else fib(n-1) + fib(n-2)).
- `fn shout(msg: Text)` (no return type = Void) printing `HEY {msg}`.
- Top level: print the result of `fib(10)`, then call `shout("misa")`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `55` and `HEY misa`.

## Rules
- Max 15 check/fix iterations. Read the v0.2 spec files first
  (SYNTAX.md, SEMANTICS.md, STDLIB.md).
- Use `misa check --json` for machine-readable errors.
