# Phase 7 (extension): Concurrency for Misa

Status: SPEC (2026-10-06). Ordered by Hamis: "implement concurrency too and
ours should even be better and faster than anything out there right now."

## Design goals

1. **Structured** — no leaked tasks, ever (Go's fire-and-forget goroutines
   are the anti-pattern). A task that outlives its scope is cancelled.
2. **Typed** — `spawn` returns `Task<T>`; `await` yields `T`; channels are
   `Chan<T>`. Type errors at compile time, not deadlocks at runtime.
3. **Budgeted** — `await t timeout ms` makes timeouts a first-class,
   catchable error. This is Misa's budget philosophy applied to time.
4. **No silent failures** — a task that throws captures the error; `await`
   rethrows it. A task never awaited reports its error at program end.
5. **Portable v1** — cooperative tasks, not OS threads. Runs identically
   on interpreter, host AOT, Android, iOS, and web. True multi-core
   parallelism is explicitly v2 (documented, not promised).

## Syntax

```misa
// spawn a concurrent task; T inferred from the block's return type
let t: Task<Text> = spawn {
  return http.get("https://api.example.com/a")
}

// do other work here...

// wait for the result (yields to the scheduler until done)
let body: Text = await t

// wait with a timeout budget (milliseconds); timeout is a catchable Error
let body = await t timeout 5000

// sleep without blocking other tasks
sleep(100)

// typed channels
let ch = chan<Int>()
ch.send(42)
let v: Int = ch.recv()   // yields until a value arrives

// parallel fetch combinator (the 90% use case, one line)
let pages: [Text] = fetch_all([
  "https://a.example.com",
  "https://b.example.com",
])
```

- `spawn { }` takes a block; the block's `return` type is `T`.
- `await` already exists for model `TokenStream`s (E205); it now also
  accepts `Task<T>` and yields `T`. `await` on anything else is still E205.
- `await t timeout <Int>` — the timeout must be a non-negative Int literal
  or Int expression; on expiry the await raises a catchable `Error`
  `{code: "E306", message: "task timed out after <ms>ms"}`. New E-code E306.
- `sleep(ms: Int)` — yields the current task for at least `ms` ms.
- `chan<T>()` — unbounded FIFO channel v1 (bounded channels are v2).
  `send` never blocks v1; `recv` yields until a value is available.
  `close(ch)` — optional v1: after close, `recv` drains then raises
  catchable E306 `"channel closed"`. (Decide during implementation whether
  close ships v1; document the decision.)
- `fetch_all(urls: [Text]) -> [Text]` — parallel HTTP GET preserving order;
  any single failure raises the first error (catchable). Built on the task
  system, not special runtime magic.

## Semantics

- **Scheduling:** cooperative round-robin. A task runs until it `await`s,
  `recv`s on an empty channel, `sleep`s, or finishes. No preemption: a
  task that never yields never yields (document this; infinite CPU loops
  starve the scheduler — same as JavaScript).
- **Scopes:** a task spawned inside a function is a child of that
  invocation. If the function returns while children are still running,
  the children are cancelled (their `await`s raise E306 "task cancelled").
  No orphans, no leaks.
- **Errors:** a throwing task captures `{code, message}`. `await` rethrows
  it at the awaiter (catchable with try/catch). If a task is never
  awaited, its error is printed to stderr at program end (never silent).
- **Program end:** the runtime waits for all outstanding tasks (bounded by
  an internal deadline); tasks still running are cancelled with E306.
- **Shared state:** tasks share memory (like Go). No data-race detector
  v1 — document that mutable sharing across tasks is the programmer's
  responsibility. Prefer channels / task results.
- **Determinism note:** scheduling order is not specified. Programs that
  depend on interleaving order are buggy by definition — same as Go.

## Types

- `Task<T>` and `Chan<T>` are builtin generic types (like `List<T>`).
- `spawn { return e }` : `Task<T>` where `T` is the type of `e`.
  A block with no `return` is `Task<Void>`.
- `await t` where `t: Task<T>` : `T`.
- `chan<T>()` : `Chan<T>`; `ch.send(v: T)` : `Void`; `ch.recv()` : `T`.
- Passing a `Task`/`Chan` across an FFI boundary is E225 (v1).

## Backends

- **Interpreter:** one Python thread per task behind the cooperative
  facade (GIL limits CPU parallelism; I/O parallelizes fully). Semantics
  as specified; scheduling order unspecified.
- **AOT:** portable fiber scheduler in the C runtime (own stacks via a
  small fiber primitive, round-robin). Blocking I/O (`http.get`) goes
  through non-blocking transports where available (curl_multi for
  parallel fetch); `sleep` via timers. Fibers must work on Linux, Android
  (Bionic), iOS, and Emscripten — no `ucontext` (deprecated/unavailable);
  use a minimal setjmp/longjmp stack-switch or equivalent, ~200 lines,
  no new dependencies.
- **Targets:** must work on all five. If Emscripten fibers prove
  infeasible, web falls back to a single-task cooperative loop with a
  clean E600 only for `spawn` (document; do not silently serialize).

## E-codes

- E205 (existing): `await` on non-awaitable (now: not Task<T> and not
  TokenStream).
- **E306** (new): task timeout / task cancelled / channel closed.
  Catchable, carries a message naming the cause.
- Reuse E300 for scheduler-internal failures (should be unreachable;
  any E300 from the scheduler is a bug).

## v1 non-goals (documented, not promised)

- Multi-core CPU parallelism (cooperative v1; the 90% I/O case is fully
  parallel).
- Bounded channels with backpressure, `select`, work-stealing.
- Data-race detector.
- `Task`/`Chan` across FFI.

## Acceptance

- `spawn`/`await`/`sleep`/`chan`/`fetch_all` on interpreter + host AOT,
  byte-identical observable behavior (modulo timing).
- Parallel `fetch_all` of N URLs measurably faster than sequential
  (benchmark it; report the number).
- Timeout, cancellation-on-scope-exit, and never-awaited-error paths all
  covered by tests.
- Fibers build on all five targets (web fallback documented if needed).
- Docs: `docs/tour.md` (concurrency chapter), `docs/stdlib.md`
  (`Task`/`Chan`/functions), `spec/ERRORS.md` (E306).
