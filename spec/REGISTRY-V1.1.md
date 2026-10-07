# Registry v1.1 — trust, identity, and docs
**Ordered by Hamis 2026-10-07.** "Most advanced, most thorough."

## Threat model

**Assets:** package tarballs (integrity), publisher identities,
installing users' machines, the registry database itself.

| Threat | Mitigation (v1.1) |
|---|---|
| Tarball tampered in transit / on mirror | Ed25519 signatures; verify on publish AND install |
| Impersonation / typosquatting | Namespaces (v1) + verification tiers (v1.1) + signed badges |
| Stolen publisher token | OAuth device flow: short-lived codes, no secret typed into the CLI; tokens revocable |
| Compromised publisher key | Key rotation (`pkg keygen --rotate`); server keeps key history; old signatures remain verifiable |
| Malicious code in a package | Signing proves *provenance*, not safety — tiers + advisories + capability declarations are the safety story; never claim "signed = safe" |
| Replay of a signed tarball under a new version | Signature covers (namespace, name, version, tarball bytes) — version is part of the signed payload |

**Explicitly NOT claimed:** signed ≠ audited. The UI/badges say
"signed by <handle>" and "tier: verified", never "safe".

## 1. Package signing (Ed25519)

- `misa pkg keygen [--rotate]` → generates Ed25519 keypair in
  `~/.misa/keys/<handle>.key` (0600) / `.pub`. `--rotate` keeps the
  old public key in `~/.misa/keys/<handle>.pub.old` and registers the
  new one (server keeps history).
- Publish signs `SHA256(namespace || name || version || tarball)`
  with the publisher's private key. The signature, public key, and
  key fingerprint travel in the multipart publish.
- Server verifies the signature against the publisher's registered
  key (or the presented key on first publish, which becomes the
  registered key — TOFU with admin visibility).
- `misa pkg install` verifies the tarball signature against the
  registry's recorded public key before unpacking. Mismatch →
  hard failure with a clear message.
- Badges: `signed: true/false`, `signed_by: <fingerprint[:16]>` in
  `/v1/packages/...`, search results, and `pkg show`.

## 2. OAuth device login

`misa pkg login` (no args) now does the device flow:
1. CLI `POST /v1/oauth/device/code` → `{device_code, user_code,
   verification_uri, expires_in}`.
2. CLI prints: "Visit <uri> and enter code XXXX-XXXX".
3. Publisher (or registry admin) approves at
   `POST /v1/oauth/device/approve` (admin token) or via the browser
   page at `/v1/oauth/device` (human enters the user code).
4. CLI polls `POST /v1/oauth/device/token` every 5s until
   `{token, handle}` or expiry.
5. Token stored exactly like today (`~/.misa/credentials`).

The old paste-a-token flow remains as `misa pkg login --token`
(explicit, for CI).

## 3. Hosted HTML docs

- At publish, the server renders per-version docs from the tarball's
  `README.md` (if present) + doc comments (`//` doc lines on public
  `fn`/`type` items in `src/*.misa`) into static HTML.
- `GET /v1/docs/:ns/:name/:version/` → HTML page.
- `GET /v1/docs/:ns/:name/` → latest version (302).
- Stored on disk under `REGISTRY_DATA/docs/`, not in SQLite.
- Sanitized: markdown → escaped HTML, no raw HTML passthrough.

## 4. Verification tiers

- `publishers.tier`: `unverified` (default) → `verified`
  (admin-confirmed identity) → `trusted` (hand-picked, e.g. `@misa`
  namespace owners).
- `POST /v1/admin/publishers/:handle/tier` (admin token) sets it.
- Tier appears in search/show/package JSON and as a badge.
- Policy: only `verified`+ may publish under `@misa`; anyone may
  publish elsewhere (namespace ownership still enforced).

## 5. `misa pkg graph --json`

- `misa pkg graph [--json]` prints the resolved dependency graph of
  the current project (from `misa.lock`): nodes (name, version,
  signed, tier) and edges (depends-on).
- `--json` emits machine-readable `{nodes, edges}`.

## 6. Signed publish over JSON (spec revision)

The v1 spec said multipart, but the Misa server deliberately keeps
binary out of Misa Text (`tarball_b64` in JSON) — multipart would need
byte-level parsing the language doesn't do safely yet. v1.1 keeps the
JSON publish and adds a `signature` object to the body:

```json
{
  "manifest": {...},
  "tarball_b64": "...",
  "tarball_sha256": "...",
  "signatures": [...],
  "capabilities_declared": [...],
  "signature": {
    "pubkey": "<hex ed25519>",
    "sig": "<hex signature>",
    "fingerprint": "<hex sha256(pubkey)>"
  }
}
```

Multipart remains a later optimization (it saves ~33% base64 overhead,
no security properties). The threat model doesn't need it.

## Schema changes (v1 → v1.1)

```sql
ALTER TABLE publishers ADD COLUMN tier TEXT DEFAULT 'unverified';
ALTER TABLE publishers ADD COLUMN pubkey TEXT;          -- hex, current
CREATE TABLE publisher_keys (                           -- key history
  handle TEXT, fingerprint TEXT, pubkey TEXT,
  created_at TEXT, revoked INTEGER DEFAULT 0,
  PRIMARY KEY (handle, fingerprint));
ALTER TABLE versions ADD COLUMN signature TEXT;         -- hex or NULL
ALTER TABLE versions ADD COLUMN key_fingerprint TEXT;   -- or NULL
CREATE TABLE device_codes (
  device_code TEXT PRIMARY KEY, user_code TEXT UNIQUE,
  handle TEXT, approved INTEGER DEFAULT 0,
  expires_at TEXT, created_at TEXT);
```

## API additions

| Method & path | Auth | Description |
|---|---|---|
| `POST /v1/oauth/device/code` | none | Issue device+user codes |
| `POST /v1/oauth/device/token` | none | Poll for approval → token |
| `GET /v1/oauth/device` | none | Human approval page (HTML form) |
| `POST /v1/oauth/device/approve` | admin | Approve a user_code for a handle |
| `GET /v1/docs/:ns/:name/:version/` | none | Hosted docs HTML |
| `GET /v1/graph/:ns/:name/:version` | none | Dependency graph JSON |
| `POST /v1/admin/publishers/:handle/tier` | admin | Set verification tier |

## Deliberately deferred (still)

- OS-level capability sandboxing at install/run (platform work).
- Sigstore-style transparency log.
- Automated ML malware scanning.
- Scheduled example re-runs (needs job infra; weekly cron is the
  documented interim).
- Full-text search over hosted docs.
