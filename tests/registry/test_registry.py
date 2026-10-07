#!/usr/bin/env python3
"""Misa registry SERVER API tests.

Boots the real registry/server.misa on localhost (PORT env, default
18081) with a temp REGISTRY_DATA dir, then exercises the full API over
HTTP with urllib (stdlib only):

  publish -> search -> version -> signatures -> dependents -> tarball ->
  downloads -> yank -> backup -> report -> claim -> advisories

asserting status codes and JSON shapes. The publish-pipeline test
packages are TINY (the server's `misa check`/`misa test` subprocesses
are slow).

Run:  python3 tests/registry/test_registry.py   (from the repo root)

Also asserts the server binds 0.0.0.0 (not localhost-only) by
inspecting the listening socket while the server runs.
"""
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
MISA_BIN = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]
PASS = 0
FAIL = 0
FAILURES = []


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def fail(name, detail=""):
    global FAIL
    FAIL += 1
    FAILURES.append(name)
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        fail(name, detail)


class Client:
    def __init__(self, base, token=None):
        self.base = base
        self.token = token

    def req(self, method, path, body=None, token=True):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token and self.token:
            headers["Authorization"] = "Bearer " + self.token
        r = urllib.request.Request(self.base + path, data=data,
                                   headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"_raw": raw}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_pkg(root, name, version, files, manifest_extra=""):
    d = os.path.join(root, name.replace("@", "").replace("/", "_"))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "src"))
    os.makedirs(os.path.join(d, "examples"))
    with open(os.path.join(d, "misa.pkg"), "w") as f:
        f.write('[package]\nname = "%s"\nversion = "%s"\n'
                'description = "test"\nlicense = "MIT"\n%s'
                % (name, version, manifest_extra))
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return d


