---
type: quick
status: complete
description: Restrict group dragging to a far-left handle
date: 2026-09-27
---

# Group drag handle

## User intent and locked decisions

The user wants to avoid accidentally moving a group when interacting with its title bar. Dragging must start from a dedicated place in the header. The user chose the far-left position, before the collapse arrow, and requested an implementation plan.

```text
[⠿] [▾] Group title      count      [edit] [delete]
```

- The dotted handle is always visible on each draggable group, including the default group and visible nested groups.
- Only the handle starts dragging; the rest of the header does not.
- Match the existing bookmark handles visually and provide a padded touch target.
- Show grab/grabbing feedback on the handle.
- Allow ordinary touch scrolling and mouse title selection outside the handle.

## Acceptance criteria

| ID | Observable behavior |
| --- | --- |
| GDRG-01 | The handle is the leftmost header child, before the existing fold button, in both light and dark themes. |
| GDRG-02 | A primary mouse, touch, or pen gesture starting on the handle can reorder a group after the existing 6 CSS-pixel threshold; a click or smaller movement makes no reorder request. |
| GDRG-03 | Gestures starting on the title, count, domain text, badge, or blank header space never start group dragging. Existing fold/edit/delete controls remain usable. |
| GDRG-04 | Touch panning outside the handle scrolls a scrollable page; the group title can be selected with a mouse. The handle has a non-shrinking target of at least 2rem by 2rem, matching the existing bookmark handle minimum. |
| GDRG-05 | Existing sibling-only ordering, default-group reordering, folded descendants, drop feedback, persisted order, error recovery, and private-state lifetime protections continue to work. Cancellation releases capture and clears temporary state. |
| GDRG-06 | Existing bookmark dragging still works; local design mode demonstrates the new interaction; committed browser artifacts and the repository quality gate are current. |

## Implementation decisions

- Reuse the existing framework-free Pointer Events implementation and dotted `⠿` text glyph. No Metro UI package or new icon dependency is needed.
- Change the group drag registration API to accept a handle explicitly. Keep pointer capture on the existing header so `ActiveDrag` and the existing capture-preservation behavior need no model redesign.
- Keep the complete header as a drop target. A smaller drag-start area does not narrow drop destinations or change reorder semantics.
- Follow the current bookmark handle convention: a non-tab-stop span with a descriptive tooltip and a decorative glyph. Do not introduce a button that has no keyboard activation behavior. Keyboard group reordering is separate scope; preserve the existing controls and their tab order.
- Show an interactive handle when a drag action is supplied. Read-only design examples without an action must not advertise an interactive grip; the interactive design example must exercise the real handle.

## Scope and delivery

This is one quick task, independent of the archived v0.0.1 milestone. Notes remains deferred. The original request authorized planning; the subsequent instruction, "start implementation", authorized execution. Implementation and verification are complete; see SUMMARY.md and VERIFICATION.md in this directory.

The change affects group rendering, drag registration, CSS, local design mode, browser tests, generated browser modules, and usage documentation. It does not change the backend, API, database, group reparenting rules, dependencies, or deployment.

## Code evidence

- `web/src/features/bookmarks/view.ts`: currently registers `makeHeaderDraggable` on the full header; bookmark rows already render `.url-drag-handle` with `⠿`.
- `web/src/features/bookmarks/drag.ts`: header-wide pointerdown, 6px activation threshold, header-owned capture, sibling-only targets, cancellation, and private request lifetime checks.
- `web/static/styles.css`: `.group-header` currently applies `cursor: grab`, `touch-action: none`, and `user-select: none` to the entire band.
- `tests/frontend/test_group_reorder.py`: covers mouse/touch ordering, captured-header continuity, foreign targets, disposal, persistence, and API errors. Generic drag helpers are also used by `test_url_drag.py`.
- `web/src/design/main.ts`: wires the same group drag action into the interactive local gallery.

Planning and review use the GSD quick workflow inline. Existing user decisions supply the discussion context; no additional product decision is required.
