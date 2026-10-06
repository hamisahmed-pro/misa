# Misa Launch Bar — Phase 7 exit criteria

Private until every box is checked. No public release without Hamis's
explicit approval. (Roadmap: `~/workspace/your_files/misa/thousand-mile-roadmap.md`)

## Language & toolchain
- [x] Phase 5 type system (5a/5b/5c), DB story, stdlib2 — shipped
- [x] Phase 6 self-hosting — Misa compiles Misa
- [x] Phase 7 FFI — `extern` blocks, libc/libm proven
- [x] Adversarial audit — 33/33 gaps fixed (spec/GAP-AUDIT.md)
- [x] LSP + VS Code extension

## Proof
- [ ] **Benchmark suite** — agent-velocity benchmark run and recorded:
      same agent, same app spec, zero human help, Misa vs Python/alternatives;
      metrics: completion rate, wall time, tokens/cost, correction loops,
      LOC, binary size, startup time, AI-call failure rate.
      Results stay PRIVATE until Hamis approves publication.
- [ ] **iOS on hardware** — PARKED (Hamis 2026-10-06: cost concern; GitHub
      Actions billing blocked). Unparks when billing is fixed or he says so.
- [ ] **Docs** — user-facing: getting started, language tour, stdlib
      reference, FFI guide, error catalog. (Specs are internal; docs are
      for developers.)
- [ ] **Demo gallery** — ≥5 showcase apps, each running on interpreter +
      AOT binary, demonstrating distinct strengths.

## Readiness
- [ ] `misa check` clean on all examples; full suites green
      (currently 233/233 + 21/21 AOT parity)
- [ ] No known CRITICAL/HIGH gaps (audit: spec/GAP-AUDIT.md)
- [ ] Hamis's explicit approval to go public

## Explicitly out of scope for the bar
- Package registry, debugger, concurrency, Postgres, argv —
  tracked as future work, not launch blockers.
