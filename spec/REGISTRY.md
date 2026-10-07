# Misa Package Registry — v1 Specification

**Status:** design spec (Phase 9). Spec only — no implementation.
**Bar (Hamis, 2026-10-07):** *"v1 doesn't mean weak or poor. It should have
a lot of great things, including things that programmers and agents yearn
for and the things that others do not have yet which will make Misa the
first and best to have it."*

## 0. Principles

1. **Computed, not trusted.** Every registry on earth trusts the author:
   npm trusts the semver bump, PyPI trusts the README, crates.io trusts
   the dependency list. The Misa registry *computes* what others trust —
   from the checker's typed signatures, throws graphs, and executed
   examples. The type system is registry infrastructure.
2. **API first, website second.** Every registry capability is a versioned
   REST endpoint returning JSON before it is a web page. Agents are
   first-class consumers, humans second.
3. **Reproducible by default.** Content-addressed installs, hash-locked
   lockfiles, vendoring as a first-class command. `misa.lock` pins bytes,
   not wishes.
4. **Dogfooded.** The registry server is written in Misa itself
   (`http.serve` + `table`/SQLite + `crypto.sha256`), deployed to a
   single Oracle Cloud Always Free ARM VM ($0/month).
5. **Ambitious v1, honest v2.** v1 must be deployable and genuinely
   useful — not a toy — but the spec says exactly what is *later*.

Related: `spec/PKG.md` (package layout, manifest v1, git/path sources —
this spec extends it, it does not replace it), `bench/GAPS-language.md`
§I (the gap this closes), `spec/SEMANTICS.md` §throws and §interfaces
(the type-system features the registry exploits).

---

## 1. Manifest (`misa.pkg`, v2)

TOML. Extends `spec/PKG.md` §1.1; all v1 fields keep working.

