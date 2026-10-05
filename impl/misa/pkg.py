"""Misa package manager — `misa pkg` (spec/PKG.md).

Layout:
    my-pkg/
      misa.pkg            # TOML manifest ([package] name/version/...)
      src/
        lib.misa          # entry point for `import "my-pkg"`
        sub/thing.misa    # `import "my-pkg/sub/thing"`

Resolution (used by the loader, so every entry point gets it):
  1. walk up from the importing file: <dir>/misa_pkgs/<name>
  2. ~/.misa/pkgs/<name>
  3. <misa-home>/stdlib/<name>   (MISA_HOME env overrides the toolchain dir)

Errors: E112 (not found), E113 (bad manifest).
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


NAME_RE = re.compile(r"^[a-z0-9-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
LOCK = "misa.lock"
PKGS_DIR = "misa_pkgs"

USAGE = """usage:
  misa pkg init [name] [--force]   scaffold misa.pkg + src/lib.misa here
  misa pkg add <git-url|path> [--force]
                                   install into misa_pkgs/, update misa.lock
  misa pkg install                 install everything in misa.lock
  misa pkg list                    installed packages
  misa pkg remove <name>           remove from misa_pkgs/ and misa.lock"""


# -- toolchain layout -------------------------------------------------

def misa_home() -> str:
    """Toolchain root: <repo>/; MISA_HOME overrides (used by tests)."""
    return os.environ.get("MISA_HOME") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stdlib_dir() -> str:
    return os.path.join(misa_home(), "stdlib")


# -- errors -----------------------------------------------------------

class PkgError(Exception):
    """Bad manifest / bad package dir. Message is the E113 text."""


class PkgResolveError(Exception):
    """Failed import resolution. Carries the E-code for the loader."""
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


class PkgCliError(Exception):
    """User-facing `misa pkg` failure."""


# -- manifest ---------------------------------------------------------

def read_manifest(pkg_dir: str) -> dict:
    """Parse and validate <pkg_dir>/misa.pkg. Raises PkgError (E113)."""
    path = os.path.join(pkg_dir, "misa.pkg")
    if not os.path.isfile(path):
        raise PkgError(f"no misa.pkg in '{pkg_dir}'")
    if tomllib is None:
        raise PkgError("reading misa.pkg needs Python 3.11+ (tomllib)")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise PkgError(f"invalid TOML in misa.pkg: {e}")
    pkg = data.get("package")
    if not isinstance(pkg, dict):
        raise PkgError("misa.pkg: missing [package] table")
    name = pkg.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PkgError(
            f"misa.pkg: bad package name {name!r} "
            "(lowercase letters, digits, hyphens)")
    version = pkg.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise PkgError(
            f"misa.pkg: bad version {version!r} (semver MAJOR.MINOR.PATCH)")
    lib = pkg.get("lib", "src/lib.misa")
    if not isinstance(lib, str) or not lib:
        raise PkgError("misa.pkg: 'lib' must be a non-empty path string")
    return {"name": name, "version": version, "lib": lib,
            "description": pkg.get("description", "")}


# -- resolution -------------------------------------------------------

def _candidate_dirs(name: str, from_dir: str):
    cands = []
    d = os.path.abspath(from_dir)
    while True:
        cands.append(os.path.join(d, PKGS_DIR, name))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    cands.append(os.path.join(os.path.expanduser("~"), ".misa", "pkgs", name))
    cands.append(os.path.join(stdlib_dir(), name))
    return cands


def _within(root: str, path: str) -> bool:
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    return path == root or path.startswith(root + os.sep)


def resolve_package(spec: str, from_dir: str) -> str:
    """Resolve a package import (`my-pkg` or `my-pkg/sub/thing`).

    Returns the absolute .misa path. Raises PkgResolveError with
    code E112 (not found) or E113 (bad manifest).
    """
    parts = spec.split("/")
    name = parts[0]
    rest = parts[1:]
    if not NAME_RE.match(name):
        raise PkgResolveError(
            "E112", f"invalid package name '{name}'",
            hint="package names use lowercase letters, digits, hyphens")
    searched = []
    for cand in _candidate_dirs(name, from_dir):
        searched.append(cand)
        if not os.path.isfile(os.path.join(cand, "misa.pkg")):
            continue
        try:
            man = read_manifest(cand)
        except PkgError as e:
            raise PkgResolveError("E113", str(e),
                                  hint=f"in {cand}{os.sep}misa.pkg")
        if man["name"] != name:
            raise PkgResolveError(
                "E113",
                f"package name mismatch: directory '{name}' but "
                f"misa.pkg says '{man['name']}'",
                hint=f"in {cand}{os.sep}misa.pkg")
        if rest:
            rel = os.path.join("src", *rest)
            if not rel.endswith(".misa"):
                rel += ".misa"
        else:
            rel = man["lib"]
        target = os.path.normpath(os.path.join(cand, rel))
        if not _within(cand, target):
            raise PkgResolveError(
                "E112", f"import '{spec}' escapes package '{name}'")
        if not os.path.isfile(target):
            which = "/".join(rest) if rest else man["lib"]
            raise PkgResolveError(
                "E112",
                f"cannot find module '{which}' in package '{name}'",
                hint=f"looked in {target}")
        return target
    raise PkgResolveError(
        "E112", f"cannot find package '{name}'",
        hint="searched:\n  " + "\n  ".join(searched))


# -- lockfile ---------------------------------------------------------

def _tq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_lock(path: str = LOCK) -> list:
    if not os.path.isfile(path) or tomllib is None:
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)
    pkgs = data.get("package", [])
    return pkgs if isinstance(pkgs, list) else []


def write_lock(entries: list, path: str = LOCK):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# generated by `misa pkg`; do not edit by hand\n")
        for e in entries:
            f.write("[[package]]\n")
            f.write(f"name = {_tq(e['name'])}\n")
            f.write(f"version = {_tq(e['version'])}\n")
            f.write(f"source = {_tq(e['source'])}\n\n")


def lock_set(name: str, version: str, source: str, path: str = LOCK):
    entries = [e for e in read_lock(path) if e.get("name") != name]
    entries.append({"name": name, "version": version, "source": source})
    entries.sort(key=lambda e: e.get("name", ""))
    write_lock(entries, path)


def lock_remove(name: str, path: str = LOCK):
    entries = [e for e in read_lock(path) if e.get("name") != name]
    if not entries:
        if os.path.isfile(path):
            os.remove(path)
    else:
        write_lock(entries, path)


# -- add/install helpers ----------------------------------------------

def _run(cmd: list, what: str) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise PkgCliError(f"{what}: '{cmd[0]}' not found on PATH")
    if p.returncode != 0:
        raise PkgCliError(f"{what} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def _is_git_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://",
                         "git://", "file://")) or s.endswith(".git")


def _strip_vcs(d: str):
    gitdir = os.path.join(d, ".git")
    if os.path.isdir(gitdir):
        shutil.rmtree(gitdir)


def _stage_git(src: str):
    """Clone a git URL into a temp dir. Returns (manifest, staged, source)."""
    url, _, rev = src.partition("#")
    tmp = tempfile.mkdtemp(prefix="misa-pkg-")
    staged = os.path.join(tmp, "pkg")
    if rev:
        _run(["git", "clone", url, staged], f"clone {url}")
        _run(["git", "-C", staged, "checkout", rev], f"checkout {rev}")
    else:
        _run(["git", "clone", "--depth", "1", url, staged],
             f"clone {url}")
    sha = _run(["git", "-C", staged, "rev-parse", "HEAD"],
               "resolve revision").strip()
    _strip_vcs(staged)
    try:
        man = read_manifest(staged)
    except PkgError as e:
        raise PkgCliError(f"{url}: {e}")
    return man, staged, f"git+{url}@{sha}"


def _stage_path(src: str):
    """Copy a local dir into a temp dir. Returns (manifest, staged, source)."""
    src_abs = os.path.abspath(src)
    if not os.path.isdir(src_abs):
        raise PkgCliError(f"not a directory: {src}")
    try:
        man = read_manifest(src_abs)
    except PkgError as e:
        raise PkgCliError(f"{src}: {e}")
    dest = os.path.join(os.path.abspath(PKGS_DIR), man["name"])
    if _within(src_abs, dest) or _within(dest, src_abs):
        raise PkgCliError("cannot add a package into itself")
    tmp = tempfile.mkdtemp(prefix="misa-pkg-")
    staged = os.path.join(tmp, man["name"])
    shutil.copytree(src_abs, staged, ignore=shutil.ignore_patterns(".git"))
    return man, staged, f"path+{src_abs}"


# -- CLI --------------------------------------------------------------

def _fail(msg: str) -> int:
    sys.stderr.write(f"misa: error: {msg}\n")
    return 1


def cmd_init(name: str | None, force: bool) -> int:
    name = name or os.path.basename(os.path.abspath(os.getcwd()))
    if not NAME_RE.match(name):
        return _fail(f"bad package name {name!r} "
                     "(lowercase letters, digits, hyphens)")
    if os.path.exists("misa.pkg") and not force:
        return _fail("misa.pkg already exists here (use --force to overwrite)")
    with open("misa.pkg", "w", encoding="utf-8") as f:
        f.write("[package]\n"
                f"name = {_tq(name)}\n"
                'version = "0.1.0"\n'
                'description = ""\n')
    os.makedirs("src", exist_ok=True)
    lib = os.path.join("src", "lib.misa")
    if not os.path.exists(lib) or force:
        with open(lib, "w", encoding="utf-8") as f:
            f.write(f"// {name} — entry point (`import \"{name}\"` loads this).\n")
    print(f"initialized package '{name}'")
    return 0


def cmd_add(src: str, force: bool) -> int:
    try:
        if os.path.exists(src):
            man, staged, source = _stage_path(src)
        elif _is_git_url(src):
            man, staged, source = _stage_git(src)
        else:
            raise PkgCliError(f"not found: {src}")
        dest = os.path.join(PKGS_DIR, man["name"])
        if os.path.exists(dest):
            if not force:
                raise PkgCliError(
                    f"{dest} already exists (use --force to overwrite)")
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            else:
                os.remove(dest)
        os.makedirs(PKGS_DIR, exist_ok=True)
        shutil.move(staged, dest)
        lock_set(man["name"], man["version"], source)
    except PkgCliError as e:
        return _fail(str(e))
    print(f"added {man['name']} {man['version']}")
    return 0


def cmd_install() -> int:
    try:
        if not os.path.isfile(LOCK):
            print("no misa.lock: nothing to install")
            return 0
        entries = read_lock()
        os.makedirs(PKGS_DIR, exist_ok=True)
        for e in entries:
            name, source = e.get("name", ""), e.get("source", "")
            dest = os.path.join(PKGS_DIR, name)
            if os.path.isdir(dest):
                print(f"{name}: already installed")
                continue
            if source.startswith("git+"):
                url, _, rev = source[4:].partition("@")
                tmp = tempfile.mkdtemp(prefix="misa-pkg-")
                staged = os.path.join(tmp, "pkg")
                _run(["git", "clone", url, staged], f"clone {url}")
                if rev:
                    _run(["git", "-C", staged, "checkout", rev],
                         f"checkout {rev}")
                _strip_vcs(staged)
                shutil.move(staged, dest)
            elif source.startswith("path+"):
                src = source[5:]
                if not os.path.isdir(src):
                    raise PkgCliError(f"{name}: source missing: {src}")
                shutil.copytree(src, dest,
                                ignore=shutil.ignore_patterns(".git"))
            else:
                raise PkgCliError(f"{name}: unknown source {source!r}")
            print(f"{name}: installed")
        locked = {e.get("name") for e in entries}
        for d in sorted(os.listdir(PKGS_DIR)):
            if d not in locked and os.path.isdir(os.path.join(PKGS_DIR, d)):
                print(f"warning: {PKGS_DIR}/{d} is not in {LOCK} "
                      "(left in place)")
    except PkgCliError as e:
        return _fail(str(e))
    return 0


def cmd_list() -> int:
    rows = []
    lock = {e.get("name"): e for e in read_lock()}
    if os.path.isdir(PKGS_DIR):
        for d in sorted(os.listdir(PKGS_DIR)):
            pdir = os.path.join(PKGS_DIR, d)
            if not os.path.isfile(os.path.join(pdir, "misa.pkg")):
                continue
            try:
                man = read_manifest(pdir)
            except PkgError as e:
                rows.append((d, "?", f"(bad manifest: {e})"))
                continue
            rows.append((man["name"], man["version"],
                         lock.get(d, {}).get("source", "(no lock entry)")))
    if not rows:
        print("no packages installed")
        return 0
    for n, v, s in rows:
        print(f"{n} {v}  {s}")
    return 0


def cmd_remove(name: str) -> int:
    try:
        dest = os.path.join(PKGS_DIR, name)
        if not os.path.isdir(dest):
            raise PkgCliError(f"not installed: {name}")
        shutil.rmtree(dest)
        lock_remove(name)
    except PkgCliError as e:
        return _fail(str(e))
    print(f"removed {name}")
    return 0


def cmd_pkg(argv: list) -> int:
    """Entry point for `misa pkg ...`. Returns an exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    force = "--force" in rest
    rest = [a for a in rest if a != "--force"]
    try:
        if sub == "init" and len(rest) <= 1:
            return cmd_init(rest[0] if rest else None, force)
        if sub == "add" and len(rest) == 1:
            return cmd_add(rest[0], force)
        if sub == "install" and not rest:
            return cmd_install()
        if sub == "list" and not rest:
            return cmd_list()
        if sub == "remove" and len(rest) == 1:
            return cmd_remove(rest[0])
    except PkgCliError as e:
        return _fail(str(e))
    sys.stderr.write(f"misa: error: unknown pkg usage\n{USAGE}\n")
    return 1
