# Phase 5 — Type-system tranche: first-class functions, sum types, generics

**Ordered by Hamis 2026-10-06.** These three features were unscheduled in
phases 5–9; they now land in Phase 5, staged as 5a → 5b → 5c. Each stage
ships in the Python implementation (interpreter + checker + AOT) with
tests before the next begins. The self-hosted (Phase 6a) ports being
written concurrently target the v0.2 language and are NOT blocked on this
tranche; they gain the new features as a follow-up port.

## 5a — First-class functions

**Syntax.**
- Function types: `(Int, Text) -> Bool` as a type expression (params,
  `->`, return type). `() -> Void` for thunks.
- Lambda values keep the existing `|params| body` / `|params| expr`
  syntax; params may carry types: `|x: Int| x * 2`. Untyped params infer
  from the expected function type at the use site; an error (new E-code)
  when neither is available.
- Any `fn` decl is usable as a value: `let g = fib` gives `g: (Int) -> Int`.

**Semantics.**
- Closures capture their defining scope (interpreter: close over `Env`;
  AOT: closure-convert to `{fn_ptr, env_ptr}` pairs).
- Arity/type mismatch at call sites is E102/E211 as today.
- `str`, `int`, etc. remain builtins, not values (no `let f = print`).

**Implementation notes.**
- Checker: new `FuncType` internal representation; assignability is
  structural on `(params) -> ret`.
- AOT: monomorphize per signature at call sites; closures become an
  env struct + static function. No GC change (Misa values are refcounted
  already — verify env structs participate).

## 5b — Sum types + `match`

**Syntax.**
```misa
type Shape {
  Circle(radius: Float),
  Rect(w: Float, h: Float),
}

let s: Shape = Shape.Circle(1.0)

match s {
  Shape.Circle(r) => r * 3.14,
  Shape.Rect(w, h) => w * h,
}
```
- Variant construction is `Type.Variant(args)`; `match` scrutinee arms
  bind variant fields as fresh variables.
- `match` is an expression (yields a value); arms use `=>`.

**Semantics.**
- Exhaustiveness: the checker errors (new E-code) when `match` arms miss
  a variant and there is no `_` wildcard arm.
- Unreachable-arm detection is a warning, not an error (v1).
- Variants with no fields: `type Opt { None, Some(Int) }` — `Opt.None`.

**Implementation notes.**
- Runtime representation: tagged struct `{tag: Int, payload: ...}`.
  Interpreter: Python tuple/dict. AOT: LLVM struct with tag + largest
  payload (union layout).
- `match` compiles to a tag switch (interpreter: if-chain; AOT: LLVM
  `switch`).

## 5c — Generics

**Syntax.**
```misa
fn first<T>(xs: [T]) -> T {
  return xs[0]
}

type Box<T> {
  value: T,
}

let b: Box<Int> = Box<Int>(42)
```
- Type parameters on `fn` and `type`; bound at use: `first([1,2,3])`
  infers `T = Int`, `first<Int>(...)` pins explicitly.
- No constraints in v1 (unconstrained `<T>` only); `where` clauses are
  a later phase.

**Semantics.**
- Call-site inference: unify argument types against parameter types;
  ambiguity or conflict is a type error (new E-code).
- Generic sum types compose: `type Result<T> { Ok(T), Err(Text) }`.

**Implementation notes.**
- Interpreter: erasure (values are already dynamically tagged; generics
  are a checking-time concept).
- AOT: monomorphization — stamp out one specialization per concrete
  type tuple per generic fn/type at build time. Recursion through
  generics (`type L<T> { Cons(T, L<T>) }`) needs lazy specialization;
  cap depth and error cleanly rather than looping.

## Exit criteria (Phase 5 type tranche)

- [ ] Spec examples above run identically on interpreter and AOT
      (byte-identical stdout/exit/errors).
- [ ] New E-codes documented in spec/ERRORS.md with tests.
- [ ] `examples/` gains `generics.misa`, `sums.misa`, `closures.misa`.
- [ ] Existing suite stays green (74/74 + on-device + AOT parity).

## Deliberately deferred

- `where`-clause constraints, higher-kinded types, variadic generics.
- Exhaustiveness warnings → errors; pattern guards in `match`.
- Tail-call optimization (orthogonal).
