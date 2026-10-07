# Misa Registry — Client API Contract (`/v1/`)

What `misa pkg` (see `impl/misa/pkg.py`) codes against. This is the
**client-side** contract; the server (a Misa program, spec/REGISTRY.md
§8) must honor it. All bodies are UTF-8 JSON. Errors are
`{"error": {"code": "R...", "message": "..."}}`.

## Registry URL resolution

`--registry <url>` flag → `MISA_REGISTRY` env → `~/.misa/config` TOML
(`[registries] default = "..."`) → built-in default
`https://pkg.misa.website`.

## Names in URLs

A namespaced name (`@acme/router`) uses the spec's two-segment URL
form, one segment per part: `/v1/packages/@acme/router`. Each segment
is percent-encoded separately, so the `/` separator is never ambiguous.

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/packages/<name>` | – | metadata + version list |
| GET | `/v1/packages/<name>/<version>` | – | version metadata (sha256, capabilities, throws surface, examples) |
| GET | `/v1/packages/<name>/<version>/signatures` | – | typed signature index (§6.1) |
| GET | `/v1/packages/<name>/dependents` | – | reverse deps (§7.3) |
| GET | `/v1/tarballs/<name>/<version>` | – | `{"sha256", "tarball_b64"}` (v1: JSON, not raw bytes) |
| GET | `/v1/search?q=…&kind=keyword\|semantic` | – | §6.1 typed semantic search |
| GET | `/v1/advisories?package=<name>` | – | §7.5 advisory DB |
| POST | `/v1/publish` | Bearer | JSON body (v1: not multipart) |
| POST | `/v1/yank` | Bearer | `{pkg, version, reason}` |
| POST | `/v1/deprecate` | Bearer | `{pkg, message}` |
| POST | `/v1/tokens/revoke` | Bearer | `{token_sha256}` (client-defined; the spec has no revoke endpoint) |

### `GET /v1/packages/<name>`

```json
{
  "name": "@acme/router",
  "description": "Typed HTTP router for Misa",
  "deprecated": null,
  "versions": [
    {"version": "1.2.0", "yanked": false,
     "published_at": "2026-10-07T00:00:00Z"}
  ]
}
```

### `GET /v1/packages/<name>/<version>`

```json
{
  "name": "@acme/router", "version": "1.2.0",
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "license": "MIT", "description": "…",
  "homepage": "https://…", "repository": "https://…",
  "yanked": false, "yank_reason": null,
  "deprecated": null,
  "capabilities": {"declared": ["network"], "verified": ["network"]},
  "throws_surface": {"serve": ["E304"], "get": ["E304"]},
  "examples": {"total": 4, "passing": 4},
  "docs_url": "https://…/docs/@acme/router/1.2.0/",
  "downloads_30d": 12408
}
```

### `GET /v1/packages/<name>/<version>/signatures`

```json
{"signatures": [
  {"kind": "fn", "name": "get",
   "params": [["u", "Text"]], "ret": "Text",
   "throws": ["E304"], "doc": "fetches a url", "file": "src/lib.misa"},
  {"kind": "interface", "name": "Cache",
   "methods": [{"name": "get", "params": [], "ret": "Text",
                "throws": []}],
   "doc": "", "file": "src/cache.misa"},
  {"kind": "impl", "name": "Cache for MemCache",
   "iface": "Cache", "target": "MemCache",
   "methods": ["get"], "doc": "", "file": "src/cache.misa"}
]}
```

`_`-prefixed fns are excluded (private by convention; Misa has no
visibility modifier). `throws` is the checker's *inferred* set.

### `GET /v1/tarballs/<name>/<version>`

```json
{"sha256": "e3b0…", "tarball_b64": "H4sIAAAAAAAC…"}
```

The client verifies `sha256(base64decode(tarball_b64))` against both
this `sha256` and the lockfile's hash **before** unpacking.

### `GET /v1/search?q=…&kind=…`

```json
{"results": [
  {"name": "@acme/router", "version": "1.2.0",
   "description": "…", "capabilities": ["network"],
   "examples": {"total": 4, "passing": 4}}
]}
```

The typed query language (§6.1) is passed through verbatim in `q`;
`kind=semantic` is auto-selected when the query contains typed
tokens (`fn(`, `->`, `throws E…`, `capability:`, `impl`, `interface`,
`examples:`).

### `GET /v1/advisories?package=<name>`

```json
{"advisories": [
  {"id": "MISA-2026-0001", "package": "@acme/router",
   "severity": "high", "summary": "…",
   "affected": "<1.2.0", "fixed_in": "1.2.1",
   "published_at": "2026-10-07T00:00:00Z", "state": "published"}
]}
```

`affected` uses the same range syntax as the client
(`^ ~ = < <= > >=`, `,`-AND).

### `POST /v1/publish`

```json
{
  "manifest": {"package": {"name": "@acme/router", "version": "1.2.0",
                           "description": "…", "license": "MIT", …},
               "capabilities": {"network": true},
               "dependencies": {"@acme/middleware": "^1.4.0"}},
  "tarball_b64": "H4sIAAAAAAAC…",
  "tarball_sha256": "e3b0…",
  "signatures": [ … as above … ],
  "capabilities_declared": ["network"]
}
```

### `POST /v1/yank` / `/v1/deprecate`

```json
{"pkg": "@acme/router", "version": "1.2.0", "reason": "…"}
{"pkg": "@acme/router", "message": "use @acme/router2"}
```

## `--json` schemas (all carry `"schema": "misa.pkg.v1"`)

- `pkg list --json` → `{"packages": [{name, version, source,
  capabilities}]}`
- `pkg search --json` → `{"query", "kind", "results": […]}` (result
  rows as above)
- `pkg show --json` → version metadata + `signatures`,
  `dependents_count`
- `pkg dependents --json` → `{"package", "dependents":
  [{name, version, range}]}`
- `pkg audit --json` → `{"findings": [{package, version, advisory}]}`
- `pkg outdated --json` → `{"packages": [{name, current,
  latest_compatible, range}]}`

## Lockfile v2 (`misa.lock`)

```toml
version = 2
registry = "https://pkg.misa.website"

[[package]]
name = "@acme/router"
version = "1.2.0"
sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
capabilities = ["network", "fs"]
source = "registry"
range = "^1.2.0"        # client extension: resolved-from range
registry = "https://pkg.misa.website"   # client extension: per-entry source
```

`range`/`registry` are client extensions beyond spec §5 (needed by
`update`/`outdated` and private registries). v1 lockfiles (git/path
only, no `version` header) remain valid.

## Deviations from spec/REGISTRY.md

The full list lives in the header of
`tests/pkg/run_registry_tests.py` (each deviation is covered by a
test). In short: JSON publish body (not multipart); tarballs served
as JSON `{sha256, tarball_b64}`; single-segment percent-encoded
names; `misa fmt --check` implemented client-side via the Formatter
(the toolchain has no such flag); client-defined
`POST /v1/tokens/revoke`; `_`-prefixed fns excluded from the
signature index; removed `impl` counts as minor; lockfile `range` /
`registry` extensions; bare `1.2.0` in a range means exact;
yanked versions excluded from fresh resolution; v1 login is
paste-a-token.
