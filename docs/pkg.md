# `misa pkg` — packages and the registry

Misa's package manager installs versioned, hash-locked dependencies from
the Misa package registry (and from git / local paths, as before).
Every install is content-verified: `misa.lock` pins the exact bytes
(sha256) of each dependency, and yanked versions never break a locked
build.

> **Alpha honesty.** The registry is new and small. The client commands
> below are fully working against a live server; the public package
> corpus is still being seeded.

## Registry configuration

The registry URL is configuration, never hardcoded:

```toml
# ~/.misa/config
[registries]
default = "https://pkg.misa.website"
```

Precedence: `--registry <url>` flag → `MISA_REGISTRY` env var →
`~/.misa/config` → the default above. Point it at a private registry
(e.g. `https://pkgs.acme.corp`) for company-internal packages.

## Using packages

```bash
misa pkg add @acme/router            # latest, caret range (^)
misa pkg add @acme/router@^1.2.0     # compatible range
misa pkg add @acme/router@~1.2.0     # patch-only range
misa pkg add @acme/router@=1.2.0     # exact version
```

`add` resolves the range, downloads the tarball, **verifies its sha256
before unpacking**, and updates `misa.lock`:

```bash
$ misa pkg add @acme/router
resolving @acme/router@^1.2.0 -> 1.2.0 (sha256:e3b0c442…)
capabilities: network fs          # verified by the registry, not claimed
throws surface: E304 E303         # what it can raise into YOUR code
examples: 4/4 passing             # executed by registry CI
lockfile updated: misa.lock
```

Then use it in code — namespaced imports resolve to `misa_pkgs/`:

```misa
import "@acme/router"

fn main() {
  let r = router.get("https://example.com")
  print(r)
}
```

Other commands:

```bash
misa pkg install                  # install exactly from misa.lock (CI)
misa pkg list                     # installed packages + capabilities
misa pkg remove @acme/router      # remove from misa_pkgs/ and misa.lock
misa pkg search "http router"     # keyword search
misa pkg search "fn(Text) -> [Text] throws E304"   # typed semantic search
misa pkg show @acme/router        # metadata, throws surface, dependents
misa pkg show @acme/router --json # machine-readable (schema: misa.pkg.v1)
misa pkg outdated                 # newer compatible versions available
misa pkg update [@acme/router]    # re-resolve ranges, rewrite misa.lock
misa pkg dependents @acme/router  # reverse dependencies
misa pkg audit                    # check misa.lock against advisories
misa pkg vendor                   # copy verified tarballs into vendor/
misa pkg install --offline        # install from vendor/ only, no network
```

## Typed semantic search

The registry indexes every published version's **typed function
signatures** — search by type, not keyword:

```
fn(Text) -> [Text] throws E304      # functions taking Text, returning [Text], raising E304
impl Drawable                        # packages implementing interface Drawable
interface Cache                      # packages declaring interface Cache
throws E400                          # anything whose error surface includes E400
capability:network -capability:model # capability filters
examples:passing                      # only packages with green examples
```

Queries compose: `fn(*)->Response throws E304 capability:network
examples:passing`. This is why `misa pkg search` understands the query
language directly.

## Publishing packages

```bash
misa pkg init @myorg/mypkg        # scaffold misa.pkg + src/lib.misa + examples/
misa pkg login                    # paste a publisher token (stored in ~/.misa/credentials, mode 0600)
misa pkg publish --dry-run        # run all pre-publish gates, upload nothing
misa pkg publish                  # publish for real
misa pkg logout                   # forget the token locally
misa pkg logout --revoke          # also revoke it server-side
```

`misa.pkg` for publishing:

```toml
[package]
name = "@myorg/mypkg"              # required: @namespace/name
version = "1.0.0"                  # required: semver
description = "What it does"       # required for publish
license = "MIT"                    # required for publish (SPDX)
lib = "src/lib.misa"

[capabilities]                     # declared; VERIFIED by the registry
network = false
fs = true
process = false
model = false

[dependencies]
"@acme/middleware" = "^1.4.0"
```

**Pre-publish gates** (all must pass): `misa check` clean · `misa test`
green including `examples/*.misa` (at least one example required) ·
manifest valid · canonical formatting · breaking-change self-check
(declared bump must be ≥ the computed bump from the signature diff) ·
deterministic tarball + sha256.

**Computed, not trusted.** The registry re-verifies your package at
publish time: it runs the checker itself, extracts signatures, infers
capabilities from the throws graph (declared ⊋ inferred → rejected;
declared ⊊ inferred → upgraded with a warning), executes your examples,
and diffs your signatures against the previous version — a minor bump
that removes a function is **rejected**. See `spec/REGISTRY.md` §6.

Namespace management:

```bash
misa pkg yank @myorg/mypkg@1.0.0 --reason "..."   # hide from search/resolution (lockfiles keep working)
misa pkg deprecate @myorg/mypkg --message "use @myorg/mypkg2"
```

## The lockfile

`misa.lock` (v2) pins bytes, not wishes:

```toml
version = 2
registry = "https://pkg.misa.website"

[[package]]
name = "@acme/router"
version = "1.2.0"
sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
capabilities = ["network", "fs"]
source = "registry"
```

`misa pkg install` verifies every hash before unpacking; a mismatch
fails loudly with expected vs actual. Commit `misa.lock` — it's the
audit input (`pkg audit`) and the vendoring input (`pkg vendor`).

## Offline and private registries

```bash
misa pkg vendor                    # materialize all locked tarballs into vendor/
misa pkg install --offline         # resolve from vendor/ only
misa pkg add @acme/secret --registry https://pkgs.acme.corp
```

API reference for agents and tooling: `docs/REGISTRY-API.md`
(everything is JSON-first, `--json` on all read commands).
