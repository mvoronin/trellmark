# Coding Conventions

**Analysis Date:** 2026-08-29

## Naming Patterns

**Files:**
- Use lowercase `snake_case.py` for Python modules, as in `trellmark/bookmarks/domain.py`, `trellmark/platform/runtime.py`, and `tests/api/test_group_hierarchy_api.py`.
- Name Python tests `test_<behavior>.py` and test functions `test_<expected_behavior>`, as in `tests/api/test_url_editing.py` and `tests/frontend/test_group_folding.py`.
- Use lowercase `camelCase` only inside TypeScript code; TypeScript filenames are short lowercase names such as `web/src/api/client.ts` and `web/src/main.ts`.
- Treat `web/src/generated/openapi.json`, `web/src/generated/openapi.d.ts`, the complete `web/static/**/*.js` tree, `web/index.html`, and `web/design.html` as generated artifacts; update them through the recipes in `justfile`, not by hand.

**Functions:**
- Use `snake_case` for Python functions and methods, with a leading underscore for module-private helpers: `create_app` and `_content_length` in `trellmark/app.py`, `_validate_parent` in `trellmark/storage.py`, and `_canonical_public_origin` in `trellmark/config.py`.
- Use `camelCase` for TypeScript functions: `requiredElement` in `features/bookmarks/index.ts`, `errorMessage` in `shared/format.ts`, and `createBookmarksModel` in `features/bookmarks/model.ts`; `jsonRequest` and `siteIconPath` in `web/src/api/client.ts`.
- Name pytest fixtures and helper functions by the resource or action they provide: `database`, `app`, and `title_fetcher` in `tests/conftest.py`; `http_json`, `group_in`, and `run_async` in `tests/helpers.py`.

**Variables:**
- Use `snake_case` for Python locals and parameters, including explicit units or meanings where ambiguity matters: `max_body_size` in `trellmark/app.py`, `expected_version` in `trellmark/storage.py`, and `base_url` in `tests/conftest.py`.
- Use `UPPER_SNAKE_CASE` for Python module constants and sentinel/error codes: `MAX_REQUEST_BODY_BYTES` in `trellmark/app.py`, `DATABASE_URL_ENV` in `trellmark/config.py`, and `GROUP_NAME_CONFLICT` in `trellmark/storage.py`.
- Use `camelCase` for TypeScript locals and module state, such as `csrfToken`, `sessionExpiredHandler`, and `expiryTransitionSent` in `web/src/api/client.ts`.
- Reserve `UPPER_SNAKE_CASE` in TypeScript for true constants shared across multiple operations, such as `SVG_NS` in `shared/icons.ts` and `FOLDED_KEY` in `features/bookmarks/model.ts`; API path constants remain descriptive `camelCase` in `web/src/api/client.ts`.

**Types:**
- Use `PascalCase` for Python classes, exceptions, dataclasses, and type aliases, as in `RequestBodyLimitMiddleware`, `PostgresUnavailable`, and `URLRecord` in `trellmark/bookmarks/domain.py`.
- Use Python 3.14 type-alias statements for reusable aliases: `CommandHandler` in `trellmark/cli.py`, `JSONObject` in `trellmark/handlers.py`, and storage result aliases in `trellmark/storage.py`.
- Use frozen/slotted dataclasses and recursive tuples for Bookmarks group/URL values in `trellmark/bookmarks/domain.py`; all Bookmark Pydantic contracts, including combined metadata, live only in `trellmark/bookmarks/api.py` with explicit `url_to_wire`/`group_to_wire` mapping. URL commands/outcomes use the same immutable records as group queries. Identity contracts live only in `trellmark/identity/api.py`; legacy `models.py` and `storage_types.py` have been removed. SQLAlchemy rows and any `TypedDict` mappings remain persistence-local.
- Use frozen/slotted `PortableDocument`, `PortableGroup`, and `PortableURL` values in `backup/domain.py`; Backup hierarchy validation consumes the existing recursive Bookmarks `GroupRecord`. Pydantic import/export schemas and explicit JSON/domain mapping live only in `backup/api.py`; core code imports neither Pydantic nor persistence row types.
- Use frozen/slotted Identity commands, sessions, and outcome unions in `trellmark/identity/domain.py`. Exclude credentials, opaque cookies, CSRF values, and login/source fields from generated representations. Argon2 and SQLAlchemy rows belong only in `identity/persistence.py`.
- Use `PascalCase` for TypeScript types and interfaces (`SessionPayload` in `web/src/api/client.ts`, `ActiveDrag` in `features/bookmarks/model.ts`) and `camelCase` for their fields, except when a generated API field intentionally matches JSON such as `csrf_token` in `web/src/generated/openapi.d.ts`.

