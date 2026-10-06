# taskman — frozen benchmark spec v1 (2026-10-06)

A small CLI task manager with SQLite persistence and JSON export/import.
This spec is FROZEN. Do not change it between trials or languages.
Anything not specified here is up to the implementer, but the verifier
(`bench/verify_taskman.sh`) is the sole judge of completion.

## Interface

The program is driven ENTIRELY by environment variables (no argv, no stdin):

| Variable     | Meaning                                                        |
|--------------|----------------------------------------------------------------|
| `TASKMAN_CMD`| Command: `add`, `list`, `doing`, `done`, `delete`, `export`, `import`, `help`. Unset or empty → `help`. |
| `TASKMAN_ARG`| Argument: title for `add`; id for `doing`/`done`/`delete`; file path for `export`/`import`. |
| `TASKMAN_DB` | SQLite database path. Default: `./tasks.db` in the current working directory. |

## Database

SQLite. Schema (create if not exists on every run):

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  created TEXT NOT NULL
);
```

`status` is one of `todo`, `doing`, `done`. New tasks start as `todo`.
`created` is a timestamp string in any non-empty format (ISO-8601 preferred).

## Commands

- `add` — `TASKMAN_ARG` is the title. If unset/empty: print
  `Error: title required`, exit 1. Else insert and print
  `Added #<id>: <title>`, exit 0. `<id>` is the new row id.
- `list` — Print one line per task: `#<id> [<status>] <title>`,
  ordered by ascending id. If no tasks: print `No tasks.`. Exit 0.
- `doing` / `done` — `TASKMAN_ARG` is the task id. If it is not a
  positive integer: print `Error: bad id '<value>'`, exit 1. If no such
  task: print `Error: no task #<id>`, exit 1. Else update status and
  print `Marked #<id> as doing` (or `as done`), exit 0.
- `delete` — Same id handling as above. On success print
  `Deleted #<id>`, exit 0.
- `export` — `TASKMAN_ARG` is the destination file path. If unset/empty:
  print `Error: file required`, exit 1. Write a JSON array of all tasks
  ordered by ascending id, each object shaped exactly like
  `{"id": 1, "title": "...", "status": "todo", "created": "..."}`.
  Print `Exported <n> tasks to <path>`, exit 0. (`<n>` may be 0.)
- `import` — `TASKMAN_ARG` is the source file path. If unreadable:
  print `Error: cannot read <path>`, exit 1. If the content is not a JSON
  array: print `Error: bad JSON in <path>`, exit 1. Each item must have a
  string `title`; `status` is optional (default `todo`) but if present
  must be `todo`/`doing`/`done` else `Error: bad status '<s>'`, exit 1.
  Insert each item as a NEW task (new ids are fine). Print
  `Imported <n> tasks from <path>`, exit 0.
- `help` (or unset/empty `TASKMAN_CMD`) — Print usage text that mentions
  all of: `add`, `list`, `doing`, `done`, `delete`, `export`, `import`.
  Exit 0.
- Any other `TASKMAN_CMD` — Print `Error: unknown command '<cmd>'`
  followed by the usage text. Exit 1.

## Output & exit codes

- Normal output on stdout. Error lines may go to stdout or stderr
  (the verifier reads both combined).
- Exit 0 on success, exit 1 on any user error above.
- Programs must not crash with a traceback/stack dump on any input
  the verifier sends (bad ids, missing files, bad JSON, unknown
  commands). Clean `Error: ...` lines only.

## Notes

- `created` values are never asserted exactly — only that they are
  non-empty strings.
- Exported JSON is parsed and validated structurally, not textually.
- Concurrent runs are not tested. One command per process invocation.
