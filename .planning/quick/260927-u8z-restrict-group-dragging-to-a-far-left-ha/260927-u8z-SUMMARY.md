---
status: complete
description: Restrict group dragging to a far-left handle
commit: b14fa9e
completed: 2026-09-27
requirements-completed: [GDRG-01, GDRG-02, GDRG-03, GDRG-04, GDRG-05, GDRG-06]
---

# Group drag handle

Every draggable group now has a dotted grip at the far left of its header, before the collapse arrow. Only the grip initiates dragging. The remaining header permits normal touch scrolling and mouse title selection.

The grip reuses the bookmark handle glyph and a non-shrinking 2rem minimum touch target. Grab/grabbing cursors apply to the grip. Long group titles wrap so handles and collapse controls remain usable in narrow, nested headers.

The existing header remains the pointer-capture owner and drop target. The 6px threshold, sibling-only reordering, default-group behavior, folded descendants, persistence, error recovery, and private request lifetimes remain intact. Unexpected capture loss now cancels cleanly alongside Escape, pointer cancellation, rendering, and disposal.

The live application and interactive local gallery use the same updated drag action. Read-only gallery examples do not show an interactive grip. Usage guidance and generated browser modules are current.

## Verification

- Focused Chromium browser regressions: **82 passed** in 68.88 seconds.
- Full `just check`: **1,441 tests passed** in 382.21 seconds, plus successful generated OpenAPI/browser artifact checks, all 14 backend import contracts, frontend import contracts, Ruff lint/format checks, and strict Python typing.
- `npm run typecheck` passed separately; frontend generation and artifact checking also compile the TypeScript tree.
- `git diff --cached --check` passed before the implementation commit.
- Native mouse/touch tests cover excluded header regions, movement across the grip after starting elsewhere, small movements, cancellation/retry, text selection, actual page scrolling, and persisted reordering. Mouse and pen factory tests retain lifecycle and foreign-target checks.
- Layout checks exercise visible handles and long titles at all three hierarchy levels in light/dark themes at 360px and 1440px. Existing contrast and overflow tests passed.

## Execution notes

- Added focused input-boundary tests in `tests/frontend/test_group_drag_handle.py` instead of further expanding the existing reorder test module. Existing contrast coverage in `test_design_tokens.py` was reused unchanged.
- Corrected test expectations for CSS-uppercase selected text, scoped theme controls to the authenticated view, and measured touch panning before drop completion because the success message changes layout.
- Locked Python dependency updates from `main` required downloading. The bundled Chromium 153 download stalled and was stopped. The successful focused and full runs used **Playwright 1.63.0 with installed Google Chrome 152.0.7977.64**, selected through a temporary browser path outside the repository, and disposable **PostgreSQL 18**. No test harness or dependency changes were committed for this fallback.
- Execution and verification followed the GSD quick workflow inline. The feature is on `feature/group-drag-handle`; implementation commit: `b14fa9e`.

All planned acceptance criteria are covered. No implementation tasks remain.
