#!/usr/bin/env python3
"""External cold trial for Misa.

An external LLM with zero knowledge of Misa (no model on earth has seen it)
writes a Misa program from the spec alone. This harness runs the loop:

  spec + task -> LLM -> program.misa -> misa check --json -> errors -> LLM ...

No human in the loop. Convergence = all acceptance checks pass.

Usage: cold_trial.py T04            (single task)
       cold_trial.py --all          (all 12 tasks, 4 parallel workers)

Results: /tmp/misa-trials-ext/<TID>/result.json
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

HOME = os.path.expanduser("~")
SPEC_DIR = os.path.join(HOME, "workspace/misa/spec")
TASK_DIR = os.path.join(HOME, "workspace/misa/agent-trials/tasks")
MISA = os.path.join(HOME, "workspace/misa/impl/bin/misa")
OUT = "/tmp/misa-trials-ext"

BASE = "https://api.hcnsec.cn/v1"
CRED = "custom.hcnsec"
ENTRY = "access_token"
HOSTS = ["api.hcnsec.cn"]
MODEL = os.environ.get("MISA_TRIAL_MODEL", "DeepSeek-V4.1-Flash")
MAX_CHECK_ITERS = 15
MAX_ACCEPT_ITERS = 2
COST_PER_MTOK = 0.15  # USD, approx blended

SYSTEM = (
    "You are an expert programmer in a language learnability experiment. "
    "You are given the complete specification of Misa, a new programming "
    "language you have never seen. Read the spec carefully and follow it "
    "exactly; do not invent syntax. Output ONLY the program source inside "
    "a single ```misa code fence, with no explanations or other text. "
    "When you receive checker errors, fix ALL of them and output the "
    "complete corrected program again in a ```misa fence."
)


def api_chat(messages, max_tokens=2500, temperature=0.2, retries=3):
    payload = {"model": MODEL, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    data = json.dumps(payload).encode()
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(BASE + "/chat/completions", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        add_surrogate_to_request(req, CRED, entry_name=ENTRY, allowed_hosts=HOSTS)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = read_json_response(resp)
            choice = (body.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            usage = body.get("usage") or {}
            return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:500]}")
        except Exception as e:  # timeout / transient: retry with backoff
            last_err = e
            import time
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"API failed after {retries} attempts: {last_err}")


def extract_code(text):
    m = re.search(r"```misa\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


def run_cli(args, cwd, env_extra=None):
    env = dict(os.environ)
    env["MOCK_KEY"] = "dummy"
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([MISA] + args, cwd=cwd, capture_output=True,
                       text=True, timeout=120, env=env)
    return p.returncode, p.stdout, p.stderr


def check_json(prog_path, workdir):
    rc, out, err = run_cli(["check", "--json", prog_path], workdir)
    try:
        data = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        data = {"raw": out, "stderr": err}
    if isinstance(data, list):
        errors = data  # `misa check --json` emits a bare array
    elif isinstance(data, dict):
        errors = data.get("errors", [])
    else:
        errors = []
    return errors, data


# ---------------------------------------------------------------- acceptance
def _lines(out):
    return [ln for ln in out.splitlines()]


def accept(tid, workdir, prog, mock):
    """Returns (ok, description_of_failure)."""
    args = ["run", prog]
    if mock:
        args += ["--mock", mock]
    rc, out, err = run_cli(args, workdir)
    if rc != 0:
        return False, f"run exited {rc}\nstdout:\n{out}\nstderr:\n{err[-1500:]}"

    def has(s):
        return s in out

    if tid == "T01":
        exp = []
        for i in range(1, 31):
            s = ""
            if i % 3 == 0:
                s += "Fizz"
            if i % 5 == 0:
                s += "Buzz"
            exp.append(s or str(i))
        if _lines(out) != exp:
            return False, f"output mismatch:\n{out[:800]}"
        return True, ""
    if tid == "T02":
        return (has("total: 19.0") and has("items: 3"), f"output:\n{out[:500]}")
    if tid == "T03":
        return (has("longest: banana") and has("vowel-start: 1"),
                f"output:\n{out[:500]}")
    if tid == "T04":
        rc2, out2, _ = run_cli(["test", prog, "--mock", mock], workdir)
        if "1/1" not in out2:
            return False, f"eval failed:\n{out2[:800]}"
        return has("positive"), f"output:\n{out[:500]}"
    if tid == "T05":
        return has("Bonjour"), f"output:\n{out[:500]}"
    if tid == "T06":
        return has("positive: upbeat words"), f"output:\n{out[:500]}"
    if tid == "T07":
        return has("budget ok"), f"output:\n{out[:500]}"
    if tid == "T08":
        rc2, out2, _ = run_cli(["test", prog, "--mock", mock], workdir)
        if "1/1" not in out2:
            return False, f"eval failed:\n{out2[:800]}"
        return (has("2+2?") and _lines(out)[-1].strip() == "4",
                f"output:\n{out[:500]}")
    if tid == "T09":
        ok = (out.count("--- screen Main ---") == 2
              and out.count("Banner") == 1
              and out.count("--- screen Detail ---") == 0)
        return ok, f"output:\n{out[:800]}"
    if tid == "T10":
        ls = _lines(out)
        return ("A: A" in ls and "B: B" in ls and "C: F" in ls,
                f"output:\n{out[:500]}")
    if tid == "T11":
        return has("hp: 2, gold: 75"), f"output:\n{out[:500]}"
    if tid == "T12":
        return (has("Ada from Lagos") and has("nick: nil"),
                f"output:\n{out[:500]}")
    return False, "unknown task"


MOCKS = {
    "T04": {"cls": {"label": "positive", "confidence": 0.9}},
    "T05": {"tr": {"text": "Bonjour"}},
    "T06": {"a": {"label": "positive"}, "b": {"reason": "upbeat words"}},
    "T07": {"m": {"ok": True}},
    "T08": {"q": {"question": "2+2?",
                   "options": ["3", "4", "5"], "answer": 1}},
}

EXPECT_HINT = {
    "T01": "30 lines: FizzBuzz 1..30 (line 3 'Fizz', line 5 'Buzz', line 15 'FizzBuzz')",
    "T02": "prints 'total: 19.0' and 'items: 3'",
    "T03": "prints 'longest: banana' and 'vowel-start: 1'",
    "T04": "misa test: 1/1 evals passed; misa run prints 'positive'",
    "T05": "prints 'Bonjour'",
    "T06": "prints 'positive: upbeat words'",
    "T07": "prints 'budget ok' with no E500",
    "T08": "misa test: 1/1 evals passed; misa run prints '2+2?' then '4'",
    "T09": "'--- screen Main ---' twice, 'Banner' once, no Detail screen",
    "T10": "prints 'A: A', 'B: B', 'C: F'",
    "T11": "prints 'hp: 2, gold: 75'",
    "T12": "prints 'Ada from Lagos' and 'nick: nil'",
}


def trial(tid):
    workdir = os.path.join(OUT, tid)
    os.makedirs(workdir, exist_ok=True)
    prog = os.path.join(workdir, "program.misa")
    mock = None
    if tid in MOCKS:
        mock = os.path.join(workdir, "mocks.json")
        with open(mock, "w") as f:
            json.dump(MOCKS[tid], f)

    with open(os.path.join(TASK_DIR, [f for f in os.listdir(TASK_DIR)
                                      if f.startswith(tid)][0])) as f:
        task_text = f.read()
    spec = ""
    for name in ("SYNTAX.md", "SEMANTICS.md", "STDLIB.md"):
        with open(os.path.join(SPEC_DIR, name)) as f:
            spec += f"\n\n===== {name} =====\n" + f.read()

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
         f"Here is the complete Misa language specification:\n{spec}\n\n"
         f"Your task:\n{task_text}\n\nWrite the Misa program."},
    ]
    ptok = ctok = 0
    codes = []
    iters = 0
    converged = False
    notes = ""

    try:
        content, p, c = api_chat(messages)
        ptok += p
        ctok += c
        messages.append({"role": "assistant", "content": content})
        with open(prog, "w") as f:
            f.write(extract_code(content) + "\n")

        for i in range(1, MAX_CHECK_ITERS + 1):
            iters = i
            errors, _ = check_json(prog, workdir)
            if not errors:
                break
            for e in errors:
                if isinstance(e, dict) and e.get("code") not in codes:
                    codes.append(e["code"])
            content, p, c = api_chat(messages + [{
                "role": "user",
                "content": "The Misa checker's structured diagnostics:\n"
                           + json.dumps(errors, indent=2) +
                           "\nFix EVERY error. Output the complete corrected "
                           "program in a single ```misa fence, nothing else."}])
            ptok += p
            ctok += c
            messages.append({"role": "user", "content": "[checker errors fed back]"})
            messages.append({"role": "assistant", "content": content})
            with open(prog, "w") as f:
                f.write(extract_code(content) + "\n")
        else:
            notes = "check-iteration cap reached"

        if not notes:
            errors, _ = check_json(prog, workdir)
            if errors:
                notes = "check-iteration cap reached"
            else:
                for _ in range(MAX_ACCEPT_ITERS + 1):
                    ok, fail = accept(tid, workdir, prog, mock)
                    if ok:
                        converged = True
                        break
                    content, p, c = api_chat(messages + [{
                        "role": "user",
                        "content": "The program checks clean, but acceptance "
                                   f"failed.\nExpected: {EXPECT_HINT[tid]}\n"
                                   f"Actual result:\n{fail[:2000]}\n"
                                   "Fix the program logic. Output the "
                                   "complete corrected program in a single "
                                   "```misa fence, nothing else."}])
                    ptok += p
                    ctok += c
                    messages.append({"role": "user",
                                     "content": "[acceptance failure fed back]"})
                    messages.append({"role": "assistant", "content": content})
                    with open(prog, "w") as f:
                        f.write(extract_code(content) + "\n")
                    errors, _ = check_json(prog, workdir)
                    if errors:
                        notes = "regression: check errors after accept fix"
                        break
                else:
                    notes = "acceptance-iteration cap reached"
                if not converged and not notes:
                    notes = "acceptance failed"
    except Exception as e:  # noqa: BLE001
        notes = f"harness error: {type(e).__name__}: {e}"[:300]

    result = {"task": tid, "model": MODEL, "converged": converged,
              "check_iterations": iters, "error_codes_seen": codes,
              "prompt_tokens": ptok, "completion_tokens": ctok,
              "est_cost_usd": round((ptok + ctok) / 1e6 * COST_PER_MTOK, 4),
              "notes": notes}
    with open(os.path.join(workdir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    print(f"{tid}: converged={converged} iters={iters} "
          f"tok={ptok + ctok} notes={notes}", flush=True)
    return result


def main():
    os.makedirs(OUT, exist_ok=True)
    if "--all" in sys.argv:
        tids = [f"T{i:02d}" for i in range(1, 13)]
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(trial, tids))
    elif len(sys.argv) == 2:
        results = [trial(sys.argv[1])]
    else:
        print(__doc__)
        sys.exit(2)
    conv = sum(1 for r in results if r["converged"])
    cost = sum(r["est_cost_usd"] for r in results)
    print(f"\n{conv}/{len(results)} converged, est cost ${cost:.4f}")


if __name__ == "__main__":
    main()
