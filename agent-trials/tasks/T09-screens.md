# Task T09 — Screens (Tier 3: conditional UI + navigation)

## Goal
Write a Misa program with two screens, conditional rendering, and
navigation between them.

## Requirements
- `screen Main(show: Bool)` containing a `Column` with:
  - `Heading(text: "Welcome")`
  - an `if show` block rendering `Banner(text: "shown", style: "info")`
  - `Button(label: "Go", onTap: || { navigate(Detail()) })`
    (the handler body will not run in v0.1; it must still parse and
    check cleanly)
- `screen Detail` containing a `Column` with `Text(text: "detail")`.
- Top level: `navigate(Main(true))` then `navigate(Main(false))`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `--- screen Main ---` twice,
  `--- screen Detail ---` zero times (handlers don't fire in v0.1),
  and the word `Banner` exactly once (only the `show=true` render).

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
