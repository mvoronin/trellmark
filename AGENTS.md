<!-- GSD:project-start source:PROJECT.md -->

## Project

**Trellmark**

Trellmark is a private, single-user knowledge organizer that keeps saved URLs and first-class Markdown notes in ordered, nested group hierarchies. The existing product manages bookmarks; the next milestone adds an independent Notes area without weakening current link behavior, authentication, visibility, or safety boundaries.

**Core Value:** A single user can safely organize durable personal knowledge as bookmarks and Markdown notes without losing data or exposing private content.

### Constraints

- **Language**: All planning artifacts, tasks, documentation, and commit messages must be written in English — repository work must remain consistently reviewable
- **Repository safety**: Never commit credentials, real user data, private hostnames, database dumps, exports, or home-server access details — the repository is public
- **Compatibility**: Preserve current bookmark API, UI, schema behavior, Safe mode, visibility rules, and import success semantics while prerequisites are completed — the milestone extends a working brownfield system
- **Architecture**: Keep one self-contained deployable service while separating domain logic from FastAPI, Pydantic, and SQLAlchemy through narrow ports and adapters — Notes must be first-class rather than coupled to Bookmarks
- **Persistence**: PostgreSQL 18 and Alembic remain authoritative; hierarchy and transaction guarantees must be enforced and tested at the database boundary — no SQLite or mock substitute may define production semantics
- **Frontend**: Continue using the existing framework-free TypeScript SPA and generated OpenAPI contract workflow — generated declarations and browser artifacts must stay synchronized
- **Security**: Markdown rendering must not execute scripts, accept raw HTML, use unsafe URL schemes, or weaken authentication/CSRF/private-content controls — note content is untrusted input
- **Deployment boundary**: Trellmark owns no Caddy configuration or public edge infrastructure — those changes belong in the separate infrastructure repository

<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->

## Technology Stack

## Languages

- Python 3.14 - ASGI application, JSON API, authentication, PostgreSQL access, migrations, metadata fetching, CLI, and tests in `trellmark/`, `server.py`, `migrations/`, `scripts/export_openapi.py`, and `tests/`; the minimum is enforced by `pyproject.toml` and `uv.lock`.
- TypeScript 7.0.2 - Framework-free browser application in `web/src/app.ts` and typed API client in `web/src/api.ts`; the exact compiler version is pinned in `package.json`, `package-lock.json`, and checked by `scripts/verify_typescript.cjs`.
- HTML and CSS - The single-page application shell and presentation live in `web/index.html` and `web/static/styles.css`; browser assets are served by `trellmark/app.py`.
- SQL and PL/pgSQL - SQLAlchemy Core defines runtime tables and queries in `trellmark/storage.py` and `trellmark/identity/repository.py`; the PostgreSQL baseline, `ltree` functions, and triggers live in `migrations/versions/0001_trellmark_baseline.py`.
- Bash and Just recipes - Image auditing, local containers, checks, deployment, backup, and restore are automated by `scripts/container_images.sh` and `justfile`.
- CommonJS JavaScript - Small Node-based install and compiler checks live in `scripts/verify_typescript.cjs` and `scripts/normalize_gsd_hooks.cjs`; the root package mode is declared in `package.json`.

## Runtime

- CPython 3.14 is the application runtime, specified by `pyproject.toml`, used in `.github/workflows/ci.yml`, and supplied by the `python:3.14-slim` base in `deploy/Containerfile`.
- Uvicorn 0.52.4 runs the FastAPI ASGI application; `trellmark/cli.py` is the production and development launcher and `server.py` is the repository entry point. The exact version is locked in `uv.lock`.
- Node.js 24 is the CI and development build runtime in `.github/workflows/ci.yml`; Node is not included in the production image described by `deploy/Containerfile`.
- PostgreSQL 18 is the only supported data runtime, documented in `README.md`, used by `.github/workflows/ci.yml`, and pinned to `postgres:18-alpine` in `deploy/quadlet/trellmark-postgres.container` and `justfile`.
- uv 0.12.3 manages Python environments and locked installs; its version is pinned in `deploy/Containerfile` and `.github/workflows/ci.yml`.
- Python lockfile: `uv.lock` is present and records Python `>=3.14` plus production and development dependency resolutions.
- npm manages the frontend compiler and the OpenAPI generation workspace declared in `package.json`.
- JavaScript lockfile: `package-lock.json` (lockfile version 3) is present and installed with `npm ci` in `.github/workflows/ci.yml`.
- Just is the project task runner; validation, builds, migrations, containers, deployment, and backups are exposed by `justfile`.

## Frameworks

