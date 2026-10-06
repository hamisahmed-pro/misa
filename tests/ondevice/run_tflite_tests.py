#!/usr/bin/env python3
"""On-device AI milestone M4 tests (spec/ONDEVICE.md section 6).

Run: python3 tests/ondevice/run_tflite_tests.py   (from the repo root)

Covers: TFLite interpreter backend via ai-edge-litert ([Float], Float,
Text), E501/E502/E504 for .tflite, the offline guarantee (sockets
blocked), and the plantdoc_edge_tflite example. Mirrors the M1 ONNX
suite (tests/ondevice/run_ondevice_tests.py); the .tflite models are
faithful twins of the .onnx ones (same seed-7 weights).
"""
import ast as py_ast
import os
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "impl"))

import numpy as np

try:
    from ai_edge_litert.interpreter import Interpreter  # noqa: F401
    HAS_LITERT = True
except ImportError:
    HAS_LITERT = False

MISA = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
MODELS = os.path.join(ROOT, "examples", "models")

PASS = 0
FAIL = 0
FAILURES = []


def test(name):
    def deco(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print(f"  ok  {name}")
        except AssertionError as e:
            FAIL += 1
            FAILURES.append((name, str(e)))
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            FAIL += 1
            FAILURES.append((name, f"{type(e).__name__}: {e}"))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    return deco


def run_misa(*args, cwd=None):
    return subprocess.run(MISA + list(args), capture_output=True, text=True,
                          cwd=cwd or ROOT, timeout=120)


def write_tmp(suffix, content):
    d = tempfile.mkdtemp(prefix="misa_tflite_")
    p = os.path.join(d, "t" + suffix)
    with open(p, "w") as f:
        f.write(content)
    return p


PROV_CLASSIFY = """provider edge {
    model: file("%s")
}

models {
    clf: edge("tiny"),
}
""" % os.path.join(MODELS, "tiny_classifier.tflite")


@test("ai-edge-litert importable")
def _():
    assert HAS_LITERT, "ai-edge-litert is required for TFLite tests"


@test("tflite models exist")
def _():
    for m in ("tiny_classifier.tflite", "tiny_affine.tflite",
              "tiny_strid.tflite"):
        assert os.path.exists(os.path.join(MODELS, m)), m


@test("classify matches numpy reference")
def _():
    p = write_tmp(".misa", PROV_CLASSIFY + """
model classify(features: [Float]) -> [Float]
    with edge.clf
    budget { latency: 5s }
    prompt v1 "classify"

print(classify([0.5, -0.2, 0.9, 0.1]))
""")
    r = run_misa("run", p)
    assert r.returncode == 0, r.stderr
    got = py_ast.literal_eval(r.stdout.strip().splitlines()[-1])
    # numpy reference: MatMul/Add/Relu/MatMul/Add/Softmax, seed 7
    # (identical weights to tiny_classifier.onnx)
    rng = np.random.RandomState(7)
    W1 = rng.randn(4, 8).astype(np.float32) * 0.5
    B1 = rng.randn(8).astype(np.float32) * 0.1
    W2 = rng.randn(8, 3).astype(np.float32) * 0.5
    B2 = rng.randn(3).astype(np.float32) * 0.1
    x = np.array([0.5, -0.2, 0.9, 0.1], dtype=np.float32)
    h = np.maximum(x @ W1 + B1, 0)
    l = h @ W2 + B2
    e = np.exp(l - l.max())
    want = (e / e.sum()).tolist()
    assert len(got) == 3, got
    for g, w in zip(got, want):
        assert abs(g - w) < 1e-5, (got, want)


@test("scalar Float model")
def _():
    prov = PROV_CLASSIFY.replace("tiny_classifier.tflite",
                                 "tiny_affine.tflite")
    p = write_tmp(".misa", prov + """
model affine(x: Float) -> Float
    with edge.clf
    budget { latency: 5s }
    prompt v1 "affine"

print(affine(4.0))
""")
    r = run_misa("run", p)
    assert r.returncode == 0, r.stderr
    got = float(r.stdout.strip().splitlines()[-1])
    assert abs(got - 9.0) < 1e-5, got  # y = 2.5x - 1


@test("Text model (string tensor)")
def _():
    mp = os.path.join(MODELS, "tiny_strid.tflite")
    p = write_tmp(".misa", """provider edge {
    model: file("%s")
}

models {
    e: edge("str"),
}

model echo(s: Text) -> Text
    with edge.e
    budget { latency: 5s }
    prompt v1 "echo"

print(echo("hello"))
""" % mp)
    r = run_misa("run", p)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "hello", r.stdout


@test("offline: sockets blocked, tflite still works")
def _():
    sys.path.insert(0, os.path.join(ROOT, "impl"))
    import misa.interp  # noqa: F401  (import first: ssl subclasses socket)
    orig_sock, orig_conn = socket.socket, socket.create_connection

    def blocked(*a, **k):
        raise OSError("network disabled: offline test")
    socket.socket = blocked
    socket.create_connection = blocked
    try:
        from misa.loader import load as misa_load
        from misa.checker import Checker
        from misa.interp import Interpreter
        path = os.path.join(ROOT, "examples", "plantdoc_edge_tflite.misa")
        merged, load_errors, _order, file_of = misa_load(path)
        assert merged is not None, load_errors
        errs = load_errors + Checker().check(merged, file_of=file_of)
        assert not errs, errs
        interp = Interpreter(
            main_dir=os.path.dirname(os.path.abspath(path)))
        interp.file_of = file_of
        interp.run(merged)
    finally:
        socket.socket = orig_sock
        socket.create_connection = orig_conn


@test("E501: tensor shape mismatch")
def _():
    p = write_tmp(".misa", PROV_CLASSIFY + """
model classify(features: [Float]) -> [Float]
    with edge.clf
    prompt v1 "classify"

print(classify([0.5, 0.1]))
""")
    r = run_misa("run", p)
    assert r.returncode == 1, r.stdout
    assert "E501" in r.stderr, r.stderr
    assert "expects 4 values, got 2" in r.stderr, r.stderr


@test("E502: unsupported param type")
def _():
    p = write_tmp(".misa", PROV_CLASSIFY + """
model classify(n: Int) -> [Float]
    with edge.clf
    prompt v1 "classify"

print(classify(3))
""")
    r = run_misa("run", p)
    assert r.returncode == 1, r.stdout
    assert "E502" in r.stderr, r.stderr


@test("E502: unsupported return type")
def _():
    p = write_tmp(".misa", PROV_CLASSIFY + """
model classify(features: [Float]) -> Int
    with edge.clf
    prompt v1 "classify"

print(classify([0.5, -0.2, 0.9, 0.1]))
""")
    r = run_misa("run", p)
    assert r.returncode == 1, r.stdout
    assert "E502" in r.stderr, r.stderr


@test("E504: tflite build for host target refuses (needs Android)")
def _():
    d = tempfile.mkdtemp(prefix="e504-tflite-")
    src = os.path.join(MODELS, "tiny_classifier.tflite")
    p = write_tmp(".misa", """provider edge {
    model: file("%s")
}
models {
    t: edge("x"),
}
model m(x: [Float]) -> [Float]
    with edge.t
    prompt v1 "x"
print(m([1.0, 2.0, 3.0, 4.0]))
""" % src)
    r = run_misa("build", p, "-o", os.path.join(d, "m"))
    assert r.returncode == 1, r.stdout
    assert "E504" in r.stdout + r.stderr, r.stdout + r.stderr


@test("E401: missing model file")
def _():
    p = write_tmp(".misa", """provider edge {
    model: file("./nope/missing.tflite")
}

models {
    t: edge("x"),
}

model classify(features: [Float]) -> [Float]
    with edge.t
    prompt v1 "classify"

print(classify([0.5, -0.2, 0.9, 0.1]))
""")
    r = run_misa("run", p)
    assert r.returncode == 1, r.stdout
    assert "E401" in r.stderr, r.stderr


@test("plantdoc_edge_tflite example runs")
def _():
    r = run_misa("run", os.path.join(ROOT, "examples",
                                     "plantdoc_edge_tflite.misa"))
    assert r.returncode == 0, r.stderr
    assert "diagnosis:" in r.stdout, r.stdout


if __name__ == "__main__":
    # Tests self-register and run via the @test decorator at import;
    # reaching here means the module body (all tests) has executed.
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failures:")
        for name, err in FAILURES:
            print(f"  - {name}: {err}")
        sys.exit(1)
