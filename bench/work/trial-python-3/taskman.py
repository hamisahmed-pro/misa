#!/usr/bin/env python3
"""taskman - a small CLI task manager driven entirely by environment variables.

TASKMAN_CMD: add | list | doing | done | delete | export | import | help
             (unset or empty -> help)
TASKMAN_ARG: title for add; id for doing/done/delete; file path for export/import
TASKMAN_DB : SQLite database path (default ./tasks.db)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  created TEXT NOT NULL
);
"""

USAGE = """Usage: taskman (driven by environment variables)
  TASKMAN_CMD=add, TASKMAN_ARG=<title>    Add a task
  TASKMAN_CMD=list                        List tasks
  TASKMAN_CMD=doing, TASKMAN_ARG=<id>     Mark a task as doing
  TASKMAN_CMD=done, TASKMAN_ARG=<id>      Mark a task as done
  TASKMAN_CMD=delete, TASKMAN_ARG=<id>    Delete a task
  TASKMAN_CMD=export, TASKMAN_ARG=<file>  Export tasks to a JSON file
  TASKMAN_CMD=import, TASKMAN_ARG=<file>  Import tasks from a JSON file
  TASKMAN_CMD=help                        Show this help
  TASKMAN_DB: SQLite database path (default ./tasks.db)"""

STATUSES = ("todo", "doing", "done")


def err(msg):
    print(msg)


def main():
    cmd = os.environ.get("TASKMAN_CMD")
    arg = os.environ.get("TASKMAN_ARG")
    db_path = os.environ.get("TASKMAN_DB") or "./tasks.db"

    if cmd is None or cmd == "":
        print(USAGE)
        return 0

    try:
        con = sqlite3.connect(db_path)
    except Exception:
        err("Error: cannot open database")
        return 1
    try:
        con.execute(SCHEMA)
        con.commit()

        if cmd == "add":
            title = arg or ""
            if title == "":
                err("Error: title required")
                return 1
            created = datetime.now(timezone.utc).isoformat()
            cur = con.execute(
                "INSERT INTO tasks (title, status, created) VALUES (?, 'todo', ?)",
                (title, created),
            )
            con.commit()
            print(f"Added #{cur.lastrowid}: {title}")
            return 0

        if cmd == "list":
            rows = con.execute(
                "SELECT id, title, status FROM tasks ORDER BY id ASC"
            ).fetchall()
            if not rows:
                print("No tasks.")
            else:
                for tid, title, status in rows:
                    print(f"#{tid} [{status}] {title}")
            return 0

        if cmd in ("doing", "done", "delete"):
            raw = arg or ""
            tid = None
            if raw.isdigit():
                tid = int(raw)
            if tid is None or tid < 1:
                err(f"Error: bad id '{raw}'")
                return 1
            row = con.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None:
                err(f"Error: no task #{tid}")
                return 1
            if cmd == "delete":
                con.execute("DELETE FROM tasks WHERE id = ?", (tid,))
                con.commit()
                print(f"Deleted #{tid}")
            else:
                con.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (cmd, tid)
                )
                con.commit()
                print(f"Marked #{tid} as {cmd}")
            return 0

        if cmd == "export":
            path = arg or ""
            if path == "":
                err("Error: file required")
                return 1
            rows = con.execute(
                "SELECT id, title, status, created FROM tasks ORDER BY id ASC"
            ).fetchall()
            tasks = [
                {"id": tid, "title": title, "status": status, "created": created}
                for tid, title, status, created in rows
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False)
            print(f"Exported {len(tasks)} tasks to {path}")
            return 0

        if cmd == "import":
            path = arg or ""
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                err(f"Error: cannot read {path}")
                return 1
            except (json.JSONDecodeError, ValueError):
                err(f"Error: bad JSON in {path}")
                return 1
            if not isinstance(data, list):
                err(f"Error: bad JSON in {path}")
                return 1
            for item in data:
                if not isinstance(item, dict):
                    err(f"Error: bad JSON in {path}")
                    return 1
                title = item.get("title")
                if not isinstance(title, str):
                    err(f"Error: bad title in {path}")
                    return 1
                status = item.get("status", "todo")
                if status not in STATUSES:
                    err(f"Error: bad status '{status}'")
                    return 1
            now = datetime.now(timezone.utc).isoformat()
            for item in data:
                created = item.get("created")
                if not isinstance(created, str) or created == "":
                    created = now
                con.execute(
                    "INSERT INTO tasks (title, status, created) VALUES (?, ?, ?)",
                    (item["title"], item.get("status", "todo"), created),
                )
            con.commit()
            print(f"Imported {len(data)} tasks from {path}")
            return 0

        if cmd == "help":
            print(USAGE)
            return 0

        err(f"Error: unknown command '{cmd}'")
        print(USAGE)
        return 1
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
