You are an autonomous coding agent in a benchmark trial (trial id: trial-python-2).
Build the application described in the frozen spec. There is NO human help — do not ask
questions; work with what is given and report honestly at the end.

## The spec
Read: /home/hatch/workspace/misa/bench/specs/taskman.md
This spec is frozen. The verifier below is the sole judge of completion.

## The verifier
Run it yourself as often as you like:
  bash /home/hatch/workspace/misa/bench/verify_taskman.sh "python3 taskman.py" /home/hatch/workspace/misa/bench/work/trial-python-2
It prints PASS/FAIL per check and a final line `RESULT: PASS 27/27` or `RESULT: FAIL n/m`.
Completion = `RESULT: PASS 27/27`.

## Your language and resources
Language: Python 3.12.
- Standard library ONLY: sqlite3, json, os, sys, datetime. No pip installs, no network.
- Deliverable: /home/hatch/workspace/misa/bench/work/trial-python-2/taskman.py (a single file).
- The verifier will invoke it as:  cd /home/hatch/workspace/misa/bench/work/trial-python-2 && python3 taskman.py
  (Make sure `python3 taskman.py` runs from your workdir.)

## Rules
- Work ONLY inside /home/hatch/workspace/misa/bench/work/trial-python-2. Create it if needed.
- Do NOT read any files outside: your workdir, the spec, the verifier, and (for Misa)
  the listed docs. In particular do NOT read ~/workspace/misa/examples/,
  ~/workspace/misa/agent-trials/, ~/workspace/misa/selfhost/, or ~/workspace/misa/impl/.
- For Python: standard library ONLY (sqlite3, json, os, sys, datetime). No pip, no network.
- Iterate freely: write code, check/build, run the verifier, fix, repeat.
- COUNT EVERY FAILED ATTEMPT: each `misa check` error, each build failure, each
  verifier FAIL line counts as one correction loop. Keep a running tally and a short
  log of what each failure was.

## When to stop
Stop when the verifier prints `RESULT: PASS 27/27`, or when you judge that further
effort will not converge. Then write /home/hatch/workspace/misa/bench/work/trial-python-2/report.json with EXACTLY this shape:
{
  "trial": "trial-python-2",
  "language": "python",
  "completed": true,
  "correction_loops": <int: your honest count of failed attempts>,
  "failure_log": ["<one line per failed attempt>"],
  "notes": "<anything relevant: what was hard, what helped>"
}
Set "completed": false if the verifier does not fully pass. Be honest — a failed
trial is data, not disgrace. Then stop; your final message should just say done.
