# Requirements: Trellmark Notes MVP

**Defined:** 2026-08-29
**Core Value:** A single user can safely organize durable personal knowledge as bookmarks and Markdown notes without losing data or exposing private content.

## v1 Requirements

### Atomic Import Prerequisite

- [x] **IMPT-01**: User can import one valid Trellmark document through a single PostgreSQL connection and outer transaction covering every group, URL, membership, metadata, hierarchy, and ordering mutation.
- [x] **IMPT-02**: When an import fails after any mutation stage, all pre-import logical bookmark data remains unchanged.
- [x] **IMPT-03**: An invalid import document is rejected before any database mutation occurs.
- [x] **IMPT-04**: Expected duplicate URLs remain successful skipped results, and successful imports preserve current bookmark behavior and response semantics.
- [x] **IMPT-05**: Import failures return stable, safe errors without database details, credentials, private data, or partially committed state.

### Modular Architecture Prerequisite

- [x] **ARCH-01**: Bookmark domain and application code do not depend on FastAPI, Pydantic, SQLAlchemy, or browser transport types.
- [x] **ARCH-02**: Every mutating use case owns an explicit transaction boundary through narrow unit-of-work and repository ports.
- [x] **ARCH-03**: SQLAlchemy adapters preserve existing PostgreSQL constraints, hierarchy behavior, locking, concurrency semantics, and typed application errors without leaking storage sentinels.
- [x] **ARCH-04**: Focused API routers and a small composition root preserve current bookmark paths, payloads, generated contracts, security middleware, and behavior in one deployable service.
- [ ] **ARCH-05**: The browser is separated into a shared authenticated shell and an independent Bookmarks feature, and automated dependency rules reject forbidden backend and frontend imports.

### Note Groups

- [ ] **NGRP-01**: User has an independent ordered note-group hierarchy, separate from bookmark groups, with a maximum depth of three and exactly one protected default `Inbox`.
- [ ] **NGRP-02**: User can create, rename, and move a non-`Inbox` note group while database-enforced hierarchy invariants prevent cycles and excessive depth.
- [ ] **NGRP-03**: User can reorder note groups within one sibling set and the order remains stable after reload.
- [ ] **NGRP-04**: User can delete an empty non-`Inbox` note group, while deletion of `Inbox` or a group containing notes or child groups is rejected without data loss.

### Notes

- [ ] **NOTE-01**: User can create a note with a title and raw Markdown body in a selected note group, defaulting to `Inbox`, and receives server-owned created/updated timestamps and an optimistic version.
- [ ] **NOTE-02**: User can view a note's title, rendered preview, group, created timestamp, and updated timestamp.
- [ ] **NOTE-03**: User can explicitly save changes to a note's title and raw Markdown body using the version they originally loaded; a successful save advances the updated timestamp and version without changing the created timestamp.
- [ ] **NOTE-04**: User can move a note to another existing note group without changing its identity or content.
- [ ] **NOTE-05**: User can permanently delete a note only after confirmation and with version-aware conflict protection.

### Markdown Preview

