# Task T15 — Errors (v0.2: try/catch/throw, fs)

## Goal
Write a Misa program using error handling.

## Requirements
- `fn load_or(path: Text, fallback: Text) -> Text`: try `fs.read(path)`
  and return it; on error (`catch e`) return `fallback`.
- Top level: print the result of `load_or("/nope/missing.txt",
  "fallback-ok")`.
- Then: inside a `try` block, `throw { code: "E777", message: "demo" }`;
  in the `catch` block print `caught {e.code}`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `fallback-ok` and `caught E777`.

## Rules
- Max 15 check/fix iterations. Read the v0.2 spec files first
  (SYNTAX.md, SEMANTICS.md, STDLIB.md).
- Use `misa check --json` for machine-readable errors.
