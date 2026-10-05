# Task T12 — Nested (Tier 3: nested records + optional field)

## Goal
Write a Misa program using nested record types and an optional field.

## Requirements
- `type Address { city: Text, zip: Text }`
- `type Person { name: Text, address: Address, nickname: Text? }`
- `let p: Person = { name: "Ada",`
  `address: { city: "Lagos", zip: "10001" }, nickname: nil }`
- Print `{p.name} from {p.address.city}`.
- Then set `p.nickname` — no: `nickname` must stay `nil`; instead
  print `nick: {p.nickname}` (renders as `nil`).

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `Ada from Lagos` and `nick: nil`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
