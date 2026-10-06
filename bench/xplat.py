#!/usr/bin/env python3
"""Cross-platform agent-velocity benchmark (benchmark 2).

Same protocol as bench/run.py (benchmark 1): the orchestrating agent spawns
one subagent per generated prompt, records spawn/complete times in
bench/work2/<trial>/meta.json; each trial writes report.json.

  python3 bench/xplat.py gen-prompts [--trials N] [--langs misa,python]
  python3 bench/xplat.py collect

Methodology: spec/BENCHMARK.md limitations L1-L7 apply, plus
bench/specs/crossplatform.md (frozen spec v1).
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/workspace/misa")
BENCH = os.path.join(ROOT, "bench")
WORK = os.path.join(BENCH, "work2")
PROMPTS = os.path.join(WORK, "prompts")
RESULTS = os.path.join(BENCH, "results")
MISA = os.path.join(ROOT, "impl/bin/misa")
SPEC = os.path.join(BENCH, "specs/crossplatform.md")
TASKMAN_SPEC = os.path.join(BENCH, "specs/taskman.md")
VERIFY_CLI = os.path.join(BENCH, "verify_taskman.sh")
VERIFY_APK = os.path.join(BENCH, "verify_apk.sh")

MISA_DOCS = [
    "spec/SYNTAX.md", "spec/SEMANTICS.md", "spec/STDLIB.md",
    "spec/ERRORS.md", "spec/PHASE5-DB.md", "spec/PHASE5-TYPES.md",
    "docs/targets.md",
]

PROMPT_TEMPLATE = """You are an autonomous coding agent in a benchmark trial (trial id: {trial_id}).
Build the application described in the frozen spec. There is NO human help — do not ask
questions; work with what is given and report honestly at the end.

## The spec
Read: {spec}
Also read the CLI detail spec: {taskman_spec}
These specs are frozen. The verifiers below are the sole judges of completion.

## The verifiers
CLI (run as often as you like):
  bash {verify_cli} "{cli_run_cmd}" {workdir}
  Completion = `RESULT: PASS 27/27`.
APK (structure only — this box has no emulator/device, so on-device runtime
is unverifiable; build + structure + signature is the bar):
  bash {verify_apk} {workdir}/taskman.apk {workdir} {lang}
  Completion = `APK-RESULT: PASS 6/6`.

## Your language and resources
{lang_section}

## Rules
- Work ONLY inside {workdir}. Create it if needed.
- Do NOT read any files outside: your workdir, the specs, the verifiers, and (for Misa)
  the listed docs. In particular do NOT read ~/workspace/misa/examples/,
  ~/workspace/misa/agent-trials/, ~/workspace/misa/selfhost/,
  ~/workspace/misa/impl/, or ~/workspace/misa/bench/work/.
- Misa: toolchain via `python3 {misa}` (it is a Python program).
- Python CLI: standard library ONLY (sqlite3, json, os, sys, datetime).
  Python APK leg: pip + network allowed.
- Iterate freely: write code, check/build, run the verifiers, fix, repeat.
- COUNT EVERY FAILED ATTEMPT, split by artifact: each `misa check` error,
  each build failure, each verifier FAIL line. Keep `cli_loops` and
  `apk_loops` tallies plus a one-line failure log.

## When to stop
Stop when both verifiers pass, or when you judge further effort will not
converge. Then write {workdir}/report.json with EXACTLY this shape:
{{
  "trial": "{trial_id}",
  "language": "{lang}",
  "cli_completed": true,
  "apk_completed": true,
  "cli_loops": <int>,
  "apk_loops": <int>,
  "failure_log": ["<one line per failed attempt>"],
  "apk_build_seconds": <int or null: wall time of your final successful APK build>,
  "apk_route": "<python only: e.g. buildozer+kivy>",
  "notes": "<anything relevant>"
}}
Set a completion flag false if its verifier does not fully pass. Be honest —
non-completion is data, not disgrace. Then stop; your final message just says done.
"""

MISA_LANG = """Language: Misa. ONE .misa file produces BOTH artifacts.
- Toolchain: `python3 {misa} check <file>`, `python3 {misa} build <file> -o <out>`.
- Docs you may read (and ONLY these): {docs}.
- CLI artifact: build `taskman.misa` with
  `python3 {misa} build taskman.misa -o taskman`; the verifier runs it as
  `cd {workdir} && ./taskman` with TASKMAN_* env vars set.
