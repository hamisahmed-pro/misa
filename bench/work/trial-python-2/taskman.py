#!/usr/bin/env python3
"""taskman: a small CLI task manager driven entirely by environment variables."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_DEFAULT = "./tasks.db"
USAGE = (
    "Usage: set TASKMAN_CMD to one of:\n"
    "  add <title>     - add a task\n"
    "  list            - list tasks\n"
    "  doing <id>      - mark a task as doing\n"
    "  done <id>       - mark a task as done\n"
    "  delete <id>     - delete a task\n"
    "  export <file>   - export tasks to a JSON file\n"
    "  import <file>   - import tasks from a JSON file\n"
    "  help            - show this help\n"
)


def out(msg):
    sys.stdout.write(msg + "\n")


def err(msg):
    sys.stdout.write(msg + "\n")
    return 1


def get_conn():
    path = os.environ.get("TASKMAN_DB") or DB_DEFAULT
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id INTEGER PRIMARY KEY, "
        "title TEXT NOT NULL, "
        "status TEXT NOT NULL, "
        "created TEXT NOT NULL)"
    )
    return conn


def parse_id(value):
    try:
        n = int(str(value).strip())
    except (ValueError, TypeError):
        return None
    if n < 1:
        return None
    return n


def cmd_add(arg, conn):
    if not arg:
        return err("Error: title required")
    created = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO tasks (title, status, created) VALUES (?, 'todo', ?)",
        (arg, created),
    )
    conn.commit()
    out("Added #%d: %s" % (cur.lastrowid, arg))
    return 0


def cmd_list(arg, conn):
    rows = conn.execute("SELECT id, title, status FROM tasks ORDER BY id ASC").fetchall()
    if not rows:
        out("No tasks.")
        return 0
    for tid, title, status in rows:
        out("#%d [%s] %s" % (tid, status, title))
    return 0


def cmd_status(arg, conn, new_status):
    tid = parse_id(arg)
    if tid is None:
        return err("Error: bad id '%s'" % arg)
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        return err("Error: no task #%d" % tid)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, tid))
    conn.commit()
    out("Marked #%d as %s" % (tid, new_status))
    return 0


def cmd_delete(arg, conn):
    tid = parse_id(arg)
    if tid is None:
        return err("Error: bad id '%s'" % arg)
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        return err("Error: no task #%d" % tid)
    conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    conn.commit()
    out("Deleted #%d" % tid)
    return 0


def cmd_export(arg, conn):
    if not arg:
        return err("Error: file required")
    rows = conn.execute(
        "SELECT id, title, status, created FROM tasks ORDER BY id ASC"
    ).fetchall()
    tasks = [
        {"id": tid, "title": title, "status": status, "created": created}
        for tid, title, status, created in rows
    ]
    with open(arg, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)
        f.write("\n")
    out("Exported %d tasks to %s" % (len(tasks), arg))
    return 0


def cmd_import(arg, conn):
    if not arg:
        return err("Error: cannot read %s" % arg)
    try:
        with open(arg, "r", encoding="utf-8") as f:
            data = f.read()
    except (OSError, UnicodeDecodeError):
        return err("Error: cannot read %s" % arg)
    try:
        items = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return err("Error: bad JSON in %s" % arg)
    if not isinstance(items, list):
        return err("Error: bad JSON in %s" % arg)
    count = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            return err("Error: bad JSON in %s" % arg)
        status = item.get("status", "todo")
        if status not in ("todo", "doing", "done"):
            return err("Error: bad status '%s'" % status)
        created = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO tasks (title, status, created) VALUES (?, ?, ?)",
            (item["title"], status, created),
        )
        count += 1
    conn.commit()
    out("Imported %d tasks from %s" % (count, arg))
    return 0


def main():
    cmd = os.environ.get("TASKMAN_CMD") or ""
    arg = os.environ.get("TASKMAN_ARG") or ""
    if not cmd:
        cmd = "help"

    if cmd == "help":
        out(USAGE.rstrip("\n"))
        return 0

    conn = get_conn()
    try:
        if cmd == "add":
            return cmd_add(arg, conn, )
        if cmd == "list":
            return cmd_list(arg, conn)
        if cmd in ("doing", "done"):
            return cmd_status(arg, conn, cmd)
        if cmd == "delete":
            return cmd_delete(arg, conn)
        if cmd == "export":
            return cmd_export(arg, conn)
        if cmd == "import":
            return cmd_import(arg, conn)
        out("Error: unknown command '%s'" % cmd)
        out(USAGE.rstrip("\n"))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