## Code Style

**Formatting:**
- Format Python with Ruff using `uv run ruff format .` or `just format`; verify it with `uv run ruff format --check .` or `just format-check`, as configured in `pyproject.toml` and `justfile`.
- Follow Ruff's default formatter shape in Python: four-space indentation, double-quoted strings, parenthesized multiline calls, and trailing commas, as demonstrated by `trellmark/app.py` and `trellmark/models.py`.
- Format TypeScript consistently with `web/src/api/client.ts` and `web/src/main.ts`: two-space indentation, double-quoted strings, semicolons, braces on the declaration line, and trailing commas in multiline parameter/argument lists.
- No Prettier, ESLint, or Biome configuration exists in `package.json` or the repository root; `tsc` in `tsconfig.json` is the enforced TypeScript check, so preserve the established manual formatting instead of assuming an unavailable formatter.

**Linting:**
- Run `uv run ruff check .` or `just lint`; `pyproject.toml` selects `E4`, `E7`, `E9`, `F`, and `I`, covering import order, key syntax/runtime errors, and Pyflakes findings.
- Keep `.codex/` out of application lint results through the `extend-exclude` setting in `pyproject.toml`; application changes belong under paths such as `trellmark/`, `tests/`, `migrations/`, and `scripts/`.
- Run `uv run basedpyright trellmark` or `just typecheck`; `pyproject.toml` enables strict Python type checking for `trellmark/` on Python 3.14.
- Run `npm run typecheck` or `just typecheck-frontend`; `tsconfig.json` enables `strict`, `noEmitOnError`, bundler resolution, and case-sensitive filename checks for every configured `web/src/**/*.ts` source, including unreachable modules.
- Use `just check` before handing off a change; `justfile` combines generated-artifact drift checks, Python lint/format/type checks, TypeScript/native frontend import checks, backend import checks, and pytest.

## Import Organization

**Order:**
1. Put Python standard-library imports first, as in `trellmark/handlers.py` (`asyncio`, `json`, `datetime`, `typing`, `urllib.parse`).
2. Put third-party imports next, as in `trellmark/handlers.py` (`fastapi`, `pydantic`) and `trellmark/storage.py` (`alembic`, `sqlalchemy`).
3. Put project imports last; production package modules use relative imports such as `.models` in `trellmark/handlers.py`, while tests use absolute imports such as `import trellmark` and `from tests.helpers import http_json` in `tests/api/test_urls_create_and_delete.py`.
4. Let Ruff split and alphabetize Python imports; the deliberately aliased storage imports in `trellmark/handlers.py` and re-exports in `trellmark/__init__.py` remain separate when names would otherwise collide.

**Path Aliases:**
- Python has no configured import alias; `pyproject.toml` places the repository root on pytest's `pythonpath`, and production code imports within the `trellmark` package relatively.
- TypeScript has no `paths` alias in `tsconfig.json`; use relative imports, include the runtime `.js` suffix for emitted browser modules as `web/src/main.ts` does with `./api/client.js`, and use `import type` for type-only dependencies as in `web/src/api/client.ts`.

## Error Handling

**Identity application:** Unexpected failures propagate unchanged through the bounded runner. `LoginFailureRecorded` and `LoginThrottled` are successful policy effects with durable accounting/housekeeping, so their UoW commits explicitly before API mapping or compatibility exception translation. `SessionMissing` leaves write scopes uncommitted. Cleanup always attempts connection close and preserves an existing operation exception; it propagates its own failure when no primary exception exists. Broad safe database-to-503 handling stays in the existing HTTP/ASGI adapters.

**Identity transport:** Capture the exact immutable service in `build_identity_router` and await its typed commands. Keep the existing capacity-1 login admission around the async service call; the capacity-2 runner owns the entire worker scope. Own all Pydantic classes and redacted request validation in the adapter. Use one `map_identity_outcome` ending in `assert_never` for bodies, throttle headers, and cookie transitions; logout checks Origin/Fetch Metadata/CSRF before calling revocation. Retain shared public tuples, readiness exclusion, canonical-origin cookie selection and exact attributes. Do not import SQL, legacy repositories/policy, or contracts through the package facade. The framework-free Identity facade exports only the application service and command/session values.

