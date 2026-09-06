# Trellmark

## What This Is

Trellmark is a private, single-user knowledge organizer that keeps saved URLs and first-class Markdown notes in ordered, nested group hierarchies. The existing product manages bookmarks; the next milestone adds an independent Notes area without weakening current link behavior, authentication, visibility, or safety boundaries.

## Core Value

A single user can safely organize durable personal knowledge as bookmarks and Markdown notes without losing data or exposing private content.

## Requirements

### Validated

- ✓ User can create, edit, move, order, and delete saved URLs in an ordered group hierarchy up to three levels deep — existing
- ✓ User can organize links with titles, importance flags, domain rules, Safe mode, private visibility, and site icons — existing
- ✓ User can authenticate through the single-administrator session model with CSRF and same-origin protections — existing
- ✓ Application persists content in PostgreSQL, enforces hierarchy invariants, and refuses to serve against an outdated schema — existing
- ✓ User can export and import the current bookmark/group JSON document format — existing
- ✓ User can import the supported bookmark document atomically; failures preserve prior logical data and duplicate skips remain successful — Phase 1
- ✓ FastAPI contracts generate the TypeScript client types consumed by the framework-free browser UI — existing
- ✓ Automated API, PostgreSQL migration, security, Chromium browser, generated-artifact, and container-contract checks protect current behavior — existing

- ✓ Backend feature cores are framework-free, with explicit transaction ports, focused adapters/routers, bounded workers, and executable dependency contracts in one deployable service — Phase 2

