# Phase 5 — Database story: SQLite-first, typed queries

**Ordered by Hamis 2026-10-06.** Misa had no database story; this specs it.
Lands in Phase 5's ecosystem tranche. Does NOT wait for Phase 7 FFI —
see "Implementation strategy".

## API (`db` namespace)

```misa
let db = db.open("app.db")          // file; db.open(":memory:") for tests
db.exec(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
db.exec(db, "INSERT INTO users (name) VALUES (?)", ["Hamis"])
let rows = db.query(db, "SELECT id, name FROM users")
// rows: [{id: Int, name: Text}]
for r in rows {
  print(r.name)
}
db.close(db)
```
// M-9: `db.exec`/`db.query`/`db.close` take the handle as the first
// argument (the `db.` prefix is the namespace, not the variable — any
// variable holding a Db handle works, e.g. `mydb.exec(mydb, ...)`).

| Function | Signature | Description |
|---|---|---|
| `db.open` | `db.open(path: Text) -> Db` | Open (creating if needed) a SQLite database. |
| `db.exec` | `db.exec(db: Db, sql: Text, args: [any]?) -> Int` | Run a statement; returns rows changed. `?` placeholders. |
| `db.query` | `db.query(db: Db, sql: Text, args: [any]?) -> [record]` | Run a SELECT; returns list of records. |
| `db.close` | `db.close(db: Db) -> Void` | Close the handle. |

(Bind args are heterogeneous scalars — `Int`, `Float`, `Bool`, `Text`,
or `nil` — bound natively via sqlite3 on both backends.)

## Typed queries (the Misa-shaped idea)

Table schemas declared in Misa; the checker validates SQL at compile time:

```misa
table users {
  id: Int @primary,
  name: Text,
  email: Text?,
}

let db = db.open(":memory:")
let rows = db.query(db, "SELECT id, name FROM users")
// checker KNOWS rows: [{id: Int, name: Text}]

let bad = db.query(db, "SELECT id, nam FROM users")
// E-code: column "nam" does not exist on table "users"
```

**v1 checking:** table names and column names in SELECT/FROM/WHERE must
exist in declared `table` schemas; result record type inferred from the
select list. **Deferred:** expression type-checking inside WHERE,
JOIN validation, migrations.

**Why this matters for agents:** a Python agent's SQL bugs surface at
runtime (often in production). A Misa agent's SQL bugs surface at
`misa check` — before the app ever runs. Same flywheel as typed model
calls: move the error left, into the machine-checkable zone.

## Implementation strategy (no FFI wait)

- **Interpreter (Python):** Python's own `sqlite3` module (stdlib, zero
  new dependencies). `db.*` builtins map straight onto it.
- **AOT (native):** bundle the SQLite amalgamation (`sqlite3.c`) into
  the C runtime and expose `misa_db_*` functions, exactly like
  `misa_fs_read` / `misa_http_get` today. Works on Linux, Android
  (NDK), iOS, and Wasm (via the existing platform shims) with no
  general FFI required.
- **Postgres:** deferred to Phase 7 FFI (`libpq`) or a pure-Misa
  wire-protocol package. SQLite covers the offline-first story that
  Phase 5's exit ("an app with fully offline AI") needs — local data
  plus on-device models, zero network.

## Exit criteria

- [ ] `db.*` works identically on interpreter and AOT (Linux + Android).
- [ ] `table` decls + SELECT/FROM/WHERE name validation in the checker,
      with new E-codes and tests.
- [ ] `examples/diary.misa`: offline diary app — SQLite storage, no
      network, runs on the Android APK build.
- [ ] Existing suite stays green.

## Deliberately deferred

- Postgres driver (Phase 7 FFI).
- Migrations (`db.migrate` versioned schema evolution).
- WHERE/JOIN expression type-checking, query builder DSL.
- Encryption (SQLCipher) — note as Phase 7/8 option.
