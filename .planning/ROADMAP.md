# Roadmap: Trellmark

## Archived milestone

- [x] **v0.0.1: Trellmark Foundations** — Phases 1–3 complete; archived as an override closeout. [Archive](milestones/v0.0.1-ROADMAP.md)

## Backlog

The Notes MVP is deferred with minimal priority. Phases 4–6 remain future work and are not active.

## Overview

This brownfield milestone proceeds through six prerequisite-heavy horizontal technical layers. It first makes the existing bookmark import atomic (Issue #2), then establishes enforceable backend and frontend domain boundaries (Issue #3), and only then adds the Notes schema/API, browser experience, safe Markdown handling, and whole-product backup contract (Issue #1). Every phase preserves existing bookmark behavior, single-user authentication, Safe mode, visibility, generated contracts, and repository hygiene while creating a verifiable boundary for the next phase.

## Phases

- [x] **Phase 1: Atomic Bookmark Import** - Complete Issue #2 with one validated, all-or-nothing PostgreSQL import transaction. (completed 2026-08-30)
- [x] **Phase 2: Backend Modular Monolith** - Begin Issue #3 by establishing domain-oriented backend boundaries without changing product behavior. (completed 2026-09-05)
- [x] **Phase 3: Frontend Boundaries and Deterministic Build** - Complete Issue #3 with an authenticated shell, independent browser features, and enforced build boundaries. (completed 2026-09-05)
- [ ] **Phase 4: Notes Domain, Schema, and API** - Begin Issue #1 with independent Notes persistence, hierarchy rules, contracts, and conflict-safe APIs.
- [ ] **Phase 5: Notes Browser Experience and Safe Markdown** - Deliver the authenticated Notes interface, draft-safe conflicts, Safe mode, and inert Markdown preview.
- [ ] **Phase 6: Cross-Domain Backup and Release Gate** - Finish Issue #1 with versioned whole-product portability and full regression, security, and hygiene verification.

## Phase Details

### Phase 1: Atomic Bookmark Import

**Goal**: Users can restore the supported bookmark document without any risk of a partially applied import.
**Depends on**: Nothing (first phase)
**Requirements**: IMPT-01, IMPT-02, IMPT-03, IMPT-04, IMPT-05
**Success Criteria** (what must be TRUE):

  1. User can import a valid supported document, receive the existing duplicate-skip results, and observe all bookmark mutations committed together.
  2. A failure injected after any import mutation leaves every pre-import logical bookmark row and relationship unchanged and returns a stable redacted error.
  3. An invalid document is rejected before the database is mutated.

**Plans**: 4/4 plans executed

Plans:
**Wave 1**

- [x] 01-01-PLAN.md — Prove and expand one-transaction import rollback semantics.

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 01-02-PLAN.md — Enforce the shared immediate bookmark mutation boundary.

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 01-03-PLAN.md — Publish redacted failure responses and generated browser contracts.

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 01-04-PLAN.md — Deliver retained manual retry, documentation, and the release gate.

### Phase 2: Backend Modular Monolith

**Goal**: Existing bookmark, identity, backup, API, and persistence behavior operates through explicit domain-oriented backend boundaries in one deployable service.
**Depends on**: Phase 1
**Requirements**: ARCH-01, ARCH-02, ARCH-03, ARCH-04
**Success Criteria** (what must be TRUE):

  1. User-visible bookmark paths, payloads, authentication, Safe mode, visibility, metadata behavior, and generated API contracts remain unchanged after the backend reorganization.
  2. Every bookmark mutation completes through one application-owned unit of work while PostgreSQL hierarchy, locking, concurrency, and typed error behavior remain intact.
  3. A developer can verify that bookmark domain and application code are framework-free, adapters depend inward, and the composition root contains wiring rather than product rules.

**Plans**: 22/22 plans executed

Plans:

**Wave 1**

- [x] 02-01-PLAN.md — Freeze the untouched compatibility baseline, then prove the composed Bookmark tracer and bounded rollback-by-default transaction path.

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 02-02-PLAN.md — Establish safe content and Identity database error boundaries before moving feature operations.

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 02-03-PLAN.md — Migrate complete Bookmark group behavior into focused domain, application, persistence, and API boundaries.
- [x] 02-04-PLAN.md — Move Identity policy, application operations, and PostgreSQL persistence behind inward-facing ports.

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 02-05-PLAN.md — Migrate ordinary Bookmark URL, membership, version, and conflict behavior without storage sentinels.
- [x] 02-06-PLAN.md — Move Backup normalization and hierarchy policy out of Pydantic into a framework-free domain boundary.

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 02-07-PLAN.md — Publish ordinary URL behavior through the focused Bookmarks API.

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 02-17-PLAN.md — Prove bounded work, rollback-by-default lifecycle, PostgreSQL group outcomes, and complete logical-gate coverage.

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 02-09-PLAN.md — Move create/title behavior behind Bookmarks ports with network-free transaction sequencing.

**Wave 8** *(blocked on Wave 7 completion)*

- [x] 02-18-PLAN.md — Publish create/title workflows through focused Bookmarks routes and remove their transitional handler surfaces.

**Wave 9** *(blocked on Wave 8 completion)*

- [x] 02-10-PLAN.md — Move icon fetch/cache behavior behind Bookmarks ports with a separate ungated derived-state UoW.

**Wave 10** *(blocked on Wave 9 completion)*

- [x] 02-19-PLAN.md — Publish metadata routes, wire lifecycle ownership, and delete obsolete site-icon/app-state seams.

**Wave 11** *(blocked on Wave 10 completion)*

- [x] 02-11-PLAN.md — Deliver coordinator-owned atomic import and repeatable-read export through Backup application boundaries.

**Wave 12** *(blocked on Wave 11 completion)*

- [x] 02-12-PLAN.md — Publish import/export through a focused Backup router while preserving browser round trips.

**Wave 13** *(blocked on Wave 12 completion)*

- [x] 02-13-PLAN.md — Publish Identity through a focused API adapter backed by explicit services.

**Wave 14** *(blocked on Wave 13 completion)*

- [x] 02-20-PLAN.md — Move the default-deny Identity boundary onto explicit services and prove security equivalence.

**Wave 15** *(blocked on Wave 14 completion)*

- [x] 02-14-PLAN.md — Finalize composition and relocate Alembic/runtime database operations before storage deletion.

**Wave 16** *(blocked on Wave 15 completion)*

- [x] 02-15-PLAN.md — Delete broad transport and persistence implementation modules after every caller migrates.

**Wave 17** *(blocked on Wave 16 completion)*

- [x] 02-21-PLAN.md — Delete URL/title compatibility forwarders and prove the complete legacy inventory is absent.

**Wave 18** *(blocked on Wave 17 completion; Import Linter plan has a blocking package checkpoint)*

- [x] 02-08-PLAN.md — Verify and install Import Linter, enforce zero-exemption dependency contracts, and update conventions.
- [x] 02-22-PLAN.md — Prove post-deletion runtime/error behavior and preserve CLI/deployment/secret contracts.

**Wave 19** *(blocked on Wave 18 completion)*

- [x] 02-16-PLAN.md — Run a focused architecture/transaction preflight and one non-duplicated complete release gate.

### Phase 3: Frontend Boundaries and Deterministic Build

**Goal**: Deliver all of [frontend restructuring epic #36](https://github.com/mvoronin/trellmark/issues/36), completing the frontend part of Issue #3: explicit server/UI state, reusable async and DOM primitives, an authenticated shell, independent feature modules, enforced dependencies, reproducible emitted artifacts, and a backend-free component gallery while retaining the framework-free SPA and existing product behavior.
**Depends on**: Phase 2
**Requirements**: ARCH-05, FRONT-01, FRONT-02, FRONT-03, FRONT-04, FRONT-05, FRONT-06, FRONT-07, FRONT-08, FRONT-09
**Success Criteria** (what must be TRUE):

  1. Authentication, logout/session expiry, CSRF, bookmarks, Safe mode, drag/reorder/folding, metadata, and import/export preserve existing behavior. Existing frontend regression assertions remain intact; gallery coverage is additive. API, schema, backup format, and generated API contracts do not change. Preserve DOM structure and appearance; narrowly scoped existing CSS defects may be fixed with regression coverage under D-18.
  2. Server state and UI state are distinct structures updated through named operations; rendering reads state without assigning it. One DOM-independent request-serial primitive handles all existing stale-response/in-flight cases, and a typed safe `h()` helper replaces existing element-construction chains without HTML-string sinks or changing DOM/classes/ARIA.
  3. `main.ts` composes `shared/`, `api/`, `shell/`, and `features/bookmarks/`; Bookmarks owns its model, view, and public surface with no shared element-handle growth or catch-all `app.ts`. Automated checks in `just check` and CI reject forbidden feature/shell/shared/generated-type edges and extensionless relative imports, naming each offending edge.
  4. A strict, unbundled TypeScript source-tree build reproduces the complete committed emitted tree, including gallery output. Local and CI checks detect missing, corrupted, and stale outputs without a maintained filename list, explicitly exclude hand-maintained assets, and are proven by deliberate negative fixtures.
  5. A numbered, dated ADR records the framework-free decision, measured context, alternatives, strongest counterargument, consequences, and observable revisit triggers. `web/design.html` uses real side-effect-free view functions and synthetic fixtures to show every component and rare state without login, API calls, or a database, with switchable themes and contrast/narrow-viewport coverage. Run it with `just design-mode` using a loopback static server; the page and dedicated artifacts are excluded from production. Shared HTML fragments are composed at build time.

**Epic scope and ordering** (all children are required; these are issue dependencies, not executable plans):

| Requirement | Issue | Deliverable | Depends on |
|-------------|-------|-------------|------------|
| FRONT-01 | [#37](https://github.com/mvoronin/trellmark/issues/37) | Framework decision ADR and reusable record format | None |
| FRONT-02 | [#38](https://github.com/mvoronin/trellmark/issues/38) | Source-tree compilation and whole emitted-tree drift checks | None; precedes module splitting |
| FRONT-03 | [#39](https://github.com/mvoronin/trellmark/issues/39) | Separate server/UI state and explicit operations | #38 |
| FRONT-04 | [#40](https://github.com/mvoronin/trellmark/issues/40) | One shared request-serial/in-flight primitive | #38 |
| FRONT-05 | [#41](https://github.com/mvoronin/trellmark/issues/41) | Typed, HTML-string-free element helper | #38 |
| FRONT-06 | [#42](https://github.com/mvoronin/trellmark/issues/42) | Authenticated shell and wiring-only main.ts | #38, #39 |
| FRONT-07 | [#43](https://github.com/mvoronin/trellmark/issues/43) | Independent Bookmarks model/view/public surface | #41, #42 |
| FRONT-08 | [#44](https://github.com/mvoronin/trellmark/issues/44) | Enforced frontend dependency graph | #42, #43; final integration enforcement |
| FRONT-09 | [#45](https://github.com/mvoronin/trellmark/issues/45) | Static gallery using real views and rare-state fixtures | #43 |

**Scope constraints**: No framework adoption, bundler, new runtime dependency, JavaScript test runner, reactivity/diffing system, rich client domain model, or visual redesign. Preserve strict TypeScript checks and browser ES-module `.js` imports. The element helper only replaces already-generated DOM; existing shell markup remains in HTML. Import/export stays in the shell until Phase 6. Notes editor state and Markdown rendering remain in their existing Notes phases. The epic's measurements are a dated baseline, not assumed current counts.

**Resolved discussion decisions**: `03-CONTEXT.md` is authoritative for D-01–D-22, including the separate design composition entry, owner-scoped HTML handles, build-time shared fragments, local-only design artifacts, and additive automated coverage. The user performs visual review after Phase 3 completion; reference screenshots and automatic visual comparisons are excluded. Verify framework-output claims in the ADR while retaining the framework-free decision and Svelte revisit preference.

**Scope provenance**: User expanded Phase 3 on 2026-09-05 to include the complete open epic #36 and children #37–#45. Each linked child's detailed acceptance criteria remain part of scope. No later phase is renumbered or moved; Phase 4 still depends on completion of this expanded Phase 3.

**Plans**: 14/14 plans executed
**Wave 1**

- [x] 03-01-PLAN.md — Whole-tree build and artifact drift prerequisite
- [x] 03-02-PLAN.md — Measured framework decision and reusable ADR format

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 03-03-PLAN.md — Safe reusable DOM construction in the live bookmark path

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 03-04-PLAN.md — Explicit Bookmarks server and interaction state

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 03-05-PLAN.md — Shared monotonic request lifetimes and stale completion protection

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 03-06-PLAN.md — Build-time shared HTML with explicit shell and editor ownership

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 03-07-PLAN.md — Authenticated shell session and theme factories

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 03-08-PLAN.md — Shell dialog lifecycle and retained import/export

**Wave 8** *(blocked on Wave 7 completion)*

- [x] 03-09-PLAN.md — Reusable Bookmarks views and feature-owned editors

**Wave 9** *(blocked on Wave 8 completion)*

- [x] 03-10-PLAN.md — Complete feature composition and delete the catch-all entry

**Wave 10** *(blocked on Wave 9 completion)*

- [x] 03-11-PLAN.md — Native TypeScript import graph with fail-closed negative proofs

**Wave 11** *(blocked on Wave 10 completion)*

- [x] 03-12-PLAN.md — Interactive design overview using authentic shared views

**Wave 12** *(blocked on Wave 11 completion)*

- [x] 03-13-PLAN.md — Exact local design command and production exclusion

**Wave 13** *(blocked on Wave 12 completion)*

- [x] 03-14-PLAN.md — Complete regression, boundary documentation and release proof

**Cross-cutting constraints:**

- D-01: All epic children remain assigned to Phase 3; source-tree build completes before module extraction.
- D-02: Framework-free strict TypeScript, relative .js modules, committed generated output and OpenAPI workflow remain; no new framework/bundler/runtime dependency or JS test runner.
- D-21: Behavior/DOM, contrast/overflow, complete generated trees and backend/API/PostgreSQL/build gates remain enforced.
- D-22: A numbered dated ADR uses current measured context, fair alternatives/counterargument, consequences and observable reconsideration triggers, retaining Svelte preference with verified technical claims.
- D-15: Bookmarks model/view/index own the feature, injected shell capabilities replace imports, safe h covers existing HTML construction and SVG stays namespaced.
- D-16: Existing frontend expected behavior and DOM assertions are preserved; only setup changes and additive cases.
- D-17: Structure, IDs, classes, ARIA and focus order remain stable despite source/file organization and HTML whitespace changes.
- D-03: Existing authentication, CSRF, Safe mode, bookmark/metadata and shell import/export semantics remain unchanged.
- D-04: Explicit server/UI state and named operations use one DOM-independent monotonic lifetime primitive; renders do not mutate state.
- D-10: Real side-effect-free component views, shared HTML and application styles are reused; only page-frame styles are separate.
- D-11: Shell/dialog/Bookmark static HTML has a single shared fragment source consumed by both pages.
- D-12: HTML fragments compose at build time into complete pages, with no runtime fragment fetch.
- D-13: Shell owns modal lifecycle/confirmations; Bookmarks owns fields/editor rules; handles are root-scoped.
- D-05: Ownership respects shared/API/shell/feature directions and named composition exceptions, with generated declarations behind the adapter.
- D-14: Only main.ts and design/main.ts are named composition entry exceptions; other design modules cannot compose owners.
- D-09: Fold/important/dialog interactions use synthetic in-memory data, reset on reload and make no API or remote requests.
- D-18: Only demonstrated existing CSS defects receive narrow fixes paired with regression coverage.
- D-19: Reference screenshots, visual snapshots and automatic screenshot comparison are excluded.
- D-20: Manual appearance inspection is a post-completion user handoff, never a completion gate or a claimed completed test.

**UI hint**: yes

### Phase 4: Notes Domain, Schema, and API

**Goal**: Authenticated API consumers can manage independent hierarchical Notes data with database-enforced invariants and optimistic conflict protection.
**Depends on**: Phase 3
**Requirements**: NGRP-01, NGRP-02, NGRP-03, NGRP-04, NOTE-01, NOTE-03, NOTE-04, CONF-01, SECU-01, SECU-02, DLVR-01, DLVR-02
**Success Criteria** (what must be TRUE):

  1. User has exactly one protected Notes `Inbox` and can create, rename, move, reorder, and safely delete eligible groups in an independent hierarchy limited to three levels.
  2. User can create, explicitly save, and move a note while its identity, raw Markdown, server-owned timestamps, group, and optimistic version remain authoritative.
  3. A stale update or delete receives a stable conflict response and never changes the newer stored note.
  4. Every Notes API route enforces the existing authentication, Origin, Fetch Metadata, session, and CSRF boundaries before a mutation can occur.
  5. The Alembic migration works both from the current schema and on an empty database, and the generated OpenAPI and TypeScript contracts expose the Notes API without handwritten wire types.

**Plans**: TBD

### Phase 5: Notes Browser Experience and Safe Markdown

**Goal**: Users can manage and safely preview Notes through a dedicated conflict-safe browser experience without exposing private or active content.
**Depends on**: Phase 4
**Requirements**: NOTE-02, NOTE-05, MARK-01, MARK-02, MARK-03, MARK-04, CONF-02, CONF-03, SECU-03, SECU-04, DLVR-03, DLVR-05
**Success Criteria** (what must be TRUE):

  1. User can navigate directly to `/notes/`, view note details and timestamps, manage the Notes hierarchy, and confirm a version-aware permanent note deletion.
  2. User can preview the approved Markdown subset as allowlisted DOM nodes, including safe links, without any HTML parsing sink.
  3. Raw HTML, scripts, styles, images, embeds, unsupported nodes, and unsafe URL schemes remain inert and trigger neither code execution nor external resource requests while raw source stays editable.
  4. After a stale save, the complete local title and Markdown draft remain visible until the user explicitly keeps or copies it, or chooses to load the current server version.
  5. Safe mode hides unsafe note groups and descendants across every Notes view, while Notes pages and responses retain no-store, request-size, authentication, and redacted-error protections.

**Plans**: TBD
**UI hint**: yes

### Phase 6: Cross-Domain Backup and Release Gate

**Goal**: Users can port bookmarks and Notes as one consistent product document, and the complete milestone passes its release safety gates.
**Depends on**: Phase 5
**Requirements**: PORT-01, PORT-02, PORT-03, PORT-04, PORT-05, DLVR-04, DLVR-06
**Success Criteria** (what must be TRUE):

  1. User can export one explicitly versioned document containing complete bookmark and Notes data from one internally consistent snapshot.
  2. User can restore a fully normalized bookmark-and-Notes document through one transaction, and a failure in either domain leaves both domains unchanged.
  3. User can still import the supported bookmark-only version 1 document with its existing duplicate-skip and hierarchy semantics.
  4. PostgreSQL/API and Chromium coverage proves migrations, lifecycle, conflicts, security, Safe mode, Markdown safety, atomic round trips, and bookmark regressions, while the full quality and container smoke gates pass using synthetic, non-sensitive data only.

**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in strict numeric order: 1 → 2 → 3 → 4 → 5 → 6. Issue #2 completes in Phase 1; Issue #3 completes in Phase 3; Issue #1 implementation starts only in Phase 4.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Atomic Bookmark Import | 4/4 | Complete    | 2026-08-30 |
| 2. Backend Modular Monolith | 22/22 | Complete    | 2026-09-05 |
| 3. Frontend Boundaries and Deterministic Build | 14/14 | Complete    | 2026-09-05 |
| 4. Notes Domain, Schema, and API | 0/TBD | Deferred | - |
| 5. Notes Browser Experience and Safe Markdown | 0/TBD | Deferred | - |
| 6. Cross-Domain Backup and Release Gate | 0/TBD | Deferred | - |
