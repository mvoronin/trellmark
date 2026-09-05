import { hostFor } from "../../shared/format.js";
const FOLDED_KEY = "foldedGroups";
function loadFoldedGroupIds(storage) {
    try {
        const ids = JSON.parse(storage?.().getItem(FOLDED_KEY) ?? "[]");
        return new Set(Array.isArray(ids) ? ids.filter((id) => Number.isInteger(id)) : []);
    }
    catch {
        return new Set();
    }
}
export function createBookmarksModel(storage) {
    let server = { groups: [] };
    let ui = {
        sortMode: "added-desc", safeMode: true, foldedGroupIds: loadFoldedGroupIds(storage),
        editingGroup: null, editingUrl: null, activeDrag: null, createParentId: null,
    };
    const setFolded = (groupId, folded) => {
        const foldedGroupIds = new Set(ui.foldedGroupIds);
        if (folded)
            foldedGroupIds.add(groupId);
        else
            foldedGroupIds.delete(groupId);
        ui = { ...ui, foldedGroupIds };
        try {
            storage?.().setItem(FOLDED_KEY, JSON.stringify([...foldedGroupIds]));
        }
        catch { } // Browser preference persistence is optional; the UI still changes.
    };
    return {
        get server() { return server; },
        get ui() { return ui; },
        // Async callers retain their private-state capture checks before replacement;
        // request ticket mechanics belong to the following lifetime extraction.
        replaceGroups(groups) {
            server = { groups };
            if (!treeGroups(groups).some(({ group, depth }) => group.id === ui.createParentId && depth < 3))
                ui = { ...ui, createParentId: null };
        },
        clearGroups() {
            server = { groups: [] };
            ui = { ...ui, editingGroup: null, editingUrl: null, activeDrag: null, createParentId: null };
        },
        setSortMode(sortMode) { ui = { ...ui, sortMode }; },
        setSafeMode(safeMode) { ui = { ...ui, safeMode }; },
        foldGroup(groupId) { setFolded(groupId, true); },
        unfoldGroup(groupId) { setFolded(groupId, false); },
        openGroupEditor(editingGroup) { ui = { ...ui, editingGroup }; },
        closeGroupEditor() { ui = { ...ui, editingGroup: null }; },
        openUrlEditor(editingUrl) { ui = { ...ui, editingUrl }; },
        closeUrlEditor() { ui = { ...ui, editingUrl: null }; },
        startDrag(drag) {
            ui = { ...ui, activeDrag: { ...drag, moving: false, dropTarget: null } };
        },
        moveDrag() {
            if (ui.activeDrag)
                ui = { ...ui, activeDrag: { ...ui.activeDrag, moving: true } };
        },
        setDropTarget(dropTarget) {
            if (ui.activeDrag)
                ui = { ...ui, activeDrag: { ...ui.activeDrag, dropTarget } };
        },
        finishDrag() { ui = { ...ui, activeDrag: null }; },
        setCreateParentId(createParentId) { ui = { ...ui, createParentId }; },
    };
}
export function sortUrls(urls, mode) {
    if (mode === "domain") {
        return [...urls].sort((a, b) => hostFor(a.url).localeCompare(hostFor(b.url)) ||
            a.created_at.localeCompare(b.created_at));
    }
    if (mode === "added-desc") {
        return [...urls].reverse();
    }
    return urls; // added-asc: preserve the authoritative insertion order.
}
export function visibleGroups(groups, safeMode) {
    return safeMode ? groups.filter((group) => !group.nsfw) : groups;
}
// The server owns hierarchy. This projection only bounds presentation depth.
export function treeGroups(source) {
    const result = [];
    const walk = (nodes, depth) => {
        if (depth > 3)
            return;
        for (const group of nodes) {
            result.push({ group, depth });
            walk(group.children, depth + 1);
        }
    };
    walk(source, 1);
    return result;
}