```toml
[package]
name = "@acme/router"          # required: "@namespace/name" (see §2)
version = "1.2.0"              # required: semver MAJOR.MINOR.PATCH
description = "Typed HTTP router for Misa"   # required for publish
license = "MIT"                # required for publish (SPDX identifier)
homepage = "https://acme.dev/router"         # optional
repository = "https://github.com/acme/misa-router"  # optional
lib = "src/lib.misa"           # optional, default "src/lib.misa"
readme = "README.md"           # optional, default "README.md" if present

# What the package CAN do. Declared by the author, VERIFIED by the
# registry against the checker's inferred throws graph (§6.3).
# Declared superset of inferred -> publish rejected with E-code-style
# explanation. Declared subset -> registry upgrades the record and warns.
[capabilities]
network = true    # http.*, net.*, model.* calls (E304/E400 surface)
fs = true         # fs.read / fs.write (E303 surface)
process = false   # process.run (E303 surface)
model = false     # model.* calls (E400/E500 surface)

[dependencies]
"@acme/middleware" = "^1.4.0"
"@std/json-ext" = "~0.3.0"

[dev-dependencies]
"@acme/testkit" = "^2.0.0"

# Registry-only metadata, written by `misa pkg publish`, never by hand.
[registry]
published_at = "2026-10-07T00:00:00Z"
tarball_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

Manifest validation errors stay `E113` (extends the existing code with
new variants: bad namespace, undeclared capability use, license missing
at publish).

**Examples convention:** `examples/*.misa` are runnable examples.
Each must be a self-contained program; the registry executes every one
with `misa test` at publish time (§6.4). At least one example is
required for publish.

---

## 2. Namespaces

- Every registry package lives under a namespace: `@org/pkg`.
  Bare names (`router`) are reserved for the future std organization.
- Namespaces blunt squatting (`reqwest` vs `request` typosquats):
  trust attaches to `@acme/*`, not to a global flat name.
- Namespace registration: first publisher to claim an unused namespace
  owns it (registry records `claimed_by`, `claimed_at`). Claim disputes
  go through the report flow (§8.6).
- Package rename: not allowed (immutable history). Deprecate + point
  to the new name (§3, yank/deprecate).
- Import syntax: `import "@acme/router"` resolves exactly like
  `import "my-pkg"` in `spec/PKG.md` §2–3, with the `@org/` prefix part
  of the package name. `misa_pkgs/@acme/router/` on disk.

---

## 3. Versioning, yank, deprecate

- **Semver**, `MAJOR.MINOR.PATCH`, with `^` (compatible), `~` (patch),
  `=` (exact) ranges in `[dependencies]`. Pre-1.0: `^0.x.y` means
  `>=0.x.y <0.(x+1).0` (standard).
- **Yank** (`misa pkg yank @acme/router@1.2.0`): the version disappears
  from search and from fresh resolution, but existing lockfiles keep
  installing it byte-identically (crates.io model — yank never breaks
  a locked build). Yank requires a reason string; yanked versions show
  a banner on their docs page.
- **Deprecate** (`misa pkg deprecate @acme/router "use @acme/router2"`):
  package-level message shown on install, search, and docs. Does not
  block installs.
- **The registry computes breaking-ness** (§6.5): the author's declared
  bump is checked against the signature diff. Declared `minor`, computed
  `major` → **publish rejected** with a precise explanation
  (`fn get(u: Text) -> Text` removed; `throws` set grew: +E306).
  This is the end of "oops, that was actually breaking" releases.

---

## 4. CLI

```
misa pkg init [@org/name]          scaffold misa.pkg + src/lib.misa + examples/hello.misa
misa pkg add @org/pkg[@^1.2.0]     resolve + download + verify + update misa.lock
misa pkg remove @org/pkg           remove from misa_pkgs/ and misa.lock
misa pkg install                   install exactly from misa.lock (CI/offline)
misa pkg list                      installed: name, version, source, capabilities
misa pkg outdated                  which locked deps have newer compatible versions
misa pkg update [@org/pkg]         re-resolve ranges, rewrite misa.lock
misa pkg search <query> [--json]   keyword + typed semantic search (§6.1)
misa pkg show @org/pkg[@1.2.0] [--json]
                                   metadata, signatures, throws surface, capabilities,
                                   examples status, docs URL, dependents count
misa pkg publish [--dry-run]       pack, run pre-publish gates, upload
misa pkg yank @org/pkg@1.2.0 --reason "..."
misa pkg deprecate @org/pkg --message "..."
misa pkg audit [--json]            check lockfile against advisories (§7.5)
misa pkg vendor                    copy all locked deps into vendor/ (§7.4)
misa pkg login / logout            publisher token flow (§8.3)
misa pkg dependents @org/pkg [--json]
                                   reverse-dependency graph (§7.3)
```

Global flags: `--registry <url>` (private registries, §7.4),
`--offline` (cache/vendor only — fails cleanly instead of hitting
network), `--json` on every read command.

**`misa pkg add` output (capability transparency):**
```
$ misa pkg add @acme/router
resolving @acme/router@^1.2.0 -> 1.2.0 (sha256:e3b0…)
capabilities: network fs          # verified by registry, not claimed
throws surface: E304 E303         # what it can raise into YOUR code
examples: 4/4 passing             # executed by registry CI
  + @acme/middleware@1.4.2 (capabilities: -)
lockfile updated: misa.lock
```

**`misa pkg publish` pre-gates (all local, all must pass):**
1. `misa check` clean on the package (with dev-dependencies).
2. `misa test` green, including `examples/*.misa`.
3. Manifest valid (E113 otherwise); license present; description present.
4. `misa fmt --check` clean (one canonical formatting — docs render
   from source).
5. Breaking-change self-check: `misa pkg diff @org/pkg 1.1.0` computed
   locally against the registry's signatures API; declared bump must be
   >= computed (§6.5).
6. Tarball pack: deterministic (sorted files, fixed mtimes, no
   absolute paths) → `sha256` recorded.

`--dry-run` runs gates 1–5 without uploading.

---

## 5. Lockfile (`misa.lock`, v2)

TOML. Pins **bytes**, not versions:

```toml
version = 2
registry = "https://registry.misa.dev"

[[package]]
name = "@acme/router"
version = "1.2.0"
sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
capabilities = ["network", "fs"]
source = "registry"   # or: source = "git+https://...@rev" / "path+../x" (PKG.md v1 sources keep working)

[[package]]
name = "@acme/middleware"
version = "1.4.2"
sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
capabilities = []
source = "registry"
```

- `misa pkg install` verifies every tarball's sha256 before unpacking;
  mismatch → clean error naming the package, expected vs actual hash.
- The lockfile is the **audit input** (`misa pkg audit` reads it) and
  the **vendoring input** (`misa pkg vendor` materializes it).
- v1 lockfiles (git/path only) remain valid; the resolver handles mixed
  sources.

---

## 6. Agent-first features

These are the differentiators. Each is specified concretely enough to
implement.

### 6.1 Typed semantic search — *first in the world*

The registry indexes every published version's **typed function
signatures**, extracted by running Misa's own checker at publish time.
Search is over types, not keywords.

**Signature query language** (also accepted by `misa pkg search`):

```
fn(Text) -> [Text] throws E304      # functions taking Text, returning [Text], raising E304
impl Drawable                        # packages providing an impl of interface Drawable
interface Cache                      # packages declaring interface Cache
throws E400                          # anything whose error surface includes E400
capability:network -capability:model # capability filters
examples:passing                      # only packages whose examples execute green
```

Queries compose: `fn(*)->Response throws E304 capability:network
examples:passing`.

**How it works:** at publish, the registry runs the checker over the
package, extracts every public `fn` (name, param types, return type,
declared+inferred throws set, doc comment, source location), every
`interface` declaration, and every `impl` block, and stores them in the
`signatures` table (§8.1). Search compiles the query to SQL over that
table. Type matching is structural on Misa's type syntax (`[Text]`,
`Map<Text,Int>`, `T?`, `Task<T>`, `fn(A)->B`).

**Why no incumbent can easily copy this:** npm/PyPI index dynamically
typed code — there are no signatures to extract, retroactively, for
millions of packages. crates.io has types but no agent-first mandate,
no query language, and no checked error codes in signatures to search
over. This requires a typed language *plus* the checker as registry
infrastructure *plus* the decision to expose it.

### 6.2 Throws-contract indexing — *first in the world*

Every package version gets a computed **error surface**: the union of
inferred throws sets across its public API, per function.

```
$ misa pkg show @acme/router --json | jq .throws_surface
{
  "serve": ["E304"],
  "get": ["E304"],
  "post_json": ["E304", "E300"]
}
$ misa pkg show @acme/router
throws surface (computed, not declared):
  serve(port: Int, handler: fn(Request)->Response) -> Void throws E304
  get(u: Text) -> Text throws E304
```

An agent deciding whether to `add` a dependency sees *exactly* which
E-codes can flow into its code and can generate the `try/catch` or
`throws` clauses before writing a line. `misa check` already enforces
E227 at the call site; the registry makes the information available
*before* the dependency exists locally.

**Why uncopyable:** requires closed, checkable error codes as part of
function signatures (Misa's `throws`). Rust's `Result<T, E>` doesn't say
*which* errors; Go/Python are unchecked; Java's checked exceptions died
on verbosity and were never indexed anywhere.

### 6.3 Capability declarations — computed, not trusted

Packages declare capabilities in the manifest (§1), but the registry
**verifies the declaration against the checker's inferred throws
graph**:

| Inferred E-code surface | Implied capability |
|---|---|
| E303 from `fs.*` | `fs` |
| E303 from `process.*` / `db.*` | `process` (+ `fs` for db file access) |
| E304 | `network` |
| E400 / E500 | `model` (+ `network`) |

- Declared ⊋ inferred → publish rejected ("you claim `network` but
  nothing in the package can raise E304").
- Declared ⊊ inferred → registry records the inferred set, warns the
  publisher, shows "declared: fs · verified: fs network" on the package
  page. Verification beats declaration, always.
- `misa pkg add` prints capabilities; `misa.lock` records them;
  `misa check` warns (E-code, new) when a dependency's verified
  capabilities exceed what the lockfile recorded (supply-chain change
  detection: a patch release that suddenly gains `network` is *visible*).
- **Sandboxing** (explicitly later, §10): OS-level sandboxing per
  capability. v1 gives transparency + verification, which is already
  more than any incumbent (npm has no capability model at all).

**Why agents yearn for this:** supply-chain safety becomes *reasoning*,
not vibes. "Does this markdown parser touch the network?" is answered
by the type system, not by reading 4,000 lines.

### 6.4 Verified examples — executed, not README'd

- `examples/*.misa` are mandatory (≥1 for publish).
- At publish, registry CI runs `misa test` over the package **including
  every example** in a sandbox (no network unless `network` capability;
  60s timeout each; deterministic seed for `random`).
- Badge: `examples: 4/4 passing` (per version; re-run weekly — bit-rot
  detection; a version whose examples start failing gets flagged, not
  yanked).
- Search filter: `examples:passing`. Agents learn from examples that
  **provably execute** — the registry is a corpus of working code, which
  is also training-data-grade material for Misa's future model
  presence.

### 6.5 Computable breaking changes — *first in the world*

On publish of version N+1, the registry diffs the **exported typed
signatures** of N and N+1 (from the `signatures` table):

| Change | Verdict |
|---|---|
| Public `fn` removed | **breaking** |
| Parameter added (no default) | **breaking** |
| Parameter type changed | **breaking** |
| Return type changed | **breaking** |
| `throws` set grew (`+E306`) | **breaking** (callers' E227!) |
| `throws` set shrank | ok |
| New public `fn` / new `impl` | ok |
| Doc comment changed | ok |
| `interface` gained a method | **breaking** (existing impls are now incomplete) |

- The registry computes the *minimum* bump (major/minor/patch).
- Declared bump < computed → **publish rejected** with the exact
  diff. Declared > computed → allowed with a note (over-caution is
  harmless).
- `misa pkg diff @org/pkg 1.1.0 1.2.0` shows the same diff locally,
  before publishing. `misa pkg update` refuses to cross a computed-major
  unless the range allows it (ranges already encode this, but now the
  *registry* guarantees the author's bump was honest).

**Why uncopyable:** requires the registry to run the language's own
type checker as infrastructure, plus a closed error-code system so the
*error surface* is diffable. No mainstream registry diffs types at
publish time; all of them trust the human's bump.

### 6.6 Structured everything

- `--json` on every read command (`search`, `show`, `list`,
  `outdated`, `audit`, `dependents`). Output schemas are versioned
  (`"schema": "misa.pkg.v1"`) and documented in `docs/REGISTRY-API.md`.
- Dependency graph as JSON: `misa pkg graph --json` → nodes
  (name@version+sha256+capabilities) and edges (ranges).
- Machine-readable changelogs: `CHANGELOG.md` convention with
  `### Breaking` / `### Added` / `### Fixed` sections; the registry
  parses them and cross-checks `### Breaking` against the computed
  diff (§6.5) — mismatch is a publish warning.
- The website is a *consumer* of `/v1/`, not the source of truth.

---

## 7. Programmer-yearned features

### 7.1 Hosted docs (docs.rs-style)

- At publish, the registry extracts `///` doc comments + signatures
  and renders per-version HTML docs: `https://docs.misa.dev/@acme/router/1.2.0/`.
- Docs include the throws surface per function (§6.2), capability
  badges, example links (each example runnable in the browser playground
  — Phase 8 — via "try it" deep links, when the playground exists).
- Latest-version redirect + version switcher. Old versions' docs stay
  up forever (reproducibility).

### 7.2 Download stats

- Public per package/version/day: `GET /v1/packages/@acme/router/downloads?range=30d`.
- `misa pkg show` prints `downloads (30d): 12,408`. Stats are
  informational, never ranking (no gamified leaderboard — crates.io
  lesson: leaderboards breed squatting).

### 7.3 Reverse dependencies

- `GET /v1/packages/@acme/router/dependents` → list of packages +
  versions depending on it, with ranges.
- `misa pkg dependents @acme/router` renders the tree.
- Powers the yank blast radius view: "yanking 1.2.0 affects 14
  packages; 3 pin it exactly."

### 7.4 Private registries + vendoring

- `--registry https://pkgs.acme.corp` + `~/.misa/config.toml`:
  ```toml
  [registries]
  default = "https://registry.misa.dev"
  "acme" = "https://pkgs.acme.corp"     # token in ~/.misa/credentials
  [[namespace-route]]
  namespace = "@acme"                   # @acme/* resolves via the acme registry
  registry = "acme"
  ```
- `misa pkg vendor`: copies every locked tarball (verified by sha256)
  into `vendor/`, rewrites resolution to prefer it. Offline,
  reproducible, enterprise-auditable builds. (The gap matrix's
  "vendoring by default" overtake — cargo can't do this in one command
  this cleanly.)
- Mixed sources keep working: registry + git + path in one lockfile.

### 7.5 `misa pkg audit` + advisories

- Registry hosts a curated advisory DB:
  `GET /v1/advisories?package=@acme/router` →
  `[{id, severity, summary, affected: "<1.2.0", fixed_in: "1.2.1",
  published_at}]`.
- `misa pkg audit` walks `misa.lock`, prints findings with severity
  and the minimal fix version, exits non-zero on `high`+.
- Advisory states: `published` → `fixed` (by release) — never deleted
  (history matters for locked old builds).
- v1 curation is manual (report flow, §8.6); automated scanning hooks
  are the later step.

### 7.6 Provenance & signing

- Publishers generate an ed25519 keypair:
  `misa pkg keygen` (private key in `~/.misa/keys/`, **never** leaves
  the machine; public key uploaded at `misa pkg login`).
- `misa pkg publish` signs the tarball sha256; the registry verifies
  against the publisher's registered key and shows a **signed** badge.
- Unsigned publishes are allowed in v1 (adoption beats purity) but
  visibly marked; `misa pkg add` warns on unsigned.
- Transparency log (sigstore-style, append-only) is explicitly later.

---

## 8. Server architecture — written in Misa

The registry server is a Misa program. This is both dogfooding and a
forcing function: if `http.serve` + `table`/SQLite + `crypto.sha256`
can't serve the registry, they aren't done.

```misa
// registry/main.misa (sketch — the shape, not the code)
table packages      { name: Text, namespace: Text, description: Text,
                      homepage: Text, repository: Text, claimed_by: Text,
                      claimed_at: Int, deprecated_msg: Text }
table versions      { pkg: Text, version: Text, sha256: Text,
                      tarball_path: Text, published_at: Int,
                      publisher: Text, license: Text, yanked: Bool,
                      yank_reason: Text, breaking_computed: Text }
table dependencies  { pkg: Text, version: Text, dep: Text, range: Text }
table signatures    { pkg: Text, version: Text, kind: Text,  // fn|interface|impl
                      name: Text, params: Text, ret: Text,
                      throws: Text, doc: Text }              // JSON-encoded lists
table capabilities  { pkg: Text, version: Text, cap: Text,
                      declared: Bool, verified: Bool }
table examples      { pkg: Text, version: Text, name: Text,
                      passing: Bool, last_run_at: Int }
table downloads     { pkg: Text, version: Text, day: Text, count: Int }
table advisories    { id: Text, pkg: Text, affected: Text, fixed_in: Text,
                      severity: Text, summary: Text, published_at: Int,
                      state: Text }
table publishers    { id: Text, handle: Text, created_at: Int,
                      pubkey: Text }
table tokens        { token_sha: Text, publisher_id: Text, scopes: Text,
                      created_at: Int, revoked: Bool }
table reports       { id: Text, pkg: Text, version: Text, reason: Text,
                      state: Text, created_at: Int }         // state: open|actioned|dismissed

http.serve(8080, |req: Request| -> Response { return route(req) })
```

### 8.1 Data model

The tables above. Notes:
- `signatures.throws` stores the **inferred** throws set (JSON array of
  E-codes); the declared clause is validated at publish and need not
  be stored separately (declared ⊆ inferred is enforced — §6.2).
- `downloads` is append-aggregated daily (a `net` worker batches the
  raw hit log; the hot path never writes SQLite per download).
- Tarball bytes live on disk (§8.4); the DB stores paths + hashes only.

### 8.2 API (`/v1/`, JSON, versioned)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/packages/:ns/:name` | – | metadata + version list |
| GET | `/v1/packages/:ns/:name/:version` | – | version metadata, sha256, capabilities, throws surface |
| GET | `/v1/packages/:ns/:name/:version/signatures` | – | typed signature index (§6.1) |
| GET | `/v1/packages/:ns/:name/dependents` | – | reverse deps (§7.3) |
| GET | `/v1/packages/:ns/:name/downloads?range=30d` | – | stats (§7.2) |
| GET | `/v1/tarballs/:ns/:name/:version.tar.gz` | – | download (302 to blob store when mirrored) |
| GET | `/v1/search?q=...&kind=keyword\|semantic` | – | §6.1 |
| GET | `/v1/advisories?package=` | – | §7.5 |
| POST | `/v1/publish` | token | multipart: tarball + manifest; runs the publish pipeline (§8.5) |
| POST | `/v1/yank` | token | `{pkg, version, reason}` |
| POST | `/v1/deprecate` | token | `{pkg, message}` |
| POST | `/v1/namespace/claim` | token | `{namespace}` |
| POST | `/v1/report` | –* | `{pkg, version?, reason}` (*rate-limited, no auth needed to lower the bar) |
| GET/POST | `/v1/advisories` | token(moderator) | curation endpoints |

All errors are JSON: `{"error": {"code": "E..."/"R...", "message": "..."}}`.
Rate limits: 60 req/min anonymous, 600 req/min authenticated, publish
5/hour/namespace (anti-spam; raisable on request).

### 8.3 Auth model — token-based, no passwords

- `misa pkg login`: CLI prints a URL + short code; the publisher
  approves in the browser (already logged in via any OAuth provider —
  GitHub first); the CLI polls and receives a **token shown once**,
  stored in `~/.misa/credentials` (mode 0600).
- Tokens are **scoped**: `publish:@acme/*`, `yank:@acme/*`,
  `admin:*`. Least privilege by default.
- The server stores only `sha256(token)`; tokens are revocable
  (`misa pkg logout --revoke`) and expire after 1 year (refresh via
  re-login).
- **Must do:** authenticate publisher identity, scope tokens,
  rate-limit, never log tokens, constant-time token comparison.
- **Must NOT do:** passwords (none exist), token reuse across
  namespaces, long-lived non-expiring tokens, storing the raw token
  anywhere server-side.

### 8.4 Storage layout

```
/data/
  tarballs/@acme/router/1.2.0.tar.gz   # served to clients
  blobs/sha256/e3/b0/e3b0c44...        # content-addressed dedupe copy
  registry.db                          # SQLite (the tables in §8)
  docs/@acme/router/1.2.0/             # rendered HTML (§7.1)
```

At v1 scale this is one directory on one VM. Growth path (later):
blob store → R2/S3 with the 302-redirect already in the API.

### 8.5 Publish pipeline (server side)

1. Authenticate token + scope check.
2. Validate manifest (E113 rules), namespace ownership.
3. Unpack tarball to scratch; verify deterministic layout; recompute
   sha256 (must match client-sent hash).
4. Run `misa check` (clean required), extract signatures → `signatures`
   table; compute inferred capabilities; compare with declared (§6.3).
5. Run `misa test` incl. `examples/*.misa` in sandbox → `examples`
   table (§6.4).
6. Compute breaking diff vs previous version → `breaking_computed`;
   compare with declared bump (§6.5); reject or accept.
7. Malware/scan hooks (v1: secret-scan for leaked tokens/keys in the
   tarball + pluggable scanner interface; block on high-confidence
   hits, queue the rest for human review).
8. Write DB rows, store tarball, render docs (§7.1), invalidate search
   index. All inside one SQLite transaction — publish is atomic.

Publish latency target: <60s end-to-end (dominated by example tests).

### 8.6 Moderation story

- **Report flow:** anyone can `POST /v1/report` (malware, squatting,
  license violation, spam). Reports are triaged by moderators
  (Hamis + appointees in v1 — no pretense of a committee yet).
- **Yank policy:** malware → yank + advisory published (§7.5) +
  publisher notified; squatting → transfer or rename-with-redirect;
  spam → remove + namespace ban.
- **Publisher verification tiers (later):** verified-org badges,
  2FA enforcement for popular namespaces. v1: email-verified OAuth
  only, stated plainly.
- **Transparency:** every moderation action (yank, advisory, ban)
  writes a public log entry (who/when/why, not the reporter's
  identity). Secret moderation is how trust dies.

---

## 9. Deployment — $0 on Oracle Cloud Always Free

- **VM:** Ampere A1, 2 OCPUs / 12GB RAM (half the free allowance —
  headroom for later), Ubuntu 24.04, one 100GB boot volume.
- **What runs:** the Misa registry binary (AOT-compiled `main.misa`),
  behind Caddy (automatic HTTPS, reverse proxy) — both as systemd
  units. SQLite needs no daemon.
- **Backups:** nightly `sqlite3 .backup` + tarball rsync to a second
  free-tier block volume; weekly copy to Hamis's machine.
- **Idle-reclaim guard:** Oracle has reclaimed idle Always Free
  instances before — a 5-minute health-check ping (from the VM to
  itself via cron + an external uptime monitor) keeps it warm and
  proves uptime publicly.
- **Cost:** $0/month (Always Free tier; the $1 card verification is
  one-time, Hamis's hands). Egress: 10TB/month free — downloads are
  never the constraint at v1.
- **Scaling later (not v1):** read replica for the DB, blob store →
  R2, second VM for CI runners. The API's 302-redirect design (§8.2)
  already anticipates the blob move.

### 9.1 Deployment (Render free tier) — current

Oracle rejected the trial registration (2026-10-07), so v1 deploys to
**Render's free tier** instead. Everything in §9 still applies in
spirit; the differences are below.

- **What runs:** `registry/Dockerfile` (`python:3.12-slim`, repo root
  as build context). The server runs via the interpreter —
  `python3 impl/bin/misa run registry/server.misa` — which needs
  nothing beyond the Python standard library (no gcc, no LLVM; the
  65MB `impl/misa/aot` tree is excluded via `.dockerignore`).
- **Port binding:** Render injects `$PORT`. The server reads it from
  the `PORT` env var (default 8080) and `http.serve` binds `0.0.0.0`
  (verified: `net_listen` binds `("", port)`).
- **Ephemeral filesystem (the big tradeoff):** Render free has no
  persistent disks — the SQLite DB and tarball files **are wiped on
  every restart/redeploy**. Mitigations, in order:
  1. **Seed script:** `registry/seed.misa` re-publishes the seed
     corpus (`registry/seed_pkgs/`) from scratch against a fresh
     server. Recovery procedure: run `registry/admin.misa` once
     (mints the admin token), boot the server with
     `REGISTRY_ADMIN_TOKEN` set, run the seed script. The registry
     is back to its seed state in minutes.
  2. **Admin backup endpoint:** `GET /v1/admin/backup` (Bearer
     admin token, 401 otherwise) streams a full SQLite dump as raw
     SQL text. Pull it on a schedule from anywhere (a cron on any
     always-on machine) and keep off-site copies. The dump restores
     with `sqlite3 registry.db < backup.sql` — verified byte-clean.
  3. **Tarballs are content-addressed** (`blobs/sha256/xx/yy/rest`),
     so a future blob store (R2/S3) can be repopulated from any
     surviving copy by hash; nothing depends on filenames surviving.
- **Keep-awake:** Render free sleeps after 15 minutes idle. A 5-minute
  cron ping (any external cron) against `/v1/` keeps it warm — same
  idle-reclaim guard as §9, different provider.
- **The registry's own URL is configuration, never hardcoded:**
  the server reads `REGISTRY_URL` (default
  `https://pkg.misa.website`); the client resolves `--registry` >
  `MISA_REGISTRY` env > `~/.misa/config` > the same default. Moving
  domains later is a config change, not a code change (lockfiles pin
  content hashes, not URLs).
- **What this defers:** at public-launch scale the registry needs a
  persistent disk or a VM (back to §9's shape). The Render deployment
  is the private-phase home; the API and data model don't change when
  it moves.

---

## 10. Phasing — v1 vs later

**v1 (this spec — ambitious but shippable):**
§1 manifest v2 · §2 namespaces · §3 semver/yank/deprecate ·
§4 full CLI · §5 hash-locked lockfile ·
§6.1 typed semantic search · §6.2 throws-contract indexing ·
§6.3 capability declarations (verified; sandboxing later) ·
§6.4 verified examples (executed at publish + weekly re-run) ·
§6.5 computable breaking changes (reject on under-bumped major) ·
§6.6 structured everything (--json, graph, machine changelogs) ·
§7.1 hosted per-version docs · §7.2 download stats · §7.3 reverse deps ·
§7.4 private registries + `misa pkg vendor` · §7.5 audit + advisory DB
(manually curated at first) · §7.6 signing (optional-but-badged) ·
§8 server in Misa · §9 $0 deployment.

**Explicitly later:**
- OS-level capability sandboxing at install/run time.
- Sigstore-style transparency log for signatures.
- Publisher verification tiers, organizations/teams with roles.
- Automated ML malware scanning (v1: secret-scan + hooks + humans).
- Regional mirrors / CDN in front of tarballs.
- `misa pkg` integration with the browser playground ("try this
  package" buttons).
- Full-text search over hosted docs.

What v1 does **not** try to be: a social network (no stars/likes —
crates.io's lesson), a CI provider, or a billing platform.

---

## 11. Where it places Misa — the honest section

**What this does not change.** On launch day the registry has ~0
packages and npm has 2M+. No design closes a decades-wide count gap;
anyone claiming otherwise is selling something. Ecosystem *size* is a
time-horizon asset and the spec doesn't pretend otherwise.

**What it does change.**
1. **It removes every structural excuse.** After v1, "Misa has no
   registry" is gone, and what's there is *better designed* than the
   incumbents' — not merely equivalent.
2. **It makes each package worth more.** A Misa package ships with
   machine-checked contracts (typed signatures, throws surface,
   verified capabilities, executed examples, computed semver
   honesty). Ten thousand such packages are worth more than ten
   thousand npm packages of unknown provenance and untested READMEs.
   Quality per package is the lever a newcomer can actually move.
3. **It creates the only growth engine that can close the gap.**
   npm/PyPI/crates.io grow at *human* speed: a human writes, tests,
   documents, and publishes each package. The Misa registry is designed
   for **agents as package authors**: typed semantic search lets an
   agent find the right dependency without training data; verified
   examples give it working patterns to copy; computable breaking
   changes let it upgrade dependencies without reading changelogs;
   `--json` everywhere makes the whole thing scriptable. An agent
   that writes a useful utility can `misa pkg publish` it in the same
   session. If agents publish at machine speed, the ecosystem grows at
   machine speed — and no incumbent is positioned to do this, because
   their registries were designed for humans and their languages can't
   supply the computed contracts (untyped codebases, unchecked errors).

**Why incumbents can't easily copy the agent-first half** (the three
"first in the world" features):
1. **Typed semantic search** — npm/PyPI have no signatures to index
   (dynamically typed, millions of packages, retroactively
   impossible); crates.io has types but no checked error codes and no
   agent-first mandate. Requires: typed language + checker-as-
   infrastructure + the decision to expose it.
2. **Computable breaking changes** — requires the registry to run the
   language's own type checker at publish time *and* a closed error-
   code system so error surfaces are diffable. Rust's `Result<T,E>`
   doesn't name the errors; Java's checked exceptions died and were
   never indexed anywhere.
3. **Verified examples as a publish gate** — requires a hermetic,
   fast, deterministic test runner plus capability declarations that
   make sandboxing examples feasible. `misa test` + capabilities are
   the prerequisites; incumbents' examples are arbitrary slow code.

The registry doesn't make Misa "the best by a thousand miles" on day
one. It builds the machine that *can* get there — and it's a machine
only Misa's type system can power.
