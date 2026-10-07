#!/usr/bin/env python3
"""Deterministic runtime benchmarks: AOT binary size + startup latency.

No agent involved. Builds hello-world and the showcase apps with
`misa build`, measures artifact bytes and startup latency (min of 15 runs),
vs equivalent Python. Also measures interpreter startup.

Writes bench/results/runtime-<ts>.json and prints a markdown table.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/workspace/misa")
MISA = os.path.join(ROOT, "impl/bin/misa")
RESULTS = os.path.join(ROOT, "bench/results")
N = 15


def run(cmd, cwd=None, env=None, timeout=120):
    e = dict(os.environ)
    if env:
        e.update(env)
    t0 = time.perf_counter()
    p = subprocess.run(cmd, shell=True, cwd=cwd, env=e,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    return (time.perf_counter() - t0) * 1000.0, p.returncode


def startup_min(cmd, cwd=None, env=None):
    samples = []
    for _ in range(N):
        ms, rc = run(cmd, cwd, env)
        if rc != 0:
            return None, rc
        samples.append(ms)
    return min(samples), 0


def build_misa(src, out):
    p = subprocess.run([sys.executable, MISA, "build", src, "-o", out],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0 or not os.path.exists(out):
        return False, (p.stdout + p.stderr)[-500:]
    return True, ""


def main():
    tmp = tempfile.mkdtemp(prefix="bench-rt-")
    os.makedirs(RESULTS, exist_ok=True)
    hello_misa = os.path.join(tmp, "hello.misa")
    hello_py = os.path.join(tmp, "hello.py")
    open(hello_misa, "w").write('print("hello")\n')
    open(hello_py, "w").write('print("hello")\n')

    rows = []

    def bench(name, build_fn, run_cmd, run_env=None, lang="misa-aot",
              artifact="app"):
        workdir = tempfile.mkdtemp(prefix="bench-run-", dir=tmp)
        ok, err = build_fn(workdir)
        if not ok:
            rows.append({"name": name, "lang": lang, "build_ok": False,
                         "build_error": err})
            print(f"{name}: BUILD FAILED")
            return
        art = os.path.join(workdir, artifact)
        size = os.path.getsize(art)
        ms, rc = startup_min(run_cmd, cwd=workdir, env=run_env)
        rows.append({"name": name, "lang": lang, "build_ok": True,
                     "artifact_bytes": size,
                     "startup_ms_min15": round(ms, 2) if ms else None,
                     "run_rc": rc})
        print(f"{name}: {size} bytes, startup {ms and round(ms,2)}ms")

    # Misa AOT: hello, tasks, diary
    bench("hello", lambda d: build_misa(hello_misa, d + "/app"),
          "./app")
    bench("tasks.misa", lambda d: build_misa(
        os.path.join(ROOT, "examples/tasks.misa"), d + "/app"),
        "./app --db ./t.db list",
        run_env={"HOME": tempfile.mkdtemp(dir=tmp)})
    bench("diary.misa", lambda d: build_misa(
        os.path.join(ROOT, "examples/diary.misa"), d + "/app"),
        "./app", run_env={"HOME": tempfile.mkdtemp(dir=tmp)})

    # Python equivalents
    def copy_py(src, d):
        shutil.copy(src, d + "/app.py")
        return True, ""
    bench("hello.py", lambda d: copy_py(hello_py, d),
          "python3 app.py", lang="python", artifact="app.py")
    ms, rc = startup_min(
        f"{sys.executable} {MISA} run {hello_misa}", cwd=tmp)
    rows.append({"name": "hello.misa", "lang": "misa-interp",
                 "build_ok": True, "artifact_bytes": os.path.getsize(hello_misa),
                 "startup_ms_min15": round(ms, 2) if ms else None, "run_rc": rc})
    print(f"misa interp hello: startup {ms and round(ms,2)}ms")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS, f"runtime-{ts}.json")
    with open(path, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "n_runs": N, "metric": "startup=min of 15 runs, ms",
                   "rows": rows}, f, indent=2)
    print("\nwrote", path)
    print("\n| app | backend | bytes | startup (min of 15) |")
    print("|---|---|---|---|")
    for r in rows:
        print(f"| {r['name']} | {r['lang']} | "
              f"{r.get('artifact_bytes') or 'BUILD-FAIL'} | "
              f"{r.get('startup_ms_min15') or 'n/a'} ms |")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
