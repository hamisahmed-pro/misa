# Misa Packages — `misa pkg` (Phase 5)

Packages are how Misa code is shared and reused. This document defines
the package layout, manifest, import resolution, lockfile, CLI, and the
bundled standard library. Private phase: git + path sources only; a
central registry arrives with the public launch (Phase 8).

## 1. Package layout

```
my-pkg/
  misa.pkg            # manifest (TOML, required)
  src/
    lib.misa          # package entry point (default; overridable)
    sub/
      thing.misa      # importable as "my-pkg/sub/thing"
```

### 1.1 Manifest (`misa.pkg`)

```toml
[package]
name = "my-pkg"            # required: lowercase letters, digits, hyphens
version = "0.1.0"          # required: semver MAJOR.MINOR.PATCH
description = "..."        # optional
lib = "src/lib.misa"       # optional, default "src/lib.misa"
```

Validation errors are reported as `E113` (bad manifest: missing/invalid
fields, bad name, bad version). A directory without `misa.pkg` is not a
package.

## 2. Import syntax (extends `importDecl`)

The grammar is unchanged (`importDecl := "import" STRING`); the
*interpretation* of the string gains package forms:

| Form | Meaning |
|---|---|
| `import "./util.misa"` | Relative file (existing behavior) |
| `import "util.misa"` | Relative file (existing behavior) |
| `import "my-pkg"` | Package `my-pkg`, entry from its manifest |
| `import "my-pkg/sub/thing"` | Package `my-pkg`, file `src/sub/thing.misa` |

Rules:
- A string ending in `.misa` is always a relative file import
  (unchanged from v0.2).
- A string starting with `./`, `../`, or `/` is always a file path
  (must end in `.misa`, else E112 as before).
- Anything else is a package import: the first `/`-separated segment is
  the package name; the remainder (if any) is a path under the
  package's `src/`, with `.misa` appended when absent.
- `import "foo"` (no slash, no `.misa`) was an error in v0.2, so this
  introduces no backward incompatibility.

Inside a package, relative imports keep working (resolved against the
importing file), and package imports resolve through the same search
described below.

## 3. Resolution

For `import "my-pkg/..."` appearing in a file at directory D, search in
order:

1. Walk up from D: `<dir>/misa_pkgs/my-pkg/` for D, then each parent.
2. `~/.misa/pkgs/my-pkg/` (user-global packages).
3. The toolchain's bundled stdlib: `<misa-home>/stdlib/my-pkg/`.

The first directory containing a valid `misa.pkg` with matching `name`
wins. The imported file is `src/<rest>.misa`, or the manifest's `lib`
when no rest is given.

Failure modes (all `E112` unless noted):
- No package found → `cannot find package 'my-pkg'` (hint lists the
  searched locations).
- Manifest invalid → `E113` with the specific problem.
- Entry/module file missing → `cannot find module '...' in package
  'my-pkg'`.
- Name mismatch between directory/manifest → `E113`.

Cycles across packages are detected by the existing E111 machinery
(resolution is by absolute file path, so it just works).

## 4. Lockfile (`misa.lock`)

TOML, written by `misa pkg add`, read by `misa pkg install`:

```toml
[[package]]
name = "my-pkg"
version = "0.1.0"
source = "git+https://example.com/my-pkg.git@a1b2c3d"
# or: source = "path+../my-pkg"
```

`misa pkg install` reproduces `misa_pkgs/` exactly from the lockfile.
If `misa.lock` is absent, `install` is a no-op with a note.

## 5. CLI

```
misa pkg init [name]        scaffold misa.pkg + src/lib.misa in .
misa pkg add <git-url|path> install into misa_pkgs/, update misa.lock
misa pkg install            install from misa.lock
misa pkg list               installed packages (name, version, source)
misa pkg remove <name>      remove from misa_pkgs/ and misa.lock
```

- `add <git-url>`: `git clone` (shallow, default branch) into
  `misa_pkgs/<name>/`; record `git+<url>@<rev>` in the lock. A `#rev`
  suffix pins a revision.
- `add <path>`: copy the directory into `misa_pkgs/<name>/`; record
  `path+<abs>`. (Copy, not symlink: reproducible builds.)
- All commands refuse to overwrite without `--force`.

Registry commands (`misa pkg publish`, `search`) are explicitly
deferred to the public phase.

## 6. Standard library (bundled)

`<misa-home>/stdlib/` ships packages maintained with the toolchain,
importable as `import "std/..."`:

| Package | Contents (seed) |
|---|---|
| `std/strings` | `trim`, `split`, `join`, `starts_with`, `ends_with`, `contains`, `repeat`, `pad_left`, `pad_right` |
| `std/lists` | `map`, `filter`, `fold`, `any`, `all`, `flatten`, `zip`, `range` |
| `std/json` | `stringify_pretty`, `get_path` (safe nested lookup returning a default) |
| `std/http` | `get_json` (http.get + JSON parse with catchable errors), `post_json` |
| `std/result` | `ok`/`err` helpers and `unwrap_or` over the `Error` type |

Rules for stdlib:
- Pure Misa only (no native code); must pass `misa test` in CI.
- One spelling per helper; names are `snake_case`.
- Every public `fn` has a doc comment; `docs/STDLIB.md` is generated
  from them (keep the generator trivial: extract `///` comments).
- New modules need a rationale in the PR: what app needed it.

## 7. Integration points

- **Loader** (`impl/misa/loader.py`): package resolution happens here,
  so `run`, `check`, `test`, `build`, `fmt`, and the LSP all get it
  for free.
- **AOT/build**: unchanged — the loader merges packages into one
  program before lowering.
- **LSP**: `publishDiagnostics` must surface E112/E113 for bad imports
  (it uses the loader already); add completion for installed package
  names (nice-to-have, not required).
- **`misa serve`**: serves apps whose imports include packages with no
  changes (build handles it).

## 8. Non-goals (private phase)

- Central registry, publishing, search, version ranges, dependency
  solver (exact pins only), private registries, yank.
- Namespace packages, vendoring beyond `misa_pkgs/`.

## 9. Test plan

- `tests/pkg/`: manifest validation (good/bad), resolution order
  (local beats parent beats global beats stdlib), entry vs module
  imports, missing package/module errors (E112), bad manifest (E113),
  cycles across packages (E111), lockfile round-trip
  (add → remove lock → install → identical tree), `pkg init` scaffold.
- Stdlib: each module has `misa test` coverage; CI runs it.
