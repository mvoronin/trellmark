import type * as client from "../../api/client.js";
import { errorMessage } from "../../shared/format.js";
import type { RequestLifetime } from "../../shared/request.js";
import { treeGroups, type BookmarksModel, type BookmarkGroup } from "./model.js";

export interface BookmarkDragOperations {
  model: BookmarksModel;
  privateLifetime: RequestLifetime;
  reorder: typeof client.reorderGroups;
  replace(groups: readonly BookmarkGroup[]): void;
  load(): Promise<void>;
  status(message: string, state?: string): void;
}

export function createBookmarkDrag(groupsContainer: HTMLElement, operations: BookmarkDragOperations) {
  const { model: bookmarks, privateLifetime, reorder: apiReorderGroups,
    replace: replaceGroups, load: loadGroups, status: setStatus } = operations;
  const listeners = new AbortController();
  let disposed = false;

  // Reorder groups by dragging a title line. This uses Pointer Events rather than
  // the HTML5 drag-and-drop API so a single path works for mouse, touch (Android),
  // and pen. The header carries touch-action: none so a touch-drag reorders
  // instead of scrolling the page.
  const DRAG_THRESHOLD = 6; // px of movement before a press becomes a drag

  function makeHeaderDraggable(
    header: HTMLElement,
    section: HTMLElement,
    groupId: number,
    parentId: number | null,
  ): void {
    header.addEventListener("pointerdown", (event) => {
      // Only the primary button/finger, and never when the press starts on the
      // fold toggle (that stays a plain click).
      if (disposed || bookmarks.ui.activeDrag || event.button !== 0 || !event.isPrimary) {
        return;
      }
      if (event.target instanceof Element && event.target.closest("button")) {
        return;
      }

      bookmarks.startDrag({
        groupId,
        parentId,
        header,
        section,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
      });
      header.setPointerCapture(event.pointerId);
      header.addEventListener("pointermove", onDragMove);
      header.addEventListener("pointerup", onDragEnd);
      header.addEventListener("pointercancel", onDragCancel);
    }, { signal: listeners.signal });
  }

  function onDragMove(event: PointerEvent): void {
    if (!bookmarks.ui.activeDrag || event.pointerId !== bookmarks.ui.activeDrag.pointerId) {
      return;
    }

    if (!bookmarks.ui.activeDrag.moving) {
      const distance = Math.hypot(
        event.clientX - bookmarks.ui.activeDrag.startX,
        event.clientY - bookmarks.ui.activeDrag.startY,
      );
      if (distance < DRAG_THRESHOLD) {
        return;
      }
      bookmarks.moveDrag();
      bookmarks.ui.activeDrag.section.classList.add("dragging");
    }

    event.preventDefault();
    updateDropTarget(event.clientX, event.clientY);
  }

  function updateDropTarget(x: number, y: number): void {
    if (!bookmarks.ui.activeDrag) {
      return;
    }
    const drag = bookmarks.ui.activeDrag;
    clearDropIndicators();

    const element = groupsContainer.ownerDocument.elementFromPoint(x, y);
    const targetHeader = element?.closest<HTMLElement>(".group-header") ?? null;
    if (!targetHeader || !groupsContainer.contains(targetHeader) || targetHeader === drag.header) {
      bookmarks.setDropTarget(null);
      return;
    }

    const targetParentId = targetHeader.dataset.parentId === ""
      ? null
      : Number(targetHeader.dataset.parentId);
    if (targetParentId !== drag.parentId) {
      bookmarks.setDropTarget(null);
      return;
    }

    const rect = targetHeader.getBoundingClientRect();
    const before = y < rect.top + rect.height / 2;
    bookmarks.setDropTarget({ groupId: Number(targetHeader.dataset.groupId), before });
    targetHeader.classList.add("drag-over");
  }

  function onDragEnd(event: PointerEvent): void {
    if (!bookmarks.ui.activeDrag || event.pointerId !== bookmarks.ui.activeDrag.pointerId) {
      return;
    }

    const { moving, groupId, parentId, dropTarget } = bookmarks.ui.activeDrag;
    finishDrag();

    if (!moving || !dropTarget) {
      return;
    }
    const ordered = reorderedGroupIds(
      parentId,
      groupId,
      dropTarget.groupId,
      dropTarget.before,
    );
    if (ordered) {
      persistGroupOrder(parentId, ordered);
    }
  }

  function onDragCancel(event: PointerEvent): void {
    if (!bookmarks.ui.activeDrag || event.pointerId !== bookmarks.ui.activeDrag.pointerId) {
      return;
    }
    finishDrag();
  }

  function finishDrag(): void {
    if (!bookmarks.ui.activeDrag) {
      return;
    }
    const { header, section, pointerId } = bookmarks.ui.activeDrag;
    header.removeEventListener("pointermove", onDragMove);
    header.removeEventListener("pointerup", onDragEnd);
    header.removeEventListener("pointercancel", onDragCancel);
    if (header.hasPointerCapture(pointerId)) {
      header.releasePointerCapture(pointerId);
    }
    section.classList.remove("dragging");
    clearDropIndicators();
    bookmarks.finishDrag();
  }

  function clearDropIndicators(): void {
    for (const marked of groupsContainer.querySelectorAll<HTMLElement>(
      ".group-header.drag-over",
    )) {
      marked.classList.remove("drag-over");
    }
  }

  // Build the full ordered id list after moving sourceId next to targetId.
  // Returns null when the drop leaves the order unchanged.
  function reorderedGroupIds(
    parentId: number | null,
    sourceId: number,
    targetId: number,
    before: boolean,
  ): number[] | null {
    const current = treeGroups(bookmarks.server.groups)
      .map(({ group }) => group)
      .filter((group) => group.parent_id === parentId)
      .map((group) => group.id);
    const ids = current.filter((id) => id !== sourceId);
    const targetIndex = ids.indexOf(targetId);
    if (targetIndex === -1) {
      return null;
    }

    ids.splice(before ? targetIndex : targetIndex + 1, 0, sourceId);
    if (ids.every((id, index) => id === current[index])) {
      return null;
    }
    return ids;
  }

  async function persistGroupOrder(
    parentId: number | null,
    orderedIds: number[],
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    setStatus("");

    try {
      const data = await apiReorderGroups({
        parent_id: parentId,
        group_ids: orderedIds,
      });
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
      setStatus("Reordered.", "is-success");
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      setStatus(errorMessage(error), "is-error");
      // Drop the optimistic move and show the server's current order.
      loadGroups().catch((loadError: unknown) => {
        if (!disposed && privateLifetime.isCurrent(ticket)) setStatus(errorMessage(loadError), "is-error");
      });
    }
  }

  return {
    makeHeaderDraggable, cancel: finishDrag,
    dispose(): void {
      disposed = true;
      finishDrag();
      listeners.abort();
    },
  };
}
