#!/usr/bin/env python3
"""Misa registry client tests (spec/REGISTRY.md §1-5, §6.6, §7.4-7.5).

Run: python3 tests/pkg/run_registry_tests.py   (from the repo root)

Covers: semver parse/compare/range satisfaction (incl. the pre-1.0
^0.x.y rule), lockfile v2 read/write with hashes, manifest validation
variants (E113), namespace validation, registry URL resolution, and
client<->server integration against a stub HTTP server (Python
http.server) implementing just enough of the API contract for:
publish -> add -> install -> search -> yank, plus audit, outdated,
update, vendor, dependents, deprecate, diff, login/logout.

Intentional deviations from spec/REGISTRY.md (client + stub):
  1. POST /v1/publish takes a JSON body {manifest, tarball_b64,
     tarball_sha256, signatures, capabilities_declared}; the spec says
     multipart. JSON keeps bodies UTF-8 and agent-friendly.
  2. GET /v1/tarballs/<name>/<version> returns JSON
     {sha256, tarball_b64}, not raw tarball bytes (spec's path implies
     raw bytes; the task's v1 contract says JSON).
  3. Namespaced names use the spec's /v1/packages/:ns/:name two-segment
     form ("/v1/packages/@acme/router"); each segment is percent-encoded
     separately so the '/' separator is never ambiguous.
  4. `misa fmt --check` does not exist in the toolchain; the publish
     gate implements the equivalent check directly via the Formatter
     (gate 4).
  5. POST /v1/tokens/revoke is client-defined: the spec has no revoke
     endpoint, but `pkg logout --revoke` needs one. It takes
     {token_sha256} (the server stores only sha256(token)) under
     Bearer auth.
  6. Signature extraction skips `_`-prefixed fns (private by
     convention); Misa has no visibility modifier.
  7. A removed `impl` counts as a minor change: the §6.5 breaking
     table lists impl removal nowhere (new impls are "ok").
  8. Lockfile v2 entries carry optional `range` and `registry` keys
     beyond the spec's v2 lockfile: `update`/`outdated` need the range
     the entry was resolved from, and private registries need the
     source recorded per entry.
  9. Bare "1.2.0" in a range position means exact (=), like npm.
 10. Yanked versions are excluded from fresh resolution (spec §3).
 11. v1 `pkg login` is paste-a-token (OAuth device flow is v1.1 per
     the task brief); the token is stored in ~/.misa/credentials,
     mode 0600, TOML {token, handle}.
 12. `pkg add` range defaults to ^latest; `pkg outdated` compares
     against the entry's stored range (or ^locked-version).
"""
import base64
import getpass
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "impl"))

import misa.pkg as P
from misa.pkg import (SemVer, satisfies, max_satisfying, bump_kind,
                      parse_range, resolve_registry, read_manifest,
                      PkgError, PkgCliError, pack_tarball, diff_signatures,
                      extract_signatures, lock_set, read_lock,
                      read_lock_full, write_lock, NAMESPACE_RE,
                      _tq)

PASS = 0
FAIL = 0
FAILURES = []

MISA = [sys.executable, os.path.join(ROOT, "impl", "bin", "misa")]


def test(name):
    def deco(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print(f"  ok  {name}")
        except AssertionError as e:
            FAIL += 1
            FAILURES.append((name, str(e)[:400]))
            print(f" FAIL {name}: {e}")
        except Exception as e:
            FAIL += 1
            FAILURES.append((name, f"{type(e).__name__}: {e}"[:400]))
            print(f" FAIL {name}: {type(e).__name__}: {e}")
    return deco


def eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r}, got {a!r}"


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# -- semver -----------------------------------------------------------------

@test("semver: parse valid")
def _():
    v = SemVer.parse("1.2.3")
    eq((v.major, v.minor, v.patch, v.pre), (1, 2, 3, ()))
    eq(str(SemVer.parse("0.0.0")), "0.0.0")
    eq(str(SemVer.parse("1.0.0-alpha.1")), "1.0.0-alpha.1")
    eq(str(SemVer.parse("2.1.0+build.5")), "2.1.0")  # build ignored


@test("semver: parse invalid raises")
def _():
    for bad in ("1.2", "v1.2.3", "1.2.3.4", "", "abc", "1.2.x"):
        try:
            SemVer.parse(bad)
        except PkgCliError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


@test("semver: comparison incl prerelease")
def _():
    assert SemVer.parse("2.0.0") > SemVer.parse("1.99.99")
    assert SemVer.parse("1.2.4") > SemVer.parse("1.2.3")
    assert SemVer.parse("1.0.0") > SemVer.parse("1.0.0-rc.1")
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-alpha.1")
    assert SemVer.parse("1.0.0-alpha.1") < SemVer.parse("1.0.0-beta")
    assert SemVer.parse("1.0.0-1") < SemVer.parse("1.0.0-a")
    eq(SemVer.parse("1.2.3"), SemVer.parse("1.2.3"))


@test("semver: caret ranges incl pre-1.0 rule")
def _():
    assert satisfies("1.9.0", "^1.2.3")
    assert not satisfies("2.0.0", "^1.2.3")
    assert not satisfies("1.2.2", "^1.2.3")
    # ^0.x.y means >=0.x.y <0.(x+1).0
    assert satisfies("0.2.9", "^0.2.3")
    assert not satisfies("0.3.0", "^0.2.3")
    assert not satisfies("0.2.2", "^0.2.3")
    # ^0.0.z means >=0.0.z <0.0.(z+1)
    assert satisfies("0.0.3", "^0.0.3")
    assert not satisfies("0.0.4", "^0.0.3")
    assert not satisfies("0.1.0", "^0.0.3")