- ✓ Framework-free frontend has explicit server/UI state, shared request/DOM primitives, authenticated shell, independent Bookmarks, native import enforcement, deterministic whole-tree builds, a measured ADR and local-only design mode — Phase 3 (epic #36, children #37–#45)

### Active

- [ ] Future work (minimal priority): Add Notes as an independent domain with a dedicated `/notes/` page and a separate ordered group hierarchy up to three levels deep, including a protected default `Inbox` (GitHub Issue #1)
- [ ] Let the user create, view, edit, move, and delete notes and note groups; each note has a title, raw Markdown body, created/updated timestamps, and optimistic version protection
- [ ] Ensure stale note edits never overwrite newer content or discard the user's local draft
- [ ] Render Markdown through safe DOM construction without `innerHTML`, executable/raw HTML, scripts, images, or unsafe URL schemes
- [ ] Preserve bookmark behavior, single-user authentication, Safe mode, and group visibility boundaries across the Notes milestone
- [ ] Extend the PostgreSQL schema and Alembic migrations, FastAPI/OpenAPI contracts, generated TypeScript types, browser UI, and atomic import/export format for notes
- [ ] Cover atomic rollback, modular dependency rules, Notes API behavior, Markdown safety, user-visible browser flows, generated artifacts, and container smoke behavior with automated tests

### Out of Scope

- Multi-user accounts, ownership, and permissions — Trellmark remains a single-user application for this milestone
- Collaboration or shared note editing — requires multi-user identity and conflict semantics outside this milestone
- Browser synchronization — tracked separately and not required for first-class notes
- Caddy configuration, TLS, routing, or deployment changes — owned by a separate infrastructure repository
- Tags and descriptions — separate product work not required for Notes MVP
- New theme work — unrelated to delivering Notes MVP
- Compatibility with legacy applications or untracked historical export formats — the repository defines only Trellmark's supported format

## Context

- The Notes MVP is the repository's next milestone and follows the explicit dependency order: Issue #2, then Issue #3, then Issue #1.
- The current backend is a FastAPI modular monolith using strict Pydantic contracts, SQLAlchemy Core, PostgreSQL 18, Alembic, and `ltree`-backed group hierarchies.
- The current browser application is framework-free TypeScript with OpenAPI-generated declarations and committed JavaScript build artifacts.
- Phase 3 includes the complete frontend epic #36 and children #37–#45. It retains existing product behavior, shell HTML, and the unbundled TypeScript toolchain; the interactive design overview is served locally by `just design-mode` and excluded from every production runtime layer.
- The backend now separates Bookmarks, Identity, Backup, and platform responsibilities. Domain/application cores use immutable dataclasses and narrow ports; adapters own SQLAlchemy, Pydantic, HTTP, and external metadata. Fourteen zero-ignore Import Linter contracts enforce dependencies. Frontend boundaries are now complete: `main.ts` composes shared/API/shell/Bookmarks owners, and a pinned native TypeScript graph checker enforces their imports. Notes domain/schema/API work is next.
- Bookmark import now owns one PostgreSQL transaction, rejects overlapping logical bookmark writers immediately, rolls back every mutation stage, and exposes stable redacted errors with explicit manual retry.
- Notes mirror the existing ordered, maximum-three-level hierarchy semantics but use an independent note-group forest; notes never reuse bookmark groups or masquerade as URLs.
- The protected note `Inbox` is the default destination. Direct navigation to `/notes/` must work within the existing application and authentication boundary.
- The milestone is done when the user can manage and safely preview hierarchical Markdown notes, stale edits are conflict-safe, and one import transaction restores notes and bookmarks together without regressing link management.

## Constraints

- **Language**: All planning artifacts, tasks, documentation, and commit messages must be written in English — repository work must remain consistently reviewable
- **Repository safety**: Never commit credentials, real user data, private hostnames, database dumps, exports, or home-server access details — the repository is public
- **Compatibility**: Preserve current bookmark API, UI, schema behavior, Safe mode, visibility rules, and import success semantics while prerequisites are completed — the milestone extends a working brownfield system
- **Architecture**: Keep one self-contained deployable service while separating domain logic from FastAPI, Pydantic, and SQLAlchemy through narrow ports and adapters — Notes must be first-class rather than coupled to Bookmarks
- **Persistence**: PostgreSQL 18 and Alembic remain authoritative; hierarchy and transaction guarantees must be enforced and tested at the database boundary — no SQLite or mock substitute may define production semantics
- **Frontend**: Continue using the existing framework-free TypeScript SPA and generated OpenAPI contract workflow — generated declarations and browser artifacts must stay synchronized
- **Security**: Markdown rendering must not execute scripts, accept raw HTML, use unsafe URL schemes, or weaken authentication/CSRF/private-content controls — note content is untrusted input
- **Deployment boundary**: Trellmark owns no Caddy configuration or public edge infrastructure — those changes belong in the separate infrastructure repository

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Complete Issue #2 before Issue #3, and Issue #3 before Issue #1 | Atomic backup/restore and domain boundaries are prerequisites for adding a second content domain safely | ✓ Issue #2 completed in Phase 1; Issue #3 backend/frontend prerequisites completed in Phases 2–3 |
| Serialize logical bookmark writes with a transaction-scoped advisory try-lock | The API requires immediate first-wins conflict handling before mutations; serializable isolation can abort later and does not replace this gate | ✓ Phase 1 |
| Publish flat import failures with stable codes | Browser recovery needs safe, machine-readable `invalid_import`, `import_conflict`, and `import_failed` outcomes | ✓ Phase 1 |
| Retain retryable import files until explicit user action | Recovery must preserve the current view without background resubmission or discarded input | ✓ Phase 1 |
| Deliver the complete frontend epic #36 before Notes | Explicit state, reusable primitives, feature isolation, deterministic builds, and a real-view gallery prepare a repeatable feature pattern | ✓ Phase 3; all 14 plans and 10 requirements verified |
| Give Notes an independent domain and note-group forest | Notes are first-class content and must not be represented as URLs or coupled to bookmark groups | — Pending |
| Mirror existing ordered three-level hierarchy semantics for note groups | Reuses established user interaction and database invariants without sharing domain ownership | — Pending |
| Use a protected `Inbox` as the default note group | Every note must belong to exactly one group and always has a safe initial destination | — Pending |
| Protect note updates with optimistic version checks | Stale browser state must never silently overwrite newer note content | — Pending |
| Build Markdown output with safe DOM APIs and exclude raw HTML and images | Avoids script execution, unsafe attributes, and active/external content while retaining useful Markdown | — Pending |
| Preserve one deployable modular monolith | Feature-owned cores, ports/adapters, focused routers, and executable dependency rules keep one service maintainable | ✓ Phase 2 |
| Read group visibility and memberships from one snapshot | Concurrent imports must not combine old visibility with new private content | ✓ Phase 2 |
| Retain workload admission through worker cleanup | Native and scoped cancellation must not abandon database work or return capacity early | ✓ Phase 2 |
| Preserve primary database failures through cleanup | A second cleanup failure must not change error classification or hide the original cause | ✓ Phase 2 |
| Keep framework-free TypeScript and unbundled ES modules | A measured ADR records alternatives, Svelte reconsideration preference and observable triggers without adding runtime tooling | ✓ Phase 3 |
| Compose shared HTML at build time and check every emitted path and byte | Reusable real views and deterministic artifacts prepare later features without runtime template fetching or filename allowlists | ✓ Phase 3 |
| Keep design mode local and synthetic | Real components, rare states and dialogs can be inspected without backend/data access; dedicated assets never enter runtime layers | ✓ Phase 3 |
| Retain private request ownership until logout succeeds | Failed logout must not discard in-progress results or strand controls; actual cleanup/expiry still invalidates before clearing private content | ✓ Phase 3 review repair |

## Evolution

### Current State

Milestone `v0.0.1` is archived after Phase 3. The implemented bookmark safety, backend modular monolith, and frontend boundary work is on `main`. Notes MVP work is deferred.

### Next Milestone Goals

To be defined from the next user-prioritized task. Notes MVP remains backlog work with minimal priority.

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `$gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `$gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-09-05 after Phase 3*
