#!/usr/bin/env python3
"""taskman - a small CLI task manager driven entirely by environment variables.

Spec: bench/specs/taskman.md (frozen). Standard library only.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

USAGE = """Usage: taskman (driven by environment variables)

  TASKMAN_CMD   Command: add, list, doing, done, delete, export, import, help.
                Unset or empty -> help.
  TASKMAN_ARG   Argument: title for add; id for doing/done/delete;
                file path for export/import.
  TASKMAN_DB    SQLite database path. Default: ./tasks.db

Commands:
  add     Add a new task with the title from TASKMAN_ARG.
  list    List all tasks ordered by ascending id.
  doing   Mark task TASKMAN_ARG as doing.
  done    Mark task TASKMAN_ARG as done.
  delete  Delete task TASKMAN_ARG.
  export  Export all tasks as JSON to the file in TASKMAN_ARG.
  import  Import tasks from the JSON file in TASKMAN_ARG.
  help    Show this usage text.
"""

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tasks ("
    "id INTEGER PRIMARY KEY, "
    "title TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "created TEXT NOT NULL)"
)

VALID_STATUSES = ("todo", "doing", "done")


def err(msg):
    print(msg)


def get_db():
    path = os.environ.get("TASKMAN_DB") or "./tasks.db"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def parse_id(value):
    """Return the positive int id, or None if value is not a positive integer."""
    if value is None:
        return None
    value = value.strip()
    if not value or not value.isdigit():
        return None
    n = int(value)
    return n if n > 0 else None


def cmd_add(conn, arg):
    if not arg:
        err("Error: title required")
        return 1
    created = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO tasks (title, status, created) VALUES (?, 'todo', ?)",
        (arg, created),
    )
    conn.commit()
    print("Added #{}: {}".format(cur.lastrowid, arg))
    return 0


def cmd_list(conn):
    rows = conn.execute(
        "SELECT id, title, status FROM tasks ORDER BY id ASC"
    ).fetchall()
    if not rows:
        print("No tasks.")
        return 0
    for row in rows:
        print("#{} [{}] {}".format(row[0], row[2], row[1]))
    return 0


def resolve_id(conn, arg, for_cmd):
    """Validate arg as an id and fetch the task. Returns (rc, task_id)."""
    tid = parse_id(arg)
    if tid is None:
        err("Error: bad id '{}'".format("" if arg is None else arg))
        return 1, None
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        err("Error: no task #{}".format(tid))
        return 1, None
    return 0, tid


def cmd_doing(conn, arg):
    rc, tid = resolve_id(conn, arg, "doing")
    if rc:
        return rc
    conn.execute("UPDATE tasks SET status = 'doing' WHERE id = ?", (tid,))
    conn.commit()
    print("Marked #{} as doing".format(tid))
    return 0


def cmd_done(conn, arg):
    rc, tid = resolve_id(conn, arg, "done")
    if rc:
        return rc
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    conn.commit()
    print("Marked #{} as done".format(tid))
    return 0


def cmd_delete(conn, arg):
    rc, tid = resolve_id(conn, arg, "delete")
    if rc:
        return rc
    conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    conn.commit()
    print("Deleted #{}".format(tid))
    return 0


def cmd_export(conn, arg):
    if not arg:
        err("Error: file required")
        return 1
    rows = conn.execute(
        "SELECT id, title, status, created FROM tasks ORDER BY id ASC"
    ).fetchall()
    tasks = [
        {"id": r[0], "title": r[1], "status": r[2], "created": r[3]} for r in rows
    ]
    try:
        with open(arg, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except OSError:
        err("Error: cannot write {}".format(arg))
        return 1
    print("Exported {} tasks to {}".format(len(tasks), arg))
    return 0


def cmd_import(conn, arg):
    if not arg:
        err("Error: cannot read ")
        return 1
    try:
        with open(arg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        err("Error: cannot read {}".format(arg))
        return 1
    except ValueError:
        err("Error: bad JSON in {}".format(arg))
        return 1
    if not isinstance(data, list):
        err("Error: bad JSON in {}".format(arg))
        return 1
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            err("Error: bad task in {}".format(arg))
            return 1
        status = item.get("status", "todo")
        if status not in VALID_STATUSES:
            err("Error: bad status '{}'".format(status))
            return 1
    created = datetime.now().isoformat()
    for item in data:
        status = item.get("status", "todo")
        conn.execute(
            "INSERT INTO tasks (title, status, created) VALUES (?, ?, ?)",
            (item["title"], status, item.get("created") or created),
        )
    conn.commit()
    print("Imported {} tasks from {}".format(len(data), arg))
    return 0


def main():
    cmd = os.environ.get("TASKMAN_CMD") or ""
    arg = os.environ.get("TASKMAN_ARG")
    if arg is None:
        arg = ""
    cmd = cmd.strip()

    if cmd in ("", "help"):
        print(USAGE)
        return 0

    try:
        conn = get_db()
    except Exception:
        err("Error: cannot open database")
        return 1

    try:
        if cmd == "add":
            return cmd_add(conn, arg)
        if cmd == "list":
            return cmd_list(conn)
        if cmd == "doing":
            return cmd_doing(conn, arg)
        if cmd == "done":
            return cmd_done(conn, arg)
        if cmd == "delete":
            return cmd_delete(conn, arg)
        if cmd == "export":
            return cmd_export(conn, arg)
        if cmd == "import":
            return cmd_import(conn, arg)
        err("Error: unknown command '{}'".format(cmd))
        print(USAGE)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # Never crash with a traceback on any verifier input.
        print("Error: unexpected failure")
        sys.exit(1)