@test("semver: tilde / exact / bare ranges")
def _():
    assert satisfies("1.2.9", "~1.2.3")
    assert not satisfies("1.3.0", "~1.2.3")
    assert not satisfies("1.2.2", "~1.2.3")
    assert satisfies("1.2.3", "=1.2.3")
    assert not satisfies("1.2.4", "=1.2.3")
    assert satisfies("1.2.3", "1.2.3")      # bare = exact (npm-style)
    assert not satisfies("1.2.4", "1.2.3")


@test("semver: comparison ranges + AND")
def _():
    assert satisfies("1.5.0", ">=1.0.0, <2.0.0")
    assert not satisfies("2.0.0", ">=1.0.0, <2.0.0")
    assert satisfies("1.1.9", "<1.2.0")
    assert not satisfies("1.2.0", "<1.2.0")
    assert satisfies("2.0.0", "<=2.0.0")
    assert satisfies("3.0.0", ">2.9.9")


@test("semver: prereleases excluded unless named")
def _():
    assert not satisfies("1.5.0-alpha", "^1.2.0")
    assert satisfies("1.5.0-alpha", "=1.5.0-alpha")
    assert satisfies("1.5.0-alpha", "^1.5.0-alpha")
    assert not satisfies("2.0.0", "^latest") is False  # sanity
    assert satisfies("2.0.0", "^latest")
    assert not satisfies("2.0.0-beta", "^latest")


@test("semver: max_satisfying")
def _():
    vs = ["1.0.0", "1.5.0", "2.0.0", "0.9.0"]
    eq(max_satisfying(vs, "^1.0.0"), "1.5.0")
    eq(max_satisfying(vs, "^latest"), "2.0.0")
    eq(max_satisfying(vs, "~1.0.0"), "1.0.0")
    eq(max_satisfying(vs, "^3.0.0"), None)
    eq(max_satisfying([], "^1.0.0"), None)


@test("semver: bump_kind")
def _():
    eq(bump_kind("1.2.3", "2.0.0"), "major")
    eq(bump_kind("1.2.3", "1.3.0"), "minor")
    eq(bump_kind("1.2.3", "1.2.4"), "patch")
    eq(bump_kind("1.2.3", "1.2.3"), None)
    eq(bump_kind("0.2.3", "0.3.0"), "minor")


# -- lockfile v2 --------------------------------------------------------------

@test("lockfile: v2 write/read with hashes")
def _():
    tmp = tempfile.mkdtemp()
    lp = os.path.join(tmp, "misa.lock")
    lock_set("@acme/router", "1.2.0", "registry", path=lp,
             extra={"sha256": "ab" * 32,
                    "capabilities": ["network", "fs"],
                    "range": "^1.2.0",
                    "registry": "https://r.example.com"},
             registry="https://r.example.com")
    lock_set("oldpkg", "0.1.0", "path+/x/y", path=lp)
    raw = open(lp).read()
    assert "version = 2" in raw, raw
    assert 'registry = "https://r.example.com"' in raw, raw
    assert 'sha256 = "' + "ab" * 32 + '"' in raw, raw
    assert 'capabilities = ["network", "fs"]' in raw, raw
    assert 'range = "^1.2.0"' in raw, raw
    ver, reg, entries = read_lock_full(lp)
    eq(ver, 2)
    eq(reg, "https://r.example.com")
    by_name = {e["name"]: e for e in entries}
    eq(by_name["@acme/router"]["sha256"], "ab" * 32)
    eq(by_name["@acme/router"]["capabilities"], ["network", "fs"])
    eq(by_name["@acme/router"]["range"], "^1.2.0")
    eq(by_name["oldpkg"]["source"], "path+/x/y")
    assert "sha256" not in by_name["oldpkg"]


@test("lockfile: v1 git/path lockfiles stay headerless")
def _():
    tmp = tempfile.mkdtemp()
    lp = os.path.join(tmp, "misa.lock")
    lock_set("mypkg", "1.0.0", "git+https://e.com/r.git@abc123", path=lp)
    raw = open(lp).read()
    assert "version = 2" not in raw, raw
    ver, reg, entries = read_lock_full(lp)
    eq(ver, 1)
    eq(reg, None)
    eq(entries[0]["source"], "git+https://e.com/r.git@abc123")


# -- manifest / namespace validation --------------------------------------------

@test("manifest: @org/name accepted")
def _():
    tmp = tempfile.mkdtemp()
    write(os.path.join(tmp, "misa.pkg"),
          '[package]\nname = "@acme/router"\nversion = "1.2.0"\n'
          'description = "d"\nlicense = "MIT"\n')
    m = read_manifest(tmp)
    eq(m["name"], "@acme/router")
    eq(m["license"], "MIT")


@test("manifest: bad namespaced names -> PkgError")
def _():
    for bad in ("@Acme/router", "@acme", "acme/router", "@acme/router/x",
                "@/router", "@acme/", "@acme/ro_uter"):
        tmp = tempfile.mkdtemp()
        write(os.path.join(tmp, "misa.pkg"),
              f'[package]\nname = "{bad}"\nversion = "1.0.0"\n')
        try:
            read_manifest(tmp)
        except PkgError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


