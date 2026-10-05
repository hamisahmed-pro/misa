#!/usr/bin/env python3
"""Misa package manager tests (spec/PKG.md section 9). Zero dependencies.

Run: python3 tests/pkg/run_pkg_tests.py   (from the repo root)

Covers: manifest validation, resolution order, entry vs module imports,
missing package/module errors (E112), bad manifests (E113), cycles (E111),
lockfile round-trip, `pkg init` scaffold, `pkg add/list/remove`, and the
stdlib test files (each must `misa run` clean).
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "impl"))

from misa.loader import load as misa_load
from misa.pkg import (read_manifest, PkgError, PkgResolveError,
                      resolve_package)

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
            FAILURES.append((name, str(e)[:300]))
            print(f" FAIL {name}: {e}")
        except Exception as e:
            FAIL += 1
            FAILURES.append((name, f"{type(e).__name__}: {e}"[:300]))
            print(f" FAIL {name}: {type(e).__name__}: {e}")
    return deco


def eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r}, got {a!r}"


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def make_pkg(root, name, version="0.1.0", files=None, lib=None):
    """Create a package dir with manifest + src files. Returns the dir."""
    d = os.path.join(root, name)
    man = (f'[package]\nname = "{name}"\nversion = "{version}"\n'
           f'description = ""\n')
    if lib:
        man += f'lib = "{lib}"\n'
    write(os.path.join(d, "misa.pkg"), man)
    write(os.path.join(d, "src", "lib.misa"),
          f'// {name} entry\nfn {name.replace("-", "_")}_hello() -> Text '
          '{ return "hi" }\n')
    for rel, content in (files or {}).items():
        write(os.path.join(d, "src", rel), content)
    return d


def run_cli(args, cwd, env=None):
    e = dict(os.environ)
    e["MISA_HOME"] = ROOT
    # hermetic global package dir (tests must not see the real ~/.misa)
    e.setdefault("HOME", tempfile.mkdtemp(prefix="misa-test-home-"))
    if env:
        e.update(env)
    p = subprocess.run(MISA + args, cwd=cwd, env=e,
                       capture_output=True, text=True, timeout=120)
    return p


def load_main(tmp, src):
    main = os.path.join(tmp, "main.misa")
    write(main, src)
    env_home = os.path.join(tmp, "home")
    os.makedirs(env_home, exist_ok=True)
    old_home = os.environ.get("HOME")
    old_mh = os.environ.get("MISA_HOME")
    os.environ["HOME"] = env_home
    os.environ["MISA_HOME"] = ROOT
    try:
        return misa_load(main)
    finally:
        if old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old_home
        if old_mh is None:
            os.environ.pop("MISA_HOME", None)
        else:
            os.environ["MISA_HOME"] = old_mh


def codes(errors):
    return [e["code"] for e in errors]


# -- manifest validation ------------------------------------------------

@test("manifest: good parses")
def _():
    d = make_pkg(tempfile.mkdtemp(), "good-pkg", "1.2.3")
    m = read_manifest(d)
    eq(m["name"], "good-pkg")
    eq(m["version"], "1.2.3")
    eq(m["lib"], "src/lib.misa")


@test("manifest: bad version -> PkgError")
def _():
    d = make_pkg(tempfile.mkdtemp(), "badver", "1.2")
    try:
        read_manifest(d)
    except PkgError as e:
        assert "version" in str(e), e
        return
    raise AssertionError("expected PkgError")


@test("manifest: missing name -> PkgError")
def _():
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "noname")
    write(os.path.join(d, "misa.pkg"), '[package]\nversion = "0.1.0"\n')
    try:
        read_manifest(d)
    except PkgError as e:
        assert "name" in str(e), e
        return
    raise AssertionError("expected PkgError")


@test("manifest: bad toml -> PkgError")
def _():
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "badtoml")
    write(os.path.join(d, "misa.pkg"), "[package\nname = oops\n")
    try:
        read_manifest(d)
    except PkgError as e:
        assert "TOML" in str(e), e
        return
    raise AssertionError("expected PkgError")


@test("manifest: missing [package] -> PkgError")
def _():
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "nopkg")
    write(os.path.join(d, "misa.pkg"), '[other]\nname = "x"\n')
    try:
        read_manifest(d)
    except PkgError as e:
        assert "[package]" in str(e), e
        return
    raise AssertionError("expected PkgError")


@test("manifest: bad name chars -> PkgError")
def _():
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "Bad_Name")
    write(os.path.join(d, "misa.pkg"),
          '[package]\nname = "Bad_Name"\nversion = "0.1.0"\n')
    try:
        read_manifest(d)
    except PkgError:
        return
    raise AssertionError("expected PkgError")


# -- resolution ---------------------------------------------------------

@test("resolve: stdlib module")
def _():
    tmp = tempfile.mkdtemp()
    merged, errors, order, _ = load_main(tmp, 'import "std/strings"\n')
    eq(errors, [], errors)
    assert any(p.endswith(os.path.join("stdlib", "std", "src",
                                       "strings.misa")) for p in order), order


@test("resolve: entry vs module")
def _():
    tmp = tempfile.mkdtemp()
    _, errors, order, _ = load_main(tmp, 'import "std"\n')
    eq(errors, [], errors)
    # entry pulls every module transitively
    assert any(p.endswith("src/lib.misa") for p in order), order
    assert any(p.endswith("src/strings.misa") for p in order), order
    tmp2 = tempfile.mkdtemp()
    _, errors2, order2, _ = load_main(tmp2, 'import "std/json"\n')
    eq(errors2, [], errors2)
    assert any(p.endswith("src/json.misa") for p in order2), order2
    assert not any(p.endswith("src/strings.misa") for p in order2), order2


@test("resolve: nearest misa_pkgs wins (walk-up)")
def _():
    tmp = tempfile.mkdtemp()
    make_pkg(os.path.join(tmp, "misa_pkgs"), "orderpkg", "1.0.0")
    sub = os.path.join(tmp, "sub")
    make_pkg(os.path.join(sub, "misa_pkgs"), "orderpkg", "2.0.0")
    main = os.path.join(sub, "main.misa")
    write(main, 'import "orderpkg"\n')
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = os.path.join(tmp, "home")
    os.makedirs(os.environ["HOME"], exist_ok=True)
    try:
        _, errors, order, _ = misa_load(main)
    finally:
        if old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old_home
    eq(errors, [], errors)
    assert any(os.path.join("sub", "misa_pkgs") in p for p in order), order


@test("resolve: global ~/.misa/pkgs fallback")
def _():
    tmp = tempfile.mkdtemp()
    home = os.path.join(tmp, "home")
    make_pkg(os.path.join(home, ".misa", "pkgs"), "globalpkg", "9.9.9")
    merged, errors, order, _ = load_main(tmp, 'import "globalpkg"\n')
    # load_main already points HOME at tmp/home
    eq(errors, [], errors)
    assert any("globalpkg" in p for p in order), order


@test("resolve: missing package -> E112")
def _():
    tmp = tempfile.mkdtemp()
    _, errors, _, _ = load_main(tmp, 'import "no-such-pkg"\n')
    eq(codes(errors), ["E112"])
    assert "cannot find package" in errors[0]["message"], errors[0]
    assert "searched" in errors[0]["hint"], errors[0]


@test("resolve: missing module -> E112")
def _():
    tmp = tempfile.mkdtemp()
    _, errors, _, _ = load_main(tmp, 'import "std/bogus"\n')
    eq(codes(errors), ["E112"])
    assert "cannot find module" in errors[0]["message"], errors[0]


@test("resolve: bad manifest -> E113")
def _():
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "misa_pkgs", "badpkg")
    write(os.path.join(d, "misa.pkg"),
          '[package]\nname = "badpkg"\nversion = "x"\n')
    _, errors, _, _ = load_main(tmp, 'import "badpkg"\n')
    eq(codes(errors), ["E113"])
    assert "version" in errors[0]["message"], errors[0]


@test("resolve: name mismatch -> E113")
def _():
    tmp = tempfile.mkdtemp()
    make_pkg(os.path.join(tmp, "misa_pkgs"), "realname")
    os.rename(os.path.join(tmp, "misa_pkgs", "realname"),
              os.path.join(tmp, "misa_pkgs", "wrongname"))
    _, errors, _, _ = load_main(tmp, 'import "wrongname"\n')
    eq(codes(errors), ["E113"])
    assert "mismatch" in errors[0]["message"], errors[0]


@test("resolve: cycle across packages -> E111")
def _():
    tmp = tempfile.mkdtemp()
    pkdir = os.path.join(tmp, "misa_pkgs")
    a = make_pkg(pkdir, "pkga")
    b = make_pkg(pkdir, "pkgb")
    with open(os.path.join(a, "src", "lib.misa"), "a") as f:
        f.write('import "pkgb"\n')
    with open(os.path.join(b, "src", "lib.misa"), "a") as f:
        f.write('import "pkga"\n')
    _, errors, _, _ = load_main(tmp, 'import "pkga"\n')
    assert "E111" in codes(errors), errors


@test("resolve: import forms (file vs package)")
def _():
    tmp = tempfile.mkdtemp()
    write(os.path.join(tmp, "ok.misa"), 'print("ok")\n')
    # relative file still works
    _, errors, _, _ = load_main(tmp, 'import "ok.misa"\nprint("x")\n')
    eq(errors, [], errors)
    # ./ without .misa is still E112
    _, errors, _, _ = load_main(tmp, 'import "./ok"\n')
    eq(codes(errors), ["E112"])
    assert "must end in .misa" in errors[0]["message"], errors[0]
    # bare name is now a package import, not the old E112
    _, errors, _, _ = load_main(tmp, 'import "definitely-missing"\n')
    eq(codes(errors), ["E112"])
    assert "cannot find package" in errors[0]["message"], errors[0]


@test("resolve: path escape blocked -> E112")
def _():
    tmp = tempfile.mkdtemp()
    _, errors, _, _ = load_main(tmp, 'import "std/../../secret"\n')
    eq(codes(errors), ["E112"])
    assert "escapes" in errors[0]["message"], errors[0]


@test("resolve_package: invalid name -> E112")
def _():
    try:
        resolve_package("Bad_Name", tempfile.mkdtemp())
    except PkgResolveError as e:
        eq(e.code, "E112")
        return
    raise AssertionError("expected PkgResolveError")


# -- CLI ----------------------------------------------------------------

@test("pkg init: scaffold")
def _():
    tmp = tempfile.mkdtemp()
    p = run_cli(["pkg", "init", "my-pkg"], tmp)
    eq(p.returncode, 0, p.stderr)
    assert os.path.isfile(os.path.join(tmp, "misa.pkg")), "misa.pkg"
    assert os.path.isfile(os.path.join(tmp, "src", "lib.misa")), "lib.misa"
    c = run_cli(["check", os.path.join("src", "lib.misa")], tmp)
    eq(c.returncode, 0, c.stderr)
    # second init refuses without --force
    p2 = run_cli(["pkg", "init", "my-pkg"], tmp)
    assert p2.returncode != 0, "should refuse overwrite"
    p3 = run_cli(["pkg", "init", "my-pkg", "--force"], tmp)
    eq(p3.returncode, 0, p3.stderr)


@test("pkg init: bad name rejected")
def _():
    tmp = tempfile.mkdtemp()
    p = run_cli(["pkg", "init", "Bad_Name"], tmp)
    assert p.returncode != 0


@test("pkg add/list/remove (path source)")
def _():
    tmp = tempfile.mkdtemp()
    src = make_pkg(tmp, "srcpkg", "0.2.0",
                   files={"util.misa": 'fn util_fn() -> Text { return "u" }\n'})
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p = run_cli(["pkg", "add", src], proj)
    eq(p.returncode, 0, p.stderr)
    assert os.path.isfile(os.path.join(proj, "misa_pkgs", "srcpkg",
                                       "misa.pkg"))
    assert os.path.isfile(os.path.join(proj, "misa.lock")), "lock written"
    lock = open(os.path.join(proj, "misa.lock")).read()
    assert 'name = "srcpkg"' in lock, lock
    assert "path+" in lock and src in lock, lock
    # list shows it
    lp = run_cli(["pkg", "list"], proj)
    eq(lp.returncode, 0, lp.stderr)
    assert "srcpkg 0.2.0" in lp.stdout, lp.stdout
    # module import resolves through the installed package
    write(os.path.join(proj, "main.misa"),
          'import "srcpkg/util"\nprint(util_fn())\n')
    rp = run_cli(["run", "main.misa"], proj)
    eq(rp.returncode, 0, rp.stderr)
    assert rp.stdout.strip() == "u", rp.stdout
    # add again without --force fails
    p2 = run_cli(["pkg", "add", src], proj)
    assert p2.returncode != 0
    # remove
    rm = run_cli(["pkg", "remove", "srcpkg"], proj)
    eq(rm.returncode, 0, rm.stderr)
    assert not os.path.exists(os.path.join(proj, "misa_pkgs", "srcpkg"))
    lock2 = open(os.path.join(proj, "misa.lock")).read() \
        if os.path.exists(os.path.join(proj, "misa.lock")) else ""
    assert "srcpkg" not in lock2
    # remove missing fails
    rm2 = run_cli(["pkg", "remove", "srcpkg"], proj)
    assert rm2.returncode != 0


@test("pkg: lockfile round-trip (add -> wipe -> install)")
def _():
    tmp = tempfile.mkdtemp()
    src = make_pkg(tmp, "roundpkg", "1.0.0",
                   files={"a.misa": 'fn ra() -> Int { return 1 }\n',
                          "sub/b.misa": 'fn rb() -> Int { return 2 }\n'})
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    eq(run_cli(["pkg", "add", src], proj).returncode, 0)
    before = {}
    pkgdir = os.path.join(proj, "misa_pkgs", "roundpkg")
    for dp, _, fns in os.walk(pkgdir):
        for fn in fns:
            fp = os.path.join(dp, fn)
            before[os.path.relpath(fp, pkgdir)] = open(fp).read()
    shutil.rmtree(os.path.join(proj, "misa_pkgs"))
    eq(run_cli(["pkg", "install"], proj).returncode, 0)
    after = {}
    for dp, _, fns in os.walk(pkgdir):
        for fn in fns:
            fp = os.path.join(dp, fn)
            after[os.path.relpath(fp, pkgdir)] = open(fp).read()
    eq(after, before, "install must reproduce the tree")


@test("pkg install: no lockfile is a no-op")
def _():
    tmp = tempfile.mkdtemp()
    p = run_cli(["pkg", "install"], tmp)
    eq(p.returncode, 0, p.stderr)
    assert "no misa.lock" in p.stdout, p.stdout


@test("pkg add: git source (local repo)")
def _():
    if shutil.which("git") is None:
        print("    (skip: no git)")
        return
    tmp = tempfile.mkdtemp()
    repo = os.path.join(tmp, "repo")
    make_pkg(tmp, "repo", "0.1.0")  # creates tmp/repo
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
               HOME=os.path.join(tmp, "home"))
    os.makedirs(env["HOME"], exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=True, env=env,
                   timeout=60)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"],
                   check=True, env=env, timeout=60)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"],
                   check=True, env=env, timeout=60)
    subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env,
                   timeout=60)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True,
                   env=env, timeout=60)
    subprocess.run(["git", "-C", repo, "tag", "v1"], check=True, env=env,
                   timeout=60)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    p = run_cli(["pkg", "add", f"file://{repo}#v1"], proj, env=env)
    eq(p.returncode, 0, p.stderr + p.stdout)
    assert os.path.isfile(os.path.join(proj, "misa_pkgs", "repo",
                                       "misa.pkg"))
    assert not os.path.exists(os.path.join(proj, "misa_pkgs", "repo",
                                           ".git")), ".git stripped"
    lock = open(os.path.join(proj, "misa.lock")).read()
    assert "git+file://" in lock and "@" in lock, lock


# -- stdlib ---------------------------------------------------------------

@test("stdlib: all test files run clean")
def _():
    tdir = os.path.join(ROOT, "stdlib", "std", "tests")
    files = sorted(f for f in os.listdir(tdir) if f.endswith(".misa"))
    assert len(files) >= 5, files
    for f in files:
        p = run_cli(["run", os.path.join(tdir, f)], ROOT)
        eq(p.returncode, 0, f"{f}: {p.stderr}")


@test("stdlib: entry import loads everything")
def _():
    tmp = tempfile.mkdtemp()
    merged, errors, order, _ = load_main(tmp, 'import "std"\nprint("x")\n')
    eq(errors, [], errors)
    for mod in ("strings", "lists", "json", "http", "result"):
        assert any(p.endswith(os.path.join("src", mod + ".misa"))
                   for p in order), (mod, order)


def main():
    # The @test decorators already executed at definition time;
    # main() just reports.
    print(f"\n{PASS}/{PASS + FAIL} passed")
    if FAILURES:
        print("failures:")
        for n, e in FAILURES:
            print(f"  {n}: {e}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # The @test decorators execute at import time; main() just reports.
    sys.exit(main())
