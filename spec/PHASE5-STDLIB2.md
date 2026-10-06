# Phase 5 — Stdlib growth: JSON, HTTP, time, math

**Ordered by Hamis 2026-10-06** (with FFI; showcase app follows when both
are significant). Closes the most glaring stdlib gaps for real apps.

## `json` namespace

| Function | Signature | Description |
|---|---|---|
| `json.parse` | `json.parse(s: Text) -> value` | Parse JSON. Objects → Misa records, arrays → lists, numbers → Int/Float (Int when no fraction/exponent), strings → Text, booleans → Bool, null → nil. Throws `E300`-family code on invalid JSON. |
| `json.stringify` | `json.stringify(v: value) -> Text` | Canonical compact JSON. Records → objects, lists → arrays. Throws on non-JSON values (e.g. `Db`, functions). |

`value` here means "any JSON-representable Misa value" — the checker
treats it as the existing dynamic value universe (like `print`'s
parameter). Full static JSON typing is a later phase.

## `http` namespace additions

| Function | Signature | Description |
|---|---|---|
| `http.post` | `http.post(url: Text, body: Text) -> Text` | POST with text body; returns response body. Throws `E304` like `http.get`. |
| `http.post_json` | `http.post_json(url: Text, v: value) -> Text` | `json.stringify` the value, POST with `Content-Type: application/json`. |

(Headers/cookies/auth are a later phase; `misa serve` covers the
server side.)

## `time` namespace

| Function | Signature | Description |
|---|---|---|
| `time.now` | `time.now() -> Int` | Unix epoch seconds. |
| `time.now_ms` | `time.now_ms() -> Int` | Unix epoch milliseconds. |
| `time.format` | `time.format(epoch: Int, fmt: Text) -> Text` | `strftime`-style formatting (UTC). |
| `time.parse` | `time.parse(s: Text, fmt: Text) -> Int` | Parse to epoch seconds; throws `E300` on failure. |

## `math` namespace

| Function | Signature | Description |
|---|---|---|
| `math.sqrt` | `math.sqrt(x: Float) -> Float` | |
| `math.pow` | `math.pow(x: Float, y: Float) -> Float` | |
| `math.abs` | `math.abs(x: Float) -> Float` | Also accepts Int. |
| `math.floor` | `math.floor(x: Float) -> Int` | |
| `math.ceil` | `math.ceil(x: Float) -> Int` | |
| `math.round` | `math.round(x: Float) -> Int` | Half away from zero. |

## Implementation notes

- Interpreter: Python `json`, `urllib` (POST), `time`/`datetime`,
  `math` modules — all stdlib.
- AOT: reuse patterns — JSON via a small bundled C JSON parser
  (or generate from the C runtime); HTTP POST extends the existing
  libcurl wrapper; time via `time()`/`strftime`; math via libm
  (`-lm`, already linked for most targets — verify).
- Every function needs AOT parity (byte-identical behavior).

## Exit criteria

- [ ] All four namespaces work identically on interpreter + AOT.
- [ ] New E-codes documented; full suite green.
- [ ] `examples/` gains `json_demo.misa` exercising parse/stringify
      round-trips.
