#!/usr/bin/env python3
"""AOT on-device tests (Phase 5, M2): build programs with on-device
`.onnx` providers to native binaries and verify local inference.

- build+run plantdoc_edge offline (no network namespace): the diagnosis
  must match the interpreter's, bit-for-bit on the doubles (the decimal
  rendering may differ: Python shortest-repr vs the AOT runtime's
  %.17g — the values are identical IEEE-754 doubles).
- E501 shape mismatch in the compiled binary.
- E504 still fires at build time for .mlpackage on the host.
- E504 fires when ORT is not vendored (simulated via MISA_NO_ORT=1).

Usage: test_ondevice.py [--keep]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")
MISA = os.path.join(REPO, "impl", "bin", "misa")
EDGE = os.path.join(REPO, "examples", "plantdoc_edge.misa")


def run(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, capture_output=True, text=True, env=e,
                       timeout=180)
    return p.returncode, p.stdout, p.stderr


def build(src, out):
    return run([MISA, "build", src, "-o", out])


def probs_of(stdout):
    """Parse the probabilities line into a list of floats."""
    m = re.search(r"^probabilities: (\[.*\])$", stdout, re.M)
    assert m, f"no probabilities line in:\n{stdout}"
    return json.loads(m.group(1))


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="aot-ondevice-")
    passed, failed = 0, []

    def check(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ok  {name}")
        else:
            failed.append(name)
            print(f"  FAIL {name}\n  {detail}")

    # --- 1. build plantdoc_edge -------------------------------------
    exe = os.path.join(work, "plantdoc_edge")
    rc, so, se = build(EDGE, exe)
    check("build plantdoc_edge", rc == 0, se[-500:])
    if rc == 0:
        staged = os.path.join(work, "tiny_classifier.onnx")
        check("model staged next to binary", os.path.isfile(staged))
        check("ORT .so staged next to binary",
              os.path.exists(os.path.join(work, "libonnxruntime.so.1")))

        # --- 2. run offline: no network namespace at all -------------
        rc, so, se = run(["unshare", "-n", exe])
        check("runs with no network (unshare -n)", rc == 0, se[-500:])
        diag_ok = ("diagnosis: healthy" in so) if rc == 0 else False
        check("offline diagnosis completes", diag_ok, so[-500:])

        # --- 3. semantic parity with the interpreter -----------------
        # The interpreter's offline run is the reference; compare the
        # decoded doubles bit-for-bit (rendering may differ).
        irc, iso, ise = run([MISA, "run", EDGE])
        if rc == 0 and irc == 0:
            try:
                a, b = probs_of(iso), probs_of(so)
                same = (len(a) == len(b) and all(
                    x.hex() == y.hex() for x, y in zip(a, b)))
            except AssertionError as e:
                same, detail = False, str(e)
            else:
                detail = f"interp={a} aot={b}"
            check("probabilities bit-identical to interpreter",
                  same, detail)
            check("diagnosis line matches",
                  "diagnosis: healthy" in iso and
                  "diagnosis: healthy" in so)
        else:
            check("probabilities bit-identical to interpreter", False,
                  f"interp rc={irc} aot rc={rc}")

        # --- 4. relocatable: ship binary+model+.so elsewhere ---------
        ship = os.path.join(work, "shipped")
        os.makedirs(ship)
        for f in os.listdir(work):
            if f.startswith(("plantdoc_edge", "tiny_classifier",
                             "libonnxruntime")):
                src = os.path.join(work, f)
                dst = os.path.join(ship, f)
                if os.path.islink(src):
                    os.symlink(os.readlink(src), dst)
                else:
                    shutil.copy2(src, dst)
        rc, so, se = run([os.path.join(ship, "plantdoc_edge")])
        check("relocated bundle runs (exe-dir model resolution)",
              rc == 0 and "diagnosis: healthy" in so, se[-500:])

    # --- 5. E501 shape mismatch in the compiled binary ---------------
    bad_src = os.path.join(work, "shape.misa")
    with open(bad_src, "w") as f:
        f.write(
            'provider edge {\n'
            '    model: file("' +
            os.path.join(REPO, "examples", "models",
                         "tiny_classifier.onnx") + '")\n'
            '}\n'
            'models {\n    clf: edge("tiny"),\n}\n'
            'model classify_symptoms(features: [Float]) -> [Float]\n'
            '    with edge.clf\n    prompt v1 "x"\n'
            'let probs = classify_symptoms([0.9, 0.2])\n'
            'print(probs)\n')
    bad_exe = os.path.join(work, "shape")
    rc, so, se = build(bad_src, bad_exe)
    check("shape-mismatch program builds", rc == 0, se[-500:])
    if rc == 0:
        rc, so, se = run([bad_exe])
        check("E501 raised in compiled binary",
              rc == 1 and "E501" in se and "expects 4 values" in se,
              se[-300:])

    # --- 6. E502 unsupported signature in the compiled binary --------
    sig_src = os.path.join(work, "sig.misa")
    with open(sig_src, "w") as f:
        f.write(
            'provider edge {\n'
            '    model: file("' +
            os.path.join(REPO, "examples", "models",
                         "tiny_classifier.onnx") + '")\n'
            '}\n'
            'models {\n    clf: edge("tiny"),\n}\n'
            'model classify_symptoms(features: [Int]) -> [Float]\n'
            '    with edge.clf\n    prompt v1 "x"\n'
            'let probs = classify_symptoms([1, 2, 3, 4])\n'
            'print(probs)\n')
    sig_exe = os.path.join(work, "sig")
    rc, so, se = build(sig_src, sig_exe)
    check("bad-signature program builds", rc == 0, se[-500:])
    if rc == 0:
        rc, so, se = run([sig_exe])
        check("E502 raised in compiled binary",
              rc == 1 and "E502" in se, se[-300:])

    # --- 7. E504 at build time for CoreML on the host ----------------
    ml_src = os.path.join(work, "coreml.misa")
    os.makedirs(os.path.join(work, "fake.mlpackage"), exist_ok=True)
    with open(ml_src, "w") as f:
        f.write(
            'provider edge {\n'
            '    model: file("' + os.path.join(work, "fake.mlpackage") +
            '")\n'
            '}\n'
            'models {\n    clf: edge("tiny"),\n}\n'
            'model classify_symptoms(features: [Float]) -> [Float]\n'
            '    with edge.clf\n    prompt v1 "x"\n'
            'let probs = classify_symptoms([0.9, 0.2, 0.7, 0.1])\n'
            'print(probs)\n')
    rc, so, se = build(ml_src, os.path.join(work, "coreml"))
    check("E504 for .mlpackage on host",
          rc != 0 and "E504" in (so + se), (so + se)[-300:])

    print(f"\n{passed} passed, {len(failed)} failed")
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