- FastAPI 0.141.1 - Defines the ASGI application, API routes, dependencies, OpenAPI contract, static file serving, and middleware assembly in `trellmark/app.py`; exact resolution is in `uv.lock`.
- Starlette 1.6.0 - Supplies the ASGI middleware and request/response primitives used directly by `trellmark/app.py` and `trellmark/identity/boundary.py`; exact resolution is in `uv.lock`.
- Uvicorn 0.52.4 - Serves the application with trusted-loopback proxy header handling in `trellmark/cli.py`; exact resolution is in `uv.lock`.
- SQLAlchemy 2.0.52 - Provides synchronous PostgreSQL engines, pooling, SQL expressions, tables, transactions, and error types in `trellmark/storage.py`, `trellmark/identity/repository.py`, and `migrations/env.py`; exact resolution is in `uv.lock`.
- Pydantic 2.13.4 - Defines strict request and response contracts in `trellmark/models.py`; exact resolution is in `uv.lock`.
- Vanilla DOM APIs - The SPA intentionally has no browser UI framework; `web/src/app.ts` and `web/src/api.ts` compile directly with TypeScript according to `tsconfig.json`.
- pytest 9.1.1 - Runs API, storage, security, schema, deployment-contract, and browser tests under `tests/`; configuration is embedded in `pyproject.toml` and the exact version is in `uv.lock`.
- Playwright 1.62.0 for Python - Drives Chromium browser tests under `tests/frontend/`; installation and execution are wired through `justfile` and `.github/workflows/ci.yml`, with the exact version in `uv.lock`.
- Disposable PostgreSQL 18 - Integration tests use isolated PostgreSQL databases via `tests/postgres.py` and the CI service in `.github/workflows/ci.yml`.
- TypeScript 7.0.2 - Compiles `web/src/app.ts` and its imports to committed output in `web/static/` using `tsconfig.json`; `justfile` verifies committed artifacts against a fresh build.
- openapi-typescript 7.13.0 - Generates `web/src/generated/openapi.d.ts` from `web/src/generated/openapi.json`; it is isolated in `tools/openapi-types/package.json` with TypeScript 5.9.3 because the generator depends on the TypeScript 5 compiler API.
- Alembic 1.19.1 - Applies and authors PostgreSQL migrations via `trellmark/storage.py`, `trellmark/cli.py`, `migrations/env.py`, and `alembic.ini`; exact resolution is in `uv.lock`.
- Ruff 0.16.4 - Enforces Python imports, selected lint rules, and formatting from `pyproject.toml` and `justfile`; exact resolution is in `uv.lock`.
- basedpyright 1.39.10 - Enforces strict Python 3.14 typing for `trellmark/` via `pyproject.toml` and `justfile`; exact resolution is in `uv.lock`.
- Podman/Buildah-compatible container tooling - Builds and audits `deploy/Containerfile`, starts disposable local pods, and manages deployment through `justfile` and `scripts/container_images.sh`.

## Key Dependencies

- `aiohttp` 3.14.3 - Performs bounded asynchronous outbound HTTP requests for page titles, YouTube oEmbed metadata, and site icons in `trellmark/page_titles.py` and `trellmark/site_icons.py`; exact resolution is in `uv.lock`.
- `argon2-cffi` 25.1.0 - Implements the custom administrator password verifier using the Argon2id policy in `trellmark/identity/policy.py` and authentication repository in `trellmark/identity/repository.py`; exact resolution is in `uv.lock`.
- `psycopg[binary]` 3.3.4 - Is the only supported PostgreSQL DBAPI driver selected by `trellmark/config.py` and consumed through SQLAlchemy in `trellmark/storage.py`; exact resolution is in `uv.lock`.
- `anyio` 4.14.2 - Supplies the dedicated capacity limiter used to serialize password verification work in `trellmark/identity/api.py`; exact resolution is in `uv.lock`.
- Alembic 1.19.1 - Owns schema revisioning and the baseline migration in `migrations/versions/0001_trellmark_baseline.py`; exact resolution is in `uv.lock`.
- PostgreSQL `ltree` extension - Stores and indexes nested group paths; it is installed and used by `migrations/versions/0001_trellmark_baseline.py` and represented by a SQLAlchemy user-defined type in `trellmark/storage.py`.
- Rootless Podman Quadlet - Defines the production pod, application, database, data volume, secrets injection, and health checks in `deploy/quadlet/`.
- GitHub Actions and GHCR - Validate changes in `.github/workflows/ci.yml` and publish tagged OCI images from `.github/workflows/publish.yml`.

## Configuration