def pack_publish(client, pkgdir):
    """Pack pkgdir deterministically, build the publish body, POST it."""
    tmp = tempfile.mkdtemp()
    tgz = os.path.join(tmp, "pkg.tar.gz")
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "registry", "helpers",
                                      "pack.py"), pkgdir, tgz],
        check=True, capture_output=True)
    raw = open(tgz, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    man = subprocess.run(
        [sys.executable, os.path.join(ROOT, "registry", "helpers",
                                      "pkg_to_json.py"), pkgdir],
        check=True, capture_output=True, text=True).stdout
    body = {"manifest": json.loads(man),
            "tarball_b64": base64.b64encode(raw).decode(),
            "tarball_sha256": sha,
            # accepted but ignored: the server always recomputes
            "signatures": [{"kind": "fn", "name": "bogus"}]}
    st, resp = client.req("POST", "/v1/publish", body)
    shutil.rmtree(tmp, ignore_errors=True)
    return st, resp, sha, raw


TINY_LIB = 'fn answer() -> Int {\n  return 42\n}\n'
TINY_EX = 'import "../src/lib.misa"\nprint(answer())\n'
TINY_TEST = ('import "../src/lib.misa"\n'
             'test answer_is_42 {\n  assert_eq(answer(), 42)\n}\n')


def main():
    global FAIL
    data = tempfile.mkdtemp(prefix="regdata-")
    work = tempfile.mkdtemp(prefix="regwork-")
    port = int(os.environ.get("PORT", "18081"))
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["MISA_ROOT"] = ROOT
    env["REGISTRY_DATA"] = data
    env["PORT"] = str(port)

    print("== init (admin.misa)")
    r = subprocess.run(MISA_BIN + ["run", "registry/admin.misa"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=120)
    check("admin.misa exits 0", r.returncode == 0, r.stderr[-500:])
    admin_token = None
    for line in r.stdout.splitlines():
        s = line.strip()
        if len(s) == 64 and all(c in "0123456789abcdef" for c in s):
            admin_token = s
    check("admin token printed", bool(admin_token))
    env["REGISTRY_ADMIN_TOKEN"] = admin_token

    print("== boot server.misa")
    srv = subprocess.Popen(
        MISA_BIN + ["run", "registry/server.misa"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    cli = Client(base, admin_token)
    ready = False
    for _ in range(60):
        try:
            st, _ = Client(base).req("GET", "/v1/")
            if st == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1)
    check("server boots and answers /v1/", ready)
    if not ready:
        print(srv.stdout.read() if srv.stdout else "")
        srv.kill()
        sys.exit(1)

    print("== bind address (must be 0.0.0.0, not localhost-only)")
    bound_all = False
    try:
        out = subprocess.run(["ss", "-tln"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{port}" in line and "0.0.0.0" in line:
                bound_all = True
    except Exception:
        pass
    check("listening socket is 0.0.0.0:%d" % port, bound_all)

    try:
        print("== basic read endpoints (empty registry)")
        st, b = Client(base).req("GET", "/v1/")
        check("GET /v1/ -> 200", st == 200 and b.get("api") == "v1", str(b)[:120])
        st, b = cli.req("GET", "/v1/packages/@t/rt")
        check("missing package -> 404 + error shape", st == 404
              and b.get("error", {}).get("code") == "R404", str(b)[:120])
        st, b = cli.req("GET", "/v1/search?q=zzz_no_such_pkg_zzz")
        check("empty search -> 200 []", st == 200
              and b.get("results") == [], str(b)[:120])

        print("== publish @t/rt 0.1.0 (full pipeline)")
        pkgdir = write_pkg(work, "@t/rt", "0.1.0",
                           {"src/lib.misa": TINY_LIB,
                            "examples/demo.misa": TINY_EX,
                            "tests/a_test.misa": TINY_TEST})
        st, b, sha, raw = pack_publish(cli, pkgdir)
        check("publish -> 201", st == 201, str(b)[:200])
        check("publish body shape", b.get("package") == "@t/rt"
              and b.get("version") == "0.1.0"
              and b.get("sha256") == sha, str(b)[:200])
        check("publish ignores client signatures",
              b.get("breaking_computed") == "none", str(b)[:200])

        print("== publish errors")
        st, b, _, _ = pack_publish(cli, pkgdir)
        check("duplicate version -> 409", st == 409
              and b["error"]["code"] == "R409", str(b)[:150])
        st, b = cli.req("POST", "/v1/publish", {"manifest": {}},
                        token=False)
        check("no auth -> 401", st == 401, str(b)[:120])
        bad = write_pkg(work, "@t3/bad", "0.1.0",
                        {"src/lib.misa": TINY_LIB,
                         "examples/demo.misa": TINY_EX})
        # strip the license line -> E113 manifest violation
        mp = os.path.join(bad, "misa.pkg")
        content = open(mp).read().replace('license = "MIT"\n', "")
        open(mp, "w").write(content)
        st, b, _, _ = pack_publish(cli, bad)
        check("missing license -> 422", st == 422
              and b["error"]["code"] == "R422", str(b)[:150])
        sm = write_pkg(work, "@t3/sm", "0.1.0",
                       {"src/lib.misa": TINY_LIB,
                        "examples/demo.misa": TINY_EX})
        tmp = tempfile.mkdtemp()
        tgz = os.path.join(tmp, "p.tgz")
        subprocess.run([sys.executable,
                        os.path.join(ROOT, "registry", "helpers",
                                     "pack.py"), sm, tgz],
                       check=True, capture_output=True)
        raw2 = open(tgz, "rb").read()
        man = json.loads(subprocess.run(
            [sys.executable, os.path.join(ROOT, "registry", "helpers",
                                          "pkg_to_json.py"), sm],
            check=True, capture_output=True, text=True).stdout)
        st, b = cli.req("POST", "/v1/publish",
                        {"manifest": man,
                         "tarball_b64": base64.b64encode(raw2).decode(),
                         "tarball_sha256": "00" * 32})
        check("sha256 mismatch -> 422", st == 422
              and "mismatch" in b["error"]["message"], str(b)[:150])
        shutil.rmtree(tmp, ignore_errors=True)

        print("== read endpoints")
        st, b = cli.req("GET", "/v1/packages/@t/rt")
        check("package detail -> 200", st == 200
              and b["package"] == "@t/rt"
              and b["latest"] == "0.1.0", str(b)[:150])
        st, b = cli.req("GET", "/v1/packages/@t/rt/0.1.0")
        check("version detail -> 200", st == 200
              and b["version"] == "0.1.0"
              and b["sha256"] == sha
              and b["capabilities"]["verified"] == []
              and b["examples"] == {"pass": 1, "total": 1},
              str(b)[:200])
        check("throws surface computed",
              any(f["name"] == "answer" for f in b["throws_surface"]),
              str(b["throws_surface"])[:150])
        st, b = cli.req("GET", "/v1/packages/@t/rt/0.1.0/signatures")
        check("signatures -> 200", st == 200
              and any(s["name"] == "answer" and s["kind"] == "fn"
                      for s in b["signatures"]), str(b)[:200])

        print("== search")
        st, b = cli.req("GET", "/v1/search?q=answer")
        check("keyword search finds @t/rt", st == 200
              and any(r["package"] == "@t/rt" for r in b["results"]),
              str(b)[:150])
        st, b = cli.req("GET", "/v1/search?q=fn()->Int&kind=semantic")
        check("typed fn()->Int finds answer", st == 200
              and any(r["package"] == "@t/rt"
                      and any(m["name"] == "answer" for m in r["matched"])
                      for r in b["results"]), str(b)[:200])
        st, b = cli.req("GET", "/v1/search?q=fn(Text)->Int&kind=semantic")
        check("typed fn(Text)->Int finds nothing", st == 200
              and b["results"] == [], str(b)[:150])

        print("== dependents + tarball + downloads")
        dep = write_pkg(work, "@t/dep", "0.1.0",
                        {"src/lib.misa": TINY_LIB,
                         "examples/demo.misa": TINY_EX},
                        manifest_extra='[dependencies]\n"@t/rt" = "^0.1.0"\n')
        st, b, _, _ = pack_publish(cli, dep)
        check("publish dependent -> 201", st == 201, str(b)[:150])
        st, b = cli.req("GET", "/v1/packages/@t/rt/dependents")
        check("dependents lists @t/dep", st == 200
              and any(d["package"] == "@t/dep" for d in b["dependents"]),
              str(b)[:150])
        st, b = cli.req("GET", "/v1/tarballs/@t/rt/0.1.0")
        check("tarball -> 200 + sha match", st == 200
              and b["sha256"] == sha, str(b)[:120])
        tgz_raw = base64.b64decode(b["tarball_b64"])
        names = tarfile.open(fileobj=io.BytesIO(tgz_raw)).getnames()
        check("tarball bytes are a valid tar", "src/lib.misa" in names,
              str(names)[:120])
        st, b = cli.req("GET", "/v1/packages/@t/rt/downloads?range=30d")
        check("downloads counted", st == 200 and b["total"] >= 1,
              str(b)[:120])

        print("== breaking-change gate")
        brk = write_pkg(work, "@t4/brk", "0.1.0",
                        {"src/lib.misa": TINY_LIB,
                         "examples/demo.misa": TINY_EX})
        st, b, _, _ = pack_publish(cli, brk)
        check("baseline 0.1.0 -> 201", st == 201, str(b)[:150])
        brk2 = write_pkg(work, "@t4/brk", "0.2.0",
                         {"src/lib.misa":
                          "fn answer2() -> Int {\n  return 7\n}\n",
                          "examples/demo.misa":
                          'import "../src/lib.misa"\nprint(answer2())\n'})
        st, b, _, _ = pack_publish(cli, brk2)
        check("removed fn on minor bump -> 422", st == 422
              and "major" in b["error"]["message"], str(b)[:200])
        brk3 = write_pkg(work, "@t4/brk", "1.0.0",
                         {"src/lib.misa":
                          "fn answer2() -> Int {\n  return 7\n}\n",
                          "examples/demo.misa":
                          'import "../src/lib.misa"\nprint(answer2())\n'})
        st, b, _, _ = pack_publish(cli, brk3)
        check("removed fn on major bump -> 201", st == 201
              and b["breaking_computed"] == "major", str(b)[:200])

        print("== secret scan + capability over-declaration")
        evil = write_pkg(work, "@t5/evil", "0.1.0",
                         {"src/lib.misa": TINY_LIB,
                          "examples/demo.misa": TINY_EX,
                          "src/k.txt": 'key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"\n'})
        st, b, _, _ = pack_publish(cli, evil)
        check("leaked secret -> 422", st == 422
              and "secret" in b["error"]["message"].lower(),
              str(b)[:200])
        over = write_pkg(work, "@t6/over", "0.1.0",
                         {"src/lib.misa": TINY_LIB,
                          "examples/demo.misa": TINY_EX},
                         manifest_extra="[capabilities]\nnetwork = true\n")
        st, b, _, _ = pack_publish(cli, over)
        check("declared cap unused -> 422", st == 422
              and "capabilities" in b["error"]["message"], str(b)[:200])

        print("== yank")
        st, b = cli.req("POST", "/v1/yank",
                        {"pkg": "@t/rt", "version": "0.1.0",
                         "reason": "test"})
        check("yank -> 200", st == 200 and b.get("yanked") is True,
              str(b)[:150])
        st, b = cli.req("GET", "/v1/packages/@t/rt/0.1.0")
        check("yanked flag visible", st == 200 and b["yanked"] is True)
        st, b = cli.req("GET", "/v1/search?q=answer")
        check("yanked excluded from search", st == 200
              and not any(r["package"] == "@t/rt" for r in b["results"]),
              str(b)[:150])

        print("== tokens (admin) + scopes")
        st, b = cli.req("POST", "/v1/admin/tokens",
                        {"scopes": ["publish:@sc/*", "yank:@sc/*"]})
        check("mint token -> 201", st == 201 and len(b.get("token", "")) == 64
              and b["scopes"] == ["publish:@sc/*", "yank:@sc/*"],
              str(b)[:200])
        scoped = Client(base, b["token"])
        st, b = scoped.req("POST", "/v1/admin/tokens",
                           {"scopes": ["admin:*"]})
        check("non-admin cannot mint -> 403", st == 403
              and b["error"]["code"] == "R403", str(b)[:120])
        sc = write_pkg(work, "@sc/app", "0.1.0",
                       {"src/lib.misa": TINY_LIB,
                        "examples/demo.misa": TINY_EX})
        st, b, _, _ = pack_publish(scoped, sc)
        check("scoped token publishes in-scope -> 201", st == 201,
              str(b)[:150])
        sc2 = write_pkg(work, "@t/sneaky", "9.9.9",
                        {"src/lib.misa": TINY_LIB,
                         "examples/demo.misa": TINY_EX})
        st, b, _, _ = pack_publish(scoped, sc2)
        check("scoped token out-of-scope -> 403", st == 403
              and b["error"]["code"] == "R403", str(b)[:150])

        print("== token revoke")
        import hashlib as _hl
        sc_tok = scoped.token
        st, b = scoped.req("POST", "/v1/tokens/revoke",
                           {"token_sha256": _hl.sha256(
                               sc_tok.encode()).hexdigest()})
        check("owner revokes own token -> 200", st == 200
              and b.get("revoked") is True, str(b)[:150])
        st, b = scoped.req("POST", "/v1/admin/tokens",
                           {"scopes": ["publish:@sc/*"]})
        check("revoked token is dead -> 401", st == 401, str(b)[:120])
        st, b = scoped.req("POST", "/v1/tokens/revoke",
                           {"token_sha256": "00" * 32})
        check("revoke unknown token -> 401 (dead token)", st == 401,
              str(b)[:120])

        print("== namespace claim / report / advisories / backup")
        st, b = cli.req("POST", "/v1/namespace/claim",
                        {"namespace": "@t"})
        check("claim owned ns -> already claimed by you", st == 200
              and b.get("by") == "admin", str(b)[:150])
        st, b = cli.req("POST", "/v1/namespace/claim",
                        {"namespace": "@brandnew"})
        check("claim fresh ns -> 201", st == 201
              and b.get("claimed") is True, str(b)[:150])
        st, b = Client(base).req(
            "POST", "/v1/report",
            {"pkg": "@t/rt", "reason": "test report"}, token=False)
        check("report without auth -> 201", st == 201
              and b.get("state") == "open" and b.get("id"), str(b)[:150])
        st, b = cli.req("POST", "/v1/advisories",
                        {"pkg": "@t/rt", "affected": "<1.0.0",
                         "fixed_in": "1.0.0", "severity": "high",
                         "summary": "test advisory"})
        check("post advisory (admin) -> 201", st == 201
              and b.get("id", "").startswith("GHSA-"), str(b)[:150])
        st, b = cli.req("GET", "/v1/advisories?package=@t/rt")
        check("advisories filter -> 200", st == 200
              and any(a["package"] == "@t/rt" for a in b["advisories"]),
              str(b)[:200])
        st, b = Client(base).req("GET", "/v1/admin/backup", token=False)
        check("backup without admin -> 401", st == 401
              and b["error"]["code"] == "R401", str(b)[:120])
        # backup returns text/plain SQL; fetch raw
        r = urllib.request.Request(
            base + "/v1/admin/backup",
            headers={"Authorization": "Bearer " + admin_token})
        with urllib.request.urlopen(r, timeout=60) as resp:
            dump = resp.read().decode()
        check("backup dump shape",
              dump.startswith("-- misa-registry backup")
              and "CREATE TABLE versions" in dump
              and "INSERT INTO" in dump, dump[:120])
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failures:", FAILURES)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
