# GAP-AUDIT — parent's own probes (2026-10-06)

## CRITICAL-1: Integer overflow — silent interp/AOT divergence, spec violation
- Spec (spec/SEMANTICS.md:92): `Int` is "Arbitrary-precision integer".
- Interpreter (Python bigints): `9223372036854775807 + 1` → `9223372036854775808` ✓ spec-compliant
- AOT (int64): `9223372036854775807 + 1` → `-9223372036854775808` (silent wraparound) ✗
- Worse: `int("99999999999999999999999")` → interp `99999999999999999999999`, AOT silently clamps to `9223372036854775807` (INT64_MAX), then `+1` wraps to `-9223372036854775808`.
- Repro (`/tmp/edge1.misa`, `/tmp/edge6.misa`):
  ```misa
  print(9223372036854775807 + 1)   // interp: 9223372036854775808 | AOT: -9223372036854775808
  let x = int("99999999999999999999999")
  print(x)                          // interp: 99999999999999999999999 | AOT: 9223372036854775807
  ```
- No error on either side — silent wrong numbers. Affects any program doing arithmetic near int64 boundaries; AOT spec-compliance is broken for `Int`.

## HIGH-1: Division by zero — interp traceback, AOT clean E300 (divergence)
- `print(1 / 0)` / `print(1.0 / 0.0)` on interpreter → raw Python `ZeroDivisionError` traceback (no E-code).
- Same program on AOT → clean `1:11: E300: division by zero`.
- Repro (`/tmp/edge4.misa`, `/tmp/edge5.misa`):
  ```misa
  print(1.0 / 0.0)   // interp: Traceback ZeroDivisionError | AOT: E300 division by zero
  ```
- The interpreter is missing the div-zero guard the AOT emitter has.

## MEDIUM-1: `{{`/`}}` brace asymmetry — JSON literal trap
- `"{{"` → `{` (escape works), but `"}}"` → `}}` (two literal chars, NOT an escape).
- So `json.parse("{{\"a\": 1}}")` actually parses `{"a": 1}}` → E300 "Extra data".
- Correct form: `json.parse("{{\"a\": 1}")`. Logical (only `{` needs escaping) but a footgun for anyone writing JSON inline; spec should document prominently.