- Configure the application only through `TRELLMARK_DATABASE_URL` and `TRELLMARK_PUBLIC_ORIGIN`; both are loaded and validated by `trellmark/config.py` with no dotenv loader or implicit database fallback.
- Use a PostgreSQL DSN for `TRELLMARK_DATABASE_URL`; `trellmark/config.py` normalizes it to the `postgresql+psycopg` driver and rejects missing database names and unsupported backends.
- Set `TRELLMARK_PUBLIC_ORIGIN` to one canonical browser origin; `trellmark/config.py` permits plain HTTP only for loopback development and uses the result to choose secure cookie behavior.
- Production injects both values as Podman secrets through `deploy/quadlet/trellmark-app.container`; creation and validation recipes live in `justfile`.
- `TRELLMARK_LOCAL_PORT` is an optional local orchestration override in `justfile`; test database configuration is isolated behind the test harness and `.github/workflows/ci.yml`.
- Python metadata, dependency groups, pytest settings, Ruff rules, and strict Pyright settings live in `pyproject.toml`; resolutions live in `uv.lock`.
- Browser target, strictness, source root, and output directory are defined in `tsconfig.json`; JavaScript dependency metadata lives in `package.json` and `package-lock.json`.
- Generated OpenAPI inputs and declarations are produced by `scripts/export_openapi.py` and `tools/openapi-types/package.json`, committed under `web/src/generated/`, and drift-checked by `justfile`.
- Browser JavaScript output is committed under `web/static/`, compiled from `web/src/`, copied into the production image by `deploy/Containerfile`, and drift-checked by `justfile`.
- Container construction is defined by `deploy/Containerfile`; runtime image provenance checks are implemented by `scripts/container_images.sh` and invoked from `justfile`.

## Platform Requirements

- Install Python 3.14, uv, Node.js/npm, and PostgreSQL 18 as documented in `README.md`; install locked dependencies with `uv sync --locked --all-groups` and `npm ci` as shown in `.github/workflows/ci.yml`.
- Use Podman when relying on the disposable PostgreSQL database, full local pod, image build, or deploy recipes in `justfile`; direct development may instead point `trellmark/config.py` at an existing PostgreSQL 18 server.
- Install Chromium through Playwright before browser tests, using the `playwright` recipe in `justfile` or the installation command in `.github/workflows/ci.yml`.
- Run the standard validation contract through `just check`; its checks and build ordering are defined in `justfile` and mirrored in `.github/workflows/ci.yml`.
- Target a Linux host with rootless Podman, user systemd, and Quadlet support; the pod and services are defined under `deploy/quadlet/` and installed by `justfile`.
- Run the application and PostgreSQL 18 in the rootless `trellmark` pod, persist database data in the named volume from `deploy/quadlet/trellmark-postgres-data.volume`, and bind the app only to `127.0.0.1:8901` per `deploy/quadlet/trellmark.pod`.
- Provide TLS and public routing with the separately managed shared Caddy service described in `README.md`; the application image in `deploy/Containerfile` owns neither TLS keys nor ports 80/443.
- Deploy the OCI image built from `deploy/Containerfile`; tagged releases are published to GitHub Container Registry by `.github/workflows/publish.yml`.

<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

## Naming Patterns

- Use lowercase `snake_case.py` for Python modules, as in `trellmark/bookmarks/domain.py`, `trellmark/platform/runtime.py`, and `tests/api/test_group_hierarchy_api.py`.
- Name Python tests `test_<behavior>.py` and test functions `test_<expected_behavior>`, as in `tests/api/test_url_editing.py` and `tests/frontend/test_group_folding.py`.
- Use lowercase `camelCase` only inside TypeScript code; TypeScript filenames are short lowercase names such as `web/src/api.ts` and `web/src/app.ts`.
- Treat `web/src/generated/openapi.json`, `web/src/generated/openapi.d.ts`, `web/static/api.js`, and `web/static/app.js` as generated artifacts; update them through the recipes in `justfile`, not by hand.
- Use `snake_case` for Python functions and methods, with a leading underscore for module-private helpers: `create_app` and `_content_length` in `trellmark/app.py`, `_validate_parent` in `trellmark/storage.py`, and `_canonical_public_origin` in `trellmark/config.py`.
- Use `camelCase` for TypeScript functions: `requiredElement`, `errorMessage`, and `loadFoldedGroupIds` in `web/src/app.ts`; `jsonRequest` and `siteIconPath` in `web/src/api.ts`.
- Name pytest fixtures and helper functions by the resource or action they provide: `database`, `app`, and `title_fetcher` in `tests/conftest.py`; `http_json`, `group_in`, and `run_async` in `tests/helpers.py`.
- Use `snake_case` for Python locals and parameters, including explicit units or meanings where ambiguity matters: `max_body_size` in `trellmark/app.py`, `expected_version` in `trellmark/storage.py`, and `base_url` in `tests/conftest.py`.
- Use `UPPER_SNAKE_CASE` for Python module constants and sentinel/error codes: `MAX_REQUEST_BODY_BYTES` in `trellmark/app.py`, `DATABASE_URL_ENV` in `trellmark/config.py`, and `GROUP_NAME_CONFLICT` in `trellmark/storage.py`.
- Use `camelCase` for TypeScript locals and module state, such as `csrfToken`, `sessionExpiredHandler`, and `expiryTransitionSent` in `web/src/api.ts`.
- Reserve `UPPER_SNAKE_CASE` in TypeScript for true constants shared across multiple operations, such as `SVG_NS` and `FOLDED_KEY` in `web/src/app.ts`; API path constants remain descriptive `camelCase` in `web/src/api.ts`.
- Use `PascalCase` for Python classes, exceptions, dataclasses, and type aliases, as in `RequestBodyLimitMiddleware`, `PostgresUnavailable`, and `URLRecord` in `trellmark/bookmarks/domain.py`.
- Use Python 3.14 type-alias statements for reusable aliases: `CommandHandler` in `trellmark/cli.py`, `JSONObject` in `trellmark/handlers.py`, and storage result aliases in `trellmark/storage.py`.
- Keep Pydantic models API-adapter-local and SQLAlchemy rows or TypedDict shapes persistence-local. Cross-layer records, commands, and discriminated outcomes are frozen, slotted dataclasses owned by feature domain/application modules.
- Use `PascalCase` for TypeScript types and interfaces (`SessionPayload` in `web/src/api.ts`, `ActiveDrag` in `web/src/app.ts`) and `camelCase` for their fields, except when a generated API field intentionally matches JSON such as `csrf_token` in `web/src/generated/openapi.d.ts`.