@test("manifest: publish validation E113 variants")
def _():
    def mkpkg(extra):
        tmp = tempfile.mkdtemp()
        write(os.path.join(tmp, "misa.pkg"),
              '[package]\nname = "@acme/p"\nversion = "1.0.0"\n' + extra)
        os.makedirs(os.path.join(tmp, "examples"), exist_ok=True)
        write(os.path.join(tmp, "examples", "hello.misa"),
              'print("hi")\n')
        return tmp
    # missing license
    try:
        P.validate_publish_manifest(
            read_manifest(mkpkg('description = "d"\n')), mkpkg(''))
        raise AssertionError("missing license should fail")
    except PkgCliError as e:
        assert "E113" in str(e) and "license" in str(e), e
    # missing description
    try:
        d = mkpkg('license = "MIT"\n')
        P.validate_publish_manifest(read_manifest(d), d)
        raise AssertionError("missing description should fail")
    except PkgCliError as e:
        assert "E113" in str(e) and "description" in str(e), e
    # no examples
    tmp = tempfile.mkdtemp()
    write(os.path.join(tmp, "misa.pkg"),
          '[package]\nname = "@acme/p"\nversion = "1.0.0"\n'
          'description = "d"\nlicense = "MIT"\n')
    try:
        P.validate_publish_manifest(read_manifest(tmp), tmp)
        raise AssertionError("missing examples should fail")
    except PkgCliError as e:
        assert "E113" in str(e) and "examples" in str(e), e


# -- registry URL resolution ------------------------------------------------------

@test("registry: resolution precedence")
def _():
    home = tempfile.mkdtemp()
    cfg = os.path.join(home, ".misa", "config")
    old_home = os.environ.get("HOME")
    old_env = os.environ.pop("MISA_REGISTRY", None)
    os.environ["HOME"] = home
    try:
        eq(resolve_registry(), "https://pkg.misa.website")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        write(cfg, '[registries]\ndefault = "https://cfg.example.com/"\n')
        eq(resolve_registry(), "https://cfg.example.com")
        os.environ["MISA_REGISTRY"] = "https://env.example.com"
        eq(resolve_registry(), "https://env.example.com")
        eq(resolve_registry("https://flag.example.com/"),
           "https://flag.example.com")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_env is not None:
            os.environ["MISA_REGISTRY"] = old_env


# -- tarball ----------------------------------------------------------------------

@test("tarball: deterministic pack")
def _():
    tmp = tempfile.mkdtemp()
    write(os.path.join(tmp, "misa.pkg"),
          '[package]\nname = "@acme/d"\nversion = "1.0.0"\n')
    write(os.path.join(tmp, "src", "lib.misa"), 'fn f() -> Int {\n    return 1\n}\n')
    write(os.path.join(tmp, "src", "a.misa"), 'fn g() -> Int {\n    return 2\n}\n')
    write(os.path.join(tmp, "examples", "hello.misa"), 'print("hi")\n')
    os.makedirs(os.path.join(tmp, ".git"))
    write(os.path.join(tmp, ".git", "x"), "junk")
    b1, h1 = pack_tarball(tmp)
    b2, h2 = pack_tarball(tmp)
    eq(b1, b2)
    eq(h1, h2)
    eq(h1, hashlib.sha256(b1).hexdigest())
    # sorted entries, fixed mtime
    import tarfile
    names = tarfile.open(fileobj=io.BytesIO(b1)).getnames()
    eq(names, sorted(names))
    for m in tarfile.open(fileobj=io.BytesIO(b1)).getmembers():
        eq(m.mtime, 0)
    assert not any(".git" in n for n in names), names
    # unpack round-trip
    dest = os.path.join(tempfile.mkdtemp(), "out")
    P.unpack_tarball(b1, dest)
    assert open(os.path.join(dest, "src", "lib.misa")).read().startswith("fn f")


# -- signature diff -----------------------------------------------------------------

def _fn(name, params=(), ret="Void", throws=(), doc=""):
    return {"kind": "fn", "name": name,
            "params": [[p, t] for p, t in params],
            "ret": ret, "throws": list(throws), "doc": doc}


@test("diff: fn removed / param type changed / throws grew -> major")
def _():
    old = [_fn("get", [("u", "Text")], "Text", ["E304"]),
           _fn("gone"), _fn("keep", [("a", "Int")], "Int")]
    new = [_fn("get", [("u", "Text")], "Text", ["E304", "E306"]),
           _fn("keep", [("a", "Text")], "Int")]
    d = diff_signatures(old, new)
    eq(d["min_bump"], "major")
    details = " ".join(c["detail"] for c in d["changes"])
    assert "gone" in details and "removed" in details
    assert "+E306" in details
    assert "keep" in details and "Text" in details


@test("diff: additions -> minor; doc/throws-shrink -> patch")
def _():
    old = [_fn("a", throws=["E300", "E301"], doc="old")]
    new = [_fn("a", throws=["E300"], doc="new"), _fn("b")]
    d = diff_signatures(old, new)
    eq(d["min_bump"], "minor")
    d2 = diff_signatures([_fn("a", doc="old")], [_fn("a", doc="new")])
    eq(d2["min_bump"], "patch")
    eq(diff_signatures([_fn("a")], [_fn("a")])["min_bump"], "patch")


@test("diff: interface gained method -> major")
def _():
    def iface(methods):
        return {"kind": "interface", "name": "Cache",
                "methods": [{"name": m} for m in methods]}
    d = diff_signatures([iface(["get"])], [iface(["get", "put"])])
    eq(d["min_bump"], "major")
    assert "gained method put" in d["changes"][0]["detail"]


# ======================================================================
# Stub registry server (implements just enough of the API contract)
# ======================================================================

