#!/usr/bin/env python3
"""Misa agent-velocity benchmark harness.

Spawning is done by the orchestrating agent (scripts cannot spawn subagents).
This script has two modes:

  python3 bench/run.py gen-prompts [--trials N] [--langs misa,python]
      Writes bench/work/prompts/trial-<lang>-<n>.md — one self-contained
      prompt per trial. The orchestrator spawns one subagent per prompt,
      records spawn time in bench/work/trial-<lang>-<n>/meta.json.

  python3 bench/run.py collect
      Scans bench/work/trial-*/report.json + meta.json, re-runs the verifier
      and measures startup latency for each completed trial, and writes
      bench/results/<utc-timestamp>.json plus a printed summary table.

Methodology: spec/BENCHMARK.md (read it before citing any number).
"""
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/workspace/misa")
BENCH = os.path.join(ROOT, "bench")
WORK = os.path.join(BENCH, "work")
PROMPTS = os.path.join(WORK, "prompts")
RESULTS = os.path.join(BENCH, "results")
MISA = os.path.join(ROOT, "impl/bin/misa")
SPEC = os.path.join(BENCH, "specs/taskman.md")
VERIFY = os.path.join(BENCH, "verify_taskman.sh")

MISA_DOCS = [
    "spec/SYNTAX.md", "spec/SEMANTICS.md", "spec/STDLIB.md",
    "spec/ERRORS.md", "spec/PHASE5-DB.md", "spec/PHASE5-TYPES.md",
]

PROMPT_TEMPLATE = """You are an autonomous coding agent in a benchmark trial (trial id: {trial_id}).
Build the application described in the frozen spec. There is NO human help — do not ask
questions; work with what is given and report honestly at the end.

## The spec
Read: {spec}
This spec is frozen. The verifier below is the sole judge of completion.

## The verifier
Run it yourself as often as you like:
  bash {verify} "{run_cmd}" {workdir}
It prints PASS/FAIL per check and a final line `RESULT: PASS 27/27` or `RESULT: FAIL n/m`.
Completion = `RESULT: PASS 27/27`.

## Your language and resources
{lang_section}

## Rules
- Work ONLY inside {workdir}. Create it if needed.
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
effort will not converge. Then write {workdir}/report.json with EXACTLY this shape:
{{
  "trial": "{trial_id}",
  "language": "{lang}",
  "completed": true,
  "correction_loops": <int: your honest count of failed attempts>,
  "failure_log": ["<one line per failed attempt>"],
  "notes": "<anything relevant: what was hard, what helped>"
}}
Set "completed": false if the verifier does not fully pass. Be honest — a failed
trial is data, not disgrace. Then stop; your final message should just say done.
"""

MISA_LANG = """Language: Misa.
- Toolchain: `python3 {misa} check <file>` and `python3 {misa} build <file> -o <out>`.
  (Run the CLI via python3; it is a Python program.)
- Docs you may read (and ONLY these): {docs}.
- Deliverable: an AOT-compiled native binary at {workdir}/taskman
  (build your `taskman.misa` with `python3 {misa} build taskman.misa -o taskman`).
- The verifier will invoke it as:  cd {workdir} && ./taskman
  (Make sure `./taskman` runs from your workdir.)"""

PYTHON_LANG = """Language: Python 3.12.
- Standard library ONLY: sqlite3, json, os, sys, datetime. No pip installs, no network.
- Deliverable: {workdir}/taskman.py (a single file).
- The verifier will invoke it as:  cd {workdir} && python3 taskman.py
  (Make sure `python3 taskman.py` runs from your workdir.)"""


def gen_prompts(ntrials=3, langs=("misa", "python")):
    os.makedirs(PROMPTS, exist_ok=True)
    for lang in langs:
        for n in range(1, ntrials + 1):
            trial_id = f"trial-{lang}-{n}"
            workdir = os.path.join(WORK, trial_id)
            os.makedirs(workdir, exist_ok=True)
            if lang == "misa":
                lang_section = MISA_LANG.format(
                    misa=MISA, workdir=workdir,
                    docs=", ".join(os.path.join(ROOT, d) for d in MISA_DOCS))
                run_cmd = "./taskman"
            else:
                lang_section = PYTHON_LANG.format(workdir=workdir)
                run_cmd = "python3 taskman.py"
            prompt = PROMPT_TEMPLATE.format(
                trial_id=trial_id, spec=SPEC, verify=VERIFY, run_cmd=run_cmd,
                workdir=workdir, lang_section=lang_section, lang=lang)
            with open(os.path.join(PROMPTS, trial_id + ".md"), "w") as f:
                f.write(prompt)
            print("wrote", os.path.join(PROMPTS, trial_id + ".md"))