## Code Style

- Format Python with Ruff using `uv run ruff format .` or `just format`; verify it with `uv run ruff format --check .` or `just format-check`, as configured in `pyproject.toml` and `justfile`.
- Follow Ruff's default formatter shape in Python: four-space indentation, double-quoted strings, parenthesized multiline calls, and trailing commas, as demonstrated by `trellmark/app.py` and `trellmark/models.py`.
- Format TypeScript consistently with `web/src/api.ts` and `web/src/app.ts`: two-space indentation, double-quoted strings, semicolons, braces on the declaration line, and trailing commas in multiline parameter/argument lists.
- No Prettier, ESLint, or Biome configuration exists in `package.json` or the repository root; `tsc` in `tsconfig.json` is the enforced TypeScript check, so preserve the established manual formatting instead of assuming an unavailable formatter.
- Run `uv run ruff check .` or `just lint`; `pyproject.toml` selects `E4`, `E7`, `E9`, `F`, and `I`, covering import order, key syntax/runtime errors, and Pyflakes findings.
- Keep `.codex/` out of application lint results through the `extend-exclude` setting in `pyproject.toml`; application changes belong under paths such as `trellmark/`, `tests/`, `migrations/`, and `scripts/`.
- Run `uv run basedpyright trellmark` or `just typecheck`; `pyproject.toml` enables strict Python type checking for `trellmark/` on Python 3.14.
- Run `npm run typecheck` or `just typecheck-frontend`; `tsconfig.json` enables `strict`, `noEmitOnError`, bundler resolution, and case-sensitive filename checks for `web/src/app.ts` and its imports.
- Use `just check` before handing off a change; `justfile` combines generated-artifact drift checks, Python lint/format/type checks, TypeScript checks, and pytest.

## Import Organization

- Python has no configured import alias; `pyproject.toml` places the repository root on pytest's `pythonpath`, and production code imports within the `trellmark` package relatively.
- TypeScript has no `paths` alias in `tsconfig.json`; use relative imports, include the runtime `.js` suffix for emitted browser modules as `web/src/app.ts` does with `./api.js`, and use `import type` for type-only dependencies as in `web/src/api.ts`.

## Error Handling

- Raise `ValueError` for invalid caller-supplied domain values and `RuntimeError` for broken configuration or impossible internal state, following `trellmark/url_normalization.py`, `trellmark/config.py`, `trellmark/models.py`, and invariant checks in `trellmark/storage.py`.
- Preserve causal chains with `raise ... from error` when translating parsing, database, or validation exceptions, as in `trellmark/config.py`, `trellmark/handlers.py`, and `trellmark/identity/repository.py`.
- Represent expected conflicts with discriminated dataclass outcomes and translate them through exhaustive feature API mappers ending in `assert_never`; never inspect raw database exception text in route handlers.
- Return API errors through `error_response()` in `trellmark/responses.py`, producing the consistent `{"error": message}` shape; authentication-specific translations in `trellmark/identity/api.py` intentionally avoid reflecting credentials or Pydantic input details.
- Catch narrow exception types where recovery is defined (`IntegrityError`, `DBAPIError`, `SQLAlchemyError`) in `trellmark/storage.py` and `trellmark/identity/api.py`; broad `Exception` catches are limited to intentionally best-effort remote metadata operations in `trellmark/handlers.py`.
- In TypeScript, turn unsuccessful fetch responses into `Error` or `SessionExpiredError` in `web/src/api.ts`, catch at the UI action boundary in `web/src/app.ts`, and normalize unknown caught values with `errorMessage(error: unknown)`.
- Use empty TypeScript `catch` blocks only for genuinely optional browser storage, as in theme and fold-state persistence in `web/src/app.ts`; observable API or DOM failures must surface to the user or restore state.
- Convert operator-facing CLI failures to `SystemExit` without tracebacks in `trellmark/cli.py`; keep messages actionable and avoid echoing secret values, following redaction in `trellmark/config.py`.

## Logging