**Identity ASGI boundary:** Inject that same constructed service through middleware arguments; never look it up from request/app state or offload a separate compatibility function. Match session outcomes exhaustively before route-body parsing, preserve source/CSRF checks, and translate broad SQLAlchemy errors only around the service await. Keep `PUBLIC_OPERATIONS` authoritative in `identity/routes.py`, with readiness outside that set. Install no-store outside authentication and body-size middleware. `identity/policy.py` and `identity/repository.py` no longer exist.

**Patterns:**
- Raise `ValueError` for invalid caller-supplied domain values and `RuntimeError` for broken configuration or impossible internal state, following `trellmark/url_normalization.py`, `trellmark/config.py`, `trellmark/models.py`, and invariant checks in `trellmark/storage.py`.
- Preserve causal chains with `raise ... from error` when translating parsing, database, or validation exceptions, as in `trellmark/config.py` and `trellmark/identity/persistence.py`.
- Represent expected conflicts with discriminated dataclass outcomes and translate them through exhaustive feature API mappers ending in `assert_never`; never inspect raw database exception text in route handlers.
- Return API errors through `error_response()` in `trellmark/platform/responses.py`, producing the consistent `{"error": message}` shape; the legacy response module has been removed, and authentication-specific translations intentionally avoid reflecting credentials or Pydantic input details.
- Catch narrow exception types where recovery is defined in persistence and boundary adapters. Title and combined metadata application catches surround only the pure `TitleFetcher` await, warn once with `exc_info=True` and no explicit private arguments, and let cancellation plus all query/repository/UoW errors propagate. Icon integrations likewise catch only the pure fetch await before cache persistence.
- In TypeScript, turn unsuccessful fetch responses into `Error` or `SessionExpiredError` in `web/src/api/client.ts`, catch at the owning shell or Bookmark action boundary, and normalize unknown caught values with `errorMessage(error: unknown)`.
- Use empty TypeScript `catch` blocks only for genuinely optional browser storage, as in theme persistence in `shared/theme.ts` and fold persistence in `features/bookmarks/model.ts`; observable API or DOM failures must surface to the user or restore state.
- Convert operator-facing CLI failures to `SystemExit` without tracebacks in `trellmark/cli.py`; keep messages actionable and avoid echoing secret values, following redaction in `trellmark/config.py`.

## Logging

**Framework:** Uvicorn runtime logging only; no application logging framework is imported by `trellmark/`, while `trellmark/cli.py` uses `print` only for successful interactive command output.

**Patterns:**
- Do not add ad hoc `print()` calls to request, storage, or authentication code in `trellmark/app.py`, `trellmark/storage.py`, or `trellmark/identity/`; failures are currently represented by responses, raised exceptions, or CLI exits.
- Keep any future diagnostics free of credentials, session tokens, raw request payloads, and unredacted database URLs; the existing safe representation is `redacted_database_url()` in `trellmark/config.py`.
- Let Uvicorn own access/server output at the process boundary in `trellmark/cli.py`; tests suppress Uvicorn access logging in `tests/conftest.py` to keep assertions deterministic.

## Comments

**When to Comment:**
- Explain security, transaction, concurrency, and browser-behavior rationale that the code cannot express, as in DSN redaction comments in `trellmark/config.py`, sibling-lock ordering in `trellmark/storage.py`, password-error handling in `trellmark/identity/api.py`, and tree/fold behavior in `features/bookmarks/model.ts`.
- Keep comments adjacent to the constraint they protect and describe why the constraint exists; `tests/postgres.py` documents destructive-test safeguards beside `TEST_DB_MARKER` and `reset_database()`.
- Avoid narrating straightforward code; small utilities such as `trellmark/request_utils.py` and `trellmark/platform/responses.py` are intentionally self-explanatory.

**JSDoc/TSDoc:**
- Python uses concise docstrings on public or non-obvious behavior, including `database_url()` in `trellmark/config.py`, `update_url_record()` in `trellmark/storage.py`, and fixtures/helpers in `tests/conftest.py` and `tests/helpers.py`.
- TypeScript uses ordinary `//` comments for module-local rationale in `web/src/main.ts`; no JSDoc/TSDoc generation convention is present in `package.json`, so add documentation comments only when they clarify a public or subtle contract.

## Function Design

