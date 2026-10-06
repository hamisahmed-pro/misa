# Phase 8 (overtake): Interfaces

Status: IMPLEMENTED (2026-10-06). Closes `bench/GAPS-language.md` §A5/A8 —
the single biggest type-system gap (no interfaces, traits, or protocols).

## The idea

Nominal interfaces with `impl` blocks, designed to be learned from one
example and to compose with Misa's existing strengths (checked `throws`,
monomorphized generics, machine-readable errors):

```misa
interface Drawable {
  fn draw(self) -> Text
  fn label(self) -> Text throws E300
}

type Circle { x: Int, y: Int, r: Int }
type Rect { w: Int, h: Int }

impl Drawable for Circle {
  fn draw(self) -> Text { return "circle({self.r})" }
  fn label(self) -> Text { return "c" }
}

impl Drawable for Rect {
  fn draw(self) -> Text { return "rect({self.w}x{self.h})" }
  fn label(self) -> Text { return "r" }
}

fn render_all(items: [Drawable]) -> [Text] {
  return items.map(|d| d.draw())
}
```

`interface` and `impl` are contextual (IDENTs, not keywords), like
`test` and `throws` — existing code using those words is unaffected.

## Dispatch strategy (v1)

Three call shapes, resolved by the checker:

| Receiver | Rewrite | Runtime |
|---|---|---|
| `d.draw()`, `d: Drawable` | `iface.Drawable.draw(d, …)` | dynamic: tag switch (AOT) / `IfaceValue` (interp) |
| `x.draw()`, `x: T` with `T: Drawable` bound | `iface.Drawable.draw(x, …)` | dynamic (arg wrapped at the generic call site) |
| `c.draw()`, `c: Circle`, exactly one impl provides `draw` | `impl.Drawable.Circle.draw(c, …)` | static: direct call, zero-cost |

Interface values carry their concrete type at runtime:

- **Interpreter:** `IfaceValue(iface, concrete, payload)`; the checker
  inserts `IfaceWrap`/`IfaceWrapList` nodes at conversion sites.
- **AOT:** an MVal\* record `{__tag, __fields: [payload], __iface_disp}`,
  reusing the sum-type machinery; method calls compile to an LLVM
  `switch` on `__tag` (closed world: every `impl` in the program is
  known at compile time). `print`/`str` of an interface value renders
  the payload (via `__iface_disp`, mirroring `__sum_disp`).

Bounded generics (`fn f<T: Drawable>(x: T)`) monomorphize as before;
method calls inside use dynamic dispatch in v1 (no devirtualization —
documented, not silent).

## Type rules

- A concrete type `T` is assignable to interface `I` iff an
  `impl I for T` exists (checked in `_assignable`, incl. `[T]` → `[I]`
  and `T` → `I?`). Conversions wrap the value (`IfaceWrap`).
- Calling a method not in the interface on an interface-typed value is
  E200 (`interface 'Drawable' has no method 'bogus'`).
- Field access on an interface-typed value is E200 (methods only, like
  Rust/Go).
- `match` on an interface-typed scrutinee is E200 (v1).
- Impl signatures must match the interface exactly (param types,
  return type); impl `throws` must be ⊆ the interface's declared
  `throws` (E200 otherwise — the interface is the contract callers see).
- Missing or extra impl methods are E200/E210.
- Impl target must be a plain declared record type (no sums, no
  generics, no builtins) — E200 otherwise.
- Duplicate `impl I for T` is E210.

## v1 limitations (documented, not silent)

- No default method bodies (a body in an `interface` method is a parse
  error).
- No blanket impls.
- No `match` on interface values (E200).
- Nested interface coercion (`[[Circle]]` → `[[Drawable]]`) works for
  list literals; for non-literal values it is E200.
- Bounded-generic method calls dispatch dynamically.
- Interface values cannot cross the FFI boundary (E225, like Task/Chan).

## Throws interplay

- The interface method's declared `throws` is the contract: dynamic
  calls (`iface.I.m`) contribute the interface's set to the caller's
  throws (checked by E227 as usual).
- Static calls (`impl.I.T.m`) contribute the impl's inferred set.
- Impl bodies are throws-checked like function bodies.