- Do not add ad hoc `print()` calls to request, storage, or authentication code in `trellmark/app.py`, `trellmark/storage.py`, or `trellmark/identity/`; failures are currently represented by responses, raised exceptions, or CLI exits.
- Keep any future diagnostics free of credentials, session tokens, raw request payloads, and unredacted database URLs; the existing safe representation is `redacted_database_url()` in `trellmark/config.py`.
- Let Uvicorn own access/server output at the process boundary in `trellmark/cli.py`; tests suppress Uvicorn access logging in `tests/conftest.py` to keep assertions deterministic.

## Comments

- Explain security, transaction, concurrency, and browser-behavior rationale that the code cannot express, as in DSN redaction comments in `trellmark/config.py`, sibling-lock ordering in `trellmark/storage.py`, password-error handling in `trellmark/identity/api.py`, and tree/fold behavior in `web/src/app.ts`.
- Keep comments adjacent to the constraint they protect and describe why the constraint exists; `tests/postgres.py` documents destructive-test safeguards beside `TEST_DB_MARKER` and `reset_database()`.
- Avoid narrating straightforward code; small utilities such as `trellmark/request_utils.py` and `trellmark/responses.py` are intentionally self-explanatory.
- Python uses concise docstrings on public or non-obvious behavior, including `database_url()` in `trellmark/config.py`, `update_url_record()` in `trellmark/storage.py`, and fixtures/helpers in `tests/conftest.py` and `tests/helpers.py`.
- TypeScript uses ordinary `//` comments for module-local rationale in `web/src/app.ts`; no JSDoc/TSDoc generation convention is present in `package.json`, so add documentation comments only when they clarify a public or subtle contract.

## Function Design

- Type every production Python parameter and return value under the strict settings in `pyproject.toml`; tests in `tests/` may remain lighter but should use clear fixtures and data shapes.
- Use keyword-only Python parameters for options that are easy to swap or safety-sensitive, as in `update_url_record(..., *, expected_version, fields)` in `trellmark/storage.py` and `error_response(..., *, headers)` in `trellmark/responses.py`.
- Inject replaceable external behavior at application/service construction boundaries, as `create_app(title_fetcher, icon_service)` does in `trellmark/app.py`; tests configure these through indirect fixtures in `tests/conftest.py`.
- Keep TypeScript parameters and return values explicit at API and DOM helper boundaries, as in `jsonRequest<ResponseBody>()` in `web/src/api.ts` and `requiredElement<T extends Element>()` in `web/src/app.ts`.
- Validate HTTP payloads with feature API-adapter Pydantic contracts; keep them synchronized with `web/src/generated/openapi.d.ts` through `just generate-api-types`.
- Return frozen, slotted dataclass records and outcomes across persistence/application boundaries; SQLAlchemy row and `TypedDict` mappings must remain inside persistence adapters.
- Use `None` for optional absence and discriminated dataclass variants for expected mutation failures; retain exceptions for invalid calls, infrastructure failures, violated invariants, and the established `BookmarkMutationConflict` gate.
- Return copies when sorting would otherwise mutate API state, as `sortUrls()` does in `web/src/app.ts`; preserve server order when no transformation is required.

## Module Design

- Re-export the supported Python package surface explicitly through `__all__` in `trellmark/__init__.py`; keep internal helpers underscore-prefixed in their owning modules.
- Prefer named TypeScript exports from `web/src/api.ts`; `web/src/app.ts` is the browser entry module and imports only the API functions and types it consumes.
- Keep route operation tuples in `trellmark/identity/routes.py`, ports in feature application modules, and cross-layer dataclass records in feature domain modules. Capture constructed services in router and lifecycle closures.
- `trellmark/__init__.py` deliberately exports only `create_app` and `main`. Internal production modules import explicit owners; never restore broad handler, storage, model, or adapter re-exports.
- No TypeScript barrel file exists under `web/src/`; import from `web/src/api.ts` or generated types directly rather than adding an index module without a concrete need.
- Generated declarations in `web/src/generated/openapi.d.ts` are the browser contract source; regenerate them with `just generate-api-types` after changing feature API routes or Pydantic models.

<!-- GSD:conventions-end -->

## Executable Backend Boundaries

