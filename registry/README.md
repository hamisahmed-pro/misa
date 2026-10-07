# Misa Package Registry — Server

The registry server is itself a Misa program (`server.misa`), per
spec/REGISTRY.md §8. It implements the full publish pipeline with
"computed, not trusted" verification: every publish runs the real
toolchain (`misa check`, signature extraction, capability inference,
`misa test`, example execution) inside one SQLite transaction.

## Quick start

```bash
# 1. One-shot init: creates ./data/registry.db + all 11 tables,
#    prints an ADMIN token (shown once; only its sha256 is stored).
MISA_ROOT=/path/to/misa REGISTRY_DATA=./data \
  python3 impl/bin/misa run registry/admin.misa

# 2. Boot the server (PORT default 8080; binds 0.0.0.0).
MISA_ROOT=/path/to/misa REGISTRY_DATA=./data \
REGISTRY_ADMIN_TOKEN=<token-from-step-1> PORT=8080 \
  python3 impl/bin/misa run registry/server.misa

# 3. Restore the seed corpus (ephemeral-disk recovery).
MISA_ROOT=/path/to/misa REGISTRY_TOKEN=<admin-or-publisher-token> \
SEED_URL=http://127.0.0.1:8080 \
  python3 impl/bin/misa run registry/seed.misa
```

## Publishing

Mint a scoped publisher token, then publish:

```bash
# Mint (admin only):
curl -X POST localhost:8080/v1/admin/tokens \
  -H "Authorization: Bearer <admin-token>" \
  -H 'Content-Type: application/json' \
  -d '{"scopes":["publish:@acme/*","yank:@acme/*"]}'

# Pack + publish a package dir (deterministic tar):
python3 registry/helpers/pack.py ./mypkg /tmp/mypkg.tar.gz
SHA=$(sha256sum /tmp/mypkg.tar.gz | cut -d' ' -f1)
B64=$(python3 -c "import base64,sys; sys.stdout.write(
  base64.b64encode(open('/tmp/mypkg.tar.gz','rb').read()).decode())")
MAN=$(python3 registry/helpers/pkg_to_json.py ./mypkg)
python3 -c "
import json
body = {'manifest': json.loads('''$MAN'''),
        'tarball_b64': '''$B64''', 'tarball_sha256': '''$SHA'''}
open('/tmp/body.json','w').write(json.dumps(body))"
curl -X POST localhost:8080/v1/publish \
  -H "Authorization: Bearer <publisher-token>" \
  -H 'Content-Type: application/json' \
  --data @/tmp/body.json
```

The publish pipeline (§8.5): Bearer auth + scope check → E113
manifest validation → namespace first-claim → sha256 recompute (must
match) → unpack → secret scan (`sk-`, `AKIA`, `ghp_`, private-key
headers, …) → `misa check --json` → signature extraction → declared-
vs-inferred capability check (§6.3: reject over-declaration, warn and
upgrade under-declaration) → examples executed (60 s timeout each) →
`misa test` → breaking diff vs previous version (§6.5: reject if the
declared bump is smaller than computed) → one SQLite transaction →
content-addressed blob storage.

## Endpoints (all JSON; errors are `{"error":{"code","message"}}`)

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/` | service info + configured registry URL |
| GET | `/v1/packages/:ns/:name` | package detail, `latest`, versions |
| GET | `/v1/packages/:ns/:name/:version` | version detail, throws surface, capabilities |
| GET | `/v1/packages/:ns/:name/:version/signatures` | extracted fns / interfaces / impls |
| GET | `/v1/packages/:ns/:name/dependents` | reverse dependencies |
| GET | `/v1/packages/:ns/:name/downloads?range=30d` | per-day download counts |
| GET | `/v1/tarballs/:ns/:name/:version` | `{"sha256","tarball_b64"}` (v1 deviation: Misa bodies are UTF-8 Text, so tarballs ride as base64 instead of raw bytes) |
| GET | `/v1/search?q=...&kind=keyword\|semantic` | typed queries: `fn(A)->B`, `throws E...`, `impl X`, `interface X`, `capability:x`, `-capability:y`, `examples:passing`, plain keywords (also match symbol names) |
| GET | `/v1/advisories?package=` | security advisories |
| POST | `/v1/publish` | Bearer; see pipeline above |
| POST | `/v1/yank` | Bearer + `yank:@ns/*`; `{pkg,version,reason}` |
| POST | `/v1/deprecate` | Bearer + `publish:@ns/*`; `{pkg,message}` |
| POST | `/v1/namespace/claim` | Bearer; `{namespace}` (first-claim wins) |
| POST | `/v1/report` | no auth (rate-limited); `{pkg,version?,reason}` |
| POST | `/v1/advisories` | admin only |
| POST | `/v1/admin/tokens` | admin only; mints scoped tokens (raw token shown once) |
| GET | `/v1/admin/backup` | admin only; full SQLite dump as SQL text |

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | listen port |
| `REGISTRY_DATA` | `./data` | SQLite DB + blob storage root |
| `REGISTRY_URL` | `https://pkg.misa.website` | canonical URL used in responses/docs links (never hardcoded) |
| `REGISTRY_ADMIN_TOKEN` | — | enables the backup + token-mint endpoints |
| `MISA_ROOT` | `.` | path to the misa repo (for `impl/bin/misa` subprocesses) |

## Auth

Tokens are Bearer; only `sha256(token)` is stored. Scopes look like
`publish:@acme/*`, `yank:@acme/*`, `admin:*`. Constant-time comparison,
1-year expiry, never logged. Rate limits: 60/min anonymous, 600/min
authenticated, 5 publishes/hour per namespace (in-memory, windowed keys).

## Docker

```bash
docker build -t misa-registry ./registry
docker run -e PORT=8080 -p 8080:8080 \
  -e REGISTRY_ADMIN_TOKEN=<token> -v regdata:/app/data misa-registry
```

The image is `python:3.12-slim` + the interpreter (stdlib only; the
65 MB LLVM AOT backend is excluded — the server never shells out to
`misa build`). `curl` is installed for `seed.misa`.

## Tests

```bash
python3 tests/registry/test_registry.py   # 45 API tests, boots a real server
```

## Spec deviations (v1)

- `GET /v1/tarballs/...` returns `{"sha256","tarball_b64"}` JSON, not
  raw bytes — Misa `http.serve` response bodies are UTF-8 Text only.
- `signatures` in the publish body is accepted but ignored: the server
  always recomputes signatures ("computed, not trusted").
- `doc` fields are empty: the Misa parser exposes no doc comments.
- `manifest.dependencies` must be a JSON list of `{name, range}` (the
  TOML `[dependencies]` table is normalized by `pkg_to_json.py`).
- Packages without `*_test.misa` files skip the `misa test` gate with a
  warning (examples are still executed — the spec's §6.4 artifact).
- Search result params use `[name, type]` pairs (Misa cannot name a
  field `.type`), and interface methods carry a rendered `sig` string.
- `http.serve` is single-threaded: a publish (~seconds) briefly blocks
  other requests. Documented as a v1 limitation.
