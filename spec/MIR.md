# Misa IR (MIR)

**Status:** experimental scalar compiler path in production AOT builds; the established AST emitter remains the general fallback. MIR is not yet the canonical backend for the whole language.

## Implemented boundary

The checked-AST lowering stage currently accepts a narrow, side-effect-free scalar subset: typed Int/Float/Bool parameters and values, scalar literals, supported arithmetic/comparisons, local immutable bindings, and a single final return. Unsupported AST constructs are rejected by lowering and handled by the existing AOT emitter; they are not silently represented as MIR.

Verified and optimized Int/Float functions eligible for raw specialization are emitted as LLVM scalars. A thin boxed wrapper remains for generic/dynamic callers. Other functions continue through the established emitter. MIR-to-LLVM does not yet represent general effects, calls, allocation, mutable state, exceptions, branches, or loops.

## Representation

A MIR value has a unique per-function SSA identifier and a static type (`i64`, `f64`, `bool`, or `MVal*`). Current stage-1 arithmetic uses primitive values; boxing/unboxing instructions are explicit. Every block has an instruction sequence, but MIR v0.1 verification and code generation deliberately accept exactly one entry block. Separate CFG terminators and cross-block SSA are rejected until their verifier and lowering support arrive together.

## Verification boundary

`verify(module)` must run before optimization and again after every optimization round. It validates:

- function and block structure, supported types, and unique non-negative value IDs;
- definition-before-use, operand identity/type agreement, and supported operation signatures;
- constant immediate type/range, including signed 64-bit Int constants;
- arithmetic and comparison operand/result types, boxing/unboxing contracts, and return type;
- exactly one final return in the supported single-block form, with no instructions after it;
- explicit rejection of unsupported instructions, extra blocks, and CFG terminators.

Invalid MIR is rejected before LLVM emission. Optimization refuses malformed input and raises if an internal pass produces invalid MIR. The verifier is intentionally stricter than the IR data classes: construction alone does not imply validity.

## Current optimization passes

The optimizer runs to a bounded fixpoint:

1. **Constant folding** for pure add/subtract/multiply. Integer folds use Misa's signed two's-complement i64 wrapping semantics; floating folds preserve IEEE values.
2. **Box/unbox elimination** for proven same-type local pairs.
3. **Dead-code elimination** for unused pure results, retaining operations marked effectful.

Each round is verifier-checked. Passes are restricted to the currently supported one-block IR; no claim of general CFG dataflow is made.

## Planned increments

1. **CFG and SSA:** explicit jump/conditional-branch/return terminators, stable block IDs, branch-target validation, predecessor construction, reachability, dominance and phi nodes.
2. **Effects and checked operations:** model calls, mutable loads/stores, allocation, I/O, model/tool effects, and catchable errors explicitly; preserve ordering and error behavior.
3. **Control-flow lowering:** lower branches/loops only with differential tests that compare values, errors, and observable side effects across interpreter, direct AOT, and MIR AOT.
4. **Representation and allocation:** type-directed primitive locals/loops, escape analysis and collection/string allocation improvements based on profiles and validated benchmarks.
5. **Broader adoption:** expand the MIR gate incrementally; keep direct-emitter fallback until an equivalent MIR path is proven for each feature family.

## Non-goals

MIR is not VM bytecode, does not replace the interpreter, and is not a new source-language feature. A successful MIR dump or verification result is not evidence that a program uses the MIR backend; the AOT eligibility gate determines that separately.
