---
status: passed
verified: 2026-09-27T20:16:21Z
score: 6/6
commit: b14fa9e
---

# Group drag handle verification

Verification performed inline against the implementation and observed test results.

| Criterion | Result | Evidence |
| --- | --- | --- |
| GDRG-01: visible far-left grip | Passed | `view.ts` inserts the grip before `fold-toggle`; the DOM contract, gallery, and light/dark nested layout tests pass. |
| GDRG-02: handle-only primary drag | Passed | `drag.ts` binds pointerdown to the grip, retains the primary-pointer guard and 6px threshold; mouse/touch browser tests and mouse/pen factory tests pass. |
| GDRG-03: other header regions do not drag | Passed | Browser gestures from title, count, domains, NSFW badge, padding, and a path crossing the grip create no reorder request. Existing fold/edit/delete tests pass in the full suite. |
| GDRG-04: ordinary scrolling and selection | Passed | Native CDP touch panning changes scroll position outside the grip; a handle drag does not pan before release. Native mouse selection returns the rendered title. Target dimensions and narrow layouts pass. |
| GDRG-05: preserve ordering and lifecycle | Passed | Existing persistence, sibling-only, folded-tree, default-group, error, and private lifetime tests pass. Escape, pointercancel, lost capture, and filter cancellation clear drag state and permit retry. |
| GDRG-06: bookmark/gallery/build regressions | Passed | Existing URL-drag tests and the interactive gallery reorder pass. `just check` passes all 1,441 tests and every static/artifact gate. |

## Critical connections

- `view.ts` passes the grip and stable header to `makeGroupDraggable`.
- `index.ts` and `design/main.ts` wire the same action from the real drag factory.
- `.group-drag-handle` owns gesture suppression; the full header retains its drop-target role and permits default interaction.
- Generated browser modules match TypeScript sources.

## Environment and limits

Browser verification used Google Chrome 152.0.7977.64 through Playwright 1.63.0 because the bundled Chromium 153 download stalled. Database-backed tests used disposable PostgreSQL 18. Pen coverage uses synthetic Pointer Events in the browser factory test, not physical pen hardware. These are the observed verification conditions; no bundled-Chromium or physical-pen run is claimed.