- [ ] **MARK-01**: User can preview paragraphs, headings, emphasis, strong emphasis, ordered and unordered lists, block quotes, thematic breaks, code spans, code blocks, and safe links from the stored raw Markdown.
- [ ] **MARK-02**: Preview rendering constructs only hardcoded allowlisted DOM nodes and text; Notes code never sends Markdown-derived content through `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, or an equivalent HTML parsing sink.
- [ ] **MARK-03**: Raw HTML, scripts, styles, images, embeds, and unsupported Markdown nodes remain inert, execute no code, and initiate no external resource request while the raw source remains preserved for editing.
- [ ] **MARK-04**: Only validated `http:` and `https:` destinations become clickable preview links; unsafe or unsupported URL schemes render without navigation behavior.

### Conflict Safety

- [ ] **CONF-01**: A stale note update or delete is rejected with a stable conflict response and never changes the newer stored note.
- [ ] **CONF-02**: After a stale save conflict, the browser keeps the user's complete local title and Markdown draft intact and visible.
- [ ] **CONF-03**: User must explicitly choose whether to keep/copy the local draft or load the current server version; the UI never auto-merges or silently replaces either version.

### Security and Visibility

- [ ] **SECU-01**: Every Notes API route is protected by the existing single-user authentication boundary and default-deny private API policy.
- [ ] **SECU-02**: Every unsafe Notes request enforces the existing Origin, Fetch Metadata, session, and CSRF controls before mutation.
- [ ] **SECU-03**: Notes pages and API responses retain the existing no-store and request-body-limit protections and return redacted errors.
- [ ] **SECU-04**: Note groups support the existing Safe mode visibility semantics so unsafe groups and their descendants are hidden in Safe mode without exposing content through another Notes view.

### Portable Import and Export

- [ ] **PORT-01**: User can export one explicitly versioned Trellmark document containing the complete independent bookmark and note-group trees, note content, timestamps, ordering, and required metadata.
- [ ] **PORT-02**: The complete bookmark and Notes document is normalized and validated before any restore mutation begins.
- [ ] **PORT-03**: User can restore bookmarks and notes together through one outer transaction, and a failure in either domain rolls back all mutations in both domains.
- [ ] **PORT-04**: A successful export taken during concurrent activity represents one internally consistent bookmark-and-notes snapshot.
- [ ] **PORT-05**: The currently supported bookmark-only version 1 document remains importable with its existing duplicate-skip and hierarchy semantics after the new cross-domain format is introduced.

### Delivery and Verification

- [ ] **DLVR-01**: An Alembic revision upgrades the current PostgreSQL schema with independent note-group and note tables, constraints, indexes, hierarchy maintenance, and protected `Inbox` initialization, and the migration also produces the correct schema from an empty database.
- [ ] **DLVR-02**: Notes request/response contracts are represented in FastAPI OpenAPI output and regenerated TypeScript declarations without handwritten duplicate wire types.
- [ ] **DLVR-03**: The framework-free TypeScript UI provides the dedicated `/notes/` experience and produces deterministic committed browser artifacts through the repository build workflow.
- [ ] **DLVR-04**: Automated PostgreSQL/API tests cover migration invariants, note and group lifecycle, atomic rollback injection points, version conflicts, security errors, import/export round trips, and bookmark regressions.
- [ ] **DLVR-05**: Automated Chromium browser tests cover direct `/notes/` navigation, authentication, hierarchy and note lifecycle, Safe mode visibility, safe Markdown adversarial cases, retained conflict drafts, import/export, and absence of unsafe resource requests.
- [ ] **DLVR-06**: The full repository quality gate and container smoke tests pass using synthetic data only, and no planning artifact, task, documentation, generated artifact, fixture, or commit contains credentials, real user data, private hostnames, database dumps, exports, or home-server access details.

## v2 Requirements

Deferred to future releases and excluded from the Notes MVP roadmap.

### Retrieval and Recovery

- **SRCH-01**: User can search note titles and Markdown bodies.
- **TRSH-01**: User can restore a deleted note from a trash area before permanent purge.
- **DRFT-01**: User can recover an unsaved local draft after a browser crash or navigation.
- **AUTO-01**: User can opt into autosave after request ordering, optimistic conflicts, and draft recovery are proven.

### Markdown Expansion

- **MDEX-01**: User can enable separately approved additional Markdown constructs with explicit parser, DOM, URL, and browser security coverage.

### Knowledge Links

- **LINK-01**: User can create stable internal note links with defined rename, move, missing-target, backlink, and routing behavior.

## Out of Scope

Explicitly excluded to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Shared bookmark and note groups | Notes are an independent first-class domain and must not reuse bookmark identities or tables |
| Multi-user accounts or ownership | Trellmark remains single-user for this milestone |
| Collaboration or shared editing | Requires multi-user identity, authorization, and merge semantics |
| Browser synchronization | Separate distributed-state work outside Notes MVP |
| Tags, descriptions, saved filters, and bulk taxonomy work | Separate product scope not required for safe note CRUD |
| Arbitrary raw HTML, scripts, images, attachments, embeds, or plug-ins | Expands execution, privacy, storage, and backup risk beyond the approved safe Markdown subset |
| WYSIWYG or rich-text editing | Creates a second editing model and lossy Markdown round trips |
| Unlimited nesting | Contradicts the established three-level hierarchy invariant |
| Recursive note-group deletion | Can destroy an unseen subtree; MVP deletion is empty-only |
| Automatic conflict merging | Optimistic rejection and explicit recovery are sufficient for the single-user MVP |
| Caddy, TLS, public routing, or deployment-edge changes | Owned by a separate infrastructure repository |
| New theme work | Unrelated to delivering Notes MVP |
| Compatibility with unspecified legacy or external export formats | Only repository-defined Trellmark document versions have an authoritative contract |

## Definition of Done

- Issues #2 and #3 are complete and verified before Issue #1 implementation begins.
- Every v1 requirement maps to exactly one roadmap phase and has automated or explicit manual verification evidence.
- Bookmarks, authentication, Safe mode, visibility, generated contracts, migrations, and portable data behavior pass regression checks.
- Notes Markdown cannot execute active content or trigger an unsafe external request in the supported browser flow.
- A stale edit cannot overwrite newer content or erase the local draft.
- A failed cross-domain restore leaves both bookmark and Notes logical data unchanged.
- The full repository quality gate and container smoke checks pass on synthetic fixtures.
- Public Git history and tracked files contain none of the prohibited sensitive data classes.

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| IMPT-01 | Phase 1 | Complete |
| IMPT-02 | Phase 1 | Complete |
| IMPT-03 | Phase 1 | Complete |
| IMPT-04 | Phase 1 | Complete |
| IMPT-05 | Phase 1 | Complete |
| ARCH-01 | Phase 2 | Complete |
| ARCH-02 | Phase 2 | Complete |
| ARCH-03 | Phase 2 | Complete |
| ARCH-04 | Phase 2 | Complete |
| ARCH-05 | Phase 3 | Pending |
| NGRP-01 | Phase 4 | Pending |
| NGRP-02 | Phase 4 | Pending |
| NGRP-03 | Phase 4 | Pending |
| NGRP-04 | Phase 4 | Pending |
| NOTE-01 | Phase 4 | Pending |
| NOTE-02 | Phase 5 | Pending |
| NOTE-03 | Phase 4 | Pending |
| NOTE-04 | Phase 4 | Pending |
| NOTE-05 | Phase 5 | Pending |
| MARK-01 | Phase 5 | Pending |
| MARK-02 | Phase 5 | Pending |
| MARK-03 | Phase 5 | Pending |
| MARK-04 | Phase 5 | Pending |
| CONF-01 | Phase 4 | Pending |
| CONF-02 | Phase 5 | Pending |
| CONF-03 | Phase 5 | Pending |
| SECU-01 | Phase 4 | Pending |
| SECU-02 | Phase 4 | Pending |
| SECU-03 | Phase 5 | Pending |
| SECU-04 | Phase 5 | Pending |
| PORT-01 | Phase 6 | Pending |
| PORT-02 | Phase 6 | Pending |
| PORT-03 | Phase 6 | Pending |
| PORT-04 | Phase 6 | Pending |
| PORT-05 | Phase 6 | Pending |
| DLVR-01 | Phase 4 | Pending |
| DLVR-02 | Phase 4 | Pending |
| DLVR-03 | Phase 5 | Pending |
| DLVR-04 | Phase 6 | Pending |
| DLVR-05 | Phase 5 | Pending |
| DLVR-06 | Phase 6 | Pending |

**Coverage:**

- v1 requirements: 41 total
- Mapped to phases: 41
- Unmapped: 0 ✓

---
*Requirements defined: 2026-08-29*
*Last updated: 2026-08-29 after roadmap creation*