**Size:** Keep validation, protocol translation, and persistence separate. Extracted group and ordinary URL contracts/routes belong to `bookmarks/api.py`, framework-free use cases to `bookmarks/application.py`, and connection-bound SQL to `bookmarks/persistence.py`. Each logical writer runs one bounded worker closure and commits only an exhaustive success outcome; other exits roll back. Feature routes await the captured service, and `map_url_outcome` has one exhaustive match with final `assert_never`. Ordinary URL transport must not import legacy handlers, models, storage, or row types. Product-neutral HTTP bases and JSON validation mechanics live in `platform/contracts.py`.

**URL boundaries:** URL/domain/title normalization is owned by `bookmarks/domain.py`; the legacy `url_normalization.py` and `title_text.py` forwarders have been removed. Conditional edits write with both id and expected version before distinguishing a failed write as missing or stale. Translate only the exact PostgreSQL `uq_urls_url` diagnostic, preserve unrelated exception identity, and keep cleanup failures from replacing a primary mutation failure.

**Title boundaries:** Inject the framework-free `TitleFetcher` when constructing the immutable Bookmarks service. Create commits insertion before fetching and never fetches duplicates; its separate update UoW re-reads the current row under the gate and conditionally writes the current version to preserve established create races. Refresh captures its expected version before fetching and retains that version for the gated conditional update. Never hold a connection, transaction, or gate across fetching. `bookmarks.integrations` owns deadlines, redirects, title/oEmbed parsing and silent known failures; `platform.network` owns only generic DNS/address/body safety. Keep shared pure title normalization in the domain so Backup does not depend on an outbound adapter.

**Create/title transport:** Preserve the existing route names, manually documented create JSON body and form parsing, response statuses, and generated schemas when moving ownership. Both routes await the captured Bookmarks service and use the shared exhaustive URL mapper. Route tests use `iter_route_contexts` for included routers; seed setup records through the real Bookmark insert UoW without invoking the test's injected fetch port. Preserve the complete route-derived writer matrix, accounting for creation's two separate gates on success.

**Icon/cache boundaries:** Inject `SiteIconGateway` into Bookmarks and a `SiteIconCache` port into the integration. Immutable icon/cache records belong to the domain; cache SQL belongs only to `PostgresSiteIconCacheRepository`. Run complete queries and rollback-by-default derived UoWs through a separate capacity-2 runner, committing successful writes explicitly. Derived work must touch only `site_icon_cache` and never acquire the logical gate. Catch anomalies only around the pure fetch await, warn without explicit private arguments, and preserve cancellation and primary database exceptions even if cleanup also fails. Share cache lookups before yielding to preserve first-caller singleflight ordering; drain both lookup and refresh tasks. Preserve independent metadata flags, pre-fetch title versions, and current-row rereads when no title is fetched. Test cache semantics with PostgreSQL; use doubles only for lifecycle and fetch barriers.

**Backup boundaries:** Normalize documents only through `backup.domain.normalize_import_document`; map an already-normalized Pydantic contract with `to_domain()` without recleaning. Preserve original group/URL sequence, lowercase name matching, second-resolution UTC URL timestamps, and original valid `exported_at` text. `BackupApplicationService` sends the complete import or export closure through one capacity-1 runner. Import acquires `(7502, 0)`, locks existing group rows, validates the resulting hierarchy on that connection before DML, and commits only `ImportSucceeded`; invalid outcomes and failures roll back. Protected Bookmark contributors accept the coordinator connection and share only pure Bookmark records/primitives, never transaction ownership. Export reads every component in one ungated read-only REPEATABLE READ scope; ordinary isolation remains READ COMMITTED. Preserve the primary exception through cleanup. Fault stages exist only in `tests/backup/helpers.py` wrappers of the injected UoW/contributor, never production callbacks. `backup.api.build_backup_router` captures the service directly and owns all named contracts, operation metadata, timestamped filenames, and the exhaustive `map_import_outcome`; unexpected database failures propagate to the existing root content exception boundary. Compose explicit contributor factories in `create_app`, preserving the test UoW-factory seam. Do not restore Backup forwards in legacy modules or the package facade.

**Parameters:**
- Type every production Python parameter and return value under the strict settings in `pyproject.toml`; tests in `tests/` may remain lighter but should use clear fixtures and data shapes.
- Use keyword-only Python parameters for options that are easy to swap or safety-sensitive, as in `update_url_record(..., *, expected_version, fields)` in `trellmark/storage.py` and `error_response(..., *, headers)` in `trellmark/platform/responses.py`.
- Inject replaceable external behavior at application/service construction boundaries, as `create_app(title_fetcher, icon_service)` does in `trellmark/app.py`; tests configure these through indirect fixtures in `tests/conftest.py`.
- Keep TypeScript parameters and return values explicit at API and DOM helper boundaries, as in `jsonRequest<ResponseBody>()` in `web/src/api/client.ts` and `h()` in `shared/dom.ts`.

