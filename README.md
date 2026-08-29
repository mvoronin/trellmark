# Trellmark

Keep links, write notes, build your map.

Trellmark is a private, single-user knowledge organizer. It currently manages
links in nested groups with titles, importance flags, domain rules,
safe/private visibility, and site icons. First-class notes are the next major
product capability.

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
- `web/static/` — committed browser output included in the application image.
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

```bash
npm ci
uv run playwright install chromium
just check
```

The test harness starts an isolated PostgreSQL container. `just check` verifies
generated OpenAPI declarations and browser JavaScript, Python and TypeScript
types, formatting, security boundaries, API behavior, and browser behavior.

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
