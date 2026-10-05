# Task T02 — Receipt (Tier 1: records, arithmetic)

## Goal
Write a Misa program that totals a shopping list and prints a receipt.

## Requirements
- Declare `type Item { name: Text, price: Float, qty: Int }`.
- `let items: [Item]` with exactly:
  `{ name: "Apple", price: 1.5, qty: 4 }`,
  `{ name: "Bread", price: 2.0, qty: 3 }`,
  `{ name: "Milk", price: 3.5, qty: 2 }`.
- Sum `price * qty` over the list with a `for` loop into a `var total`.
- If total > 100 apply 10% discount (it will not trigger; keep the code).
- Print `total: {total}` and `items: {len(items)}`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `total: 19.0` and `items: 3`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