**Return Values:**
- Validate HTTP payloads with feature API-adapter Pydantic contracts; keep them synchronized with `web/src/generated/openapi.d.ts` through `just generate-api-types`.
- Return frozen, slotted dataclass records and outcomes across persistence/application boundaries; SQLAlchemy row and `TypedDict` mappings must remain inside persistence adapters.
- Use `None` for optional absence and discriminated dataclass variants for expected mutation failures; retain exceptions for invalid calls, infrastructure failures, violated invariants, and the established `BookmarkMutationConflict` gate.
- Return copies when sorting would otherwise mutate API state, as `sortUrls()` does in `features/bookmarks/model.ts`; preserve server order when no transformation is required.

## Module Design

**Exports:**
- Re-export the supported Python package surface explicitly through `__all__` in `trellmark/__init__.py`; keep internal helpers underscore-prefixed in their owning modules.
- Prefer named TypeScript exports from `web/src/api/client.ts`; `web/src/main.ts` is the browser entry module and imports only the API functions and types it consumes.
- Keep route operation tuples in `trellmark/identity/routes.py`, ports in feature application modules, and cross-layer dataclass records in feature domain modules. Capture constructed services in router and lifecycle closures.

**Barrel Files:**
- `trellmark/__init__.py` deliberately exports only `create_app` and `main`. Import configuration, platform runtime, feature contracts/services/adapters, and test setup helpers from their explicit owners; do not restore handler/storage/model/adapter re-exports.
- The shell and each feature expose a narrow public `index.ts`. Composition imports those surfaces; only the API adapter consumes generated declarations through type-only imports. Never restore the removed `app.ts` or `api.ts` facades.
- Generated declarations in `web/src/generated/openapi.d.ts` are the browser contract source; regenerate them with `just generate-api-types` after changing feature API routes or Pydantic models.

---

*Convention analysis: 2026-08-29*

## Executable Backend Boundaries

- Use feature-first `trellmark.bookmarks`, `trellmark.backup`, and `trellmark.identity` packages. API, persistence, integration, and boundary adapters depend inward on application services and then domain policies. Exhaustive Import Linter layers require every feature module to be assigned; cores cannot import FastAPI, Starlette, Pydantic, SQLAlchemy, psycopg, or aiohttp, including indirectly.
- Keep `trellmark.platform` product-neutral: it imports no feature. Bookmarks and Identity cannot depend on other features; Backup cannot depend on Identity. Cross-layer records, commands, and outcomes are frozen, slotted dataclasses. Pydantic is API-adapter-local (shared transport mechanics may live in `platform.contracts`); SQLAlchemy rows and any `TypedDict` shapes are persistence-local.
- Backup shares Bookmark domain records and reuses Bookmark HTTP contracts only through `backup.api`. Its persistence adapter coordinates the transaction and uses the protected `bookmarks.backup` contributor on that connection plus Bookmark lock constants. Only `backup.persistence` and the composition root may import the contributor; only the explicitly listed persistence/construction modules may import Bookmark persistence. Protected rules deny Backup direct access to Bookmark application services and integrations.
- Never import the broad `trellmark` facade internally or resurrect imports of removed handlers, storage, models, storage types, app keys, title/icon helpers, response helpers, URL/title normalizers, or Identity policy/repository modules. Use their feature/platform owners.
- Run `just check-imports` for the pinned Import Linter contracts in `pyproject.toml`; ordinary `just check` includes this gate alongside existing validation. `tests/architecture/test_backend_imports.py` runs the real CLI against the repository and disposable forbidden-edge fixtures. Keep zero `ignore_imports` and zero exhaustive-layer exemptions; do not replace this gate with an AST parser.

## Executable Frontend Boundaries

