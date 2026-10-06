You are an autonomous coding agent in a benchmark trial (trial id: trial-misa-3).
Build the application described in the frozen spec. There is NO human help — do not ask
questions; work with what is given and report honestly at the end.

## The spec
Read: /home/hatch/workspace/misa/bench/specs/taskman.md
This spec is frozen. The verifier below is the sole judge of completion.

## The verifier
Run it yourself as often as you like:
  bash /home/hatch/workspace/misa/bench/verify_taskman.sh "./taskman" /home/hatch/workspace/misa/bench/work/trial-misa-3
It prints PASS/FAIL per check and a final line `RESULT: PASS 27/27` or `RESULT: FAIL n/m`.
Completion = `RESULT: PASS 27/27`.

## Your language and resources
Language: Misa.
- Toolchain: `python3 /home/hatch/workspace/misa/impl/bin/misa check <file>` and `python3 /home/hatch/workspace/misa/impl/bin/misa build <file> -o <out>`.
  (Run the CLI via python3; it is a Python program.)
- Docs you may read (and ONLY these): /home/hatch/workspace/misa/spec/SYNTAX.md, /home/hatch/workspace/misa/spec/SEMANTICS.md, /home/hatch/workspace/misa/spec/STDLIB.md, /home/hatch/workspace/misa/spec/ERRORS.md, /home/hatch/workspace/misa/spec/PHASE5-DB.md, /home/hatch/workspace/misa/spec/PHASE5-TYPES.md.
- Deliverable: an AOT-compiled native binary at /home/hatch/workspace/misa/bench/work/trial-misa-3/taskman
  (build your `taskman.misa` with `python3 /home/hatch/workspace/misa/impl/bin/misa build taskman.misa -o taskman`).
- The verifier will invoke it as:  cd /home/hatch/workspace/misa/bench/work/trial-misa-3 && ./taskman
  (Make sure `./taskman` runs from your workdir.)

## Rules
- Work ONLY inside /home/hatch/workspace/misa/bench/work/trial-misa-3. Create it if needed.
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
effort will not converge. Then write /home/hatch/workspace/misa/bench/work/trial-misa-3/report.json with EXACTLY this shape:
{
  "trial": "trial-misa-3",
  "language": "misa",
  "completed": true,
  "correction_loops": <int: your honest count of failed attempts>,
  "failure_log": ["<one line per failed attempt>"],
  "notes": "<anything relevant: what was hard, what helped>"
}
Set "completed": false if the verifier does not fully pass. Be honest — a failed
trial is data, not disgrace. Then stop; your final message should just say done.