STUB = {"packages": {}, "revoked": [], "token": "test-token",
        "advisories": [
            {"id": "ADV-TEST-1", "package": "@acme/vulnlib",
             "affected": "<9.9.9", "fixed_in": "9.9.9",
             "severity": "high", "summary": "test advisory (do not ship)",
             "published_at": "2026-10-07T00:00:00Z", "state": "published"},
        ]}


def stub_reset():
    STUB["packages"] = {}
    STUB["revoked"] = []


def _semantic_match(q, name, pkg, rec):
    """Minimal typed-query matching for kind=semantic: `fn(T) -> R`
    shape, `throws E###`, `capability:x`, `interface I`, `impl I`."""
    import re
    sigs = rec["sigs"]
    fns = [s for s in sigs if s.get("kind") == "fn"]
    for code in re.findall(r"throws\s+(E\d+)", q, re.IGNORECASE):
        code = code.upper()
        if not any(code in [c.upper() for c in s.get("throws", [])]
                   for s in fns):
            return False
    for cap in re.findall(r"capability:([a-zA-Z_]+)", q):
        if cap.lower() not in [c.lower() for c in rec["caps_decl"]]:
            return False
    m = re.search(r"fn\s*\(([^)]*)\)\s*->\s*([^\s]+)", q)
    if m:
        qparams = [p.strip().lower() for p in m.group(1).split(",")
                   if p.strip()]
        qret = m.group(2).strip().lower()
        def shape_ok(s):
            sp = [t.lower() for _, t in s.get("params", [])]
            return (len(sp) == len(qparams)
                    and all(a == b or b == "*" for a, b in zip(sp, qparams))
                    and (s.get("ret") or "").lower() == qret)
        if not any(shape_ok(s) for s in fns):
            return False
    m2 = re.search(r"\binterface\s+(\w+)", q, re.IGNORECASE)
    if m2 and not any(s.get("kind") == "interface"
                      and s.get("name", "").lower() == m2.group(1).lower()
                      for s in sigs):
        return False
    m3 = re.search(r"\bimpl\s+(\w+)", q, re.IGNORECASE)
    if m3 and not any(s.get("kind") == "impl"
                      and s.get("iface", "").lower() == m3.group(1).lower()
                      for s in sigs):
        return False
    if not re.search(r"fn\s*\(|throws\s+E\d+|capability:|\binterface\s+"
                     r"|\bimpl\s+", q, re.IGNORECASE):
        ql = q.lower()
        return ql in name.lower() or ql in pkg["description"].lower()
    return True


def _stub_version_meta(name, ver, rec, base):
    man = rec["manifest"].get("package", {})
    sigs = rec["sigs"]
    return {
        "name": name, "version": ver,
        "sha256": rec["sha256"],
        "license": man.get("license", ""),
        "description": man.get("description", ""),
        "homepage": man.get("homepage", ""),
        "repository": man.get("repository", ""),
        "yanked": rec["yanked"], "yank_reason": rec["yank_reason"],
        "deprecated": STUB["packages"][name].get("deprecated"),
        "capabilities": {"declared": rec["caps_decl"],
                         "verified": rec["caps_decl"]},
        "throws_surface": {s["name"]: s.get("throws", [])
                           for s in sigs if s.get("kind") == "fn"},
        "examples": {"total": 1, "passing": 1},
        "docs_url": f"{base}/docs/{name}/{ver}/",
        "downloads_30d": 42,
    }