def measure_startup(workdir, run_cmd, timeout=30):
    """Min of 15 runs of `list` against an empty DB. Returns ms or None."""
    env = dict(os.environ, TASKMAN_CMD="list",
               TASKMAN_DB=os.path.join(workdir, ".startup-bench.db"))
    try:
        os.remove(env["TASKMAN_DB"])
    except OSError:
        pass
    samples = []
    for _ in range(15):
        t0 = time.perf_counter()
        try:
            subprocess.run(run_cmd, shell=True, cwd=workdir, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        except Exception:
            return None
        samples.append((time.perf_counter() - t0) * 1000.0)
    try:
        os.remove(env["TASKMAN_DB"])
    except OSError:
        pass
    return min(samples)


def verify_trial(workdir, run_cmd, timeout=120):
    try:
        p = subprocess.run(["bash", VERIFY, run_cmd, workdir],
                           capture_output=True, text=True, timeout=timeout)
        tail = [l for l in p.stdout.splitlines() if l.startswith("RESULT:")]
        return (p.returncode == 0 and tail and "PASS 27/27" in tail[-1],
                tail[-1] if tail else "no RESULT line")
    except Exception as e:
        return False, f"verifier error: {e}"


def collect():
    os.makedirs(RESULTS, exist_ok=True)
    trials = []
    for name in sorted(os.listdir(WORK)):
        wdir = os.path.join(WORK, name)
        rep = os.path.join(wdir, "report.json")
        meta = os.path.join(wdir, "meta.json")
        if not (os.path.isdir(wdir) and name.startswith("trial-")
                and os.path.exists(rep)):
            continue
        with open(rep) as f:
            r = json.load(f)
        m = {}
        if os.path.exists(meta):
            with open(meta) as f:
                m = json.load(f)
        lang = r.get("language", name.split("-")[1])
        run_cmd = "./taskman" if lang == "misa" else "python3 taskman.py"
        # Independent re-verification (do not trust self-report alone).
        ok, result_line = verify_trial(wdir, run_cmd)
        src = os.path.join(wdir, "taskman" if lang == "misa" else "taskman.py")
        artifact = os.path.join(wdir, "taskman" if lang == "misa" else "taskman.py")
        loc = None
        src_file = os.path.join(wdir, "taskman.misa" if lang == "misa" else "taskman.py")
        if os.path.exists(src_file):
            with open(src_file, errors="replace") as f:
                loc = sum(1 for _ in f)
        startup = measure_startup(wdir, run_cmd) if ok else None
        wall = None
        if m.get("spawned_at") and m.get("completed_at"):
            a = datetime.fromisoformat(m["spawned_at"])
            b = datetime.fromisoformat(m["completed_at"])
            wall = (b - a).total_seconds()
        trials.append({
            "trial": name, "language": lang,
            "completed_reported": r.get("completed"),
            "completed_verified": ok, "verifier_line": result_line,
            "wall_seconds": wall,
            "correction_loops": r.get("correction_loops"),
            "failure_log": r.get("failure_log", []),
            "loc": loc,
            "artifact_bytes": os.path.getsize(artifact) if os.path.exists(artifact) else None,
            "startup_ms": startup,
            "notes": r.get("notes", ""),
        })
        print(f"{name}: verified={ok} loops={r.get('correction_loops')} "
              f"wall={wall and round(wall,1)}s loc={loc} "
              f"startup={startup and round(startup,1)}ms")

    def agg(lang, key):
        vs = [t[key] for t in trials
              if t["language"] == lang and t[key] is not None]
        return {"n": len(vs), "mean": statistics.mean(vs) if vs else None,
                "min": min(vs) if vs else None, "max": max(vs) if vs else None,
                "values": vs}

    summary = {}
    for lang in ("misa", "python"):
        sub = [t for t in trials if t["language"] == lang]
        summary[lang] = {
            "trials": len(sub),
            "completion_rate": (sum(1 for t in sub if t["completed_verified"])
                                / len(sub) if sub else None),
            "wall_seconds": agg(lang, "wall_seconds"),
            "correction_loops": agg(lang, "correction_loops"),
            "loc": agg(lang, "loc"),
            "artifact_bytes": agg(lang, "artifact_bytes"),
            "startup_ms": agg(lang, "startup_ms"),
        }
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": "spec/BENCHMARK.md (read limitations L1-L7 before citing)",
        "trials": trials, "summary": summary,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS, f"benchmark-{ts}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print("\nwrote", path)
    print("\n=== SUMMARY ===")
    for lang in ("misa", "python"):
        s = summary[lang]
        if not s["trials"]:
            print(f"{lang}: no trials"); continue
        print(f"{lang}: n={s['trials']} "
              f"completion={s['completion_rate']:.0%} "
              f"wall_mean={s['wall_seconds']['mean'] and round(s['wall_seconds']['mean'],1)}s "
              f"loops_mean={s['correction_loops']['mean'] and round(s['correction_loops']['mean'],1)} "
              f"loc_mean={s['loc']['mean'] and round(s['loc']['mean'])} "
              f"bytes_mean={s['artifact_bytes']['mean'] and round(s['artifact_bytes']['mean'])} "
              f"startup_min={s['startup_ms']['min'] and round(s['startup_ms']['min'],1)}ms")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("gen-prompts", "collect"):
        sys.exit("usage: run.py gen-prompts [--trials N] [--langs misa,python] | collect")
    if sys.argv[1] == "gen-prompts":
        n = 3
        langs = ("misa", "python")
        for a in sys.argv[2:]:
            if a.startswith("--trials"):
                n = int(a.split("=")[1])
            if a.startswith("--langs"):
                langs = tuple(a.split("=")[1].split(","))
        gen_prompts(n, langs)
    else:
        collect()
