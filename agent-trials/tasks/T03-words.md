# Task T03 — Words (Tier 1: strings, indexing)

## Goal
Write a Misa program that analyzes a word list.

## Requirements
- `let words = ["apple", "fig", "banana", "kiwi"]`.
- Find the longest word with a `for` loop; print `longest: {word}`.
- Count words starting with a vowel (a/e/i/o/u) by indexing the
  first character (`w[0]`); print `vowel-start: {count}`.

## Acceptance
- `misa check program.misa` is clean.
- `misa run program.misa` prints `longest: banana` and `vowel-start: 1`.

## Rules
- Max 15 check/fix iterations. Read the spec files first.
- Use `misa check --json` for machine-readable errors.
