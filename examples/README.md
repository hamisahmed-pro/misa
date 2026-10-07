# Misa Demo Gallery

Real, runnable programs — each one a teaching artifact for a slice of the
language. Every app passes `misa check` clean, runs on the interpreter, and
compiles with `misa build` to a native binary with **byte-identical output**.

Run any app with:

```bash
python3 impl/bin/misa run examples/<app>.misa
```

or build it:

```bash
python3 impl/bin/misa build examples/<app>.misa -o /tmp/<app> && /tmp/<app>
```

## The apps

| App | What it is | What it demonstrates | How to run it |
|---|---|---|---|
| `tasks.misa` | CLI task manager with SQLite storage, JSON export/import | `db.*` + `table` (compile-time checked SQL), sum types, `match`, generics, closures, FFI (`getenv`), `json`, `time`, `str.*`, `cli` + `argv()` | `misa run examples/tasks.misa -- add "Write report"` (also `list`, `doing`/`done`, `export`, `import`, `--db PATH`, `--help`) |
| `diary.misa` | Offline diary — zero network, SQLite-backed | `db.*` + `table`, typed query rows | `misa run examples/diary.misa` |
| `plantdoc.misa` | AI plant doctor — one codebase to web + Android APK | Typed model calls with budgets, `--mock` provider, screens/UI | `misa run examples/plantdoc.misa --mock examples/plantdoc.mock.json` |
| `apiclient.misa` | HTTP API client CLI — fetches JSON, prints a report | `http.get` (E304 handling), `json.parse`, sum-type `Result<T>` + `match`, typed records from dynamic JSON | `misa run examples/apiclient.misa` (`API_URL=...` overrides the endpoint) |
| `budget.misa` | Personal budget tracker — income/expense ledger, monthly reports | `db.*` + `table`, sum types, generics (`filter<T>`, `sum_by<T>`), `time.format`, integer-cents money math | `BUDGET_CMD=add BUDGET_ARG="Groceries" BUDGET_AMOUNT=42.50 BUDGET_KIND=expense misa run examples/budget.misa` (also `list`, `report`) |
| `sysinfo.misa` | System information reporter — writes `sysinfo-report.json` | FFI (`getenv`, `getlogin`, `sysconf`), `json.stringify`, `fs.write`, nil handling | `misa run examples/sysinfo.misa` |

## Notes

- Misa has no `argv` yet, so CLI-style apps take commands through environment
  variables (see each file's header for the exact variables).
- `sysinfo.misa` deliberately avoids `process.run`: it works in the
  interpreter but `misa build` rejects it with E600, so a gallery app that
  must compile natively sticks to FFI.
- The `sysconf(84)` constant in `sysinfo.misa` (`_SC_NPROCESSORS_ONLN`) is
  glibc/Linux-specific.
- `apiclient.misa` uses `jsonplaceholder.typicode.com`, a stable public test
  API; network failures surface as a clean `Error:`, never a traceback.
