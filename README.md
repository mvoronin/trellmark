# Trellmark

Keep links, write notes, build your map.

Trellmark is a private, single-user knowledge organizer. It currently manages
links in nested groups with titles, importance flags, domain rules,
safe/private visibility, and site icons. First-class notes are the next major
product capability.

Move a link by dragging its dotted handle onto another group's header or
contents. Mouse, touch, and pen use the same gesture; Escape cancels it.
Empty and folded groups accept drops. The move selector also supports keyboard
use. Moving changes only the source membership and preserves other groups
containing the same link.

The application is one self-contained FastAPI service backed by PostgreSQL.
The service owns both the JSON API and the vanilla TypeScript SPA. Public TLS
and routing belong to one shared Caddy instance maintained in a separate
infrastructure repository.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="doc/media/screenshot-dark.png">
  <img alt="The Trellmark link list: nested groups, importance stars, and per-link edit, refresh, move, and delete controls" src="doc/media/screenshot-light.png">
</picture>

## Repository map

- `trellmark/` — web application, API, authentication, storage, and metadata.
- `migrations/` — one fresh PostgreSQL baseline.
- `web/src/` — TypeScript source and generated OpenAPI types.
- `web/html/` — maintained page and shared fragment sources.
- `web/static/` — committed browser output; dedicated `design/` assets are local-only.
- `deploy/` — the Trellmark image and rootless Podman Quadlet units.
- `doc/` — architecture decision records and README media.
- `tests/` — API, browser, schema, security, and deployment-contract tests.

There is deliberately no migration path from any previous application and no
legacy export compatibility. Trellmark JSON documents use `version: 1`; it contains
the complete current data model. The baseline migration contains no usable
administrator password or reusable password verifier. Each installation must
initialize its own credential with `server.py set-password`.

## Develop locally

