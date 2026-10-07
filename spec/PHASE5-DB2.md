# Phase 2 — Advanced SQLite-first database story
**Ordered by Hamis 2026-10-07.** "Most advanced, most thorough, most complete."

## Audit of v1 (2026-10-07)

What exists: `db.open/exec/exec_many/begin/commit/rollback/query/close`
(interp + AOT with bundled SQLite), `table` decls, regex-based E224
name validation (single-table SELECT only), `examples/diary.misa`.

Gaps found:
1. SQL validation is regex-only: no JOINs, no table aliases, no
   subqueries, no aggregates — anything beyond `SELECT cols FROM t
   [WHERE ...]` falls back to dynamic (unchecked).
2. No expression type-checking: `WHERE int_col = 'text'` passes check.
3. No placeholder checking: `WHERE id = ?` with 0 or 2 bind args passes.
4. `INSERT/UPDATE/DELETE` not validated at all.
5. Nested `db.begin` fails at runtime ("cannot start a transaction
   within a transaction") — no savepoints.
6. Migrations deferred; no schema-drift detection.
7. No EXPLAIN/query-plan support.

## What Phase 2 builds

### 2A. Real SQL parser (`impl/misa/sqlparse.py`)
Tokenizer + recursive-descent parser → AST. Covers:
- `SELECT [DISTINCT] ... FROM t [AS] a [, ...]`
  `[INNER|LEFT|RIGHT|FULL|CROSS JOIN ... ON ...]`
  `[WHERE ...] [GROUP BY ...] [HAVING ...] [ORDER BY ...] [LIMIT ...]`
- Select list: `*`, `t.*`, columns, expressions, aliases, aggregates
  (`COUNT(*)`, `SUM/AVG/MIN/MAX`), scalar functions (subset)
- WHERE/GROUP/HAVING/ORDER expressions: full binary/unary tree,
  `LIKE`, `IN`, `BETWEEN`, `IS [NOT] NULL`, placeholders
- Derived tables: `FROM (SELECT ...) AS sub`
- `INSERT INTO t (cols) VALUES (...)`, `UPDATE t SET ... WHERE ...`,
  `DELETE FROM t WHERE ...`
- Unparseable SQL → `None` → dynamic fallback (same as v1: unchecked,
  not an error).

### 2B. Deep static analysis (checker)
- Scope resolution: aliases/qualifiers; unqualified columns must be
  unambiguous across JOINs → **E232** on ambiguity/unknown qualifier.
- Expression type-checking against column types → **E231** on
  mismatches (`int_col = 'x'`, `bool_col + 1`, `AND` on Text...).
- Placeholder count vs bind-arg count → **E233**.
- INSERT/UPDATE/DELETE column validation (extends E224).
- Result type inference: aggregates, nullability (LEFT JOIN → right
  side nullable; `Text?` stays optional), `*` expansion per table.
- Bind-arg type inference: `WHERE id = ?` with `id: Int` → arg should
  be Int (E231 on clear mismatch when the arg is a literal; dynamic
  args are not flagged).

### 2C. Nested transactions (both backends)
- Depth counter per handle. Depth 0→1: `BEGIN`; N→N+1: `SAVEPOINT
  misa_sp_N`; commit at depth N>1: `RELEASE misa_sp_{N-1}`;
  rollback at depth N>1: `ROLLBACK TO SAVEPOINT misa_sp_{N-1}` then
  `RELEASE`.
- Mismatched commit/rollback without begin → E303 (as today).

### 2D. Migrations
- `db.migrate(db, ms: [{version: Int, sql: Text}]) -> Int`
  (returns number applied). Tracks in `_misa_migrations(version)`.
  Applies pending versions in ascending order, each in its own
  transaction; duplicate versions → E303; out-of-order apply is fine
  (only pending ones run).

### 2E. Query plans
- `db.explain(db, sql, args?) -> [{id, parent, notused, detail}]`
  (rows of `EXPLAIN QUERY PLAN`).

### 2F. AOT parity + tests + docs
- `misa_db.c`: savepoint depth tracking, `misa_db_migrate`,
  `misa_db_explain`.
- AOT parity program `a35_db.misa`; unit tests for parser, E231/2/3,
  savepoints, migrations, explain.
- Spec updated; `examples/` gains `shop.misa` (JOINs, migrations,
  nested transactions).

## Deliberately deferred (still)
- Postgres (Phase 7 FFI).
- SQLCipher encryption.
- Updatable-view / trigger DDL validation (DDL is dynamic).
- Cost-based optimization hints.