- APK artifact: `cd ~/workspace/misa && python3 scripts/build_apk.py --misa {workdir}/taskman.misa --out {workdir}/taskman.apk`
  (run from the repo root; SDK/NDK are preinstalled on this box; ~20s per build).

## Screens (for the APK leg)
Your file must ALSO declare an app block and screens. Minimal example:

app TaskMan {{
    title: "TaskMan",
    version: "1.0.0",
}}

screen Home {{
    Column {{
        Heading(text: "Tasks")
        List(items: ["sample"])
        TextInput(id: title, hint: "new task title")
        Button(label: "Add", onTap: || {{
            let db = db.open("tasks.db")
            db.exec(db, "INSERT INTO tasks (title, status) VALUES (?, 'todo')", [title.text])
            db.close(db)
            navigate(Home())
        }})
    }}
}}

Components: Column, Row, Heading(text:), Text(text:), Banner(text:, style:),
List(items: [Text]), TextInput(id:, hint:), Button(label:, onTap:), Spacer().
Read a field as <id>.text inside handlers. Handlers are || {{ ... }} lambdas.
Screens may take params: screen Detail(t: Text) {{ ... }} via navigate(Detail("x")).
Table declarations (`table tasks {{ ... }}`) give compile-time checked SQL.

## Dual-mode requirement (IMPORTANT)
Your single .misa file is built TWICE: as a host CLI binary (TASKMAN_CMD *will*
be set by the verifier) and into the APK (TASKMAN_CMD will be UNSET on Android
— navigate() to your Home screen in that case). Read the env var defensively:
  var cmd = ""
  try {{ cmd = env("TASKMAN_CMD") }} catch e {{ cmd = "" }}
Dispatch on cmd for the CLI; error paths must print "Error: ..." and `throw`
(never reach navigate(), so the process exits 1). After successful CLI output
you may fall through to navigate(Home()) — extra stdout is harmless to the
verifier, which greps. On Android the unset-var path must show your UI."""

PYTHON_LANG = """Language: Python 3.12.
- CLI artifact: {workdir}/taskman.py (single file preferred; a shared logic
  module imported by both the CLI and the app is encouraged). Stdlib ONLY:
  sqlite3, json, os, sys, datetime. The verifier runs it as
  `cd {workdir} && python3 taskman.py` with TASKMAN_* env vars set.
- APK artifact: {workdir}/taskman.apk — a working Android app from the SAME
  codebase (share the storage logic; do not reimplement it). Recommended
  route: buildozer + Kivy (`pip install buildozer`, then a buildozer.spec
  and `buildozer android debug`). Alternatives you may choose instead:
  BeeWare Briefcase, Chaquopy. Document your choice in "apk_route".
- pip and network are allowed FOR THE APK LEG ONLY.
- TIMEBOX: if Android toolchain setup (SDK downloads, first builds) exceeds
  ~45 minutes without producing an APK, STOP the APK leg and report honestly.
  Check ~/.buildozer (and ~/android-sdk) FIRST — a previous trial may have
  cached the toolchain; reuse it, do not re-download blindly.
