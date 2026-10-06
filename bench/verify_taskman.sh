#!/bin/bash
# verify_taskman.sh "<run-cmd>" <workdir>
# Language-agnostic acceptance test for the frozen taskman spec (bench/specs/taskman.md).
# Prints PASS/FAIL per check and a final "RESULT: PASS n/n" / "RESULT: FAIL n/m" line.
# Exit 0 iff every check passes.
#
# Example: bench/verify_taskman.sh "./taskman" /tmp/work
#          bench/verify_taskman.sh "python3 taskman.py" /tmp/work
set -u
RUN_CMD="${1:?usage: verify_taskman.sh \"<run-cmd>\" <workdir>}"
WORKDIR="${2:?usage: verify_taskman.sh \"<run-cmd>\" <workdir>}"
cd "$WORKDIR" || { echo "FATAL: cannot cd $WORKDIR"; echo "RESULT: FAIL 0/0"; exit 1; }

PASS=0; TOTAL=0
TMPD="$(mktemp -d)"
export TASKMAN_DB="$TMPD/tasks.db"

# run_case <name> <expected_exit> <expected_grep> [VAR=val ...]
run_case() {
  local name="$1" want_exit="$2" want_grep="$3"; shift 3
  TOTAL=$((TOTAL+1))
  local out rc
  out="$(env "$@" bash -c "$RUN_CMD" 2>&1)"; rc=$?
  if [ "$rc" -eq "$want_exit" ] && printf '%s' "$out" | grep -qF "$want_grep"; then
    PASS=$((PASS+1)); echo "PASS: $name"
  else
    echo "FAIL: $name (exit=$rc want=$want_exit, grep='$want_grep')"
    echo "--- output ---"; printf '%s\n' "$out" | head -8; echo "---"
  fi
}

run_case "help lists commands"        0 "export"            TASKMAN_CMD=help
run_case "empty cmd = help"           0 "import"            TASKMAN_CMD=
run_case "empty list"                 0 "No tasks."         TASKMAN_CMD=list
run_case "add #1"                     0 "Added #1: Buy milk" TASKMAN_CMD=add TASKMAN_ARG="Buy milk"
run_case "add #2"                     0 "Added #2: Write report" TASKMAN_CMD=add TASKMAN_ARG="Write report"
run_case "list shows both"            0 "#1 [todo] Buy milk" TASKMAN_CMD=list
run_case "list shows #2"              0 "#2 [todo] Write report" TASKMAN_CMD=list
run_case "doing #1"                   0 "Marked #1 as doing" TASKMAN_CMD=doing TASKMAN_ARG=1
run_case "done #2"                    0 "Marked #2 as done"  TASKMAN_CMD=done TASKMAN_ARG=2
run_case "list statuses"              0 "#1 [doing] Buy milk" TASKMAN_CMD=list
run_case "list done status"           0 "#2 [done] Write report" TASKMAN_CMD=list
run_case "done missing id"            1 "Error: no task #99" TASKMAN_CMD=done TASKMAN_ARG=99
run_case "done bad id"                1 "Error: bad id"     TASKMAN_CMD=done TASKMAN_ARG=abc
run_case "add empty title"            1 "Error: title required" TASKMAN_CMD=add TASKMAN_ARG=
run_case "export json"                0 "Exported 2 tasks"  TASKMAN_CMD=export TASKMAN_ARG="$TMPD/tasks.json"
run_case "export file exists"         0 "tasks.json"        TASKMAN_CMD=export TASKMAN_ARG="$TMPD/tasks.json"

# Structural validation of the exported JSON (python3 is the test harness, not the subject).
TOTAL=$((TOTAL+1))
if python3 - "$TMPD/tasks.json" <<'EOF' 2>/dev/null; then
import json,sys
tasks = json.load(open(sys.argv[1]))
assert isinstance(tasks, list) and len(tasks) == 2, tasks
t1, t2 = tasks
assert t1["id"] == 1 and t1["title"] == "Buy milk" and t1["status"] == "doing" and t1["created"], t1
assert t2["id"] == 2 and t2["title"] == "Write report" and t2["status"] == "done" and t2["created"], t2
EOF
  PASS=$((PASS+1)); echo "PASS: exported JSON structure"
else
  echo "FAIL: exported JSON structure"; head -c 400 "$TMPD/tasks.json" 2>/dev/null; echo
fi

run_case "delete #1"                  0 "Deleted #1"        TASKMAN_CMD=delete TASKMAN_ARG=1
run_case "list after delete"          0 "#2 [done] Write report" TASKMAN_CMD=list
run_case "delete missing id"          1 "Error: no task #99" TASKMAN_CMD=delete TASKMAN_ARG=99

# Fresh DB: import round-trip.
export TASKMAN_DB="$TMPD/tasks2.db"
run_case "import json"                0 "Imported 2 tasks"  TASKMAN_CMD=import TASKMAN_ARG="$TMPD/tasks.json"
run_case "list after import"          0 "Buy milk"          TASKMAN_CMD=list
run_case "import keeps status"        0 "[doing] Buy milk"  TASKMAN_CMD=list
run_case "import missing file"        1 "Error: cannot read" TASKMAN_CMD=import TASKMAN_ARG="$TMPD/nope.json"
echo "not json at all" > "$TMPD/bad.json"
run_case "import bad json"            1 "Error: bad JSON"   TASKMAN_CMD=import TASKMAN_ARG="$TMPD/bad.json"
run_case "unknown command"            1 "Error: unknown command" TASKMAN_CMD=frobnicate
run_case "export no file"             1 "Error: file required" TASKMAN_CMD=export TASKMAN_ARG=

rm -rf "$TMPD"
if [ "$PASS" -eq "$TOTAL" ]; then
  echo "RESULT: PASS $PASS/$TOTAL"; exit 0
else
  echo "RESULT: FAIL $PASS/$TOTAL"; exit 1
fi
