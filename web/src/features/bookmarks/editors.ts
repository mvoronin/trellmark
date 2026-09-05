import { h } from "../../shared/dom.js";
import { errorMessage } from "../../shared/format.js";
import { createRequestLifetime, type RequestLifetime, type RequestTicket } from "../../shared/request.js";
import { treeGroups, type BookmarksModel, type BookmarkGroup, type BookmarkUrl, type TreeGroup } from "./model.js";
import { optionLabel } from "./view.js";

type GroupDeleteAction = "delete" | "move_to_default";

export interface BookmarkDialogs {
  open(id: string): Promise<string>;
  close(id: string, result?: string): void;
  isOpen(id: string): boolean;
  on(id: string, event: "close" | "cancel", callback: () => void): void | (() => void);
  confirmDeletion(url: string): Promise<boolean>;
}

export interface BookmarkEditorOptions {
  model: BookmarksModel;
  dialogs: BookmarkDialogs;
  status(message: string, state?: string): void;
  privateLifetime: RequestLifetime;
  api: Pick<typeof import("../../api/client.js"),
    "createGroup" | "createUrl" | "deleteGroup" | "deleteUrlById" | "editGroup" |
    "editUrl" | "moveUrlToGroup" | "refreshUrlMetadata" | "setUrlImportant">;
  refresh: {
    replace(groups: readonly BookmarkGroup[]): void;
    render(): void;
    load(): Promise<void>;
    ready(): Promise<void>;
  };
}