- This box has 2 CPUs and limited RAM: do NOT run parallel heavy builds."""


def gen_prompts(ntrials=3, langs=("misa", "python")):
    os.makedirs(PROMPTS, exist_ok=True)
    for lang in langs:
        for n in range(1, ntrials + 1):
            trial_id = f"xplat-{lang}-{n}"
            workdir = os.path.join(WORK, trial_id)
            os.makedirs(workdir, exist_ok=True)
            if lang == "misa":
                lang_section = MISA_LANG.format(
                    misa=MISA, workdir=workdir,
                    docs=", ".join(os.path.join(ROOT, d) for d in MISA_DOCS))
                cli_run_cmd = "./taskman"
            else:
                lang_section = PYTHON_LANG.format(workdir=workdir)
                cli_run_cmd = "python3 taskman.py"
            prompt = PROMPT_TEMPLATE.format(
                trial_id=trial_id, spec=SPEC, taskman_spec=TASKMAN_SPEC,
                verify_cli=VERIFY_CLI, verify_apk=VERIFY_APK,
                cli_run_cmd=cli_run_cmd, workdir=workdir, misa=MISA,
                lang_section=lang_section, lang=lang)
            with open(os.path.join(PROMPTS, trial_id + ".md"), "w") as f:
                f.write(prompt)
            print("wrote", os.path.join(PROMPTS, trial_id + ".md"))


def _code_lines(path):
    """Non-blank, non-comment lines (Misa // and Python #)."""
    n = 0
    with open(path, errors="replace") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("//") and not s.startswith("#"):
                n += 1
    return n


def shared_code_pct(workdir, lang):
    """Heuristic, documented in FINDINGS.md:
    Misa: lines inside fn/table blocks (logic used by both artifacts) /
          all code lines in taskman.misa.
    Python: lines in shared modules (imported by both CLI and app) /
            all code lines in delivered .py files."""
    if lang == "misa":
        src = os.path.join(workdir, "taskman.misa")
        if not os.path.exists(src):
            return None
        text = open(src, errors="replace").read()
        # crude block scan: fn/table blocks = shared logic layer
        shared = 0
        depth = 0
        in_shared = False
        for line in text.splitlines():
            s = line.strip()
            if re.match(r"^(fn|table)\b", s):
                in_shared = True
            if in_shared:
                if s and not s.startswith("//"):
                    shared += 1
            depth += s.count("{") - s.count("}")
            if in_shared and depth <= 0 and s.endswith("}"):
                in_shared = False
                depth = 0
        total = _code_lines(src)
        return round(100.0 * shared / total, 1) if total else None
    # python
    pys = []
    for root, _, files in os.walk(workdir):
        if ".buildozer" in root or "__pycache__" in root:
            continue
        for fn in files:
            if fn.endswith(".py"):
                pys.append(os.path.join(root, fn))
    if not pys:
        return None
    total = sum(_code_lines(p) for p in pys)
    # shared = modules that are not the CLI entry and not the app entry
    shared = 0
    for p in pys:
        base = os.path.basename(p)
        if base in ("taskman.py", "main.py"):
            continue
        shared += _code_lines(p)
    return round(100.0 * shared / total, 1) if total else None


def measure_startup(workdir, run_cmd, timeout=30):
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


def verify_cli(workdir, run_cmd, timeout=180):
    try:
        p = subprocess.run(["bash", VERIFY_CLI, run_cmd, workdir],
                           capture_output=True, text=True, timeout=timeout)
        tail = [l for l in p.stdout.splitlines() if l.startswith("RESULT:")]
        return (p.returncode == 0 and tail and "PASS 27/27" in tail[-1],
                tail[-1] if tail else "no RESULT line")
    except Exception as e:
        return False, f"verifier error: {e}"


def verify_apk(workdir, lang, timeout=120):
    apk = os.path.join(workdir, "taskman.apk")
    try:
        p = subprocess.run(["bash", VERIFY_APK, apk, workdir, lang],
                           capture_output=True, text=True, timeout=timeout)
        tail = [l for l in p.stdout.splitlines()
                if l.startswith("APK-RESULT:")]
        ok = (p.returncode == 0 and tail and "PASS 6/6" in tail[-1])
        return ok, (tail[-1] if tail else "no APK-RESULT line"), \
            (os.path.getsize(apk) if os.path.exists(apk) else None)
    except Exception as e:
        return False, f"apk verifier error: {e}", None


def collect():
    os.makedirs(RESULTS, exist_ok=True)
    trials = []
    for name in sorted(os.listdir(WORK)):
        wdir = os.path.join(WORK, name)
        rep = os.path.join(wdir, "report.json")
        meta = os.path.join(wdir, "meta.json")
        if not (os.path.isdir(wdir) and name.startswith("xplat-")
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
        cli_ok, cli_line = verify_cli(wdir, run_cmd)
        apk_ok, apk_line, apk_bytes = verify_apk(wdir, lang)
        loc = None
        if lang == "misa":
            src = os.path.join(wdir, "taskman.misa")
            loc = _code_lines(src) if os.path.exists(src) else None
        else:
            loc = 0
            for root, _, files in os.walk(wdir):
                if ".buildozer" in root or "__pycache__" in root:
                    continue
                for fn in files:
                    if fn.endswith(".py"):
                        loc += _code_lines(os.path.join(root, fn))
        startup = measure_startup(wdir, run_cmd) if cli_ok else None
        wall = None
        if m.get("spawned_at") and m.get("completed_at"):
            a = datetime.fromisoformat(m["spawned_at"])
            b = datetime.fromisoformat(m["completed_at"])
            wall = (b - a).total_seconds()
        trials.append({
            "trial": name, "language": lang,
            "cli_completed_reported": r.get("cli_completed"),
            "cli_completed_verified": cli_ok, "cli_verifier_line": cli_line,
            "apk_completed_reported": r.get("apk_completed"),
            "apk_completed_verified": apk_ok, "apk_verifier_line": apk_line,
            "wall_seconds": wall,
            "cli_loops": r.get("cli_loops"), "apk_loops": r.get("apk_loops"),
            "failure_log": r.get("failure_log", []),
            "loc": loc, "shared_code_pct": shared_code_pct(wdir, lang),
            "host_artifact_bytes": os.path.getsize(os.path.join(wdir, "taskman"))
            if os.path.exists(os.path.join(wdir, "taskman")) else None,
            "startup_ms": startup,
            "apk_bytes": apk_bytes,
            "apk_build_seconds": r.get("apk_build_seconds"),
            "apk_route": r.get("apk_route"),
            "notes": r.get("notes", ""),
        })
        print(f"{name}: cli={cli_ok} apk={apk_ok} wall={wall and round(wall,1)}s "
              f"loops={r.get('cli_loops')}/{r.get('apk_loops')} loc={loc} "
              f"shared={shared_code_pct(wdir, lang)}% apk={apk_bytes and round(apk_bytes/1e6,2)}MB")

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
            "cli_completion": (sum(1 for t in sub if t["cli_completed_verified"])
                               / len(sub) if sub else None),
            "apk_completion": (sum(1 for t in sub if t["apk_completed_verified"])
                               / len(sub) if sub else None),
            "wall_seconds": agg(lang, "wall_seconds"),
            "cli_loops": agg(lang, "cli_loops"),
            "apk_loops": agg(lang, "apk_loops"),
            "loc": agg(lang, "loc"),
            "shared_code_pct": agg(lang, "shared_code_pct"),
            "host_artifact_bytes": agg(lang, "host_artifact_bytes"),
            "startup_ms": agg(lang, "startup_ms"),
            "apk_bytes": agg(lang, "apk_bytes"),
            "apk_build_seconds": agg(lang, "apk_build_seconds"),
        }
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": ("spec/BENCHMARK.md L1-L7 + bench/specs/crossplatform.md; "
                        "APK runtime UNVERIFIED (no emulator/device on this box)"),
        "trials": trials, "summary": summary,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS, f"xplat-{ts}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print("\nwrote", path)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("gen-prompts", "collect"):
        sys.exit("usage: xplat.py gen-prompts [--trials N] [--langs misa,python] | collect")
    if sys.argv[1] == "gen-prompts":
        n = 3
        langs = ("misa", "python")
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--trials" and i + 1 < len(args):
                n = int(args[i + 1]); i += 2; continue
            if a.startswith("--trials="):
                n = int(a.split("=", 1)[1]); i += 1; continue
            if a == "--langs" and i + 1 < len(args):
                langs = tuple(args[i + 1].split(",")); i += 2; continue
            if a.startswith("--langs="):
                langs = tuple(a.split("=", 1)[1].split(",")); i += 1; continue
            i += 1
        gen_prompts(n, langs)
    else:
        collect()