class StubHandler(BaseHTTPRequestHandler):
    server_version = "StubRegistry/1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message):
        self._send(code, {"error": {"code": f"R{code}",
                                    "message": message}})

    def _authed(self):
        return (self.headers.get("Authorization")
                == "Bearer " + STUB["token"])

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return None

    def _route(self):
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        return parts, parse_qs(parsed.query)

    # -- GET ----------------------------------------------------------
    def do_GET(self):
        parts, qs = self._route()
        if len(parts) >= 4 and parts[0] == "v1" and parts[1] == "packages":
            # Two-segment form: /v1/packages/:ns/:name (spec §8.2)
            name = parts[2] + "/" + parts[3]
            pkg = STUB["packages"].get(name)
            if pkg is None:
                return self._err(404, f"no such package {name!r}")
            if len(parts) == 4:
                versions = [{"version": v,
                             "yanked": r["yanked"],
                             "published_at": r["published_at"]}
                            for v, r in sorted(pkg["versions"].items())]
                return self._send(200, {
                    "name": name, "description": pkg["description"],
                    "deprecated": pkg.get("deprecated"),
                    "versions": versions})
            ver = parts[4]
            base = f"http://{self.headers.get('Host')}"
            if len(parts) == 5 and ver == "dependents":
                deps = []
                for pname, pdata in STUB["packages"].items():
                    for pver, prec in pdata["versions"].items():
                        pdeps = (prec["manifest"].get("dependencies")
                                 or {})
                        if name in pdeps:
                            deps.append({"name": pname, "version": pver,
                                         "range": pdeps[name]})
                return self._send(200, {"dependents": deps})
            rec = pkg["versions"].get(ver)
            if rec is None:
                return self._err(404, f"{name}@{ver} not found")
            if len(parts) == 5:
                return self._send(200, _stub_version_meta(
                    name, ver, rec, base))
            if len(parts) == 6 and parts[5] == "signatures":
                return self._send(200, {"signatures": rec["sigs"]})
            return self._err(404, "unknown package path")
        if parts[:2] == ["v1", "tarballs"] and len(parts) == 5:
            name = parts[2] + "/" + parts[3]
            ver = parts[4]
            pkg = STUB["packages"].get(name)
            rec = (pkg or {}).get("versions", {}).get(ver)
            if rec is None:
                return self._err(404, f"{name}@{ver} not found")
            return self._send(200, {"sha256": rec["sha256"],
                                    "tarball_b64": rec["tarball_b64"]})
        if parts == ["v1", "search"]:
            q = (qs.get("q") or [""])[0]
            kind = (qs.get("kind") or ["keyword"])[0]
            results = []
            for name, pkg in STUB["packages"].items():
                live = [v for v, r in pkg["versions"].items()
                        if not r["yanked"]]
                if not live:
                    continue
                latest = max(live, key=lambda s: SemVer.parse(s))
                rec = pkg["versions"][latest]
                if kind == "semantic":
                    if not _semantic_match(q, name, pkg, rec):
                        continue
                else:
                    ql = q.lower()
                    if ql and ql not in name.lower() \
                            and ql not in pkg["description"].lower():
                        continue
                results.append({
                    "name": name, "version": latest,
                    "description": pkg["description"],
                    "capabilities": rec["caps_decl"],
                    "examples": {"total": 1, "passing": 1},
                    "kind_echo": kind})
            return self._send(200, {"results": results})
        if parts == ["v1", "advisories"]:
            pname = (qs.get("package") or [None])[0]
            advs = [a for a in STUB["advisories"]
                    if pname is None or a["package"] == pname]
            return self._send(200, {"advisories": advs})
        return self._err(404, "unknown path")

    # -- POST ---------------------------------------------------------
    def do_POST(self):
        parts, _ = self._route()
        body = self._body()
        if body is None:
            return self._err(400, "invalid JSON")
        if parts == ["v1", "publish"]:
            if not self._authed():
                return self._err(401, "bad or missing token")
            man = body.get("manifest", {}).get("package", {})
            name, ver = man.get("name"), man.get("version")
            if not name or not ver:
                return self._err(400, "manifest needs package.name/version")
            try:
                data = base64.b64decode(body.get("tarball_b64", ""))
            except Exception:
                return self._err(400, "bad tarball_b64")
            if hashlib.sha256(data).hexdigest() != body.get("tarball_sha256"):
                return self._err(400, "tarball hash mismatch")
            pkg = STUB["packages"].setdefault(
                name, {"description": man.get("description", ""),
                       "deprecated": None, "versions": {}})
            if ver in pkg["versions"]:
                return self._err(409, f"{name}@{ver} already published")
            pkg["versions"][ver] = {
                "manifest": body.get("manifest", {}),
                "sha256": body["tarball_sha256"],
                "tarball_b64": body["tarball_b64"],
                "sigs": body.get("signatures", []),
                "caps_decl": body.get("capabilities_declared", []),
                "yanked": False, "yank_reason": None,
                "published_at": "2026-10-07T00:00:00Z"}
            base = f"http://{self.headers.get('Host')}"
            return self._send(200, {"ok": True, "name": name,
                                    "version": ver,
                                    "docs_url": f"{base}/docs/{name}/{ver}/"})
        if parts == ["v1", "yank"]:
            if not self._authed():
                return self._err(401, "bad or missing token")
            pkg = STUB["packages"].get(body.get("pkg"))
            rec = (pkg or {}).get("versions", {}).get(body.get("version"))
            if rec is None:
                return self._err(404, "no such version")
            rec["yanked"] = True
            rec["yank_reason"] = body.get("reason", "")
            return self._send(200, {"ok": True})
        if parts == ["v1", "deprecate"]:
            if not self._authed():
                return self._err(401, "bad or missing token")
            pkg = STUB["packages"].get(body.get("pkg"))
            if pkg is None:
                return self._err(404, "no such package")
            pkg["deprecated"] = body.get("message", "")
            return self._send(200, {"ok": True})
        if parts == ["v1", "tokens", "revoke"]:
            if not self._authed():
                return self._err(401, "bad or missing token")
            STUB["revoked"].append(body.get("token_sha256"))
            return self._send(200, {"ok": True})
        return self._err(404, "unknown path")


_STUB_SERVER = None


def stub_url():
    global _STUB_SERVER
    if _STUB_SERVER is None:
        srv = HTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _STUB_SERVER = f"http://127.0.0.1:{srv.server_port}"
    return _STUB_SERVER


# -- integration helpers ------------------------------------------------------

LIB_V1 = '''/// fetches a url
fn get(u: Text) -> Text throws E304 {
    return http.get(u)
}

/// adds two numbers
fn add2(a: Int, b: Int) -> Int {
    return a + b
}
'''


def make_reg_pkg(root, name, version, lib_src=LIB_V1,
                 description="test package", license="MIT",
                 capabilities=None, dependencies=None):
    d = os.path.join(root, "pkg-" + name.replace("/", "_") + "-" + version)
    caps = capabilities if capabilities is not None else {"network": True}
    man = (f'[package]\nname = "{name}"\nversion = "{version}"\n'
           f'description = "{description}"\nlicense = "{license}"\n')
    man += "\n[capabilities]\n" + "".join(
        f"{k} = {'true' if v else 'false'}\n" for k, v in caps.items())
    if dependencies:
        man += "\n[dependencies]\n" + "".join(
            f'"{k}" = "{v}"\n' for k, v in dependencies.items())
    write(os.path.join(d, "misa.pkg"), man)
    write(os.path.join(d, "src", "lib.misa"), lib_src)
    write(os.path.join(d, "examples", "hello.misa"), 'print("example ok")\n')
    return d