Requirements: Python 3.14, [uv](https://docs.astral.sh/uv/), Node.js/npm, and
PostgreSQL 18. Podman can provide a disposable database:

```bash
npm ci
podman run --rm --detach --name trellmark-dev-db \
  --publish 127.0.0.1:5432:5432 \
  --env POSTGRES_USER=trellmark \
  --env POSTGRES_DB=trellmark \
  --env POSTGRES_PASSWORD=trellmark-dev-only \
  docker.io/library/postgres:18-alpine

export TRELLMARK_DATABASE_URL='postgresql+psycopg://trellmark:trellmark-dev-only@127.0.0.1:5432/trellmark'
export TRELLMARK_PUBLIC_ORIGIN='http://127.0.0.1:8000'
uv run python server.py migrate
uv run python server.py set-password
uv run python server.py
```

Open `http://127.0.0.1:8000`. Plain HTTP is accepted only for loopback
development origins. The app refuses to start when PostgreSQL is unavailable,
the schema is not at the current Alembic head, the administrator credential is
invalid, or the public origin is unsafe.

For a completely disposable application and database pod:

```bash
just run-container
# TRELLMARK_LOCAL_PORT=8902 just run-container
```

## Validate changes

### Design mode

With the locked development dependencies installed, run `just design-mode` and
open `http://127.0.0.1:8001/design.html`. The command builds the frontend, then
serves only `web` on loopback in the foreground. It needs no application
configuration, login, PostgreSQL, or personal data. An occupied port fails
visibly; stop the foreground command with Ctrl-C to release it.

The overview uses real shared views, HTML fragments and application styles with
synthetic data. Folding groups, toggling importance and opening dialogs affect
only that page; reload resets the examples. Inspect appearance manually after
the frontend phase is complete; automated DOM/behavior, contrast and overflow
checks remain part of validation.

Edit TypeScript in `web/src/` and HTML in `web/html/`, then use
`just build-frontend` to regenerate JavaScript and complete pages. The design
frame stylesheet `web/static/design/frame.css` is maintained separately; all
design JavaScript and `web/design.html` are generated and drift-checked.
Production image staging excludes the design page and the entire dedicated
`static/design/` tree before copying application assets into runtime layers.

### Checks

```bash
npm ci
uv run playwright install chromium
just check
```

The test harness starts an isolated PostgreSQL 18 container. `just check` verifies
generated OpenAPI declarations, complete generated HTML/JavaScript trees, native
frontend and backend import boundaries, Python and TypeScript types, formatting,
security, API and browser behavior, including contrast and narrow-view overflow.
CI runs the same gates with Node.js 24, Python 3.14 and uv 0.12.3.

### Browser ownership and generated files

`web/src/main.ts` composes the public `shell/index.ts` and
`features/bookmarks/index.ts` factories with the `api/client.ts` adapter and
shared primitives. The shell owns authentication, private-state invalidation,
dialog lifecycle and import/export. Bookmarks owns its read-only model
projections, views, editor fields and drag operations; it receives dialog/error
capabilities without importing the shell. `shared/` stays product-neutral and
imports only shared modules. Generated OpenAPI declarations are consumed only
through the API adapter using type-only imports.

`web/src/design/main.ts` is the other named composition entry. It reuses public
Bookmark views and shell dialogs with in-memory fixtures. Other design modules
cannot compose feature/shell internals or start API operations. Imports of
reusable modules acquire no page handles or listeners; factories own setup and
disposal. See [ADR 0001](doc/adr/0001-framework-free-frontend.md) for the decision,
its dated baseline and the criteria for reconsidering a framework.

Maintain complete-page sources and shared fragments under `web/html/`; generated
`web/index.html` and `web/design.html` contain all markup without runtime fragment
requests. `just build-frontend` compiles every configured TypeScript source,
including modules unreachable from either entry, and publishes the complete
output. Regenerate before committing source changes. `just check-frontend-artifacts`
compares every generated path and byte against a fresh temporary build without
cleaning or repairing the published tree; missing, stale and corrupted files fail.
The exact maintained exclusions are `web/static/styles.css`,
`web/static/icons.svg`, `web/static/favicon.svg`, `web/static/favicon.ico`,
`web/static/apple-touch-icon.png`, `web/static/fonts/` and
`web/static/design/frame.css`.
No extension-wide CSS or JavaScript exclusion exists.

Run `just check-frontend-imports` (`npm run check:imports`) for the native
TypeScript dependency rules and `just check-api-types` for generated API drift.
These checks and `just check-frontend-artifacts` run locally and in CI. Final
phase regression and local production-image proof are recorded in the
[Phase 3 evidence](.planning/phases/03-frontend-boundaries-and-deterministic-build/03-14-EVIDENCE.md).

## GSD workflow

Trellmark uses a project-local GSD runtime. The generated `.codex/` directory is
machine-local and ignored because the GSD installer specializes resource paths
for the current checkout. GSD planning artifacts under `.planning/` are not
ignored and should be reviewed and versioned with the project.

Install the pinned GSD runtime with its required temporary Node.js 24 executable
and an npm cache under `/tmp`:

```bash
scripts/install_gsd.sh
```

The helper runs the official installer and normalizes Codex hook commands so
they resolve from the Git root instead of depending on an npm cache or absolute
checkout path. Restart Codex from the repository root after installation:

```bash
cd /path/to/trellmark
codex
```

Review and trust the project-local configuration and hooks when Codex prompts
you; use `/hooks` to inspect them. Run `$gsd-onboard` to initialize GSD for the
existing Trellmark baseline. `$gsd-new-project` is reserved for an empty
greenfield repository.

## GitHub CI/CD

GitHub Actions runs the same checks on `main` and pull requests using a
disposable PostgreSQL 18 service and explicit test-only constants. The workflow
has read-only repository permission and does not persist checkout credentials.

Tags matching `v*` publish one tested image to
`ghcr.io/mvoronin/trellmark`. Publication uses GitHub's short-lived,
repository-scoped token. The workflow has no production hostname, database,
login, password, SSH key, deployment environment, or access to the home server.
Publishing an image is intentionally separate from deploying it.

## Deploy Trellmark

Create the local Podman secrets once, then deploy:

```bash
just db-secrets
just public-origin
just deploy
```

On the first deployment, `just deploy` notices the disabled baseline credential
and prompts for a new administrator password before starting the app. Later
deployments preserve it. Use `just set-password` to rotate it and revoke all
existing sessions.

The tracked Quadlet contract is deliberately narrow:

- PostgreSQL and Trellmark run together in the rootless `trellmark` pod.
- The app is published only as `127.0.0.1:8901 -> 8000`.
- Trellmark owns no TLS keys and does not bind host ports 80 or 443.
- `TRELLMARK_PUBLIC_ORIGIN` is the exact external HTTPS origin seen by the
  browser.

The separate infrastructure repository owns the single shared Caddy container,
its persistent state, certificates, domains, and routes. Its Trellmark site
must reverse-proxy the complete request URI to `http://127.0.0.1:8901` and must
return `404` for `/internal/*`. Run that Caddy container with host networking,
or otherwise ensure the upstream connection reaches Trellmark through a
trusted loopback path. Caddy supplies `X-Forwarded-For`, `X-Forwarded-Proto`,
and `X-Forwarded-Host`; Trellmark accepts proxy headers only from loopback.

Do not widen Trellmark's loopback bind as a substitute for configuring the
shared proxy. The `/internal/ready` endpoint is intended only for the container
health check and must not be exposed by the edge.

## Import and export

Trellmark JSON export is a portable content format. A supported, valid import
is applied all-or-nothing: either the complete bookmark document is committed,
or none of its changes are kept. Existing duplicate URLs remain successful
skips and are included in the response's skipped count. Invalid documents and
failures during an import leave the existing bookmark data unchanged.

If another bookmark change is already in progress, an import fails immediately
with HTTP `409` and the `import_conflict` code. The browser keeps the current
bookmark tree and selected file after `import_conflict` or `import_failed`; it
does not retry automatically. Use **Retry import** to make one explicit retry.
An `invalid_import` must be corrected and selected again before another attempt.

### Why bookmark writes use an advisory gate

Every logical bookmark writer first tries `pg_try_advisory_xact_lock`. This is a
transaction-scoped, non-blocking gate across bookmark mutations. The first
writer proceeds; an overlapping writer is rejected at a known point before
sibling or row locks and before data changes begin. PostgreSQL releases the gate
automatically when the transaction commits or rolls back, which lets imports map
contention predictably to `409 import_conflict`.

`SERIALIZABLE` isolation addresses a different concurrency problem. PostgreSQL's
serializable snapshot isolation can let transactions run concurrently and abort
one later, at a statement or at commit, with a serialization failure. It does
not provide the immediate, pre-mutation first-wins response required by the API.
Using it alone would also require transaction-retry handling and would change
when callers observe conflicts. It could supplement the gate if a concrete
write-skew case is found, but it would not replace the gate. The regular database
transaction still provides atomic commit and rollback, while constraints enforce
stored-data invariants.

## Backups

Use PostgreSQL tools for durable backups. JSON export is a portable Trellmark
content format, not a replacement for a database backup.

```bash
just backup
just backup file=/safe/local/path/trellmark-backup.dump
just restore file=/safe/local/path/trellmark-backup.dump
```

Take and verify a backup before every schema change or PostgreSQL major-version
upgrade. Never attach a newer PostgreSQL major directly to the existing volume.
