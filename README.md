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
tests/          zero-dependency test suite (74 checks)
tests/aot/      AOT parity suite: native binaries vs the interpreter
                (12 programs, byte-identical stdout/exit/errors)
```

## Quick start

```sh
export PATH="$PWD/impl/bin:$PATH"

misa check examples/hello.misa        # type-check
misa run examples/hello.misa          # run (interpreted)
misa build examples/hello.misa -o hello  # compile to native binary
./hello
misa fmt examples/hello.misa          # canonical format
misa test examples/plantcare.misa --mock examples/mocks.json
```

`misa build` needs Python `llvmlite==0.50.0` (`pip install -r impl/requirements.txt`), a C
compiler, and (for model calls / `http.get`) libcurl. The Boehm GC is
built from source automatically when absent. Streaming model calls are
rejected at build time with `E402` — use `misa run` for those.

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

- [x] Phase 0 — spec frozen (v0.1)
- [x] Phase 1 — interpreter: `misa run` with hot-reload-style dev loop,
      real model calls, budgets, evals, streams
- [x] Phase 2 (tooling, partial) — `misa fmt` (canonical, idempotent),
      `misa check` (+ `--json` for agents), `misa test`
- [ ] Phase 3 — LLVM AOT backend
- [ ] Phase 4 — iOS/Android toolchains, native widget rendering, WASM web
- [ ] Phase 5 — `misa pkg`, stdlib growth, on-device inference
- [ ] Phase 6 — self-hosting

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
| 6 | Cross-Platform (Android, iOS, WASM) | 🚧 in-progress — Android beta, iOS beta (ABI fixed), WASM broken (time.parse) |
| 7 | Performance Optimization | 🚧 in-progress — LLVM O2 AOT passes and amortized `list.push` shipped; Int/float unboxing and string builder done; typed arrays and optimizing MIR remain planned |
| 8 | Ignition (public launch) | ⏳ not-started — Waiting for community, visibility, launch strategy per Hamis |
| 9 | Flywheel (ecosystem growth) | ⏳ not-started |

**Tests:** 473 unit, 36 AOT parity, 10 differential

<!-- STATUS:GENERATED — do not hand-edit. Run tools/gen_readme.py to regenerate. -->
<!-- STATUS:END -->
