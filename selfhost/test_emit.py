#!/usr/bin/env python3
"""Differential test: Misa self-hosted AOT emitter vs Python emitter.

For each in-scope test program (a01..a07, a10):
1. Generate LLVM IR via the Misa emitter (selfhost/emit.misa).
2. Assemble the IR with llvmlite (stands in for clang, which is not
   installed here), link against the C runtime with gcc, run the binary.
3. Build the same program with the Python toolchain (`misa build`),
   run that binary.
4. Compare stdout + exit code EXACTLY.

Usage: python3 selfhost/test_emit.py (from repo root)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "impl"))

MISA = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
SELFHOST = os.path.join(ROOT, "selfhost")
PROG = os.path.join(ROOT, "tests", "aot", "prog")
RT_DIR = os.path.join(ROOT, "impl", "misa", "aot", "rt")
DEPS = os.path.expanduser("~/workspace/deps")

PROGS = ["a01_arith", "a02_strings", "a03_control", "a04_collections",
         "a05_errors", "a06_builtins", "a07_fs", "a10_screen"]

DRIVER = os.path.join(SELFHOST, "_emit_driver.misa")

DRIVER_SRC = '''import "lexer.misa"
import "parser.misa"
import "checker.misa"
import "emit.misa"

let path = "/tmp/_misa_emit_input.misa"
let src = fs.read(path)
let prog = parse(lex(src))
let diags = check(prog)
if len(diags) > 0 {
    print("@@CHECK-ERR@@")
    for d in diags {
        print(d.code + "|" + d.message)
    }
} else {
    try {
        let ir = emit_ir(prog)
        fs.write("/tmp/_misa_emit_out.ll", ir)
        print("@@EMIT-OK@@")
    } catch e {
        print("@@EMIT-ERR@@")
        print(e.code + "|" + e.message)
    }
}
'''

PASS = 0
FAIL = 0
FAILURES = []


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       **kw)
    return p.returncode, p.stdout, p.stderr


def emit_via_misa(src_path, work):
    """Run the Misa emitter; return (ok, ll_path_or_err)."""
    inp = "/tmp/_misa_emit_input.misa"
    with open(src_path) as f:
        data = f.read()
    with open(inp, "w") as f:
        f.write(data)
    with open(DRIVER, "w") as f:
        f.write(DRIVER_SRC)
    rc, out, err = run(MISA + ["run", DRIVER], cwd=SELFHOST)
    os.unlink(DRIVER)
    if "@@EMIT-OK@@" in out:
        return True, "/tmp/_misa_emit_out.ll"
    return False, (out + err).strip()[:1500]


def assemble_and_link(ll_path, work, name):
    """llvmlite-assemble + gcc-link the Misa IR; return binary path."""
    from llvmlite import binding
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    with open(ll_path) as f:
        mod = binding.parse_assembly(f.read())
    mod.verify()
    target = binding.Target.from_default_triple()
    tm = target.create_target_machine()
    obj_path = os.path.join(work, name + ".o")
    with open(obj_path, "wb") as f:
        f.write(tm.emit_object(mod))
    rt = [os.path.join(work, "rt", s) for s in
          ("misa_rt.o", "misa_model.o", "platform_linux.o", "ui_text.o")]
    libs = [os.path.join(DEPS, "gc", "lib", "libgc.a"),
            "/lib/x86_64-linux-gnu/libcurl.so.4.8.0", "-lm"]
    bin_path = os.path.join(work, name + "_misa")
    rc, out, err = run(["gcc", obj_path] + rt + ["-o", bin_path] + libs)
    if rc != 0:
        return None, "link failed:\n" + err[:1500]
    return bin_path, ""


def build_runtime(work):
    rtdir = os.path.join(work, "rt")
    os.makedirs(rtdir, exist_ok=True)
    defs = ["-O2", "-DMISA_USE_GC", "-DMISA_USE_CURL",
            "-I" + os.path.join(DEPS, "gc", "include"),
            "-I" + os.path.join(DEPS, "include")]
    for s in ("misa_rt.c", "misa_model.c", "platform_linux.c", "ui_text.c"):
        o = os.path.join(rtdir, s[:-2] + ".o")
        if os.path.exists(o):
            continue
        rc, out, err = run(["gcc"] + defs + ["-c",
                           os.path.join(RT_DIR, s), "-o", o])
        if rc != 0:
            print("runtime compile failed:", err[:1000])
            sys.exit(1)


def main():
    global PASS, FAIL
    work = tempfile.mkdtemp(prefix="emit-test-")
    build_runtime(work)
    for name in PROGS:
        src = os.path.join(PROG, name + ".misa")
        # 1. Misa emitter -> IR
        ok, ll_or_err = emit_via_misa(src, work)
        if not ok:
            print(f"FAIL  {name}  (misa emit failed)\n{ll_or_err}")
            FAIL += 1
            FAILURES.append(name)
            continue
        # 2. assemble + link + run
        binp, err = assemble_and_link(ll_or_err, work, name)
        if binp is None:
            print(f"FAIL  {name}  ({err})")
            FAIL += 1
            FAILURES.append(name)
            continue
        mrc, mout, merr = run([binp])
        # 3. Python toolchain binary
        pybin = os.path.join(work, name + "_py")
        rc, out, err = run(MISA + ["build", src, "-o", pybin])
        if rc != 0:
            print(f"FAIL  {name}  (python build failed)\n{out}{err}")
            FAIL += 1
            FAILURES.append(name)
            continue
        prc, pout, perr = run([pybin])
        # 4. compare
        if (mrc, mout) == (prc, pout):
            print(f"PASS  {name}")
            PASS += 1
        else:
            print(f"FAIL  {name}  (output mismatch)")
            print(f"  misa: rc={mrc} out={mout!r} err={merr[:200]!r}")
            print(f"  py:   rc={prc} out={pout!r} err={perr[:200]!r}")
            FAIL += 1
            FAILURES.append(name)
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failed:", ", ".join(FAILURES))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