export function createBookmarkEditors(root: ParentNode, options: BookmarkEditorOptions) {
  const listeners = new AbortController();
  const unsubscribeDialogs: (() => void)[] = [];
  let disposed = false;
  function on<Key extends keyof HTMLElementEventMap>(
    element: HTMLElement, event: Key, callback: (event: HTMLElementEventMap[Key]) => void,
  ): void {
    element.addEventListener(event, callback, { signal: listeners.signal });
  }
  function onDialog(id: string, event: "close" | "cancel", callback: () => void): void {
    const unsubscribe = options.dialogs.on(id, event, () => { if (!disposed) callback(); });
    if (unsubscribe) unsubscribeDialogs.push(unsubscribe);
  }
  const { model: bookmarks, dialogs, status: setStatus, privateLifetime, api, refresh } = options;
  const { replace: replaceGroups, render: renderGroups, load: loadGroups } = refresh;
  function requiredElement<T extends Element>(selector: string): T {
    const element = root.querySelector<T>(selector);
    if (!element) throw new Error(`Missing bookmark editor element: ${selector}`);
    return element;
  }
  const form = requiredElement<HTMLFormElement>("#url-form");
  const input = requiredElement<HTMLInputElement>("#url-input");
  const saveButton = requiredElement<HTMLButtonElement>("#save-button");
  const deleteImmediately = requiredElement<HTMLInputElement>("#delete-immediately");
  const groupForm = requiredElement<HTMLFormElement>("#group-form");
  const groupInput = requiredElement<HTMLInputElement>("#group-input");
  const groupDomains = requiredElement<HTMLInputElement>("#group-domains");
  const groupParent = requiredElement<HTMLSelectElement>("#group-parent");
  const groupNsfw = requiredElement<HTMLInputElement>("#group-nsfw");
  const groupButton = requiredElement<HTMLButtonElement>("#group-button");
  const groupStatus = requiredElement<HTMLParagraphElement>("#group-status");
  const urlClear = requiredElement<HTMLButtonElement>("#url-clear");
  const groupEditForm = requiredElement<HTMLFormElement>("#group-edit-form");
  const groupEditName = requiredElement<HTMLInputElement>("#group-edit-name");
  const groupEditNsfw = requiredElement<HTMLInputElement>("#group-edit-nsfw");
  const groupEditDomains = requiredElement<HTMLTextAreaElement>("#group-edit-domains");
  const groupEditParent = requiredElement<HTMLSelectElement>("#group-edit-parent");
  const groupEditStatus = requiredElement<HTMLParagraphElement>("#group-edit-status");
  const groupEditCancel = requiredElement<HTMLButtonElement>("#group-edit-cancel");
  const groupEditSave = requiredElement<HTMLButtonElement>("#group-edit-save");
  const groupDeleteName = requiredElement<HTMLSpanElement>("#group-delete-name");
  const groupDeleteSummary = requiredElement<HTMLParagraphElement>(
    "#group-delete-summary",
  );
  const urlEditForm = requiredElement<HTMLFormElement>("#url-edit-form");
  const urlEditTitle = requiredElement<HTMLInputElement>("#url-edit-title");
  const urlEditUrl = requiredElement<HTMLInputElement>("#url-edit-url");
  const urlEditStatus = requiredElement<HTMLParagraphElement>("#url-edit-status");
  const urlEditCancel = requiredElement<HTMLButtonElement>("#url-edit-cancel");
  const urlEditSave = requiredElement<HTMLButtonElement>("#url-edit-save");

  const urlEditLifetime = createRequestLifetime();
  const groupEditLifetime = createRequestLifetime();
  const createUrlLifetime = createRequestLifetime();
  const createGroupLifetime = createRequestLifetime();

  function parseDomains(value: string): string[] {
    return value
      .split(/[\n,]/)
      .map((domain) => domain.trim())
      .filter((domain) => domain !== "");
  }

  function fillGroupSelect(
    select: HTMLSelectElement,
    candidates: TreeGroup[],
    selectedId: number | null,
  ): void {
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

  function syncCreateParentSelect(): void {
    fillGroupSelect(
      groupParent,
      treeGroups(bookmarks.server.groups).filter(({ depth }) => depth < 3),
      bookmarks.ui.createParentId,
    );
    if (groupParent.selectedIndex === -1) {
      // The pending parent is gone, or can no longer hold a child. Falling back to
      // the root is harmless for a group that does not exist yet, and showing
      // "Root" beats leaving the control blank.
      groupParent.value = "";
    }
  }

  function setGroupStatus(message: string, state = ""): void {
    groupStatus.textContent = message;
    groupStatus.className = state;
  }

  function subtreeIds(group: BookmarkGroup): Set<number> {
    const ids = new Set<number>();
    const walk = (node: BookmarkGroup, remainingDepth: number): void => {
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

  function subtreeHeight(group: BookmarkGroup, remainingDepth = 2): number {
    if (remainingDepth === 0 || group.children.length === 0) {
      return 1;
    }
    return 1 + Math.max(
      ...group.children.map((child) => subtreeHeight(child, remainingDepth - 1)),
    );
  }

  // Every destination the group may move to: anything outside its own subtree
  // that still leaves the tree three levels deep, plus its current parent. Staying
  // put is always legal — the server no-ops a move that changes nothing — so the
  // current parent stays listed even if a malformed or drifted payload would put
  // it out of bounds, which keeps the select from going blank on a plain rename.
  function editParentCandidates(group: BookmarkGroup): TreeGroup[] {
    const excluded = subtreeIds(group);
    const height = subtreeHeight(group);
    return treeGroups(bookmarks.server.groups).filter(
      ({ group: candidate, depth }) =>
        !excluded.has(candidate.id)
        && (candidate.id === group.parent_id || depth + height <= 3),
    );
  }

  // selectedIndex -1 means no option matched the group's current parent, so the
  // blank control carries no intent: keep the group where it is rather than
  // reading its empty value as a deliberate move to the root.
  function editedParentId(group: BookmarkGroup): number | null {
    if (groupEditParent.selectedIndex === -1) {
      return group.parent_id;
    }
    return groupEditParent.value === "" ? null : Number(groupEditParent.value);
  }

  function showGroupEditor(group: BookmarkGroup): void {
    groupEditLifetime.invalidate();
    groupEditSave.disabled = false;
    bookmarks.openGroupEditor(group);
    groupEditName.value = group.name;
    groupEditNsfw.checked = group.nsfw;
    groupEditDomains.value = group.domains.join("\n");
    fillGroupSelect(
      groupEditParent,
      editParentCandidates(group),
      group.parent_id,
    );
    groupEditStatus.textContent = "";
    void dialogs.open("group-edit-dialog");
    groupEditName.focus();
    groupEditName.select();
  }

  function closeGroupEditor(result: string): void {
    groupEditLifetime.invalidate();
    bookmarks.closeGroupEditor();
    groupEditSave.disabled = false;
    dialogs.close("group-edit-dialog", result);
  }

  on(groupEditCancel, "click", () => closeGroupEditor("cancel"));
  onDialog("group-edit-dialog", "cancel", () => closeGroupEditor("cancel"));
  onDialog("group-edit-dialog", "close", () => {
    if (dialogs.isOpen("group-edit-dialog")) return;
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
      if (!groupEditLifetime.isCurrent(ticket) || !dialogs.isOpen("group-edit-dialog")) return;
      replaceGroups(data.groups);
      closeGroupEditor("saved");
      setGroupStatus("Group updated.", "is-success");
    } catch (error) {
      if (!groupEditLifetime.isCurrent(ticket) || !dialogs.isOpen("group-edit-dialog")) return;
      groupEditStatus.textContent = errorMessage(error);
      await loadGroups().catch(() => undefined);
    } finally {
      if (groupEditLifetime.finish(ticket)) groupEditSave.disabled = false;
    }
  });

  function showUrlEditor(record: BookmarkUrl): void {
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

  function closeUrlEditor(result: string): void {
    urlEditLifetime.invalidate();
    bookmarks.closeUrlEditor();
    urlEditSave.disabled = false;
    dialogs.close("url-edit-dialog", result);
  }

  on(urlEditCancel, "click", () => closeUrlEditor("cancel"));
  onDialog("url-edit-dialog", "cancel", () => closeUrlEditor("cancel"));
  onDialog("url-edit-dialog", "close", () => {
    if (dialogs.isOpen("url-edit-dialog")) return;
    urlEditLifetime.invalidate();
    urlEditSave.disabled = false;
    bookmarks.closeUrlEditor();
  });

  function isCurrentUrlEditRequest(ticket: RequestTicket, urlId: number): boolean {
    return (
      urlEditLifetime.isCurrent(ticket) &&
      bookmarks.ui.editingUrl?.id === urlId &&
      dialogs.isOpen("url-edit-dialog")
    );
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
    } catch (error) {
      if (!isCurrentUrlEditRequest(ticket, record.id)) {
        return;
      }
      urlEditStatus.textContent = errorMessage(error);
    } finally {
      if (urlEditLifetime.finish(ticket)) {
        urlEditSave.disabled = false;
      }
    }
  });

  async function refreshMetadataForUrl(
    record: BookmarkUrl,
    button: HTMLButtonElement,
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    setStatus("Refreshing title and icon…");
    button.disabled = true;

    try {
      const data = await api.refreshUrlMetadata(record.id);
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
      if (data.title_updated && data.icon_updated) {
        setStatus("Title and icon refreshed.", "is-success");
      } else if (data.title_updated) {
        setStatus("Title refreshed; no site icon found.", "is-success");
      } else if (data.icon_updated) {
        setStatus("Icon refreshed; no page title found.", "is-success");
      } else {
        setStatus("No page title or site icon found.");
      }
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      button.disabled = false;
      setStatus(errorMessage(error), "is-error");
    }
  }

  function confirmGroupDeletion(group: BookmarkGroup): Promise<GroupDeleteAction | null> {
    groupDeleteName.textContent = group.name;
    const linkCount = group.urls.length;
    groupDeleteSummary.textContent = linkCount === 0
      ? "This group is empty."
      : `Choose whether to preserve its ${linkCount} saved ${linkCount === 1 ? "link" : "links"}.`;
    return dialogs.open("group-delete-dialog").then((action) =>
      action === "delete" || action === "move_to_default" ? action : null,
    );
  }

  async function requestGroupDelete(group: BookmarkGroup): Promise<void> {
    const ticket = privateLifetime.capture();
    const action = await confirmGroupDeletion(group);
    if (disposed || !privateLifetime.isCurrent(ticket) || !action) {
      return;
    }

    setGroupStatus("");
    try {
      const data = await api.deleteGroup(group.id, { url_action: action });
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      bookmarks.unfoldGroup(group.id);
      replaceGroups(data.groups);
      if (action === "move_to_default") {
        const links = `${data.moved} ${data.moved === 1 ? "link" : "links"}`;
        setGroupStatus(
          data.moved === 0
            ? "Group deleted."
            : `Group deleted; ${links} moved to default.`,
          "is-success",
        );
      } else {
        const links = `${data.deleted} ${data.deleted === 1 ? "link" : "links"}`;
        setGroupStatus(
          data.deleted === 0 ? "Group deleted." : `Group and ${links} deleted.`,
          "is-success",
        );
      }
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      setGroupStatus(errorMessage(error), "is-error");
      await loadGroups().catch(() => undefined);
    }
  }

  // Skip the prompt when "Delete immediately" is on; otherwise show the modal
  // and only delete if the user confirms. The dialog's returnValue is "delete"
  // for the Delete button and "cancel"/empty for Cancel or Escape.
  async function requestDelete(
    record: BookmarkUrl,
    groupId: number,
    button: HTMLButtonElement,
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    if (!deleteImmediately.checked && !(await dialogs.confirmDeletion(record.url))) {
      return;
    }
    if (disposed || !privateLifetime.isCurrent(ticket)) return;
    deleteUrl(record.id, groupId, button);
  }

  async function deleteUrl(
    id: number,
    groupId: number,
    button: HTMLButtonElement,
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    setStatus("");
    button.disabled = true;

    try {
      const data = await api.deleteUrlById(id, groupId);
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
      setStatus("Deleted.", "is-success");
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      button.disabled = false;
      setStatus(errorMessage(error), "is-error");
    }
  }

  async function moveUrl(
    id: number,
    sourceGroupId: number,
    targetGroupId: number,
    select: HTMLSelectElement,
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    setStatus("");
    select.disabled = true;

    try {
      const data = await api.moveUrlToGroup(id, {
        group_id: targetGroupId,
        source_group_id: sourceGroupId,
      });
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
      setStatus("Moved.", "is-success");
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      // Re-render from the last known state so the select snaps back.
      renderGroups();
      setStatus(errorMessage(error), "is-error");
    }
  }

  async function toggleImportant(
    id: number,
    important: boolean,
    button: HTMLButtonElement,
  ): Promise<void> {
    const ticket = privateLifetime.capture();
    setStatus("");
    button.disabled = true;

    try {
      const data = await api.setUrlImportant(id, { important });
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      replaceGroups(data.groups);
      setStatus("Updated.", "is-success");
    } catch (error) {
      if (disposed || !privateLifetime.isCurrent(ticket)) return;
      // Re-render from the last known state so the toggle snaps back.
      renderGroups();
      setStatus(errorMessage(error), "is-error");
    }
  }

  // The in-field clear button only shows while there is something to clear.
  function syncUrlClear(): void {
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
    if (createUrlLifetime.inFlight) return;
    const ticket = createUrlLifetime.begin();
    setStatus("");
    saveButton.disabled = true;

    try {
      await refresh.ready().catch(() => undefined);
      if (!createUrlLifetime.isCurrent(ticket)) return;
      const data = await api.createUrl({ url: input.value });
      if (!createUrlLifetime.isCurrent(ticket)) return;
      input.value = "";
      syncUrlClear();
      replaceGroups(data.groups);
      setStatus("Saved.", "is-success");
    } catch (error) {
      if (!createUrlLifetime.isCurrent(ticket)) return;
      setStatus(errorMessage(error), "is-error");
    } finally {
      if (createUrlLifetime.finish(ticket)) {
        saveButton.disabled = false;
        input.focus();
      }
    }
  });

  on(groupForm, "submit", async (event) => {
    event.preventDefault();
    if (createGroupLifetime.inFlight) return;
    const ticket = createGroupLifetime.begin();
    setGroupStatus("");
    groupButton.disabled = true;

    try {
      await refresh.ready().catch(() => undefined);
      if (!createGroupLifetime.isCurrent(ticket)) return;
      const data = await api.createGroup({
        name: groupInput.value,
        nsfw: groupNsfw.checked,
        domains: parseDomains(groupDomains.value),
        parent_id: groupParent.value === "" ? null : Number(groupParent.value),
      });
      if (!createGroupLifetime.isCurrent(ticket)) return;
      groupInput.value = "";
      groupDomains.value = "";
      groupNsfw.checked = false;
      bookmarks.setCreateParentId(null);
      groupParent.value = "";
      replaceGroups(data.groups);
      setGroupStatus("Group added.", "is-success");
    } catch (error) {
      if (!createGroupLifetime.isCurrent(ticket)) return;
      setGroupStatus(errorMessage(error), "is-error");
      await loadGroups().catch(() => undefined);
    } finally {
      if (createGroupLifetime.finish(ticket)) {
        groupButton.disabled = false;
        groupInput.focus();
      }
    }
  });


  function invalidate(): void {
    urlEditLifetime.invalidate();
    groupEditLifetime.invalidate();
    createUrlLifetime.invalidate();
    createGroupLifetime.invalidate();
    // Invalidating owners reset controls; stale finally blocks never own them.
    for (const button of [saveButton, groupButton, urlEditSave, groupEditSave]) {
      button.disabled = false;
    }
  }

  function clear(): void {
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
    dispose(): void {
      if (disposed) return;
      disposed = true;
      clear();
      listeners.abort();
      for (const unsubscribe of unsubscribeDialogs) unsubscribe();
    },
  };
}
