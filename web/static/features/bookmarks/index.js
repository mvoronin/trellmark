import { errorMessage } from "../../shared/format.js";
import { createBookmarkDrag } from "./drag.js";
import { createBookmarkEditors } from "./editors.js";
import { createBookmarksModel } from "./model.js";
import { renderGroups as renderBookmarkGroups } from "./view.js";
export { createBookmarkEditors } from "./editors.js";
export { createBookmarkDrag } from "./drag.js";
export { createBookmarksModel } from "./model.js";
export { renderGroups, renderGroup, renderUrlItem, renderSiteIcon } from "./view.js";
export function createBookmarks(root, options) {
    const { api, dialogs, status: setStatus, privateLifetime } = options;
    const listeners = new AbortController();
    let disposed = false;
    function requiredElement(selector) {
        const element = root.querySelector(selector);
        if (!element) {
            throw new Error(`Missing required element: ${selector}`);
        }
        return element;
    }
    const groupsContainer = requiredElement("#groups");
    const count = requiredElement("#url-count");
    const sortSelect = requiredElement("#sort-select");
    const groupFilterButtons = root.querySelectorAll(".group-filter [data-group-filter]");
    // The API returns groups ordered by position, each holding URLs in insertion
    // order (oldest first). Keep the last loaded set so the sort control can
    // re-render each group without a round trip.
    const bookmarks = createBookmarksModel(options.storage);
    bookmarks.setSortMode(sortSelect.value);
    let initialGroupsLoad = Promise.resolve();
    const editors = createBookmarkEditors(root, {
        model: bookmarks, dialogs, status: setStatus, api, privateLifetime,
        refresh: { replace: replaceGroups, render: renderGroups, load: loadGroups, ready: () => initialGroupsLoad },
    });
    const drag = createBookmarkDrag(groupsContainer, {
        model: bookmarks, privateLifetime, reorder: api.reorderGroups,
        replace: replaceGroups, load: loadGroups, status: setStatus,
        moveUrl: editors.moveUrl,
    });
    function replaceGroups(nextGroups) {
        if (disposed)
            return;
        drag.cancel();
        bookmarks.replaceGroups(nextGroups);
        renderGroups();
    }
    function renderGroups() {
        drag.cancel();
        renderBookmarkGroups(groupsContainer, count, bookmarks.server.groups, bookmarks.ui, {
            iconSource: (record) => api.siteIconPath(record.id),
            actions: { ...editors, toggleFold, makeHeaderDraggable: drag.makeHeaderDraggable,
                makeUrlDraggable: drag.makeUrlDraggable },
        });
        editors.syncCreateParentSelect();
    }
    function toggleFold(groupId, toggle, content) {
        if (bookmarks.ui.foldedGroupIds.has(groupId)) {
            bookmarks.unfoldGroup(groupId);
        }
        else {
            bookmarks.foldGroup(groupId);
        }
        const isFolded = bookmarks.ui.foldedGroupIds.has(groupId);
        content.hidden = isFolded;
        toggle.setAttribute("aria-expanded", String(!isFolded));
    }
    sortSelect.addEventListener("change", () => {
        bookmarks.setSortMode(sortSelect.value);
        renderGroups();
    }, { signal: listeners.signal });
    for (const button of groupFilterButtons) {
        button.addEventListener("click", () => {
            bookmarks.setSafeMode(button.dataset.groupFilter !== "all");
            for (const candidate of groupFilterButtons) {
                candidate.setAttribute("aria-pressed", String(candidate.dataset.groupFilter === (bookmarks.ui.safeMode ? "safe" : "all")));
            }
            renderGroups();
        }, { signal: listeners.signal });
    }
    async function loadGroups() {
        // Reading an epoch must not supersede peer loads or unrelated mutations.
        const ticket = privateLifetime.capture();
        try {
            const data = await api.listGroups();
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            replaceGroups(data.groups);
        }
        catch (error) {
            if (!disposed && privateLifetime.isCurrent(ticket))
                throw error;
        }
    }
    function invalidate() {
        privateLifetime.invalidate();
        editors.invalidate();
    }
    function clear() {
        invalidate();
        drag.cancel();
        bookmarks.clearGroups();
        groupsContainer.replaceChildren();
        count.textContent = "0 saved";
        editors.clear();
    }
    function authenticated() {
        if (disposed)
            return;
        setStatus("");
        editors.setGroupStatus("");
        const ticket = privateLifetime.capture();
        initialGroupsLoad = loadGroups();
        initialGroupsLoad.catch((error) => {
            if (!disposed && privateLifetime.isCurrent(ticket))
                setStatus(errorMessage(error), "is-error");
        });
        queueMicrotask(() => {
            if (!disposed && privateLifetime.isCurrent(ticket))
                editors.focus();
        });
    }
    return {
        authenticated, invalidate, clear, replaceGroups,
        ready: () => initialGroupsLoad,
        dispose() {
            if (disposed)
                return;
            disposed = true;
            clear();
            drag.dispose();
            editors.dispose();
            listeners.abort();
        },
    };
}