- Use feature-first `trellmark.bookmarks`, `trellmark.backup`, and `trellmark.identity` packages. API, persistence, integration, and boundary adapters depend inward on application services and then domain policies. Exhaustive Import Linter layers require every feature module to be assigned; cores cannot import FastAPI, Starlette, Pydantic, SQLAlchemy, psycopg, or aiohttp, including indirectly.
- Keep `trellmark.platform` product-neutral: it imports no feature. Bookmarks and Identity cannot depend on other features; Backup cannot depend on Identity. Cross-layer records, commands, and outcomes are frozen, slotted dataclasses. Pydantic is API-adapter-local (shared transport mechanics may live in `platform.contracts`); SQLAlchemy rows and any `TypedDict` shapes are persistence-local.
- Backup shares Bookmark domain records and reuses Bookmark HTTP contracts only through `backup.api`. Its persistence adapter coordinates the transaction and uses the protected `bookmarks.backup` contributor on that connection plus Bookmark lock constants. Only `backup.persistence` and the composition root may import the contributor; only the explicitly listed persistence/construction modules may import Bookmark persistence. Protected rules deny Backup direct access to Bookmark application services and integrations.
- Never import the broad `trellmark` facade internally or resurrect imports of removed handlers, storage, models, storage types, app keys, title/icon helpers, response helpers, URL/title normalizers, or Identity policy/repository modules. Use their feature/platform owners.
- Run `just check-imports` for the pinned Import Linter contracts in `pyproject.toml`; ordinary `just check` includes this gate alongside existing validation. `tests/architecture/test_backend_imports.py` runs the real CLI against the repository and disposable forbidden-edge fixtures. Keep zero `ignore_imports` and zero exhaustive-layer exemptions; do not replace this gate with an AST parser.

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

## System Overview

```text

```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Process entry point | Delegate to the CLI while preserving the package's legacy public surface | `server.py` |
| CLI and startup gate | Parse serve/migration/password commands, verify configuration/schema/identity, launch Uvicorn | `trellmark/cli.py` |
| FastAPI composition root | Build middleware order, register routes, inject metadata services, serve static files and SPA fallback | `trellmark/app.py` |
| Authentication boundary | Default-deny private `/api/*` requests and enforce session, Origin, Fetch Metadata, and CSRF checks before route parsing | `trellmark/identity/boundary.py` |
| Identity endpoints | Login, session status, logout, health, readiness, and secure cookie handling | `trellmark/identity/api.py` |
| Identity repository | Password verification, login throttling, opaque sessions, session revocation, and identity readiness against PostgreSQL | `trellmark/identity/repository.py` |
| Content handlers | Parse requests, coordinate validation/storage/metadata fetches, map storage results to HTTP payloads | `trellmark/handlers.py` |
| API contracts | Strict Pydantic request/response models plus import document cleaning and hierarchy validation | `trellmark/models.py` |
| Content repository | SQLAlchemy Core schema mirror, connection pool, transactions, group hierarchy, URL membership, import/export, and icon cache | `trellmark/storage.py` |
| Feature records | Frozen, slotted dataclasses crossing persistence/application boundaries; SQLAlchemy row/TypedDict shapes stay persistence-local | Feature `domain.py` modules |
| Metadata clients | Bounded, SSRF-aware title and icon retrieval; icon TTL cache and singleflight refresh | `trellmark/page_titles.py`, `trellmark/site_icons.py` |
| Browser API client | Same-origin fetch wrapper, generated OpenAPI-derived types, CSRF header injection, session-expiry transition | `web/src/api.ts` |
| Browser application | DOM rendering, local UI state, forms/dialogs, drag reorder, authentication views | `web/src/app.ts` |
| Database baseline | Create the entire PostgreSQL schema, `ltree` hierarchy triggers, indexes, default group, and disabled initial administrator | `migrations/versions/0001_trellmark_baseline.py` |

## Pattern Overview

- One FastAPI/Uvicorn process owns the JSON API, authentication, SPA shell, and static assets; the separate infrastructure repository owns public TLS and Caddy routing (`README.md`, `deploy/Containerfile`).
- Backend behavior follows composition root → security middleware → route handler → contract/storage/service modules. It is a transaction-script style rather than a class-heavy service/domain architecture (`trellmark/app.py`, `trellmark/handlers.py`, `trellmark/storage.py`).
- PostgreSQL is the only durable backend. SQLAlchemy Core table declarations mirror an Alembic-owned schema; no ORM entities or SQLite adapter exist (`trellmark/storage.py`, `migrations/env.py`).
- Browser/server contracts originate in FastAPI/Pydantic OpenAPI, are exported to `web/src/generated/openapi.json`, and become TypeScript declarations in `web/src/generated/openapi.d.ts` (`scripts/export_openapi.py`, `tools/openapi-types/package.json`).
- The application is intentionally single-user. The identity package still forms a separate boundary with its own schema mirror, policy constants, middleware, and API (`trellmark/identity/`).

## Layers