def run_cli(args, cwd, env=None):
    e = dict(os.environ)
    e["MISA_HOME"] = ROOT
    e["MISA_REGISTRY"] = stub_url()
    e["HOME"] = tempfile.mkdtemp(prefix="misa-reg-test-home-")
    if env:
        e.update(env)
    p = subprocess.run(MISA + args, cwd=cwd, env=e,
                       capture_output=True, text=True, timeout=180)
    return p, e


def write_creds(home, token="test-token", handle="acme"):
    d = os.path.join(home, ".misa")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "credentials"), "w") as f:
        f.write(f'token = "{token}"\nhandle = "{handle}"\n')
    os.chmod(os.path.join(d, "credentials"), 0o600)


def publish_pkg(d):
    """`pkg publish` with credentials ensured. Returns (proc, env)."""
    p, e = run_cli(["pkg", "publish"], d)
    if p.returncode != 0 and "not logged in" in (p.stderr + p.stdout):
        write_creds(e["HOME"])
        p, e = run_cli(["pkg", "publish"], d, env={"HOME": e["HOME"]})
    return p, e


@test("login/logout: token stored 0600, revoke hits server")
def _():
    import builtins
    stub_reset()
    home = tempfile.mkdtemp()
    old_home = os.environ.get("HOME")
    old_input, old_getpass = builtins.input, getpass.getpass
    os.environ["HOME"] = home
    try:
        builtins.input = lambda prompt="": "acme"
        getpass.getpass = lambda prompt="": "test-token"
        eq(P.cmd_login(), 0)
        creds = os.path.join(home, ".misa", "credentials")
        assert os.path.isfile(creds)
        eq(oct(os.stat(creds).st_mode & 0o777), "0o600")
        assert 'token = "test-token"' in open(creds).read()
        # logout without revoke just forgets
        eq(P.cmd_logout(False), 0)
        assert not os.path.exists(creds)
        # again: revoke path (needs a token present)
        eq(P.cmd_login(), 0)
        old_reg = os.environ.get("MISA_REGISTRY")
        os.environ["MISA_REGISTRY"] = stub_url()
        try:
            eq(P.cmd_logout(True), 0)
        finally:
            if old_reg is None:
                os.environ.pop("MISA_REGISTRY", None)
            else:
                os.environ["MISA_REGISTRY"] = old_reg
        assert not os.path.exists(creds)
        want = hashlib.sha256(b"test-token").hexdigest()
        assert want in STUB["revoked"], STUB["revoked"]
    finally:
        builtins.input = old_input
        getpass.getpass = old_getpass
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


