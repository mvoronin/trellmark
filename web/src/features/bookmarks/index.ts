import type * as client from "../../api/client.js";
import { errorMessage } from "../../shared/format.js";
import type { RequestLifetime } from "../../shared/request.js";
import { createBookmarkDrag } from "./drag.js";
import { createBookmarkEditors, type BookmarkEditorOptions } from "./editors.js";
import { createBookmarksModel, type BookmarkGroup, type BookmarkStorage, type SortMode } from "./model.js";
import { renderGroups as renderBookmarkGroups } from "./view.js";

export { createBookmarkEditors } from "./editors.js";
export { createBookmarkDrag } from "./drag.js";
export { createBookmarksModel } from "./model.js";
export { renderGroups, renderGroup, renderUrlItem, renderSiteIcon } from "./view.js";
export type { BookmarkGroup, BookmarkUrl, BookmarksUIState } from "./model.js";
export type { BookmarkViewOptions, BookmarkViewActions } from "./view.js";

export interface BookmarksOptions {
  api: BookmarkEditorOptions["api"] & Pick<typeof client, "listGroups" | "reorderGroups" | "siteIconPath">;
  dialogs: BookmarkEditorOptions["dialogs"];
  status(message: string, state?: string): void;
  privateLifetime: RequestLifetime;
  storage?: () => BookmarkStorage;
}

export function createBookmarks(root: ParentNode, options: BookmarksOptions) {
  const { api, dialogs, status: setStatus, privateLifetime } = options;
  const listeners = new AbortController();
  let disposed = false;
  function requiredElement<T extends Element>(selector: string): T {
    const element = root.querySelector<T>(selector);
    if (!element) {
      throw new Error(`Missing required element: ${selector}`);
    }
    return element;
  }

  const groupsContainer = requiredElement<HTMLDivElement>("#groups");
  const count = requiredElement<HTMLParagraphElement>("#url-count");
  const sortSelect = requiredElement<HTMLSelectElement>("#sort-select");
  const groupFilterButtons = root.querySelectorAll<HTMLButtonElement>(
    ".group-filter [data-group-filter]",
  );

  // The API returns groups ordered by position, each holding URLs in insertion
  // order (oldest first). Keep the last loaded set so the sort control can
  // re-render each group without a round trip.
  const bookmarks = createBookmarksModel(options.storage);
  bookmarks.setSortMode(sortSelect.value as SortMode);
  let initialGroupsLoad: Promise<void> = Promise.resolve();
  const editors = createBookmarkEditors(root, {
    model: bookmarks, dialogs, status: setStatus, api, privateLifetime,
    refresh: { replace: replaceGroups, render: renderGroups, load: loadGroups, ready: () => initialGroupsLoad },
  });

  const drag = createBookmarkDrag(groupsContainer, {
    model: bookmarks, privateLifetime, reorder: api.reorderGroups,
    replace: replaceGroups, load: loadGroups, status: setStatus,
  });

  function replaceGroups(nextGroups: readonly BookmarkGroup[]): void {
    if (disposed) return;
    drag.cancel();
    bookmarks.replaceGroups(nextGroups);
    renderGroups();
  }

  function renderGroups(): void {
    renderBookmarkGroups(groupsContainer, count, bookmarks.server.groups, bookmarks.ui, {
      iconSource: (record) => api.siteIconPath(record.id),
      actions: { ...editors, toggleFold, makeHeaderDraggable: drag.makeHeaderDraggable },
    });
    editors.syncCreateParentSelect();
  }

  function toggleFold(
    groupId: number,
    toggle: HTMLButtonElement,
    content: HTMLDivElement,
  ): void {
    if (bookmarks.ui.foldedGroupIds.has(groupId)) {
      bookmarks.unfoldGroup(groupId);
    } else {
      bookmarks.foldGroup(groupId);
    }

    const isFolded = bookmarks.ui.foldedGroupIds.has(groupId);
    content.hidden = isFolded;
    toggle.setAttribute("aria-expanded", String(!isFolded));
  }

  sortSelect.addEventListener("change", () => {
    bookmarks.setSortMode(sortSelect.value as SortMode);
    renderGroups();
  }, { signal: listeners.signal });

  for (const button of groupFilterButtons) {
    button.addEventListener("click", () => {
      bookmarks.setSafeMode(button.dataset.groupFilter !== "all");
      for (const candidate of groupFilterButtons) {
        candidate.setAttribute(
          "aria-pressed",
          String(candidate.dataset.groupFilter === (bookmarks.ui.safeMode ? "safe" : "all")),
        );
      }
      renderGroups();
    }, { signal: listeners.signal });
  }

  async function loadGroups(): Promise<void> {
    // Reading an epoch must not supersede peer loads or unrelated mutations.
    const ticket = privateLifetime.capture();
    try {
      const data = await api.listGroups();
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
    } catch (error) {
      if (!disposed && privateLifetime.isCurrent(ticket)) throw error;
    }
  }

  function invalidate(): void {
    privateLifetime.invalidate();
    editors.invalidate();
  }

  function clear(): void {
    invalidate();
    drag.cancel();
    bookmarks.clearGroups();
    groupsContainer.replaceChildren();
    count.textContent = "0 saved";
    editors.clear();
  }

  function authenticated(): void {
    if (disposed) return;
    setStatus("");
    editors.setGroupStatus("");
    const ticket = privateLifetime.capture();
    initialGroupsLoad = loadGroups();
    initialGroupsLoad.catch((error: unknown) => {
      if (!disposed && privateLifetime.isCurrent(ticket)) setStatus(errorMessage(error), "is-error");
    });
    queueMicrotask(() => {
      if (!disposed && privateLifetime.isCurrent(ticket)) editors.focus();
    });
  }

  return {
    authenticated, invalidate, clear, replaceGroups,
    ready: (): Promise<void> => initialGroupsLoad,
    dispose(): void {
      if (disposed) return;
      disposed = true;
      clear();
      drag.dispose();
      editors.dispose();
      listeners.abort();
    },
  };
}
