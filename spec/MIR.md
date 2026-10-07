# Misa IR (MIR) — Design Document

**Status:** Design phase (implements Benchmark Review Plan §32-33)
**Goal:** A typed, optimizing intermediate representation between the
checked AST and LLVM IR.

## Motivation

The current AOT backend emits LLVM IR directly from the typed AST.
Optimizations are ad-hoc pattern matches in `emit.py`:
- Int unboxing: `_is_int_expr` checks scattered across BinOp/LetStmt/AssignStmt
- Float unboxing: same pattern duplicated
- String builder: AST pre-pass with fragile heuristics
- Record field indexing: type tracking in `FnCtx.var_types`

Problems:
1. Each optimization re-discovers type information independently
2. No systematic dataflow (e.g., "is this variable always Int?")
3. Optimizations can't compose (unboxed int + bounds check elimination)
4. No place for classic optimizations (constant folding, DCE, inlining)

MIR solves this: a single typed IR where optimizations are passes.

## Pipeline

```
Typed AST → MIR construction → Optimization passes → LLVM lowering
```

## MIR Design

### Values
- Every MIR value has a **static type**: `i64`, `f64`, `bool`, `MVal*`, etc.
- `MVal*` is the fallback for dynamically-typed values
- The checker's type information is preserved, not re-derived

### Instructions (SSA form)
```
%1 = const i64 42
%2 = add i64 %1, %1          # native add, no boxing
%3 = box i64 %2 -> MVal*     # explicit boxing point
%4 = call misa_print(MVal* %3)
```

Key: **boxing is explicit**. The unboxing optimization becomes:
"eliminate box/unbox pairs where the type is provably primitive."

### Basic blocks & control flow
- Standard CFG: blocks, branches, phi nodes
- Loops are explicit (easier for LICM, bounds-check elimination)

## Optimization Passes (in order)

1. **Constant folding**: `add i64 2, 3` → `const i64 5`
2. **Dead code elimination**: remove unused definitions
3. **Box/unbox elimination**: the core unboxing pass
   - `%b = box i64 %x; %y = unbox i64 %b` → replace `%y` with `%x`
   - Runs to fixpoint
4. **Inlining**: inline small functions (especially stdlib)
5. **Escape analysis**: 
   - String builder lowering: `s = s + x` in loop → builder
   - Stack allocation for non-escaping records
6. **Bounds-check elimination**: provably-safe indices skip checks
7. **Iterator fusion**: `map(filter(...))` → single loop
8. **Monomorphization**: specialized generic function bodies

## Lowering to LLVM

After optimization, MIR lowers to LLVM IR:
- `i64` → `i64`, `f64` → `double`, `bool` → `i1`
- `MVal*` → existing runtime calls (`misa_add`, etc.)
- Explicit `box`/`unbox` → `misa_int` / pointer loads

## Migration Plan

**Phase A:** MIR construction (no optimizations)
- Build MIR from typed AST
- Lower MIR to LLVM (should produce identical code to current)
- Differential test: MIR backend vs direct backend

**Phase B:** Core passes
- Constant folding, DCE, box/unbox elimination
- Should subsume current ad-hoc int/float unboxing

**Phase C:** Advanced passes
- Escape analysis, iterator fusion, monomorphization

**Phase D:** Decommission
- Remove ad-hoc optimizations from `emit.py`
- MIR becomes the only path

## What MIR Enables (per review)

- **Systematic unboxing**: not pattern-matching, but dataflow
- **String-builder lowering**: via escape analysis, not heuristics
- **Iterator fusion**: `map`/`filter` chains become single loops
- **Record layouts**: structs instead of `MVal*` with field lookup
- **Bounds-check elimination**: provably-safe array access

## Non-goals

- MIR is not a VM bytecode (that's the existing bytecode JIT)
- MIR does not replace the interpreter (tree-walking stays for dev)
- No new language features in MIR (it's an implementation detail)

---

*Derived from Benchmark Review Plan §32-33 (2026-10-07).*
*The review calls MIR "the next major compiler milestone."*
