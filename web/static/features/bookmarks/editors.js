import { h } from "../../shared/dom.js";
import { errorMessage } from "../../shared/format.js";
import { createRequestLifetime } from "../../shared/request.js";
import { treeGroups } from "./model.js";
import { optionLabel } from "./view.js";
export function createBookmarkEditors(root, options) {
    const listeners = new AbortController();
    const unsubscribeDialogs = [];
    let disposed = false;
    function on(element, event, callback) {
        element.addEventListener(event, callback, { signal: listeners.signal });
    }
    function onDialog(id, event, callback) {
        const unsubscribe = options.dialogs.on(id, event, () => { if (!disposed)
            callback(); });
        if (unsubscribe)
            unsubscribeDialogs.push(unsubscribe);
    }
    const { model: bookmarks, dialogs, status: setStatus, privateLifetime, api, refresh } = options;
    const { replace: replaceGroups, render: renderGroups, load: loadGroups } = refresh;
    function requiredElement(selector) {
        const element = root.querySelector(selector);
        if (!element)
            throw new Error(`Missing bookmark editor element: ${selector}`);
        return element;
    }
    const form = requiredElement("#url-form");
    const input = requiredElement("#url-input");
    const saveButton = requiredElement("#save-button");
    const deleteImmediately = requiredElement("#delete-immediately");
    const groupForm = requiredElement("#group-form");
    const groupInput = requiredElement("#group-input");
    const groupDomains = requiredElement("#group-domains");
    const groupParent = requiredElement("#group-parent");
    const groupNsfw = requiredElement("#group-nsfw");
    const groupButton = requiredElement("#group-button");
    const groupStatus = requiredElement("#group-status");
    const urlClear = requiredElement("#url-clear");
    const groupEditForm = requiredElement("#group-edit-form");
    const groupEditName = requiredElement("#group-edit-name");
    const groupEditNsfw = requiredElement("#group-edit-nsfw");
    const groupEditDomains = requiredElement("#group-edit-domains");
    const groupEditParent = requiredElement("#group-edit-parent");
    const groupEditStatus = requiredElement("#group-edit-status");
    const groupEditCancel = requiredElement("#group-edit-cancel");
    const groupEditSave = requiredElement("#group-edit-save");
    const groupDeleteName = requiredElement("#group-delete-name");
    const groupDeleteSummary = requiredElement("#group-delete-summary");
    const urlEditForm = requiredElement("#url-edit-form");
    const urlEditTitle = requiredElement("#url-edit-title");
    const urlEditUrl = requiredElement("#url-edit-url");
    const urlEditStatus = requiredElement("#url-edit-status");
    const urlEditCancel = requiredElement("#url-edit-cancel");
    const urlEditSave = requiredElement("#url-edit-save");
    const urlEditLifetime = createRequestLifetime();
    const groupEditLifetime = createRequestLifetime();
    const createUrlLifetime = createRequestLifetime();
    const createGroupLifetime = createRequestLifetime();
    function parseDomains(value) {
        return value
            .split(/[\n,]/)
            .map((domain) => domain.trim())
            .filter((domain) => domain !== "");
    }
    function fillGroupSelect(select, candidates, selectedId) {
        const root = h("option", { value: "" }, ["Root"]);
        select.replaceChildren(root);
        for (const { group, depth } of candidates) {
            const option = h("option", { value: String(group.id) }, [optionLabel(group, depth)]);
            select.append(option);
        }
        // An id with no matching option leaves the select on selectedIndex -1, where
        // it renders blank and reads back as "" — indistinguishable from a deliberate
        // "Root". Callers check for that state instead of trusting `.value`.
        select.value = selectedId === null ? "" : String(selectedId);
    }
    on(groupParent, "change", () => {
        bookmarks.setCreateParentId(groupParent.value === "" ? null : Number(groupParent.value));
    });
    function syncCreateParentSelect() {
        fillGroupSelect(groupParent, treeGroups(bookmarks.server.groups).filter(({ depth }) => depth < 3), bookmarks.ui.createParentId);
        if (groupParent.selectedIndex === -1) {
            // The pending parent is gone, or can no longer hold a child. Falling back to
            // the root is harmless for a group that does not exist yet, and showing
            // "Root" beats leaving the control blank.
            groupParent.value = "";
        }
    }
    function setGroupStatus(message, state = "") {
        groupStatus.textContent = message;
        groupStatus.className = state;
    }
    function subtreeIds(group) {
        const ids = new Set();
        const walk = (node, remainingDepth) => {
            ids.add(node.id);
            if (remainingDepth === 0) {
                return;
            }
            for (const child of node.children) {
                walk(child, remainingDepth - 1);
            }
        };
        walk(group, 2);
        return ids;
    }
    function subtreeHeight(group, remainingDepth = 2) {
        if (remainingDepth === 0 || group.children.length === 0) {
            return 1;
        }
        return 1 + Math.max(...group.children.map((child) => subtreeHeight(child, remainingDepth - 1)));
    }
    // Every destination the group may move to: anything outside its own subtree
    // that still leaves the tree three levels deep, plus its current parent. Staying
    // put is always legal — the server no-ops a move that changes nothing — so the
    // current parent stays listed even if a malformed or drifted payload would put
    // it out of bounds, which keeps the select from going blank on a plain rename.
    function editParentCandidates(group) {
        const excluded = subtreeIds(group);
        const height = subtreeHeight(group);
        return treeGroups(bookmarks.server.groups).filter(({ group: candidate, depth }) => !excluded.has(candidate.id)
            && (candidate.id === group.parent_id || depth + height <= 3));
    }
    // selectedIndex -1 means no option matched the group's current parent, so the
    // blank control carries no intent: keep the group where it is rather than
    // reading its empty value as a deliberate move to the root.
    function editedParentId(group) {
        if (groupEditParent.selectedIndex === -1) {
            return group.parent_id;
        }
        return groupEditParent.value === "" ? null : Number(groupEditParent.value);
    }
    function showGroupEditor(group) {
        groupEditLifetime.invalidate();
        groupEditSave.disabled = false;
        bookmarks.openGroupEditor(group);
        groupEditName.value = group.name;
        groupEditNsfw.checked = group.nsfw;
        groupEditDomains.value = group.domains.join("\n");
        fillGroupSelect(groupEditParent, editParentCandidates(group), group.parent_id);
        groupEditStatus.textContent = "";
        void dialogs.open("group-edit-dialog");
        groupEditName.focus();
        groupEditName.select();
    }
    function closeGroupEditor(result) {
        groupEditLifetime.invalidate();
        bookmarks.closeGroupEditor();
        groupEditSave.disabled = false;
        dialogs.close("group-edit-dialog", result);
    }
    on(groupEditCancel, "click", () => closeGroupEditor("cancel"));
    onDialog("group-edit-dialog", "cancel", () => closeGroupEditor("cancel"));
    onDialog("group-edit-dialog", "close", () => {
        if (dialogs.isOpen("group-edit-dialog"))
            return;
        groupEditLifetime.invalidate();
        groupEditSave.disabled = false;
        bookmarks.closeGroupEditor();
    });
    on(groupEditForm, "submit", async (event) => {
        event.preventDefault();
        if (!bookmarks.ui.editingGroup || groupEditLifetime.inFlight) {
            return;
        }
        const ticket = groupEditLifetime.begin();
        groupEditStatus.textContent = "";
        groupEditSave.disabled = true;
        try {
            const data = await api.editGroup(bookmarks.ui.editingGroup.id, {
                name: groupEditName.value,
                nsfw: groupEditNsfw.checked,
                domains: parseDomains(groupEditDomains.value),
                parent_id: editedParentId(bookmarks.ui.editingGroup),
            });
            if (!groupEditLifetime.isCurrent(ticket) || !dialogs.isOpen("group-edit-dialog"))
                return;
            replaceGroups(data.groups);
            closeGroupEditor("saved");
            setGroupStatus("Group updated.", "is-success");
        }
        catch (error) {
            if (!groupEditLifetime.isCurrent(ticket) || !dialogs.isOpen("group-edit-dialog"))
                return;
            groupEditStatus.textContent = errorMessage(error);
            await loadGroups().catch(() => undefined);
        }
        finally {
            if (groupEditLifetime.finish(ticket))
                groupEditSave.disabled = false;
        }
    });
    function showUrlEditor(record) {
        urlEditLifetime.invalidate();
        bookmarks.openUrlEditor(record);
        urlEditTitle.value = record.title ?? "";
        urlEditUrl.value = record.url;
        urlEditStatus.textContent = "";
        urlEditSave.disabled = false;
        void dialogs.open("url-edit-dialog");
        urlEditTitle.focus();
        urlEditTitle.select();
    }
    function closeUrlEditor(result) {
        urlEditLifetime.invalidate();
        bookmarks.closeUrlEditor();
        urlEditSave.disabled = false;
        dialogs.close("url-edit-dialog", result);
    }
    on(urlEditCancel, "click", () => closeUrlEditor("cancel"));
    onDialog("url-edit-dialog", "cancel", () => closeUrlEditor("cancel"));
    onDialog("url-edit-dialog", "close", () => {
        if (dialogs.isOpen("url-edit-dialog"))
            return;
        urlEditLifetime.invalidate();
        urlEditSave.disabled = false;
        bookmarks.closeUrlEditor();
    });
    function isCurrentUrlEditRequest(ticket, urlId) {
        return (urlEditLifetime.isCurrent(ticket) &&
            bookmarks.ui.editingUrl?.id === urlId &&
            dialogs.isOpen("url-edit-dialog"));
    }
    on(urlEditForm, "submit", async (event) => {
        event.preventDefault();
        if (!bookmarks.ui.editingUrl || urlEditLifetime.inFlight) {
            return;
        }
        const record = bookmarks.ui.editingUrl;
        const ticket = urlEditLifetime.begin();
        setStatus("");
        urlEditStatus.textContent = "";
        urlEditSave.disabled = true;
        try {
            const data = await api.editUrl(record.id, {
                title: urlEditTitle.value,
                url: urlEditUrl.value,
                version: record.version,
            });
            if (!isCurrentUrlEditRequest(ticket, record.id)) {
                return;
            }
            replaceGroups(data.groups);
            closeUrlEditor("saved");
            setStatus("URL updated.", "is-success");
        }
        catch (error) {
            if (!isCurrentUrlEditRequest(ticket, record.id)) {
                return;
            }
            urlEditStatus.textContent = errorMessage(error);
        }
        finally {
            if (urlEditLifetime.finish(ticket)) {
                urlEditSave.disabled = false;
            }
        }
    });
    async function refreshMetadataForUrl(record, button) {
        const ticket = privateLifetime.capture();
        setStatus("Refreshing title and icon…");
        button.disabled = true;
        try {
            const data = await api.refreshUrlMetadata(record.id);
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            replaceGroups(data.groups);
            if (data.title_updated && data.icon_updated) {
                setStatus("Title and icon refreshed.", "is-success");
            }
            else if (data.title_updated) {
                setStatus("Title refreshed; no site icon found.", "is-success");
            }
            else if (data.icon_updated) {
                setStatus("Icon refreshed; no page title found.", "is-success");
            }
            else {
                setStatus("No page title or site icon found.");
            }
        }
        catch (error) {
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            button.disabled = false;
            setStatus(errorMessage(error), "is-error");
        }
    }
    function confirmGroupDeletion(group) {
        groupDeleteName.textContent = group.name;
        const linkCount = group.urls.length;
        groupDeleteSummary.textContent = linkCount === 0
            ? "This group is empty."
            : `Choose whether to preserve its ${linkCount} saved ${linkCount === 1 ? "link" : "links"}.`;
        return dialogs.open("group-delete-dialog").then((action) => action === "delete" || action === "move_to_default" ? action : null);
    }
    async function requestGroupDelete(group) {
        const ticket = privateLifetime.capture();
        const action = await confirmGroupDeletion(group);
        if (disposed || !privateLifetime.isCurrent(ticket) || !action) {
            return;
        }
        setGroupStatus("");
        try {
            const data = await api.deleteGroup(group.id, { url_action: action });
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            bookmarks.unfoldGroup(group.id);
            replaceGroups(data.groups);
            if (action === "move_to_default") {
                const links = `${data.moved} ${data.moved === 1 ? "link" : "links"}`;
                setGroupStatus(data.moved === 0
                    ? "Group deleted."
                    : `Group deleted; ${links} moved to default.`, "is-success");
            }
            else {
                const links = `${data.deleted} ${data.deleted === 1 ? "link" : "links"}`;
                setGroupStatus(data.deleted === 0 ? "Group deleted." : `Group and ${links} deleted.`, "is-success");
            }
        }
        catch (error) {
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            setGroupStatus(errorMessage(error), "is-error");
            await loadGroups().catch(() => undefined);
        }
    }
    // Skip the prompt when "Delete immediately" is on; otherwise show the modal
    // and only delete if the user confirms. The dialog's returnValue is "delete"
    // for the Delete button and "cancel"/empty for Cancel or Escape.
    async function requestDelete(record, groupId, button) {
        const ticket = privateLifetime.capture();
        if (!deleteImmediately.checked && !(await dialogs.confirmDeletion(record.url))) {
            return;
        }
        if (disposed || !privateLifetime.isCurrent(ticket))
            return;
        deleteUrl(record.id, groupId, button);
    }
    async function deleteUrl(id, groupId, button) {
        const ticket = privateLifetime.capture();
        setStatus("");
        button.disabled = true;
        try {
            const data = await api.deleteUrlById(id, groupId);
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            replaceGroups(data.groups);
            setStatus("Deleted.", "is-success");
        }
        catch (error) {
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            button.disabled = false;
            setStatus(errorMessage(error), "is-error");
        }
    }
    async function moveUrl(id, sourceGroupId, targetGroupId, select) {
        const ticket = privateLifetime.capture();
        setStatus("");
        select.disabled = true;
        try {
            const data = await api.moveUrlToGroup(id, {
                group_id: targetGroupId,
                source_group_id: sourceGroupId,
            });
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            replaceGroups(data.groups);
            setStatus("Moved.", "is-success");
        }
        catch (error) {
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            // Re-render from the last known state so the select snaps back.
            renderGroups();
            setStatus(errorMessage(error), "is-error");
        }
    }
    async function toggleImportant(id, important, button) {
        const ticket = privateLifetime.capture();
        setStatus("");
        button.disabled = true;
        try {
            const data = await api.setUrlImportant(id, { important });
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            replaceGroups(data.groups);
            setStatus("Updated.", "is-success");
        }
        catch (error) {
            if (disposed || !privateLifetime.isCurrent(ticket))
                return;
            // Re-render from the last known state so the toggle snaps back.
            renderGroups();
            setStatus(errorMessage(error), "is-error");
        }
    }
    // The in-field clear button only shows while there is something to clear.
    function syncUrlClear() {
        urlClear.hidden = input.value === "";
    }
    on(input, "input", syncUrlClear);
    on(urlClear, "click", () => {
        input.value = "";
        syncUrlClear();
        input.focus();
    });
    syncUrlClear();
    on(form, "submit", async (event) => {
        event.preventDefault();
        if (createUrlLifetime.inFlight)
            return;
        const ticket = createUrlLifetime.begin();
        setStatus("");
        saveButton.disabled = true;
        try {
            await refresh.ready().catch(() => undefined);
            if (!createUrlLifetime.isCurrent(ticket))
                return;
            const data = await api.createUrl({ url: input.value });
            if (!createUrlLifetime.isCurrent(ticket))
                return;
            input.value = "";
            syncUrlClear();
            replaceGroups(data.groups);
            setStatus("Saved.", "is-success");
        }
        catch (error) {
            if (!createUrlLifetime.isCurrent(ticket))
                return;
            setStatus(errorMessage(error), "is-error");
        }
        finally {
            if (createUrlLifetime.finish(ticket)) {
                saveButton.disabled = false;
                input.focus();
            }
        }
    });
    on(groupForm, "submit", async (event) => {
        event.preventDefault();
        if (createGroupLifetime.inFlight)
            return;
        const ticket = createGroupLifetime.begin();
        setGroupStatus("");
        groupButton.disabled = true;
        try {
            await refresh.ready().catch(() => undefined);
            if (!createGroupLifetime.isCurrent(ticket))
                return;
            const data = await api.createGroup({
                name: groupInput.value,
                nsfw: groupNsfw.checked,
                domains: parseDomains(groupDomains.value),
                parent_id: groupParent.value === "" ? null : Number(groupParent.value),
            });
            if (!createGroupLifetime.isCurrent(ticket))
                return;
            groupInput.value = "";
            groupDomains.value = "";
            groupNsfw.checked = false;
            bookmarks.setCreateParentId(null);
            groupParent.value = "";
            replaceGroups(data.groups);
            setGroupStatus("Group added.", "is-success");
        }
        catch (error) {
            if (!createGroupLifetime.isCurrent(ticket))
                return;
            setGroupStatus(errorMessage(error), "is-error");
            await loadGroups().catch(() => undefined);
        }
        finally {
            if (createGroupLifetime.finish(ticket)) {
                groupButton.disabled = false;
                groupInput.focus();
            }
        }
    });
    function invalidate() {
        urlEditLifetime.invalidate();
        groupEditLifetime.invalidate();
        createUrlLifetime.invalidate();
        createGroupLifetime.invalidate();
        // Invalidating owners reset controls; stale finally blocks never own them.
        for (const button of [saveButton, groupButton, urlEditSave, groupEditSave]) {
            button.disabled = false;
        }
    }
    function clear() {
        invalidate();
        groupStatus.textContent = "";
        form.reset();
        groupForm.reset();
        groupEditForm.reset();
        urlEditForm.reset();
        fillGroupSelect(groupParent, [], null);
        fillGroupSelect(groupEditParent, [], null);
        groupEditStatus.textContent = "";
        urlEditStatus.textContent = "";
        groupDeleteName.textContent = "";
        groupDeleteSummary.textContent = "";
    }
    return {
        showGroupEditor, showUrlEditor, requestGroupDelete, requestDelete,
        refreshMetadataForUrl, moveUrl, toggleImportant, syncCreateParentSelect,
        setGroupStatus, invalidate, clear, focus: () => input.focus(),
        dispose() {
            if (disposed)
                return;
            disposed = true;
            clear();
            listeners.abort();
            for (const unsubscribe of unsubscribeDialogs)
                unsubscribe();
        },
    };
}
