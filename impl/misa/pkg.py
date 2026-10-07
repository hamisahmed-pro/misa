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
import base64
import getpass
import hashlib
import json
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
# Registry namespaces: @org/name (one slash; same charset per segment).
NAMESPACE_RE = re.compile(r"^@[a-z0-9-]+/[a-z0-9-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
LOCK = "misa.lock"
PKGS_DIR = "misa_pkgs"

# Registry URL resolution (never hardcoded except the default):
#   --registry flag > MISA_REGISTRY env > ~/.misa/config [registries]
#   default > built-in default.
DEFAULT_REGISTRY = "https://pkg.misa.website"

# Schema tag for --json output on read commands (spec/REGISTRY.md §6.6).
JSON_SCHEMA = "misa.pkg.v1"

USAGE = """usage:
  misa pkg init [name] [--force]   scaffold misa.pkg + src/lib.misa here
  misa pkg add <git-url|path|@org/pkg[@range]> [--force]
                                   install into misa_pkgs/, update misa.lock
  misa pkg install                 install everything in misa.lock
  misa pkg list [--json]           installed packages (+ capabilities)
  misa pkg remove <name>           remove from misa_pkgs/ and misa.lock
  misa pkg login [--token]         OAuth device flow (default) or paste token
  misa pkg logout [--revoke]       forget the token (optionally revoke it)
  misa pkg keygen [--rotate]       Ed25519 signing keypair for your handle
  misa pkg publish [--dry-run]     gates + pack + sign + upload to registry
  misa pkg search <query> [--json] [--kind keyword|semantic]
  misa pkg show @org/pkg[@ver] [--json]
  misa pkg yank @org/pkg@ver --reason "..."
  misa pkg deprecate @org/pkg --message "..."
  misa pkg audit [--json]          advisories for locked deps
  misa pkg vendor                  copy verified tarballs into vendor/
  misa pkg outdated [--json]       locked deps with newer compatible versions
  misa pkg update [@org/pkg]       re-resolve ranges, rewrite misa.lock
  misa pkg dependents @org/pkg [--json]
  misa pkg diff @org/pkg OLD NEW   signature diff + minimum bump
  misa pkg graph [--json]          resolved dependency graph (+sig/tier badges)

global flags: --registry <url>  --offline  --json"""


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

def _valid_pkg_name(name) -> bool:
    """Bare names (`my-pkg`) or namespaced (`@org/name`)."""
    return (isinstance(name, str)
            and bool(NAME_RE.match(name) or NAMESPACE_RE.match(name)))


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
    if not _valid_pkg_name(name):
        raise PkgError(
            f"misa.pkg: bad package name {name!r} "
            "(lowercase letters, digits, hyphens; '@org/name' for registry)")
    version = pkg.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise PkgError(
            f"misa.pkg: bad version {version!r} (semver MAJOR.MINOR.PATCH)")
    lib = pkg.get("lib", "src/lib.misa")
    if not isinstance(lib, str) or not lib:
        raise PkgError("misa.pkg: 'lib' must be a non-empty path string")
    caps = data.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        raise PkgError("misa.pkg: [capabilities] must be a table")
    return {"name": name, "version": version, "lib": lib,
            "description": pkg.get("description", ""),
            "license": pkg.get("license", ""),
            "homepage": pkg.get("homepage", ""),
            "repository": pkg.get("repository", ""),
            "dependencies": data.get("dependencies", {}) or {},
            "dev-dependencies": data.get("dev-dependencies", {}) or {},
            "capabilities": caps or {}}


def read_manifest_raw(pkg_dir: str) -> dict:
    """The full parsed misa.pkg (sent to the registry on publish)."""
    path = os.path.join(pkg_dir, "misa.pkg")
    if tomllib is None:
        raise PkgError("reading misa.pkg needs Python 3.11+ (tomllib)")
    with open(path, "rb") as f:
        return tomllib.load(f)


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
    if spec.startswith("@"):
        # Namespaced: the package name is "@org/name" (first two
        # segments); the rest is the module path inside src/.
        parts = spec.split("/")
        if len(parts) < 2:
            raise PkgResolveError(
                "E112", f"invalid package name '{spec}'",
                hint="namespaced packages look like '@org/name'")
        name = parts[0] + "/" + parts[1]
        rest = parts[2:]
        if not NAMESPACE_RE.match(name):
            raise PkgResolveError(
                "E112", f"invalid package name '{name}'",
                hint="namespaced packages look like '@org/name' "
                     "(lowercase letters, digits, hyphens, one slash)")
    else:
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


def read_lock_full(path: str = LOCK) -> tuple:
    """(lockfile_version, registry_url, entries). v1 -> (1, None, entries)."""
    entries = read_lock(path)
    if not os.path.isfile(path) or tomllib is None:
        return 1, None, entries
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return (data.get("version", 1), data.get("registry"), entries)


def write_lock(entries: list, path: str = LOCK, registry: str | None = None):
    # v2 header (spec/REGISTRY.md §5) whenever a registry entry with a
    # content hash is present; v1 git/path lockfiles stay headerless.
    v2 = any(e.get("source") == "registry" or e.get("sha256")
             for e in entries)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# generated by `misa pkg`; do not edit by hand\n")
        if v2:
            f.write("version = 2\n")
            if registry:
                f.write(f"registry = {_tq(registry)}\n")
            f.write("\n")
        for e in entries:
            f.write("[[package]]\n")
            f.write(f"name = {_tq(e['name'])}\n")
            f.write(f"version = {_tq(e['version'])}\n")
            if e.get("sha256"):
                f.write(f"sha256 = {_tq(e['sha256'])}\n")
            caps = e.get("capabilities") or []
            if caps:
                f.write("capabilities = ["
                        + ", ".join(_tq(c) for c in caps) + "]\n")
            f.write(f"source = {_tq(e['source'])}\n")
            # Client extensions beyond the spec's v2 lockfile: the range
            # the entry was resolved from (needed by `update`/`outdated`)
            # and the registry it came from (private registries).
            if e.get("range"):
                f.write(f"range = {_tq(e['range'])}\n")
            if e.get("registry"):
                f.write(f"registry = {_tq(e['registry'])}\n")
            f.write("\n")


def lock_set(name: str, version: str, source: str, path: str = LOCK,
             extra: dict | None = None, registry: str | None = None):
    entries = [e for e in read_lock(path) if e.get("name") != name]
    entry = {"name": name, "version": version, "source": source}
    if extra:
        entry.update(extra)
    entries.append(entry)
    entries.sort(key=lambda e: e.get("name", ""))
    _, lock_registry, _ = read_lock_full(path)
    write_lock(entries, path, registry=registry or lock_registry)


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
    if not _valid_pkg_name(name):
        return _fail(f"bad package name {name!r} "
                     "(lowercase letters, digits, hyphens; '@org/name' "
                     "for registry packages)")
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


def cmd_add(src: str, force: bool, ctx: dict | None = None) -> int:
    ctx = ctx or {}
    if _looks_registry_arg(src):
        return cmd_add_registry(src, force, ctx)
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


def cmd_install(ctx: dict | None = None) -> int:
    ctx = ctx or {}
    try:
        if not os.path.isfile(LOCK):
            print("no misa.lock: nothing to install")
            return 0
        _, lock_registry, entries = read_lock_full()
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
            elif source == "registry":
                _install_registry_entry(e, dest, ctx,
                                        lock_registry or
                                        resolve_registry(ctx.get("registry")))
            else:
                raise PkgCliError(f"{name}: unknown source {source!r}")
            print(f"{name}: installed")
        locked = {e.get("name") for e in entries}
        for inst_name, _ in _installed_dirs():
            if inst_name not in locked:
                print(f"warning: {PKGS_DIR}/{inst_name} is not in {LOCK} "
                      "(left in place)")
    except PkgCliError as e:
        return _fail(str(e))
    return 0


def _installed_dirs() -> list:
    """[(package name, dir)] for everything under misa_pkgs/.

    Namespaced packages live at misa_pkgs/@org/name/; the "@org"
    intermediate directory is descended, not listed.
    """
    out = []
    if not os.path.isdir(PKGS_DIR):
        return out
    for d in sorted(os.listdir(PKGS_DIR)):
        p = os.path.join(PKGS_DIR, d)
        if d.startswith("@") and os.path.isdir(p):
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(os.path.join(sp, "misa.pkg")):
                    out.append((f"{d}/{sub}", sp))
        elif os.path.isfile(os.path.join(p, "misa.pkg")):
            out.append((d, p))
    return out


def cmd_list(ctx: dict | None = None) -> int:
    ctx = ctx or {}
    rows = []
    lock = {e.get("name"): e for e in read_lock()}
    for _d, pdir in _installed_dirs():
        try:
            man = read_manifest(pdir)
        except PkgError as e:
            rows.append({"name": _d, "version": "?",
                         "source": f"(bad manifest: {e})",
                         "capabilities": []})
            continue
        entry = lock.get(man["name"], {})
        rows.append({"name": man["name"], "version": man["version"],
                     "source": entry.get("source", "(no lock entry)"),
                     "capabilities": entry.get("capabilities", []) or []})
    if not rows:
        if ctx.get("json"):
            print(json.dumps({"schema": JSON_SCHEMA, "packages": []},
                             indent=2))
        else:
            print("no packages installed")
        return 0
    if ctx.get("json"):
        print(json.dumps({"schema": JSON_SCHEMA, "packages": rows},
                         indent=2))
        return 0
    for r in rows:
        caps = ("  [" + " ".join(r["capabilities"]) + "]"
                if r["capabilities"] else "")
        print(f"{r['name']} {r['version']}  {r['source']}{caps}")
    return 0


def cmd_remove(name: str) -> int:
    try:
        dest = os.path.join(PKGS_DIR, name)
        if not os.path.isdir(dest):
            raise PkgCliError(f"not installed: {name}")
        shutil.rmtree(dest)
        # Prune an emptied "@org" intermediate directory.
        parent = os.path.dirname(dest)
        if (os.path.basename(parent).startswith("@")
                and os.path.isdir(parent)
                and not os.listdir(parent)):
            os.rmdir(parent)
        lock_remove(name)
    except PkgCliError as e:
        return _fail(str(e))
    print(f"removed {name}")
    return 0


def _take_flag(rest: list, flag: str) -> tuple:
    """Pull `--flag value` / `--flag=value` out of rest. -> (value, rest)."""
    out, val, i = [], None, 0
    while i < len(rest):
        a = rest[i]
        if a == flag and i + 1 < len(rest):
            val = rest[i + 1]
            i += 2
        elif a.startswith(flag + "="):
            val = a[len(flag) + 1:]
            i += 1
        else:
            out.append(a)
            i += 1
    return val, out


def cmd_pkg(argv: list) -> int:
    """Entry point for `misa pkg ...`. Returns an exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    # Global flags may appear before the subcommand
    # (`misa pkg --registry URL add ...`) or after it.
    argv = list(argv)
    ctx = {"registry": None, "offline": False, "json": False}
    while argv:
        a = argv[0]
        if a in ("--offline", "--json"):
            ctx[a[2:]] = True
            argv.pop(0)
        elif a == "--registry" and len(argv) > 1:
            ctx["registry"] = argv[1]
            argv = argv[2:]
        elif a.startswith("--registry="):
            ctx["registry"] = a.split("=", 1)[1]
            argv = argv[1:]
        else:
            break
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, args = argv[0], argv[1:]
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--registry" and i + 1 < len(args):
            ctx["registry"] = args[i + 1]
            i += 2
        elif a.startswith("--registry="):
            ctx["registry"] = a[len("--registry="):]
            i += 1
        elif a == "--offline":
            ctx["offline"] = True
            i += 1
        elif a == "--json":
            ctx["json"] = True
            i += 1
        else:
            rest.append(a)
            i += 1
    force = "--force" in rest
    rest = [a for a in rest if a != "--force"]
    try:
        if sub == "init" and len(rest) <= 1:
            return cmd_init(rest[0] if rest else None, force)
        if sub == "add" and len(rest) == 1:
            return cmd_add(rest[0], force, ctx)
        if sub == "install" and not rest:
            return cmd_install(ctx)
        if sub == "list" and not rest:
            return cmd_list(ctx)
        if sub == "remove" and len(rest) == 1:
            return cmd_remove(rest[0])
        if sub == "login":
            return cmd_login(rest, ctx)
        if sub == "keygen" and len(rest) <= 1:
            return cmd_keygen("--rotate" in rest)
        if sub == "logout" and len(rest) <= 1:
            return cmd_logout("--revoke" in rest)
        if sub == "publish" and len(rest) <= 1:
            return cmd_publish("--dry-run" in rest, ctx)
        if sub == "search" and len(rest) >= 1:
            kind, rest2 = _take_flag(rest, "--kind")
            if len(rest2) != 1:
                raise PkgCliError("usage: misa pkg search <query> "
                                  "[--kind keyword|semantic]")
            return cmd_search(rest2[0], kind, ctx)
        if sub == "show" and len(rest) == 1:
            return cmd_show(rest[0], ctx)
        if sub == "yank" and len(rest) >= 1:
            reason, rest2 = _take_flag(rest, "--reason")
            if len(rest2) != 1 or not reason:
                raise PkgCliError('usage: misa pkg yank @org/pkg@ver '
                                  '--reason "..."')
            return cmd_yank(rest2[0], reason, ctx)
        if sub == "deprecate" and len(rest) >= 1:
            message, rest2 = _take_flag(rest, "--message")
            if len(rest2) != 1 or not message:
                raise PkgCliError('usage: misa pkg deprecate @org/pkg '
                                  '--message "..."')
            return cmd_deprecate(rest2[0], message, ctx)
        if sub == "audit" and not rest:
            return cmd_audit(ctx)
        if sub == "vendor" and not rest:
            return cmd_vendor(ctx)
        if sub == "outdated" and not rest:
            return cmd_outdated(ctx)
        if sub == "update" and len(rest) <= 1:
            return cmd_update(rest[0] if rest else None, ctx)
        if sub == "dependents" and len(rest) == 1:
            return cmd_dependents(rest[0], ctx)
        if sub == "diff" and len(rest) == 3:
            return cmd_diff(rest[0], rest[1], rest[2], ctx)
        if sub == "graph" and not rest:
            return cmd_graph(ctx)
    except PkgCliError as e:
        return _fail(str(e))
    sys.stderr.write(f"misa: error: unknown pkg usage\n{USAGE}\n")
    return 1


# ======================================================================
# Registry client (spec/REGISTRY.md)
#
# API contract this client codes against (see docs/REGISTRY-API.md):
#
#   GET  /v1/packages/<pct-encoded @org/name>
#        -> {name, description, deprecated, versions:[{version, yanked,
#           published_at}]}
#   GET  /v1/packages/<name>/<version>
#        -> {name, version, sha256, license, description, yanked,
#           deprecated, capabilities:{declared:[], verified:[]},
#           throws_surface:{fn:[E...]}, examples:{total, passing},
#           docs_url, downloads_30d}
#   GET  /v1/packages/<name>/<version>/signatures
#        -> {signatures:[{kind, name, params, ret, throws, doc, ...}]}
#   GET  /v1/packages/<name>/dependents
#        -> {dependents:[{name, version, range}]}
#   GET  /v1/tarballs/<name>/<version> -> {sha256, tarball_b64}
#   GET  /v1/search?q=...&kind=keyword|semantic -> {results:[...]}
#   GET  /v1/advisories?package=<name> -> {advisories:[{id, severity,
#        summary, affected, fixed_in, published_at, state}]}
#   POST /v1/publish            (Bearer) {manifest, tarball_b64,
#        tarball_sha256, signatures, capabilities_declared}
#   POST /v1/yank               (Bearer) {pkg, version, reason}
#   POST /v1/deprecate          (Bearer) {pkg, message}
#   POST /v1/tokens/revoke      (Bearer) {token_sha256}
#
# Intentional deviations from spec/REGISTRY.md (also documented in the
# test header of tests/pkg/run_registry_tests.py):
#   * POST /v1/publish takes a JSON body (spec says multipart).
#   * GET /v1/tarballs/... returns JSON {sha256, tarball_b64} (spec's
#     tarball path implies raw bytes; bodies are UTF-8 in v1).
#   * The namespaced name uses the spec's two-segment URL form
#     ("/v1/packages/@acme/router"); each segment is percent-encoded
#     separately so the '/' separator is never ambiguous.
#   * `misa fmt --check` does not exist in the toolchain; the publish
#     gate implements the equivalent check directly via the Formatter.
#   * POST /v1/tokens/revoke is client-defined; the spec has no revoke
#     endpoint (logout --revoke needs one).
#   * Signature extraction skips `_`-prefixed fns (private by
#     convention; Misa has no visibility modifier).
#   * A removed `impl` counts as minor (the §6.5 table lists impl
#     removal nowhere; new impls are "ok").
#   * Lockfile v2 entries carry optional `range` and `registry` keys
#     beyond the spec (needed by `update`/`outdated` and private
#     registries).
#   * Bare "1.2.0" in a range position means exact (=), like npm.
#   * v1 login is paste-a-token (OAuth device flow is v1.1 per the
#     task brief).
# ======================================================================


# -- registry URL resolution ------------------------------------------

def resolve_registry(cli_value: str | None = None) -> str:
    """--registry flag > MISA_REGISTRY env > ~/.misa/config > default."""
    if cli_value:
        return cli_value.rstrip("/")
    env = os.environ.get("MISA_REGISTRY")
    if env:
        return env.rstrip("/")
    cfg = os.path.join(os.path.expanduser("~"), ".misa", "config")
    if os.path.isfile(cfg) and tomllib is not None:
        try:
            with open(cfg, "rb") as f:
                data = tomllib.load(f)
            default = (data.get("registries") or {}).get("default")
            if isinstance(default, str) and default.strip():
                return default.strip().rstrip("/")
        except Exception:
            pass
    return DEFAULT_REGISTRY


def _qname(name: str) -> str:
    """Encode @org/name for a two-segment URL path (/v1/packages/:ns/:name).

    Each segment is percent-encoded separately; the '/' separator and the
    '@' stay literal, matching spec/REGISTRY.md §8.2 and the server.
    """
    import urllib.parse
    ns, _, pkg = name.partition("/")
    return (urllib.parse.quote(ns, safe="@") + "/" +
            urllib.parse.quote(pkg, safe=""))


# -- HTTP ---------------------------------------------------------------

def _api_request(method: str, registry: str, path: str,
                 body: dict | None = None, token: str | None = None,
                 timeout: int = 60) -> dict:
    import urllib.parse
    import urllib.request
    import urllib.error
    url = registry + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "misa-pkg/1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        msg = err.get("message") or f"HTTP {e.code}"
        code = err.get("code")
        raise PkgCliError(
            f"registry error{f' ({code})' if code else ''}: {msg}")
    except urllib.error.URLError as e:
        raise PkgCliError(f"registry unreachable: {e.reason}")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        raise PkgCliError("registry returned invalid JSON")


def _api_get(path: str, registry: str, token: str | None = None,
             params: dict | None = None) -> dict:
    import urllib.parse
    if params:
        path += "?" + urllib.parse.urlencode(params)
    return _api_request("GET", registry, path, token=token)


def _api_post(path: str, registry: str, body: dict,
              token: str | None = None) -> dict:
    return _api_request("POST", registry, path, body=body, token=token)


# -- credentials ----------------------------------------------------------

def _creds_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".misa", "credentials")


def read_credentials() -> dict:
    p = _creds_path()
    if not os.path.isfile(p) or tomllib is None:
        return {}
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_credentials(token: str, handle: str):
    d = os.path.dirname(_creds_path())
    os.makedirs(d, exist_ok=True)
    with open(_creds_path(), "w", encoding="utf-8") as f:
        f.write(f"token = {_tq(token)}\nhandle = {_tq(handle)}\n")
    os.chmod(_creds_path(), 0o600)


def clear_credentials():
    p = _creds_path()
    if os.path.isfile(p):
        os.remove(p)


def _require_token() -> tuple:
    creds = read_credentials()
    token = creds.get("token")
    if not token:
        raise PkgCliError("not logged in (run `misa pkg login` first)")
    return token, creds.get("handle", "")


# -- semver ---------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$")


class SemVer:
    """Parsed semver with comparison (prerelease < release)."""

    def __init__(self, major: int, minor: int, patch: int,
                 pre: tuple = ()):
        self.major, self.minor, self.patch, self.pre = \
            major, minor, patch, tuple(pre)

    @classmethod
    def parse(cls, s: str) -> "SemVer":
        m = _SEMVER_RE.match(s or "")
        if not m:
            raise PkgCliError(f"bad semver {s!r} "
                              "(MAJOR.MINOR.PATCH)")
        major, minor, patch = int(m.group(1)), int(m.group(2)), \
            int(m.group(3))
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        return cls(major, minor, patch, pre)

    def _pre_key(self):
        # prerelease sorts before the release; compare identifiers
        # numerically when both are numeric, else lexically.
        def key(part):
            return (0, int(part)) if part.isdigit() else (1, part)
        return tuple(key(p) for p in self.pre)

    def _key(self):
        return (self.major, self.minor, self.patch,
                (1,) if not self.pre else (0, self._pre_key()))

    def __eq__(self, o):
        return isinstance(o, SemVer) and self._key() == o._key()

    def __lt__(self, o):
        return self._key() < o._key()

    def __le__(self, o):
        return self._key() <= o._key()

    def __gt__(self, o):
        return self._key() > o._key()

    def __ge__(self, o):
        return self._key() >= o._key()

    def __hash__(self):
        return hash(self._key())

    def __str__(self):
        s = f"{self.major}.{self.minor}.{self.patch}"
        return s + ("-" + ".".join(self.pre) if self.pre else "")

    def __repr__(self):
        return f"SemVer({self})"


def bump_kind(old: str, new: str) -> str | None:
    """major|minor|patch between two versions; None if equal."""
    a, b = SemVer.parse(old), SemVer.parse(new)
    if b == a:
        return None
    if b.major != a.major:
        return "major"
    if b.minor != a.minor:
        return "minor"
    return "patch"


_BUMP_RANK = {"patch": 0, "minor": 1, "major": 2}


def parse_range(s: str):
    """Parse a range string into ("exact", SemVer) | ("pred", [(op,
    SemVer)]) | ("latest",). Supports ^ ~ = bare >= > <= < and ","-AND."""
    s = (s or "").strip()
    if s in ("", "latest", "^latest", "*"):
        return ("latest",)
    preds = []
    has_pre = False
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        op, ver = "", part
        for prefix in ("^", "~", ">=", "<=", ">", "<", "="):
            if part.startswith(prefix):
                op, ver = prefix, part[len(prefix):]
                break
        v = SemVer.parse(ver)
        if v.pre:
            has_pre = True
        if op in ("", "="):
            preds.append(("=", v))
        elif op == "^":
            if v.major > 0:
                upper = SemVer(v.major + 1, 0, 0)
            elif v.minor > 0:
                upper = SemVer(0, v.minor + 1, 0)
            else:
                upper = SemVer(0, 0, v.patch + 1)
            preds.append((">=", v))
            preds.append(("<", upper))
        elif op == "~":
            parts = ver.split(".")
            if len(parts) <= 1:
                upper = SemVer(v.major + 1, 0, 0)
            else:
                upper = SemVer(v.major, v.minor + 1, 0)
            preds.append((">=", v))
            preds.append(("<", upper))
        else:
            preds.append((op, v))
    if not preds:
        return ("latest",)
    if len(preds) == 1 and preds[0][0] == "=":
        return ("exact", preds[0][1])
    return ("pred", preds, has_pre)


def satisfies(version: str, range_str: str) -> bool:
    """Does `version` satisfy `range_str`?"""
    rng = parse_range(range_str)
    v = SemVer.parse(version)
    if rng[0] == "latest":
        return not v.pre
    if rng[0] == "exact":
        return v == rng[1]
    _, preds, has_pre = rng
    # Prereleases only participate when the range itself names one
    # (npm-style).
    if v.pre and not has_pre:
        return False
    for op, bound in preds:
        if op == "=" and not v == bound:
            return False
        if op == ">=" and not v >= bound:
            return False
        if op == ">" and not v > bound:
            return False
        if op == "<=" and not v <= bound:
            return False
        if op == "<" and not v < bound:
            return False
    return True


def max_satisfying(versions: list, range_str: str) -> str | None:
    """Highest version satisfying the range; None when empty."""
    best, best_v = None, None
    for s in versions:
        if not satisfies(s, range_str):
            continue
        v = SemVer.parse(s)
        if best_v is None or v > best_v:
            best, best_v = s, v
    return best


# -- deterministic tarball ------------------------------------------------

_SKIP_TAR_DIRS = {".git", "misa_pkgs", "vendor", "__pycache__", "target",
                  "node_modules"}


def pack_tarball(pkg_dir: str) -> tuple:
    """Deterministic .tar.gz: sorted entries, fixed mtime 0, gzip mtime 0.

    Returns (bytes, sha256 hex). Everything except VCS/build/output
    dirs is included, so authors get examples/README/CHANGELOG for free.
    """
    import tarfile
    import io
    import gzip
    entries = []
    for dp, dns, fns in os.walk(pkg_dir):
        dns[:] = sorted(d for d in dns
                        if d not in _SKIP_TAR_DIRS
                        and not d.startswith("."))
        for fn in sorted(fns):
            if fn.startswith("."):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, pkg_dir).replace(os.sep, "/")
            entries.append((rel, full))
    # Global sort by archive path (a per-directory walk would put the
    # top-level files before every subdirectory's files).
    entries.sort(key=lambda e: e[0])
    if not any(rel == "misa.pkg" for rel, _ in entries):
        raise PkgCliError("cannot pack: no misa.pkg in package dir")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w",
                      format=tarfile.PAX_FORMAT) as tf:
        for rel, full in entries:
            ti = tarfile.TarInfo(rel)
            ti.size = os.path.getsize(full)
            ti.mtime = 0
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mode = 0o644
            with open(full, "rb") as f:
                tf.addfile(ti, f)
    gz = gzip.compress(buf.getvalue(), compresslevel=9, mtime=0)
    return gz, hashlib.sha256(gz).hexdigest()


def unpack_tarball(data: bytes, dest: str):
    """Unpack tarball bytes into dest (refuses absolute/escaping paths)."""
    import tarfile
    import io
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise PkgCliError(
                    f"tarball contains unsafe path {m.name!r}")
        tf.extractall(dest)


def _vendor_name(name: str, version: str) -> str:
    # "@acme/router" -> "acme__router-1.2.0.tar.gz" (no @ or / in files).
    return name.lstrip("@").replace("/", "__") + f"-{version}.tar.gz"


def _fetch_tarball(name: str, version: str, registry: str, ctx: dict,
                   expected_sha256: str | None,
                   prefer_vendor: bool = True) -> bytes:
    """Return verified tarball bytes for name@version.

    Order: vendor/ copy (verified) -> network (verified, skipped when
    --offline). The sha256 is verified BEFORE the bytes are unpacked.
    """
    if prefer_vendor:
        vp = os.path.join("vendor", _vendor_name(name, version))
        if os.path.isfile(vp):
            with open(vp, "rb") as f:
                data = f.read()
            actual = hashlib.sha256(data).hexdigest()
            if expected_sha256 and actual != expected_sha256:
                raise PkgCliError(
                    f"{name}@{version}: vendor copy corrupt: "
                    f"expected {expected_sha256}, got {actual}")
            return data
    if ctx.get("offline"):
        raise PkgCliError(
            f"{name}@{version}: --offline and no verified vendor copy "
            f"(vendor/{_vendor_name(name, version)})")
    resp = _api_get(f"/v1/tarballs/{_qname(name)}/{version}", registry)
    try:
        data = base64.b64decode(resp["tarball_b64"])
    except Exception:
        raise PkgCliError(f"{name}@{version}: bad tarball_b64 from registry")
    actual = hashlib.sha256(data).hexdigest()
    for label, want in (("registry metadata", resp.get("sha256")),
                        ("lockfile", expected_sha256)):
        if want and actual != want:
            raise PkgCliError(
                f"{name}@{version}: checksum mismatch ({label}): "
                f"expected {want}, got {actual}")
    # v1.1: verify the Ed25519 package signature when present.
    sig_obj = resp.get("signature")
    if sig_obj:
        if not _verify_tarball(name, version, data, sig_obj):
            raise PkgCliError(
                f"{name}@{version}: SIGNATURE VERIFICATION FAILED — "
                "the tarball does not match its registered signature. "
                "Not installing.")
    return data


def _install_registry_entry(entry: dict, dest: str, ctx: dict,
                            registry: str):
    name = entry.get("name", "")
    version = entry.get("version", "")
    data = _fetch_tarball(name, version, registry, ctx,
                          entry.get("sha256"))
    if os.path.exists(dest):
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        else:
            os.remove(dest)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="misa-pkg-")
    try:
        unpack_tarball(data, tmp)
        shutil.move(tmp, dest)
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    dep_msg = entry.get("deprecated")
    if dep_msg:
        print(f"note: {name} is deprecated: {dep_msg}")


# -- signatures -----------------------------------------------------------

def _type_str(te) -> str:
    """Render a TypeExpr in Misa surface syntax (for the sig index)."""
    if te is None:
        return "Void"
    if te.is_func:
        ps = ", ".join(_type_str(p) for p in te.func_params)
        return f"({ps}) -> {_type_str(te.func_ret)}"
    if te.is_map:
        return f"Map<{_type_str(te.map_key)}, {_type_str(te.map_val)}>"
    elem = te.list_elem
    if te.is_list or elem is not None:
        inner = _type_str(elem) if elem is not None else te.name
        s = f"[{inner}]"
    elif te.type_args:
        s = f"{te.name}<{', '.join(_type_str(a) for a in te.type_args)}>"
    else:
        s = te.name or "Void"
    if te.optional:
        s += "?"
    return s


def _doc_of(item) -> str:
    return "\n".join(
        c[3:].strip() for c in (item.leading_comments or [])
        if c.startswith("///"))


def _throws_for_package(pkg_dir: str, lib_rel: str,
                        fn_names: set) -> dict:
    """Run `misa check --json` on the package entry; return
    {fn_name: [E-codes inferred]} restricted to this package's fns.

    The merged program also contains imported packages' fns, so the
    graph is filtered by the names we extracted from our own sources.
    """
    misa_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "bin", "misa")
    env = dict(os.environ)
    env["MISA_HOME"] = misa_home()
    try:
        p = subprocess.run(
            [sys.executable, misa_bin, "check", lib_rel, "--json"],
            cwd=pkg_dir, env=env, capture_output=True, text=True,
            timeout=300)
    except FileNotFoundError:
        raise PkgCliError("cannot run `misa check` (toolchain not found)")
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except Exception:
        out = {}
    if p.returncode != 0 or not out.get("ok"):
        errs = out.get("errors", [])
        detail = "; ".join(
            f"{e.get('code')}: {e.get('message')}" for e in errs[:5])
        raise PkgCliError(
            f"`misa check` failed on the package"
            + (f": {detail}" if detail else ""))
    graph = out.get("throws_graph", {}) or {}
    return {n: sorted(graph.get(n, {}).get("inferred", []))
            for n in fn_names}


def extract_signatures(pkg_dir: str) -> list:
    """The registry's typed signature index for a package (§6.1).

    Walks every src/**/*.misa: top-level `fn` (except `_`-prefixed,
    private by convention), `interface`, and `impl` declarations.
    Throws sets come from `misa check --json`'s inferred throws graph.
    """
    from . import ast as mast
    from .parser import parse_source
    sigs = []
    fn_names = set()
    srcdir = os.path.join(pkg_dir, "src")
    files = []
    if os.path.isdir(srcdir):
        for dp, _, fns in os.walk(srcdir):
            for fn in sorted(fns):
                if fn.endswith(".misa"):
                    files.append(os.path.join(dp, fn))
    files.sort()
    for path in files:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        prog = parse_source(src, path)
        rel = os.path.relpath(path, pkg_dir).replace(os.sep, "/")
        for item in prog.items:
            if isinstance(item, mast.FnDecl):
                if item.name.startswith("_"):
                    continue  # private by convention
                sigs.append({
                    "kind": "fn", "name": item.name,
                    "params": [[p, _type_str(t)]
                               for p, t in item.params],
                    "ret": _type_str(item.ret),
                    "throws": [],
                    "doc": _doc_of(item), "file": rel})
                fn_names.add(item.name)
            elif isinstance(item, mast.InterfaceDecl):
                sigs.append({
                    "kind": "interface", "name": item.name,
                    "methods": [{
                        "name": m.name,
                        "params": [[p, _type_str(t)]
                                   for p, t in m.params if p != "self"],
                        "ret": _type_str(m.ret),
                        "throws": sorted(m.throws or [])}
                        for m in item.methods],
                    "doc": _doc_of(item), "file": rel})
            elif isinstance(item, mast.ImplDecl):
                sigs.append({
                    "kind": "impl",
                    "name": f"{item.iface} for {item.target}",
                    "iface": item.iface, "target": item.target,
                    "methods": [m.name for m in item.methods],
                    "doc": _doc_of(item), "file": rel})
    man = read_manifest(pkg_dir)
    throws_map = _throws_for_package(pkg_dir, man["lib"], fn_names)
    for s in sigs:
        if s["kind"] == "fn":
            s["throws"] = throws_map.get(s["name"], [])
    return sigs


def diff_signatures(old: list, new: list) -> dict:
    """Computable breaking changes (spec §6.5). Returns
    {"min_bump": "major"|"minor"|"patch", "changes": [...]}.

    Each change: {"kind": "breaking"|"addition"|"ok", "detail": str}.
    """
    def key(s):
        return (s.get("kind"), s.get("name"))

    old_m = {key(s): s for s in old}
    new_m = {key(s): s for s in new}
    changes = []

    def param_sig(ps):
        return [(p, t) for p, t in (ps or [])]

    for k, o in old_m.items():
        n = new_m.get(k)
        kind, name = k
        if n is None:
            if kind == "fn":
                changes.append({"kind": "breaking",
                                "detail": f"fn {name} removed"})
            elif kind == "interface":
                changes.append({"kind": "breaking",
                                "detail": f"interface {name} removed"})
            else:  # impl removed: not in the §6.5 table; count as minor
                changes.append({"kind": "addition",
                                "detail": f"impl {name} removed"})
            continue
        if kind == "fn":
            op, np = param_sig(o.get("params")), param_sig(n.get("params"))
            # Param names are cosmetic (positional calls); types are not.
            if [t for _, t in op] != [t for _, t in np]:
                if len(op) != len(np):
                    changes.append({
                        "kind": "breaking",
                        "detail": f"fn {name}: parameter count changed "
                                  f"{len(op)} -> {len(np)}"})
                else:
                    diffs = [f"{pn}: {pt} -> {qt}"
                             for (pn, pt), (_, qt) in zip(op, np)
                             if pt != qt]
                    changes.append({
                        "kind": "breaking",
                        "detail": f"fn {name}: parameter type changed: "
                                  + "; ".join(diffs)})
            if o.get("ret") != n.get("ret"):
                changes.append({"kind": "breaking",
                                "detail": f"fn {name}: return type changed "
                                          f"{o.get('ret')} -> {n.get('ret')}"})
            ot, nt = set(o.get("throws") or []), set(n.get("throws") or [])
            if nt - ot:
                changes.append({
                    "kind": "breaking",
                    "detail": f"fn {name}: throws set grew: "
                              + ", ".join(sorted(f"+{c}" for c in nt - ot))})
            elif ot - nt:
                changes.append({"kind": "ok",
                                "detail": f"fn {name}: throws set shrank "
                                          f"({', '.join(sorted(ot - nt))} "
                                          "no longer raised)"})
            if o.get("doc") != n.get("doc"):
                changes.append({"kind": "ok",
                                "detail": f"fn {name}: doc comment changed"})
        elif kind == "interface":
            om = {m["name"]: m for m in o.get("methods", [])}
            nm = {m["name"]: m for m in n.get("methods", [])}
            for m in nm:
                if m not in om:
                    changes.append({
                        "kind": "breaking",
                        "detail": f"interface {name} gained method {m} "
                                  "(existing impls are now incomplete)"})
            for m in om:
                if m not in nm:
                    changes.append({
                        "kind": "breaking",
                        "detail": f"interface {name} lost method {m}"})
    for k, n in new_m.items():
        if k not in old_m:
            kind, name = k
            changes.append({"kind": "addition",
                            "detail": f"new {kind} {name}"})
    if any(c["kind"] == "breaking" for c in changes):
        bump = "major"
    elif any(c["kind"] == "addition" for c in changes):
        bump = "minor"
    else:
        bump = "patch"
    return {"min_bump": bump, "changes": changes}


# -- publish validation ---------------------------------------------------

def validate_publish_manifest(man: dict, pkg_dir: str):
    """E113 variants required at publish (spec §1 + §4 gates)."""
    if not man.get("description"):
        raise PkgCliError(
            "E113: publish requires [package] description to be non-empty")
    if not man.get("license"):
        raise PkgCliError(
            "E113: publish requires [package] license (SPDX identifier)")
    exdir = os.path.join(pkg_dir, "examples")
    examples = []
    if os.path.isdir(exdir):
        examples = sorted(f for f in os.listdir(exdir)
                          if f.endswith(".misa"))
    if not examples:
        raise PkgCliError(
            "E113: publish requires at least one examples/*.misa "
            "(runnable example)")
    return examples


# -- registry commands ------------------------------------------------------

def _looks_registry_arg(s: str) -> bool:
    """`@org/pkg` or `@org/pkg@range` (not a path/git URL)."""
    if not s.startswith("@"):
        return False
    core = s[1:].split("@", 1)[0]
    return bool(NAMESPACE_RE.match("@" + core))


def _split_add_arg(s: str) -> tuple:
    """`@org/pkg[@range]` -> (name, range); bare means ^latest."""
    body = s[1:]
    if "@" in body:
        name_part, range_part = body.split("@", 1)
        rng = range_part.strip() or "^latest"
    else:
        name_part, rng = body, "^latest"
    name = "@" + name_part
    if not NAMESPACE_RE.match(name):
        raise PkgCliError(
            f"bad package name {s!r} (registry packages are '@org/name')")
    return name, rng


def _run_misa(args: list, cwd: str, timeout: int = 300):
    misa_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "bin", "misa")
    env = dict(os.environ)
    env["MISA_HOME"] = misa_home()
    return subprocess.run([sys.executable, misa_bin] + args, cwd=cwd,
                          env=env, capture_output=True, text=True,
                          timeout=timeout)


# -- signing keys (v1.1) ------------------------------------------------------

def _keys_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".misa", "keys")
    os.makedirs(d, exist_ok=True)
    return d


def _key_paths(handle: str) -> tuple:
    d = _keys_dir()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in handle)
    return (os.path.join(d, safe + ".key"),
            os.path.join(d, safe + ".pub"))


def cmd_keygen(rotate: bool) -> int:
    """Generate an Ed25519 signing keypair for a publisher handle."""
    try:
        from misa import ed25519 as _ed
    except ImportError:
        return _fail("ed25519 module not available")
    try:
        _token, handle = _require_token()
    except PkgCliError:
        handle = ""
    if not handle:
        try:
            handle = input("handle: ").strip()
        except EOFError:
            handle = ""
    if not handle:
        return _fail("handle required (login first or pass it here)")
    key_path, pub_path = _key_paths(handle)
    if os.path.exists(key_path) and not rotate:
        return _fail(f"key already exists at {key_path} "
                      "(use --rotate to replace it)")
    if rotate and os.path.exists(pub_path):
        # keep the old public key for verification of old signatures
        os.replace(pub_path, pub_path + ".old")
    sk, pk = _ed.keygen()
    with open(key_path, "w") as f:
        f.write(sk.hex())
    os.chmod(key_path, 0o600)
    with open(pub_path, "w") as f:
        f.write(pk.hex())
    fp = hashlib.sha256(pk.hex().encode("utf-8")).hexdigest()
    print(f"keypair for {handle}:")
    print(f"  private: {key_path} (0600)")
    print(f"  public:  {pub_path}")
    print(f"  fingerprint: {fp[:16]}… ({fp})")
    print("The public key is registered with the registry on your next "
          "signed publish.")
    return 0


def _load_signing_key(handle: str):
    """Returns (sk_hex, pk_hex, fingerprint) or None."""
    key_path, pub_path = _key_paths(handle)
    if not os.path.exists(key_path) or not os.path.exists(pub_path):
        return None
    with open(key_path) as f:
        sk = f.read().strip()
    with open(pub_path) as f:
        pk = f.read().strip()
    fp = hashlib.sha256(pk.encode("utf-8")).hexdigest()
    return sk, pk, fp


def _sign_payload(name: str, version: str, tarball: bytes,
                  sk_hex: str, pk_hex: str) -> dict:
    """Sign a package tarball. Returns the signature object for publish."""
    from misa import ed25519 as _ed
    ns = name.split("/")[0] if "/" in name else ""
    h = hashlib.sha256()
    h.update(ns.encode("utf-8"))
    h.update(b"\x00")
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(version.encode("utf-8"))
    h.update(b"\x00")
    h.update(tarball)
    payload_hash = h.hexdigest()
    sig = _ed.sign(bytes.fromhex(sk_hex), payload_hash.encode("utf-8"))
    fp = hashlib.sha256(pk_hex.encode("utf-8")).hexdigest()
    return {"pubkey": pk_hex, "sig": sig.hex(), "fingerprint": fp}


def _verify_tarball(name: str, version: str, tarball: bytes,
                    sig_obj: dict) -> bool:
    """Verify a downloaded tarball against its signature object."""
    from misa import ed25519 as _ed
    try:
        pk_hex = sig_obj["pubkey"]
        sig_hex = sig_obj["sig"]
        fp = sig_obj["fingerprint"]
    except (KeyError, TypeError):
        return False
    if hashlib.sha256(pk_hex.encode("utf-8")).hexdigest() != fp.lower():
        return False
    ns = name.split("/")[0] if "/" in name else ""
    h = hashlib.sha256()
    h.update(ns.encode("utf-8"))
    h.update(b"\x00")
    h.update(name.encode("utf-8"))
    h.update(b"\x00")
    h.update(version.encode("utf-8"))
    h.update(b"\x00")
    h.update(tarball)
    try:
        return _ed.verify(bytes.fromhex(pk_hex),
                          h.hexdigest().encode("utf-8"),
                          bytes.fromhex(sig_hex))
    except ValueError:
        return False


# -- login / logout ---------------------------------------------------------

def cmd_login(rest: list | None = None, ctx: dict | None = None) -> int:
    """v1.1: OAuth device flow by default; --token for the manual flow."""
    rest = rest or []
    ctx = ctx or {}
    registry = resolve_registry(ctx.get("registry"))
    if "--token" in rest or not rest:
        return _cmd_login_token()
    # Device flow.
    try:
        dc = _api_post("/v1/oauth/device/code", registry, {})
    except PkgCliError as e:
        return _fail(f"device flow unavailable: {e}")
    print("Visit this URL and enter the code:")
    print(f"  {dc.get('verification_uri')}")
    print(f"  code: {dc.get('user_code')}")
    print("Waiting for approval", end="", flush=True)
    import time as _time
    interval = int(dc.get("interval", 5))
    expires_in = int(dc.get("expires_in", 900))
    deadline = _time.time() + expires_in
    device_code = dc.get("device_code", "")
    while _time.time() < deadline:
        _time.sleep(interval)
        print(".", end="", flush=True)
        try:
            tr = _api_post("/v1/oauth/device/token", registry,
                           {"device_code": device_code})
        except PkgCliError as e:
            if "(R404)" in str(e) or "(R410)" in str(e):
                print()
                return _fail(str(e))
            continue
        if tr.get("error") == "authorization_pending":
            continue
        token = tr.get("token", "")
        handle = tr.get("handle", "")
        if token and handle:
            print()
            try:
                write_credentials(token, handle)
            except PkgCliError as e:
                return _fail(str(e))
            print(f"logged in as {handle} "
                  "(token stored in ~/.misa/credentials)")
            return 0
        print()
        return _fail(f"unexpected token response: {tr}")
    print()
    return _fail("timed out waiting for approval")


def _cmd_login_token() -> int:
    print("Paste your publisher token (issued by the registry admin).")
    try:
        token = getpass.getpass("token: ").strip()
    except Exception:
        try:
            token = input("token: ").strip()
        except EOFError:
            token = ""
    if not token:
        return _fail("no token given")
    try:
        handle = input("handle: ").strip()
    except EOFError:
        handle = ""
    if not handle:
        return _fail("handle required")
    try:
        write_credentials(token, handle)
    except PkgCliError as e:
        return _fail(str(e))
    print(f"logged in as {handle} (token stored in ~/.misa/credentials)")
    return 0


def cmd_logout(revoke: bool) -> int:
    creds = read_credentials()
    if not creds.get("token"):
        print("not logged in")
        return 0
    if revoke:
        registry = resolve_registry()
        token = creds["token"]
        sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Fail closed: if the server won't revoke, keep the local
        # credential and say so rather than silently dropping it.
        _api_post("/v1/tokens/revoke", registry, {"token_sha256": sha},
                  token=token)
        print("token revoked server-side")
    clear_credentials()
    print("logged out")
    return 0


def _norm_throws_surface(ts):
    """Normalize throws_surface to {fn: [codes]}.

    Accepts the spec's dict form and the server's richer list form
    [{name, signature, throws}].
    """
    if isinstance(ts, list):
        return {t.get("name", ""): (t.get("throws") or [])
                for t in ts if isinstance(t, dict)}
    return ts if isinstance(ts, dict) else {}


def _norm_examples(ex):
    """Normalize examples to {passing, total} (also accepts {pass, total})."""
    ex = dict(ex) if isinstance(ex, dict) else {}
    if "passing" not in ex and "pass" in ex:
        ex["passing"] = ex["pass"]
    return ex


# -- publish ------------------------------------------------------------------

def _fmt_check_file(path: str) -> bool:
    from .parser import parse_source
    from .fmt import Formatter
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return Formatter().format_program(parse_source(src, path)) == src


def _record_publish_local(pkg_dir: str, sha256: str):
    """Write the [registry] publish metadata back into misa.pkg."""
    import datetime
    path = os.path.join(pkg_dir, "misa.pkg")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    # Drop any existing [registry] section.
    out, skipping = [], False
    for ln in lines:
        s = ln.strip()
        if s == "[registry]":
            skipping = True
            continue
        if skipping and s.startswith("["):
            skipping = False
        if not skipping:
            out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    out.append("\n[registry]\n"
               f"published_at = {_tq(now)}\n"
               f"tarball_sha256 = {_tq(sha256)}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)


def cmd_publish(dry_run: bool, ctx: dict | None = None) -> int:
    ctx = ctx or {}
    pkg_dir = os.getcwd()
    try:
        try:
            man = read_manifest(pkg_dir)
        except PkgError as e:
            raise PkgCliError(f"E113: {e}")
        name = man["name"]
        if not NAMESPACE_RE.match(name):
            raise PkgCliError(
                f"E113: publish requires a namespaced name '@org/name', "
                f"got {name!r}")
        registry = resolve_registry(ctx.get("registry"))
        token, _handle = (None, "")
        if not dry_run:
            token, _handle = _require_token()
        version = man["version"]

        # Gate 3 (manifest) first — cheap, and E113 is the manifest code.
        examples = validate_publish_manifest(man, pkg_dir)

        # Gate 1: `misa check` clean on the package (dev-dependencies
        # resolve through the walk-up misa_pkgs/ like everything else).
        print(f"gate 1/6: misa check {man['lib']} ...")
        throws_map = _throws_for_package(pkg_dir, man["lib"], set())
        print("  check clean")

        # Gate 2: `misa test` green + every examples/*.misa runs clean.
        print("gate 2/6: misa test + examples ...")
        p = _run_misa(["test", "."], pkg_dir)
        if p.returncode != 0 and "no test files found" not in (
                p.stderr + p.stdout):
            raise PkgCliError(f"`misa test` failed:\n"
                              f"{p.stdout}{p.stderr}".strip())
        for ex in examples:
            p = _run_misa(["run", os.path.join("examples", ex)], pkg_dir,
                          timeout=60)
            if p.returncode != 0:
                raise PkgCliError(
                    f"example {ex} failed:\n"
                    f"{p.stdout}{p.stderr}".strip())
        print(f"  {len(examples)}/{len(examples)} examples passing")

        # Gate 4: canonical formatting (the toolchain has no
        # `fmt --check`; this is the equivalent check).
        print("gate 4/6: misa fmt clean ...")
        src_files = []
        for dp, _, fns in os.walk(os.path.join(pkg_dir, "src")):
            src_files.extend(os.path.join(dp, f) for f in sorted(fns)
                             if f.endswith(".misa"))
        src_files.extend(os.path.join(pkg_dir, "examples", ex)
                         for ex in examples)
        unformatted = [os.path.relpath(f, pkg_dir)
                       for f in sorted(src_files)
                       if not _fmt_check_file(f)]
        if unformatted:
            raise PkgCliError(
                "not canonically formatted (run `misa fmt --write`):\n  "
                + "\n  ".join(unformatted))
        print("  formatting clean")

        # Signatures (needs the check gate to have passed).
        print("gate 5/6: breaking-change self-check ...")
        sigs = extract_signatures(pkg_dir)

        # Gate 5: declared bump >= computed bump vs previous version.
        try:
            meta = _api_get(f"/v1/packages/{_qname(name)}", registry)
        except PkgCliError as e:
            if "(R404)" in str(e):
                meta = {"versions": []}  # never published: first version
            else:
                raise
        pub_versions = [v.get("version") for v in meta.get("versions", [])
                        if v.get("version")]
        if version in pub_versions:
            raise PkgCliError(
                f"{name}@{version} is already published")
        prev = None
        for pv in pub_versions:
            try:
                if SemVer.parse(pv) < SemVer.parse(version) and (
                        prev is None
                        or SemVer.parse(pv) > SemVer.parse(prev)):
                    prev = pv
            except PkgCliError:
                continue
        if prev is not None:
            old = _api_get(
                f"/v1/packages/{_qname(name)}/{prev}/signatures",
                registry).get("signatures", [])
            diff = diff_signatures(old, sigs)
            declared = bump_kind(prev, version)
            if declared is None:
                raise PkgCliError(
                    f"version {version} is not newer than {prev}")
            if _BUMP_RANK[declared] < _BUMP_RANK[diff["min_bump"]]:
                breaking = [c["detail"] for c in diff["changes"]
                            if c["kind"] == "breaking"]
                raise PkgCliError(
                    f"declared {declared} bump {prev} -> {version} is "
                    f"smaller than the computed {diff['min_bump']} bump:\n"
                    + "\n".join(f"  - {d}" for d in breaking))
            print(f"  {prev} -> {version}: declared {declared}, "
                  f"computed minimum {diff['min_bump']} — ok")
        else:
            print("  first published version — no diff to check")

        # Gate 6: deterministic pack -> sha256.
        print("gate 6/6: pack ...")
        tarball, sha = pack_tarball(pkg_dir)
        print(f"  packed {len(tarball)} bytes, sha256:{sha[:12]}…")

        if dry_run:
            print("dry run: all gates passed (not uploaded)")
            return 0

        # v1.1: sign the tarball when a key exists for this handle.
        sig_obj = None
        key = _load_signing_key(_handle)
        if key is not None:
            sk_hex, pk_hex, _fp = key
            sig_obj = _sign_payload(name, version, tarball, sk_hex, pk_hex)
            print(f"  signed with key {_fp[:16]}…")
        else:
            print("  note: no signing key for this handle "
                  "(`misa pkg keygen`); publishing unsigned")
        body = {
            "manifest": read_manifest_raw(pkg_dir),
            "tarball_b64": base64.b64encode(tarball).decode("ascii"),
            "tarball_sha256": sha,
            "signatures": sigs,
            "capabilities_declared": sorted(
                k for k, v in man["capabilities"].items() if v),
        }
        if sig_obj is not None:
            body["signature"] = sig_obj
        resp = _api_post("/v1/publish", registry, body, token=token)
        _record_publish_local(pkg_dir, sha)
        print(f"published {name}@{version}")
        if resp.get("signed"):
            print(f"signed (key {resp.get('key_fingerprint', '')[:16]}…)")
        if resp.get("docs_url"):
            print(f"docs: {resp['docs_url']}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


# -- add (registry) -------------------------------------------------------------

def cmd_add_registry(arg: str, force: bool, ctx: dict) -> int:
    try:
        name, range_str = _split_add_arg(arg)
        if ctx.get("offline"):
            raise PkgCliError(f"{name}: cannot resolve while --offline")
        registry = resolve_registry(ctx.get("registry"))
        meta = _api_get(f"/v1/packages/{_qname(name)}", registry)
        if meta.get("deprecated"):
            print(f"note: {name} is deprecated: {meta['deprecated']}")
        versions = [(v.get("version"), bool(v.get("yanked")))
                    for v in meta.get("versions", []) if v.get("version")]
        live = [v for v, yanked in versions if not yanked]
        if range_str in ("^latest", "latest", ""):
            chosen = max_satisfying(live, "^latest")
            disp_range = f"^{chosen}" if chosen else "^latest"
        else:
            chosen = max_satisfying(live, range_str)
            disp_range = range_str
        if chosen is None:
            raise PkgCliError(
                f"no published version of {name} satisfies {range_str}")
        vmeta = _api_get(f"/v1/packages/{_qname(name)}/{chosen}", registry)
        if vmeta.get("yanked"):
            raise PkgCliError(f"{name}@{chosen} is yanked")
        data = _fetch_tarball(name, chosen, registry, ctx,
                              vmeta.get("sha256"))
        dest = os.path.join(PKGS_DIR, name)
        if os.path.exists(dest):
            if not force:
                raise PkgCliError(
                    f"{dest} already exists (use --force to overwrite)")
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            else:
                os.remove(dest)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="misa-pkg-")
        try:
            unpack_tarball(data, tmp)
            shutil.move(tmp, dest)
        finally:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
        caps = vmeta.get("capabilities", {}) or {}
        verified = caps.get("verified") or caps.get("declared") or []
        if isinstance(verified, dict):
            verified = [k for k, v in verified.items() if v]
        # throws_surface may be the spec's dict {fn: [codes]} or the
        # server's richer list [{name, signature, throws}]; accept both.
        throws = _norm_throws_surface(vmeta.get("throws_surface", {}))
        # examples may be {passing, total} or {pass, total}; accept both.
        examples = _norm_examples(vmeta.get("examples", {}))
        lock_set(name, chosen, "registry",
                 extra={"sha256": vmeta.get("sha256")
                        or hashlib.sha256(data).hexdigest(),
                        "capabilities": sorted(verified),
                        "range": disp_range,
                        "registry": registry},
                 registry=registry)
        # Spec §4 capability-transparency output.
        short = (vmeta.get("sha256") or "")[:12]
        print(f"resolving {name}@{disp_range} -> {chosen} "
              f"(sha256:{short}…)")
        print("capabilities: " + (" ".join(sorted(verified))
                                  if verified else "-"))
        ecodes = sorted({c for codes in throws.values()
                         for c in (codes or [])})
        print("throws surface: " + (" ".join(ecodes) if ecodes else "-"))
        if examples:
            print(f"examples: {examples.get('passing', '?')}/"
                  f"{examples.get('total', '?')} passing")
        if vmeta.get("deprecated"):
            print(f"note: {name} is deprecated: {vmeta['deprecated']}")
        print("lockfile updated: misa.lock")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


# -- search / show / dependents / diff -------------------------------------------

def _looks_typed_query(q: str) -> bool:
    return bool(re.search(
        r"fn\s*\(|->|throws\s+E\d|capability:|examples:|\bimpl\s+"
        r"|\binterface\s+|-\s*capability:", q))


def cmd_search(query: str, kind: str | None, ctx: dict) -> int:
    try:
        registry = resolve_registry(ctx.get("registry"))
        if kind is None:
            kind = "semantic" if _looks_typed_query(query) else "keyword"
        if kind not in ("keyword", "semantic"):
            raise PkgCliError("--kind must be keyword|semantic")
        res = _api_get("/v1/search", registry,
                       params={"q": query, "kind": kind})
        results = res.get("results", [])
        if ctx.get("json"):
            print(json.dumps({"schema": JSON_SCHEMA, "query": query,
                              "kind": kind, "results": results}, indent=2))
            return 0
        if not results:
            print("no packages found")
            return 0
        for r in results:
            name = r.get("name") or r.get("package") or "?"
            desc = r.get("description", "")
            print(f"{name} {r.get('version', '')}"
                  + (f" — {desc}" if desc else ""))
            caps = r.get("capabilities") or []
            ex = _norm_examples(r.get("examples") or {})
            extra = []
            if caps:
                extra.append("capabilities: " + " ".join(caps))
            if ex:
                extra.append(f"examples: {ex.get('passing', '?')}/"
                             f"{ex.get('total', '?')} passing")
            if extra:
                print("  " + " · ".join(extra))
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_show(spec: str, ctx: dict) -> int:
    try:
        if not _looks_registry_arg(spec):
            raise PkgCliError(
                f"bad package name {spec!r} (registry packages are "
                "'@org/name[@version]')")
        body = spec[1:]
        if "@" in body:
            name_part, ver = body.split("@", 1)
        else:
            name_part, ver = body, ""
        name = "@" + name_part
        registry = resolve_registry(ctx.get("registry"))
        meta = _api_get(f"/v1/packages/{_qname(name)}", registry)
        versions = [v.get("version") for v in meta.get("versions", [])
                    if v.get("version") and not v.get("yanked")]
        if not ver:
            ver = max_satisfying(versions, "^latest")
            if ver is None:
                raise PkgCliError(f"{name}: no published versions")
        vmeta = _api_get(f"/v1/packages/{_qname(name)}/{ver}", registry)
        sigs = _api_get(f"/v1/packages/{_qname(name)}/{ver}/signatures",
                        registry).get("signatures", [])
        try:
            deps = _api_get(f"/v1/packages/{_qname(name)}/dependents",
                            registry).get("dependents", [])
        except PkgCliError:
            deps = []
        caps = vmeta.get("capabilities", {}) or {}
        if ctx.get("json"):
            print(json.dumps({
                "schema": JSON_SCHEMA,
                "name": name, "version": ver,
                "description": vmeta.get("description", ""),
                "license": vmeta.get("license", ""),
                "homepage": vmeta.get("homepage", ""),
                "repository": vmeta.get("repository", ""),
                "sha256": vmeta.get("sha256", ""),
                "yanked": bool(vmeta.get("yanked")),
                "yank_reason": vmeta.get("yank_reason"),
                "deprecated": meta.get("deprecated"),
                "capabilities": caps,
                "throws_surface": vmeta.get("throws_surface", {}),
                "examples": vmeta.get("examples", {}),
                "signatures": sigs,
                "dependents_count": len(deps),
                "docs_url": vmeta.get("docs_url"),
                "downloads_30d": vmeta.get("downloads_30d", 0),
            }, indent=2))
            return 0
        print(f"{name} {ver}")
        if vmeta.get("description"):
            print(f"  {vmeta['description']}")
        if vmeta.get("license"):
            print(f"  license: {vmeta['license']}")
        declared = caps.get("declared") or []
        verified = caps.get("verified") or []
        if declared or verified:
            print(f"  capabilities declared: "
                  + (" ".join(declared) if declared else "-"))
            print(f"  capabilities verified:  "
                  + (" ".join(verified) if verified else "-"))
        throws = _norm_throws_surface(vmeta.get("throws_surface", {}))
        if throws:
            print("  throws surface (computed, not declared):")
            for fn in sorted(throws):
                codes = " ".join(sorted(throws[fn] or [])) or "-"
                print(f"    {fn}: {codes}")
        ex = _norm_examples(vmeta.get("examples", {}))
        if ex:
            print(f"  examples: {ex.get('passing', '?')}/"
                  f"{ex.get('total', '?')} passing")
        if sigs:
            print(f"  signatures: {len(sigs)} "
                  "(fn/interface/impl, see --json)")
        print(f"  dependents: {len(deps)}")
        if vmeta.get("downloads_30d") is not None:
            print(f"  downloads (30d): {vmeta['downloads_30d']}")
        if vmeta.get("yanked"):
            print(f"  YANKED: {vmeta.get('yank_reason', '')}")
        if meta.get("deprecated"):
            print(f"  deprecated: {meta['deprecated']}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_graph(ctx: dict) -> int:
    """v1.1: print the resolved dependency graph (from misa.lock)."""
    ctx = ctx or {}
    try:
        if not os.path.isfile(LOCK):
            raise PkgCliError("no misa.lock (run `misa pkg add` first)")
        _, lock_registry, entries = read_lock_full()
        registry = lock_registry or resolve_registry(ctx.get("registry"))
        nodes = []
        edges = []
        seen = set()
        # seed with direct entries; expand via the registry graph API
        queue = [(e.get("name", ""), e.get("version", "")) for e in entries
                 if e.get("source") == "registry"]
        try:
            me = read_manifest(os.getcwd())
            me_name = me.get("name", "")
        except PkgCliError:
            me_name = ""
        while queue:
            name, version = queue.pop(0)
            key = name + "@" + version
            if key in seen or not name:
                continue
            seen.add(key)
            try:
                g = _api_get(f"/v1/graph/{_qname(name)}/{version}", registry)
            except PkgCliError:
                continue
            for n in g.get("nodes", []):
                nid = n.get("name", "") + "@" + n.get("version", "")
                if nid not in seen:
                    nodes.append(n)
            for ed in g.get("edges", []):
                edges.append(ed)
                t = ed.get("to", "")
                if "@" in t:
                    tn, tv = t.rsplit("@", 1)
                    if tn + "@" + tv not in seen:
                        queue.append((tn, tv))
        # local root node
        if me_name:
            nodes.insert(0, {"name": me_name, "version": "local",
                             "signed": False, "tier": "-"})
        if ctx.get("json"):
            print(json.dumps({"nodes": nodes, "edges": edges}, indent=2))
        else:
            for n in nodes:
                badge = ""
                if n.get("signed"):
                    badge += " [signed]"
                tier = n.get("tier", "")
                if tier and tier not in ("unverified", "-"):
                    badge += f" [{tier}]"
                print(f"{n.get('name')}@{n.get('version')}{badge}")
            for ed in edges:
                print(f"  {ed.get('from')} -> {ed.get('to')}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_dependents(name: str, ctx: dict) -> int:
    try:
        if not NAMESPACE_RE.match(name):
            raise PkgCliError(
                f"bad package name {name!r} (registry packages are "
                "'@org/name')")
        registry = resolve_registry(ctx.get("registry"))
        deps = _api_get(f"/v1/packages/{_qname(name)}/dependents",
                        registry).get("dependents", [])
        if ctx.get("json"):
            print(json.dumps({"schema": JSON_SCHEMA, "package": name,
                              "dependents": deps}, indent=2))
            return 0
        if not deps:
            print(f"no packages depend on {name}")
            return 0
        print(f"{name} is depended on by {len(deps)} package(s):")
        for d in deps:
            print(f"  {d.get('name')} {d.get('version', '')}"
                  + (f"  (range {d['range']})" if d.get("range") else ""))
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_diff(name: str, old: str, new: str, ctx: dict) -> int:
    try:
        if not NAMESPACE_RE.match(name):
            raise PkgCliError(
                f"bad package name {name!r} (registry packages are "
                "'@org/name')")
        SemVer.parse(old)
        SemVer.parse(new)
        registry = resolve_registry(ctx.get("registry"))
        old_sigs = _api_get(
            f"/v1/packages/{_qname(name)}/{old}/signatures",
            registry).get("signatures", [])
        new_sigs = _api_get(
            f"/v1/packages/{_qname(name)}/{new}/signatures",
            registry).get("signatures", [])
        diff = diff_signatures(old_sigs, new_sigs)
        declared = bump_kind(old, new)
        print(f"{name}: {old} -> {new}")
        print(f"minimum bump: {diff['min_bump']}"
              + (f" (declared: {declared})" if declared else ""))
        if not diff["changes"]:
            print("no signature changes")
        for c in diff["changes"]:
            mark = {"breaking": "!", "addition": "+", "ok": " "}[c["kind"]]
            print(f"  [{mark}] {c['detail']}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


# -- yank / deprecate -------------------------------------------------------------

def _split_name_ver(spec: str) -> tuple:
    """`@org/pkg@1.2.0` -> (name, version)."""
    body = spec[1:] if spec.startswith("@") else spec
    if "@" not in body:
        raise PkgCliError(
            f"{spec!r}: expected @org/pkg@version")
    name_part, ver = body.rsplit("@", 1)
    name = "@" + name_part
    if not NAMESPACE_RE.match(name):
        raise PkgCliError(f"bad package name {name!r}")
    SemVer.parse(ver)
    return name, ver


def cmd_yank(spec: str, reason: str, ctx: dict) -> int:
    try:
        name, ver = _split_name_ver(spec)
        token, _ = _require_token()
        registry = resolve_registry(ctx.get("registry"))
        _api_post("/v1/yank", registry,
                  {"pkg": name, "version": ver, "reason": reason},
                  token=token)
        print(f"yanked {name}@{ver}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_deprecate(name: str, message: str, ctx: dict) -> int:
    try:
        if not NAMESPACE_RE.match(name):
            raise PkgCliError(f"bad package name {name!r}")
        token, _ = _require_token()
        registry = resolve_registry(ctx.get("registry"))
        _api_post("/v1/deprecate", registry,
                  {"pkg": name, "message": message}, token=token)
        print(f"deprecated {name}: {message}")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


# -- audit / vendor / outdated / update --------------------------------------------

_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def cmd_audit(ctx: dict) -> int:
    try:
        registry = resolve_registry(ctx.get("registry"))
        entries = [e for e in read_lock()
                   if e.get("source") == "registry"]
        findings = []
        for e in entries:
            name, version = e.get("name", ""), e.get("version", "")
            advs = _api_get("/v1/advisories", registry,
                            params={"package": name}
                            ).get("advisories", [])
            for a in advs:
                if a.get("state") not in (None, "published", "fixed"):
                    continue
                affected = a.get("affected", "")
                try:
                    hit = satisfies(version, affected) if affected else True
                except PkgCliError:
                    hit = False
                if hit:
                    findings.append({"package": name, "version": version,
                                     "advisory": a})
        worst = max([_SEV_RANK.get(
            f["advisory"].get("severity", "low"), 0) for f in findings]
            or [0])
        if ctx.get("json"):
            print(json.dumps({"schema": JSON_SCHEMA,
                              "findings": findings}, indent=2))
        else:
            if not findings:
                print("no advisories for locked dependencies")
            for f in findings:
                a = f["advisory"]
                print(f"{a.get('severity', 'low').upper()} "
                      f"{f['package']}@{f['version']}: "
                      f"{a.get('id', '?')} — {a.get('summary', '')}"
                      + (f" (fixed in {a['fixed_in']})"
                         if a.get("fixed_in") else ""))
        return 1 if worst >= _SEV_RANK["high"] else 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_vendor(ctx: dict) -> int:
    try:
        _, lock_registry, entries = read_lock_full()
        reg_entries = [e for e in entries
                       if e.get("source") == "registry"]
        if not reg_entries:
            print("no registry dependencies to vendor")
            return 0
        os.makedirs("vendor", exist_ok=True)
        for e in reg_entries:
            name, version = e.get("name", ""), e.get("version", "")
            registry = (e.get("registry") or lock_registry
                        or resolve_registry(ctx.get("registry")))
            data = _fetch_tarball(name, version, registry, ctx,
                                  e.get("sha256"), prefer_vendor=False)
            vp = os.path.join("vendor", _vendor_name(name, version))
            with open(vp, "wb") as f:
                f.write(data)
            print(f"vendored {name}@{version} -> {vp} "
                  f"({len(data)} bytes, sha256 verified)")
        print("vendor/ is the offline install source "
              "(`misa pkg install --offline` prefers it)")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def _registry_entries():
    _, lock_registry, entries = read_lock_full()
    return lock_registry, [e for e in entries
                           if e.get("source") == "registry"]


def cmd_outdated(ctx: dict) -> int:
    try:
        lock_registry, entries = _registry_entries()
        if not entries:
            print("no registry dependencies")
            return 0
        rows = []
        for e in entries:
            name, version = e.get("name", ""), e.get("version", "")
            registry = (e.get("registry") or lock_registry
                        or resolve_registry(ctx.get("registry")))
            rng = e.get("range") or f"^{version}"
            meta = _api_get(f"/v1/packages/{_qname(name)}", registry)
            live = [v.get("version") for v in meta.get("versions", [])
                    if v.get("version") and not v.get("yanked")]
            latest = max_satisfying(live, rng)
            if latest and SemVer.parse(latest) > SemVer.parse(version):
                rows.append({"name": name, "current": version,
                             "latest_compatible": latest, "range": rng})
        if ctx.get("json"):
            print(json.dumps({"schema": JSON_SCHEMA,
                              "packages": rows}, indent=2))
            return 0
        if not rows:
            print("all locked dependencies are up to date")
            return 0
        for r in rows:
            print(f"{r['name']}: {r['current']} -> "
                  f"{r['latest_compatible']}  (range {r['range']})")
        return 0
    except PkgCliError as e:
        return _fail(str(e))


def cmd_update(name: str | None, ctx: dict) -> int:
    try:
        lock_registry, entries = _registry_entries()
        if not entries:
            print("no registry dependencies")
            return 0
        if name and not NAMESPACE_RE.match(name):
            raise PkgCliError(f"bad package name {name!r}")
        if ctx.get("offline"):
            raise PkgCliError("cannot update while --offline")
        targets = [e for e in entries
                   if name is None or e.get("name") == name]
        if name and not targets:
            raise PkgCliError(f"{name}: not a locked registry dependency")
        updated = 0
        for e in targets:
            pkg_name, version = e.get("name", ""), e.get("version", "")
            registry = (e.get("registry") or lock_registry
                        or resolve_registry(ctx.get("registry")))
            rng = e.get("range") or f"^{version}"
            meta = _api_get(f"/v1/packages/{_qname(pkg_name)}", registry)
            live = [v.get("version") for v in meta.get("versions", [])
                    if v.get("version") and not v.get("yanked")]
            chosen = max_satisfying(live, rng)
            if chosen is None:
                print(f"{pkg_name}: no published version satisfies "
                      f"{rng} (kept {version})")
                continue
            if SemVer.parse(chosen) <= SemVer.parse(version):
                print(f"{pkg_name}: already at newest ({version})")
                continue
            vmeta = _api_get(
                f"/v1/packages/{_qname(pkg_name)}/{chosen}", registry)
            dest = os.path.join(PKGS_DIR, pkg_name)
            _install_registry_entry(
                {"name": pkg_name, "version": chosen,
                 "sha256": vmeta.get("sha256")}, dest, ctx, registry)
            caps = vmeta.get("capabilities", {}) or {}
            verified = caps.get("verified") or caps.get("declared") or []
            if isinstance(verified, dict):
                verified = [k for k, v in verified.items() if v]
            lock_set(pkg_name, chosen, "registry",
                     extra={"sha256": vmeta.get("sha256"),
                            "capabilities": sorted(verified),
                            "range": rng, "registry": registry},
                     registry=registry)
            print(f"updated {pkg_name}: {version} -> {chosen}")
            updated += 1
        if updated:
            print("lockfile updated: misa.lock")
        return 0
    except PkgCliError as e:
        return _fail(str(e))