- `web/src/main.ts` composes public shell/feature indices, API and shared primitives. The sole additional composition exception is `web/src/design/main.ts`, which supplies synthetic data to real public views/dialogs. Design fixtures may use API types, never runtime API operations. No other module receives a composition exemption.
- `shared/` imports only shared modules; `api/` owns generated transport types and same-origin requests. Shell and features may use API/shared and their own modules; features never import shell or other features. Public surfaces expose factories, readonly projections and reusable views, not mutable singleton state.
- Shell owns session/private-state lifecycle, modal opening/closing, shared confirmation and import/export. Bookmarks owns group/URL fields, model operations, rendering and drag. Inject structural capabilities across owners, resolve handles within owned roots, and dispose listeners/requests. Imports must not acquire handles, attach listeners or start authentication.
- Use `shared/request.ts` for request tickets/in-flight state and `shared/dom.ts` for safe typed construction. Keep SVG namespace construction explicit. Rendering reads model projections; named operations change state.
- Maintain HTML in `web/html/` and shared fragments; complete `web/index.html`/`web/design.html` are generated. Run `just build-frontend` before committing source changes. `just check-frontend-artifacts` checks every generated path and byte without cleaning or repair. Only `static/styles.css`, `static/icons.svg`, `static/fonts/` and `static/design/frame.css` are maintained exclusions relative to `web/`.
- `just check-frontend-imports` (`npm run check:imports`) uses the pinned native TypeScript parser across every configured root and its dependencies. Local `just check` and CI retain this gate, `just check-api-types`, complete frontend drift checks and backend Import Linter. Negative fixtures exercise forbidden edges; no broad exemption is permitted.
- `just design-mode` builds and serves only `web/` at `http://127.0.0.1:8001/design.html`. Use real components with reload-reset synthetic interactions. Production staging excludes the page and the entire `static/design/` subtree. Preserve DOM/behavior, contrast and overflow tests; introduce no screenshot baselines. Manual appearance review happens after phase completion.

## Ubiquitous Language

**Status:** defined for GitHub issue #11 (access profiles, WebAuthn, multi-user ownership); not yet implemented. The vocabulary is fixed ahead of the work so tickets, schema, and code agree from the first commit.

Language is scoped **per bounded context**, matching the feature packages above. The same word may legitimately mean different things in two contexts; only ambiguity *within* one context is a defect.

**Identity context** (`trellmark/identity/`) — owns who you are and how you prove it. It never learns what a group is.

- `Owner` — the account that owns content. Prefer it to "user" wherever the account is what is meant.
- `Profile` — a named access scope belonging to one owner and bound to credentials. Not an account, not a persona with its own separate content, not a preference set.
- Primary profile and restricted profile — distinguished by `is_primary`; a primary profile's scope is implicit and never stored as grants. Never "admin profile": `users.role` is a separate concept that coexists with profile primacy.
- `Credential` — a WebAuthn or password credential bound to exactly one profile. Not "device": one credential can exist on several devices.
- Device-bound (`single_device`) versus syncable (`multi_device`) credential — the WebAuthn backup-eligibility flag. Primary profiles accept device-bound credentials only.
- Break-glass — recovery through the host CLI. Never "password reset" or "account recovery"; both imply a self-service flow that does not exist.

**Content contexts** (`trellmark/bookmarks/`, and Notes when it lands) — each owns its own hierarchy, its own grant table, and its own copy of the visibility rule.

- `Grant` — one row permitting one profile to see one group *in that context*. Never "permission", "ACL entry", or "rule".
- Profile-visible and hidden — the computed state of a group for a profile.
- Ancestor-closed — a group is profile-visible only when it and every one of its ancestors is granted. Not "inherited", which suggests the opposite direction.
- `landing_group` — the group a profile files new content into; replaces the hardcoded default-group lookup. Not "default group", which is one specific group's name.
- Unreviewed group — created by a restricted profile and not yet granted by the primary profile.

Bookmark grants and note grants are separate tables. A grant over bookmark groups is Bookmarks' language and a grant over note groups is Notes'; only the profile identifier crosses between them.

**Crossing contexts:** `AccessScope` is the only value Identity publishes into the content contexts — owner identifier, profile identifier, unrestricted flag, and nothing else. It is named `AccessScope` rather than `Scope` because it is read in both contexts and Identity already uses `scope` for the throttle bucket kind in `auth_login_throttle.scope`. The ASGI connection `scope` in middleware is platform vocabulary and not a factor.

**Same-context ambiguity to avoid:** `visible` already means "passed the Safe mode filter" in the browser (`visibleUrlIds` in `web/src/app.ts`). Safe mode is a client-side convenience filter; profile visibility is a server-enforced boundary. Qualify both — profile-visible versus Safe-mode filtered — and never let them share a name.

**Recorded as *not* collisions,** so they are not raised again: `private` derived icon-cache state is a docstring adjective rather than a domain term; `users.role` coexists with profile primacy; the ASGI `scope` is platform vocabulary.

**Never used in any context:** permission, ACL, tenant, workspace, persona, guest, sub-user. Each implies a generic authorization model this design does not have and should not grow by accident.
