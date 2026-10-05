# Task T11 — Dungeon (Tier 3: mutable state in a loop)

## Goal
Write a Misa program simulating 3 dungeon rooms with mutable state.

## Requirements
- `var hp = 10`, `var gold = 0`.
- `for room in ["goblin", "chest", "dragon"]`:
  - "goblin": `hp = hp - 3`, `gold = gold + 5`
  - "chest": `gold = gold + 20`
  - "dragon": `hp = hp - 5`, `gold = gold + 50`
- Print `hp: {hp}, gold: {gold}`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `hp: 2, gold: 75`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
