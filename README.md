# Misa v0.3 — the language that thinks

> **Name history:** originally "Lemma"; renamed to **Misa** on 2026-10-04
> after a trademark screen found "Lemma" taken three ways — an existing
> language (github.com/lemma/lemma), a YC AI-dev-tools startup
> (uselemma.ai), and lemma.work. "Misa" screened clean: no language, no
> dev tool, no startup; npm/crates.io `misa` look available (PyPI ships
> as `misa-lang`).

A programming language for building cross-platform, AI-native apps.
One codebase, native binaries everywhere, and AI calls as typed
language constructs — with budgets, evals, and versioned prompts
built into the syntax.

This is v0.3: a frozen spec, a working tree-walking interpreter, and
the **LLVM AOT backend** — `misa build` compiles programs to native
machine-code binaries (see `spec/AOT.md`). Mobile targets and native
UI rendering are later phases (see `../your_files/misa/vision.md`).

## Layout

```
spec/           the frozen language specification
  SYNTAX.md     lexical grammar, EBNF, canonical format, error codes
  SEMANTICS.md  execution model, model calls, budgets, evals, streams
  STDLIB.md     builtins, media types, screen components
  AOT.md        the LLVM AOT backend: pipeline, runtime, GC, coverage
impl/           the reference implementation (Python 3, stdlib only)
  misa/        lexer, parser, checker, formatter, interpreter, providers
  misa/aot/    LLVM backend: IR emitter + C runtime (values, GC,
               JSON, blocking model calls, budgets)
  bin/misa     the CLI
examples/       runnable .misa programs
tests/          interpreter, compiler, tooling, and platform contract tests
tests/aot/      AOT parity suite: native binaries vs the interpreter
                (program fixtures compare outputs, errors, and exits)
```

## Quick start

```sh
export PATH="$PWD/impl/bin:$PATH"

misa check examples/hello.misa        # type-check
misa check examples/hello.misa --json # versioned diagnostics for agents
misa run examples/hello.misa          # run (interpreted)
misa build examples/hello.misa -o hello  # compile to native binary
./hello
misa fmt examples/hello.misa          # canonical format
misa test examples/plantcare.misa --mock examples/mocks.json
misa profile examples/hello.misa --json --memory
```

`misa check --json` preserves the compatibility `errors` and `warnings`
arrays while adding `schema_version: "misa.check.v1"` and a normalized
`diagnostics` array with severity, code, message, line/column, and hint fields.
`misa profile --memory` reports the peak traced CPython heap used while
interpreting (including interpreter overhead); it is not process RSS. Use
`misa benchmark` for process-level RSS measurements.

`misa build` needs Python `llvmlite==0.50.0` (`pip install -r impl/requirements.txt`)
and a C compiler. On Debian/Ubuntu, install `libgc-dev` to enable the Boehm
collector; it is **not** installed automatically. Without it, the runtime uses
a plain-`malloc` fallback that does not reclaim temporary Misa values, so
allocation-heavy processes can grow substantially. The benchmark manifest
records which allocator was used. `libcurl` enables model calls and `http.get`.
Streaming model calls are rejected at build time with `E402` — use `misa run`.

A model call needs a provider key from the environment:

```sh
# mock provider (deterministic, for tests)
misa run examples/plantcare.misa --mock examples/mocks.json

# real providers (keys from env, never from source)
MISA_GEMINI_KEY=... misa run myapp.misa
```

Supported providers in v0.1: `mock`, `openai` (OpenAI-compatible,
`MISA_OPENAI_BASE_URL` override supported), `gemini`.

## The idea in 30 seconds

```misa
model diagnose(plant: Photo, notes: Text) -> Diagnosis
    with mock.diag
    budget { tokens: 2000, latency: 5s, cost: $0.01 }
    prompt v3 """
    You are a botanist. Photo: {plant}. Notes: {notes}.
    Respond with structured data only.
    """

let d = diagnose(Photo("leaf.jpg"), "yellow spots")
// d is a typed Diagnosis record — validated, budget-enforced.
```

```misa
eval diagnose_healthy_plant {
    model: diagnose,
    given: { plant: Photo("healthy.jpg"), notes: "looks fine" },
    expect: result.confidence > 0.7 and not result.urgent,
    runs: 5,
}
```

## Status

The project status and test inventory below are generated from
[`spec/roadmap.json`](spec/roadmap.json). The performance report distinguishes
measured results from roadmap goals; the limited MIR-to-AOT path is experimental
and does not replace the established emitter for unsupported functions.

For performance work, `python impl/bin/misa benchmark` is the canonical
correctness-gated Misa-vs-Python harness. The separate
[`bench/multilang`](bench/multilang/README.md) harness compares a matched
sieve workload across Misa, Python, Node.js, PHP, Go, and Rust; it is explicitly
a single-workload smoke benchmark, not a general language ranking. The
[comparative AI-agent evaluation protocol](agent-trials/BENCHMARK-PROTOCOL.md)
defines a controlled approach for future language/productivity trials. See
[`bench/multilang/README.md`](bench/multilang/README.md) for the six-language
benchmark matrix and `bench/profile_aot.sh` for Linux AOT profiling.

## License

Dual-licensed MIT / Apache-2.0 (see LICENSE-MIT, LICENSE-APACHE),
the same arrangement as Rust.

<!-- STATUS:START -->
## Project Status

**Version:** 0.3.0

| Phase | Name | Status |
|-------|------|--------|
| 1 | Spec & Interpreter | ✅ complete |
| 2 | LLVM AOT Backend | ✅ complete |
| 3 | Type System (generics, sum types, match) | ✅ complete |
| 4 | Concurrency (structured, typed, budgeted) | ✅ complete |
| 5 | Package Registry | ✅ complete |
| 6 | Cross-Platform (Android, iOS, WASM) | 🚧 in-progress — Android beta, iOS beta (ABI fixed); web/WASM validation remains in progress |
| 7 | Performance Optimization | 🚧 in-progress — LLVM O2, amortized list.push/list.reserve, typed list reads, and an experimental verified scalar MIR-to-LLVM path shipped; broader MIR CFG/effects and broader optimized array layouts remain planned |
| 8 | Ignition (public launch) | ⏳ not-started — Compiler/runtime stabilization and independent benchmark validation before public launch |
| 9 | Flywheel (ecosystem growth) | ⏳ not-started |

**Tests:** 477 unit, 41 AOT parity, 11 differential

<!-- STATUS:GENERATED — do not hand-edit. Run tools/gen_readme.py to regenerate. -->
<!-- STATUS:END -->