- Purpose: Validate startup invariants and assemble the deployable ASGI application.
- Location: `server.py`, `trellmark/cli.py`, `trellmark/app.py`
- Contains: CLI commands, Uvicorn settings, FastAPI application factory, route declarations, middleware order, static mounting, application lifespan.
- Depends on: Configuration, identity, handlers, metadata services, persistence.
- Used by: Local execution, container command, tests, and OpenAPI export.
- Purpose: Apply cross-route controls before FastAPI reads route-specific inputs.
- Location: `trellmark/app.py`, `trellmark/identity/boundary.py`, `trellmark/identity/routes.py`
- Contains: Global body-size cap, default-deny API authentication, Origin/Fetch Metadata/CSRF checks, response cache policy.
- Depends on: Identity repository, canonical public origin, shared error responses.
- Used by: Every HTTP request through the ASGI middleware stack.
- Purpose: Convert HTTP input into application operations and response contracts.
- Location: `trellmark/handlers.py`, `trellmark/identity/api.py`
- Contains: Request decoding, per-operation validation/error mapping, storage calls, metadata orchestration, response shaping.
- Depends on: `trellmark/models.py`, `trellmark/storage.py`, identity repository, injected title/icon services.
- Used by: Routes registered in `trellmark/app.py`.
- Purpose: Define the public JSON model and normalize values before persistence.
- Location: `trellmark/models.py`, `trellmark/url_normalization.py`, `trellmark/title_text.py`
- Contains: Pydantic contracts, strict scalar constraints, import graph validation, URL/domain/title normalization.
- Depends on: Standard library and internal storage type declarations.
- Used by: Handlers, OpenAPI generation, generated TypeScript client types.
- Purpose: Own PostgreSQL access and transactional invariants.
- Location: Feature `persistence.py` adapters, feature `domain.py` dataclasses, and `trellmark/platform/runtime.py`.
- Contains: SQLAlchemy Core table metadata, pooled engine, read/write functions, row conversion, optimistic locking, advisory/row locking, identity/session transactions.
- Depends on: `trellmark/config.py`, PostgreSQL and `ltree` schema installed by Alembic.
- Used by: Content handlers, identity API/middleware, metadata cache service, CLI readiness/password commands.
- Purpose: Retrieve untrusted public page titles and site icons within strict time/size/address bounds.
- Location: `trellmark/page_titles.py`, `trellmark/site_icons.py`, `trellmark/app_keys.py`
- Contains: `aiohttp` clients, redirect handling, DNS public-address checks, bounded body readers, icon validation, TTL cache, injected protocols.
- Depends on: Public network, storage-backed icon cache.
- Used by: URL create/title refresh/metadata/icon handlers.
- Purpose: Render and mutate the saved group/URL tree without a frontend framework.
- Location: `web/index.html`, `web/src/app.ts`, `web/src/api.ts`, `web/static/styles.css`
- Contains: Semantic HTML shell, imperative DOM rendering, dialogs/forms, drag reorder, same-origin API wrapper, in-memory and localStorage UI state.
- Depends on: Generated OpenAPI declarations and same-origin FastAPI routes.
- Used by: The browser after FastAPI serves `web/index.html` and `web/static/app.js`.

## Data Flow

### Primary Authenticated Content Request

### Browser Session and Mutation Flow

### Metadata Refresh Flow

- Durable state lives only in PostgreSQL: URLs, group hierarchy/membership/domain rules, identity, throttles, sessions, and the site-icon cache (`migrations/versions/0001_trellmark_baseline.py`).
- `trellmark/storage.py` holds one lazy process-lifetime SQLAlchemy engine/pool and disposes it during application shutdown (`trellmark/storage.py:287`, `trellmark/app.py:238`).
- `app.state` holds injectable title and icon services; `SiteIconService` keeps only per-process in-flight refresh tasks/semaphore state (`trellmark/app.py:279`, `trellmark/site_icons.py:227`).
- The SPA keeps the last authoritative group tree, session CSRF token, dialogs, and drag state in module variables. Theme and folded group IDs are browser-only `localStorage` preferences (`web/src/app.ts:210`, `web/src/app.ts:217`).

## Key Abstractions

- Purpose: Provide one composition root that production, tests, and OpenAPI export can instantiate.
- Examples: `trellmark/app.py`, `scripts/export_openapi.py`, `tests/conftest.py`
- Pattern: Factory with injected async `TitleFetcher` and structural `IconService` test seams.
- Purpose: Keep request, response, and import/export JSON explicit and reflected into OpenAPI.
- Examples: `trellmark/models.py`, `web/src/generated/openapi.d.ts`
- Pattern: Strict response models (`ContractModel`, extra forbidden) and compatibility-oriented request models (`RequestModel`, extra ignored).
- Purpose: Cross the persistence/application boundary without ORM entities and retain explicit failure reasons.
- Examples: `trellmark/bookmarks/domain.py`, `trellmark/bookmarks/application.py`
- Pattern: Frozen, slotted dataclass records and discriminated outcome unions; feature API adapters match exhaustively with a final `assert_never`.
- Purpose: Represent a maximum-three-level hierarchy with stable sibling positions and direct URL membership.
- Examples: `trellmark/storage.py`, `migrations/versions/0001_trellmark_baseline.py`, `trellmark/models.py`
- Pattern: Relational `parent_id` plus materialized `ltree path`; database triggers maintain paths, while Python assembles ordered nested records.
- Purpose: Protect the entire private API before route-specific parsing and centralize single-user session policy.
- Examples: `trellmark/identity/boundary.py`, `trellmark/identity/repository.py`, `trellmark/identity/policy.py`
- Pattern: ASGI middleware + repository transaction + opaque server-side session and a session-bound CSRF token.
- Purpose: Tie frontend request and response types to the backend's actual OpenAPI document.
- Examples: `web/src/api.ts`, `web/src/generated/openapi.json`, `web/src/generated/openapi.d.ts`
- Pattern: Generated type declarations consumed by a small handwritten fetch adapter.

