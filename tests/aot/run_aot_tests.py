#!/usr/bin/env python3
"""AOT parity tests: build each prog/*.misa to a native binary and
compare against the interpreter (stdout, exit code, and error
code/message with the location prefix normalized away).

Usage: run_aot_tests.py [--keep]
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROG = os.path.join(HERE, "prog")
MISA = os.path.join(HERE, "..", "..", "impl", "bin", "misa")

# name -> {mock: mockfile} | {"build_fail": "E402"} | {"exit": 1}
SPECIAL = {
    "a08_model": {"mock": "a08_model.mock.json"},
    "a09_budget": {"mock": "a09_budget.mock.json", "exit": 1},
    "a11_import": {},
    "a12_stream_reject": {"build_fail": "E402"},
}


def norm_err(text):
    """Strip the location prefix so `<unknown>:` and `12:3:` compare."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"^(<unknown>|\d+:\d+): ", "", line)
        out.append(line)
    return "\n".join(out)


def run(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, capture_output=True, text=True, env=e,
                       timeout=120)
    return p.returncode, p.stdout, p.stderr


def _ensure_ffi_helpers():
    """Compile tests/ffi_helpers.c -> /tmp/misa_ffi_helpers.so (once)
    for the a27_ffi2 parity test."""
    so = "/tmp/misa_ffi_helpers.so"
    src = os.path.join(HERE, "..", "ffi_helpers.c")
    if os.path.exists(so) and os.path.getmtime(src) <= os.path.getmtime(so):
        return
    r = subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-o", so, src],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"WARNING: could not build ffi helpers: {r.stderr.strip()}")
        print("a27_ffi2 will fail; install a C compiler to run it.")


def main():
    keep = "--keep" in sys.argv
    # Overtake Wave 2 (FFI): a27_ffi2 needs the C test helpers as a
    # shared object at a fixed path (the .misa references it).
    _ensure_ffi_helpers()
    progs = sorted(f for f in os.listdir(PROG) if f.endswith(".misa")
                   and not f.startswith("a11_util"))
    passed, failed = 0, []
    work = tempfile.mkdtemp(prefix="aot-tests-")
    for prog in progs:
        name = prog[:-5]
        spec = SPECIAL.get(name, {})
        src = os.path.join(PROG, prog)
        mock = spec.get("mock")
        env = {}
        # `misa run` defaults to the AOT cache when the file is
        # compilable. Force the reference interpreter so cold-cache
        # build notices do not contaminate stderr parity comparisons.
        icmd = [MISA, "run", src, "--interp"]
        if mock:
            mp = os.path.join(PROG, mock)
            icmd += ["--mock", mp]
            env["MISA_MOCK_FILE"] = mp
        irc, iout, ierr = run(icmd, env if mock else None)

        binpath = os.path.join(work, name)
        brc, bout, berr = run([MISA, "build", src, "-o", binpath])
        if "build_fail" in spec:
            if brc != 0 and spec["build_fail"] in (bout + berr):
                print(f"PASS  {name}  (build rejected "
                      f"{spec['build_fail']})")
                passed += 1
            else:
                print(f"FAIL  {name}  (expected build failure "
                      f"{spec['build_fail']})")
                failed.append(name)
            continue
        if brc != 0:
            print(f"FAIL  {name}  (build failed)\n{bout}{berr}")
            failed.append(name)
            continue
        erc, eout, eerr = run([binpath], env if mock else None)

        want_exit = spec.get("exit", irc)
        ok = (eout == iout and erc == want_exit
              and norm_err(eerr) == norm_err(ierr))
        if ok:
            print(f"PASS  {name}")
            passed += 1
        else:
            print(f"FAIL  {name}")
            if eout != iout:
                print(f"  stdout diff:\n  interp={iout!r}\n  aot   ={eout!r}")
            if erc != want_exit:
                print(f"  exit: interp={irc} aot={erc}")
            if norm_err(eerr) != norm_err(ierr):
                print(f"  stderr:\n  interp={norm_err(ierr)!r}\n"
                      f"  aot   ={norm_err(eerr)!r}")
            failed.append(name)
    print(f"\n{passed}/{passed + len(failed)} AOT parity tests passed")
    if not keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