@test("publish: dry-run passes gates without uploading")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/router", "1.0.0")
    p, e = run_cli(["pkg", "publish", "--dry-run"], d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    assert "dry run" in p.stdout, p.stdout
    assert "gate" in p.stdout
    eq(STUB["packages"], {}, "dry run must not upload")


@test("publish -> add -> install (happy path)")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/router", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    assert "published @acme/router@1.0.0" in p.stdout, p.stdout
    # [registry] metadata written back to misa.pkg
    man_raw = open(os.path.join(d, "misa.pkg")).read()
    assert "[registry]" in man_raw and "tarball_sha256" in man_raw, man_raw

    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, e2 = run_cli(["pkg", "add", "@acme/router"], proj)
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    assert "resolving @acme/router@^1.0.0 -> 1.0.0" in p2.stdout, p2.stdout
    assert "capabilities: network" in p2.stdout, p2.stdout
    assert "throws surface: E304" in p2.stdout, p2.stdout
    assert "examples: 1/1 passing" in p2.stdout, p2.stdout
    assert os.path.isfile(os.path.join(
        proj, "misa_pkgs", "@acme", "router", "misa.pkg"))
    lock_raw = open(os.path.join(proj, "misa.lock")).read()
    assert "version = 2" in lock_raw, lock_raw
    assert 'source = "registry"' in lock_raw, lock_raw
    assert "sha256" in lock_raw and "capabilities" in lock_raw, lock_raw

    # install reproduces the tree after wiping
    before = {}
    pkgdir = os.path.join(proj, "misa_pkgs", "@acme", "router")
    for dp, _, fns in os.walk(pkgdir):
        for fn in fns:
            fp = os.path.join(dp, fn)
            before[os.path.relpath(fp, pkgdir)] = open(fp, "rb").read()
    shutil.rmtree(os.path.join(proj, "misa_pkgs"))
    p3, _ = run_cli(["pkg", "install"], proj,
                    env={"HOME": e2["HOME"]})
    eq(p3.returncode, 0, p3.stderr + p3.stdout)
    after = {}
    for dp, _, fns in os.walk(pkgdir):
        for fn in fns:
            fp = os.path.join(dp, fn)
            after[os.path.relpath(fp, pkgdir)] = open(fp, "rb").read()
    eq(after, before)


@test("install: sha256 mismatch fails clean")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/safe", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, e2 = run_cli(["pkg", "add", "@acme/safe"], proj)
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    # tamper with the locked hash
    lp = os.path.join(proj, "misa.lock")
    raw = open(lp).read()
    import re
    raw2 = re.sub(r'sha256 = "[0-9a-f]+"', 'sha256 = "' + "0" * 64 + '"',
                  raw)
    assert raw2 != raw
    open(lp, "w").write(raw2)
    shutil.rmtree(os.path.join(proj, "misa_pkgs"))
    p3, _ = run_cli(["pkg", "install"], proj, env={"HOME": e2["HOME"]})
    assert p3.returncode != 0, p3.stdout + p3.stderr
    assert "expected" in p3.stderr and "got" in p3.stderr, p3.stderr
    assert "0" * 64 in p3.stderr


@test("search: keyword + typed query passthrough")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/router", "1.0.0",
                     description="typed http router")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    p2, _ = run_cli(["pkg", "search", "router", "--json"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr)
    res = json.loads(p2.stdout)
    eq(res["schema"], "misa.pkg.v1")
    assert any(r["name"] == "@acme/router" for r in res["results"]), res
    # typed query auto-selects kind=semantic and is passed through
    p3, _ = run_cli(
        ["pkg", "search", "fn(Text) -> Text throws E304", "--json"], tmp,
        env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr)
    res3 = json.loads(p3.stdout)
    eq(res3["kind"], "semantic")
    assert any(r["name"] == "@acme/router" for r in res3["results"]), res3


@test("show --json: signatures + throws surface + schema")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/router", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    p2, _ = run_cli(["pkg", "show", "@acme/router", "--json"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr)
    res = json.loads(p2.stdout)
    eq(res["schema"], "misa.pkg.v1")
    eq(res["version"], "1.0.0")
    eq(res["throws_surface"]["get"], ["E304"])
    names = {(s["kind"], s["name"]) for s in res["signatures"]}
    assert ("fn", "get") in names and ("fn", "add2") in names, names
    get_sig = next(s for s in res["signatures"] if s["name"] == "get")
    eq(get_sig["params"], [["u", "Text"]])
    eq(get_sig["ret"], "Text")
    p3, _ = run_cli(["pkg", "show", "@acme/router"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr)
    assert "throws surface" in p3.stdout, p3.stdout


@test("yank: version disappears from fresh resolution")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/doomed", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    p2, _ = run_cli(
        ["pkg", "yank", "@acme/doomed@1.0.0", "--reason", "test yank"],
        tmp, env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    assert "yanked @acme/doomed@1.0.0" in p2.stdout, p2.stdout
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p3, _ = run_cli(["pkg", "add", "@acme/doomed"], proj,
                    env={"HOME": e["HOME"]})
    assert p3.returncode != 0, p3.stdout + p3.stderr
    assert "no published version" in p3.stderr, p3.stderr
    p4, _ = run_cli(["pkg", "show", "@acme/doomed@1.0.0", "--json"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p4.returncode, 0, p4.stderr)
    assert json.loads(p4.stdout)["yanked"] is True


@test("publish: breaking self-check rejects under-bumped minor/patch")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/breaker", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    # 1.0.1 removes `add2` -> computed major, declared patch -> reject
    lib2 = LIB_V1.replace('''/// adds two numbers
fn add2(a: Int, b: Int) -> Int {
    return a + b
}
''', '').rstrip("\n") + "\n"
    assert "add2" not in lib2
    d2 = make_reg_pkg(tmp, "@acme/breaker", "1.0.1", lib_src=lib2)
    p2, _ = run_cli(["pkg", "publish"], d2, env={"HOME": e["HOME"]})
    assert p2.returncode != 0, p2.stdout + p2.stderr
    assert "major" in p2.stderr and "add2" in p2.stderr, p2.stderr
    # 2.0.0 declares the major -> accepted
    d3 = make_reg_pkg(tmp, "@acme/breaker", "2.0.0", lib_src=lib2)
    p3, _ = run_cli(["pkg", "publish"], d3, env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr + p3.stdout)
    # diff shows the removal as breaking
    p4, _ = run_cli(["pkg", "diff", "@acme/breaker", "1.0.0", "2.0.0"],
                    tmp, env={"HOME": e["HOME"]})
    eq(p4.returncode, 0, p4.stderr)
    assert "minimum bump: major" in p4.stdout, p4.stdout
    assert "add2" in p4.stdout and "removed" in p4.stdout, p4.stdout


@test("publish: non-breaking minor passes the self-check")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/grower", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    lib2 = LIB_V1 + '''
/// subtracts
fn sub2(a: Int, b: Int) -> Int {
    return a - b
}
'''
    d2 = make_reg_pkg(tmp, "@acme/grower", "1.1.0", lib_src=lib2)
    p2, _ = run_cli(["pkg", "publish"], d2, env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    assert "computed minimum minor" in p2.stdout, p2.stdout


@test("outdated + update: re-resolve ranges")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/upd", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, _ = run_cli(["pkg", "add", "@acme/upd"], proj,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    # publish a compatible minor
    lib2 = LIB_V1 + '''
/// extra
fn extra() -> Int {
    return 3
}
'''
    d2 = make_reg_pkg(tmp, "@acme/upd", "1.1.0", lib_src=lib2)
    p3, _ = run_cli(["pkg", "publish"], d2, env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr + p3.stdout)
    p4, _ = run_cli(["pkg", "outdated", "--json"], proj,
                    env={"HOME": e["HOME"]})
    eq(p4.returncode, 0, p4.stderr)
    rows = json.loads(p4.stdout)["packages"]
    eq(len(rows), 1)
    eq(rows[0]["latest_compatible"], "1.1.0")
    p5, _ = run_cli(["pkg", "update", "@acme/upd"], proj,
                    env={"HOME": e["HOME"]})
    eq(p5.returncode, 0, p5.stderr + p5.stdout)
    assert "1.0.0 -> 1.1.0" in p5.stdout, p5.stdout
    entries = {x["name"]: x for x in read_lock(os.path.join(proj,
                                                            "misa.lock"))}
    eq(entries["@acme/upd"]["version"], "1.1.0")


@test("audit: high advisory fails, clean passes")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/vulnlib", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, _ = run_cli(["pkg", "add", "@acme/vulnlib"], proj,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    p3, _ = run_cli(["pkg", "audit"], proj, env={"HOME": e["HOME"]})
    assert p3.returncode != 0, p3.stdout + p3.stderr
    assert "HIGH" in p3.stdout and "ADV-TEST-1" in p3.stdout, p3.stdout
    p4, _ = run_cli(["pkg", "audit", "--json"], proj,
                    env={"HOME": e["HOME"]})
    assert p4.returncode != 0
    findings = json.loads(p4.stdout)["findings"]
    eq(findings[0]["advisory"]["id"], "ADV-TEST-1")
    # a project without the vulnerable dep audits clean
    proj2 = os.path.join(tmp, "proj2")
    os.makedirs(proj2)
    p5, _ = run_cli(["pkg", "audit"], proj2, env={"HOME": e["HOME"]})
    eq(p5.returncode, 0, p5.stderr + p5.stdout)


@test("vendor: verified tarballs + offline install")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/vend", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, _ = run_cli(["pkg", "add", "@acme/vend"], proj,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    p3, _ = run_cli(["pkg", "vendor"], proj, env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr + p3.stdout)
    vp = os.path.join(proj, "vendor", "acme__vend-1.0.0.tar.gz")
    assert os.path.isfile(vp), p3.stdout
    # offline install from vendor/ after wiping
    shutil.rmtree(os.path.join(proj, "misa_pkgs"))
    p4, _ = run_cli(["pkg", "install", "--offline"], proj,
                    env={"HOME": e["HOME"]})
    eq(p4.returncode, 0, p4.stderr + p4.stdout)
    assert os.path.isfile(os.path.join(
        proj, "misa_pkgs", "@acme", "vend", "misa.pkg"))


@test("dependents + deprecate")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/router", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    app = make_reg_pkg(tmp, "@acme/app", "1.0.0",
                       dependencies={"@acme/router": "^1.0.0"})
    p2, _ = run_cli(["pkg", "publish"], app, env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    p3, _ = run_cli(["pkg", "dependents", "@acme/router", "--json"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr)
    deps = json.loads(p3.stdout)["dependents"]
    assert any(x["name"] == "@acme/app" for x in deps), deps
    p4, _ = run_cli(["pkg", "deprecate", "@acme/router",
                     "--message", "use @acme/router2"], tmp,
                    env={"HOME": e["HOME"]})
    eq(p4.returncode, 0, p4.stderr + p4.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p5, _ = run_cli(["pkg", "add", "@acme/router@=1.0.0"], proj,
                    env={"HOME": e["HOME"]})
    eq(p5.returncode, 0, p5.stderr + p5.stdout)
    assert "deprecated" in p5.stdout, p5.stdout


@test("add: explicit ranges resolve correctly")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    for ver in ("1.0.0", "1.1.0", "2.0.0"):
        d = make_reg_pkg(tmp, "@acme/rng", ver)
        p, e = publish_pkg(d)
        eq(p.returncode, 0, p.stderr + p.stdout)
    for i, (arg, want) in enumerate((("@acme/rng@^1.0.0", "1.1.0"),
                                      ("@acme/rng@~1.0.0", "1.0.0"),
                                      ("@acme/rng@=2.0.0", "2.0.0"),
                                      ("@acme/rng", "2.0.0"))):
        proj = os.path.join(tmp, f"p-{i}-" + want.replace(".", ""))
        os.makedirs(proj)
        p2, _ = run_cli(["pkg", "add", arg], proj,
                        env={"HOME": e["HOME"]})
        eq(p2.returncode, 0, f"{arg}: {p2.stderr}{p2.stdout}")
        assert f"-> {want} " in p2.stdout or f"-> {want}\n" in p2.stdout \
            or p2.stdout.rstrip().endswith(f"-> {want}") \
            or f"-> {want} (" in p2.stdout, p2.stdout


@test("list --json shows capabilities; remove prunes @org dir")
def _():
    stub_reset()
    tmp = tempfile.mkdtemp()
    d = make_reg_pkg(tmp, "@acme/lister", "1.0.0")
    p, e = publish_pkg(d)
    eq(p.returncode, 0, p.stderr + p.stdout)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p2, _ = run_cli(["pkg", "add", "@acme/lister"], proj,
                    env={"HOME": e["HOME"]})
    eq(p2.returncode, 0, p2.stderr + p2.stdout)
    p3, _ = run_cli(["pkg", "list", "--json"], proj,
                    env={"HOME": e["HOME"]})
    eq(p3.returncode, 0, p3.stderr)
    pkgs = json.loads(p3.stdout)["packages"]
    eq(len(pkgs), 1)
    eq(pkgs[0]["name"], "@acme/lister")
    eq(pkgs[0]["capabilities"], ["network"])
    p4, _ = run_cli(["pkg", "list"], proj, env={"HOME": e["HOME"]})
    assert "[network]" in p4.stdout, p4.stdout
    p5, _ = run_cli(["pkg", "remove", "@acme/lister"], proj,
                    env={"HOME": e["HOME"]})
    eq(p5.returncode, 0, p5.stderr + p5.stdout)
    assert not os.path.exists(os.path.join(proj, "misa_pkgs", "@acme"))


def main():
    print(f"\n{PASS}/{PASS + FAIL} passed")
    if FAILURES:
        print("failures:")
        for n, e in FAILURES:
            print(f"  {n}: {e}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