## Entry Points

- Location: `server.py`
- Triggers: `python server.py`, the container `CMD`, or one of the CLI subcommands.
- Responsibilities: Invoke `trellmark.cli.main`; default to `serve` when no subcommand is provided.
- Location: `trellmark/app.py`
- Triggers: `trellmark.cli.serve()`, tests, or `scripts/export_openapi.py`.
- Responsibilities: Construct FastAPI, middleware, routes, dependencies, services, static mount, and SPA fallback.
- Location: `migrations/env.py`
- Triggers: Alembic via `python server.py migrate` / `just migrate`.
- Responsibilities: Resolve the application DSN and run revisions online or offline.
- Location: `web/src/app.ts`
- Triggers: `<script type="module" src="/static/app.js">` in `web/index.html`.
- Responsibilities: Bind the fixed DOM shell, initialize theme/session, load the group tree, and register all interaction handlers.
- Location: `scripts/export_openapi.py`
- Triggers: `just generate-api-types`, `just check-api-types`, and CI.
- Responsibilities: Materialize `create_app().openapi()` for the TypeScript declaration generator.

## Architectural Constraints

- **Threading:** Uvicorn runs the async ASGI event loop. Password/session repository work explicitly uses AnyIO/Starlette worker threads (`trellmark/identity/api.py`, `trellmark/identity/boundary.py`), while most content handlers call synchronous SQLAlchemy directly. Icon refresh is async and capped to two concurrent origins per process (`trellmark/site_icons.py`).
- **Global state:** The lazy SQLAlchemy engine and its rendered URL live in module globals in `trellmark/storage.py`; `app.state` owns service instances; the SPA uses module-level UI/session state in `web/src/app.ts` and `web/src/api.ts`.
- **Circular imports:** Keep feature domain/application modules inward-facing and platform product-neutral. Never import the package-wide `trellmark` facade internally; enforce these boundaries with the committed Import Linter contracts.
- **Database:** PostgreSQL with the `psycopg` driver is mandatory. The schema must equal the current Alembic head before serving, and hierarchy operations depend on the PostgreSQL `ltree` extension and trigger functions (`trellmark/config.py`, `trellmark/storage.py`, `migrations/versions/0001_trellmark_baseline.py`).
- **Group hierarchy:** Depth is fixed at three, positions are unique within each sibling set, the `default` group cannot be edited/deleted, and path/parent agreement is database-enforced (`trellmark/storage.py`, `trellmark/models.py`).
- **HTTP boundary:** Request bodies are capped at 1,000,000 bytes. `/api/*` is default-deny except the enumerated public operations. Unsafe authenticated methods require exact configured Origin plus CSRF, and application/internal responses are not cacheable (`trellmark/app.py`, `trellmark/identity/boundary.py`).
- **Deployment:** The app container listens on port 8000 inside a rootless Podman pod, but Quadlet exposes it only at `127.0.0.1:8901`. Public TLS/routing and blocking `/internal/*` belong to external Caddy infrastructure (`deploy/Containerfile`, `deploy/quadlet/trellmark.pod`, `README.md`).
- **Frontend build:** Author TypeScript in `web/src/`, but the container serves committed output from `web/static/`. Generated OpenAPI files and emitted JavaScript must be regenerated and checked together (`justfile`, `tsconfig.json`).

## Anti-Patterns

### Blocking Persistence Calls Inside Async Handlers

### Expanding Monolithic Modules and the Package-Wide Facade

## Error Handling

- FastAPI/Pydantic handles typed path/query/body validation; handlers that manually parse JSON convert `ValidationError` into `RequestValidationError` or a concise `error_response()` (`trellmark/handlers.py`, `trellmark/responses.py`).
- Storage returns explicit literal error codes for expected conflicts/not-found/hierarchy failures, while unexpected database exceptions propagate (`trellmark/storage.py`).
- Identity HTTP code catches `SQLAlchemyError` and returns `503`; invalid credentials and throttle blocks have distinct `401`/`429` responses (`trellmark/identity/api.py`).
- CLI startup converts configuration/schema/identity `RuntimeError` values into actionable `SystemExit` messages before binding the server (`trellmark/cli.py`).
- Title/icon fetchers return `None` for bounded network/parser failures; handlers decide whether that is a successful no-update or a `502` (`trellmark/page_titles.py`, `trellmark/site_icons.py`, `trellmark/handlers.py`).
- There is no application-wide unexpected-exception mapper or structured application logger; unhandled exceptions use FastAPI/Uvicorn defaults (`trellmark/app.py`).

## Cross-Cutting Concerns

<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
