import { createBookmarksModel, createBookmarkEditors, createBookmarkDrag, renderGroups } from "../features/bookmarks/index.js";
import { createDialogs, createStatusView, createShellView } from "../shell/index.js";
import { h } from "../shared/dom.js";
import { createRequestLifetime } from "../shared/request.js";
import { createThemeControls } from "../shared/theme.js";
import { createFixtureApi, initialGroups, rareExamples, localIconSource } from "./fixtures.js";
const root = document.body;
const dialogs = createDialogs(root);
const { setStatus } = createStatusView(root);
const theme = createThemeControls(document.documentElement);
const model = createBookmarksModel();
const privateLifetime = createRequestLifetime();
const examples = rareExamples();
const rareGroups = new Map(examples.flatMap(example => example.groups).map(group => [group.id, group]));
const api = createFixtureApi([...initialGroups(), ...rareGroups.values()]);
const groupsRoot = required("#groups");
const count = required("#url-count");
const sort = required("#sort-select");
let ready = Promise.resolve();
function required(selector) {
    const element = root.querySelector(selector);
    if (!element)
        throw new Error(`Missing design element: ${selector}`);
    return element;
}
const sections = examples.map(example => {
    const section = h("section", { className: "design-section" });
    section.dataset.designState = example.id;
    const heading = h("h2", { id: `design-${example.id}-title` }, [example.title]);
    section.setAttribute("aria-labelledby", heading.id);
    const groups = h("div", { className: "groups" });
    const savedCount = h("p");
    section.append(heading, h("p", {}, [example.description]), savedCount, groups);
    required("#design-examples").append(section);
    for (const id of example.folded ?? [])
        model.foldGroup(id);
    return { example, groups, savedCount };
});
const editors = createBookmarkEditors(root, {
    model, api, dialogs, status: setStatus, privateLifetime,
    refresh: { replace, render, load, ready: () => ready },
});
const drag = createBookmarkDrag(groupsRoot, {
    model, privateLifetime, reorder: api.reorderGroups, replace, load, status: setStatus,
});
function replace(groups) {
    drag.cancel();
    model.replaceGroups(groups);
    render();
}
async function load() {
    replace((await api.listGroups()).groups);
}
function toggleFold(id, button, content) {
    if (model.ui.foldedGroupIds.has(id))
        model.unfoldGroup(id);
    else
        model.foldGroup(id);
    content.hidden = model.ui.foldedGroupIds.has(id);
    button.setAttribute("aria-expanded", String(!content.hidden));
}
// The modal markup exists once. Repeated read-only trees namespace only their
// generated IDs and corresponding references after the real view creates them.
function prefixIds(owner, prefix) {
    const ids = new Map(Array.from(owner.querySelectorAll("[id]"), node => [node.id, `${prefix}-${node.id}`]));
    for (const node of owner.querySelectorAll("[id]"))
        node.id = ids.get(node.id);
    for (const node of owner.querySelectorAll("[for], [aria-controls], [aria-labelledby], [aria-describedby]")) {
        for (const attribute of ["for", "aria-controls", "aria-labelledby", "aria-describedby"]) {
            const value = node.getAttribute(attribute);
            if (value)
                node.setAttribute(attribute, value.split(" ").map(id => ids.get(id) ?? id).join(" "));
        }
    }
}
function render() {
    const options = { iconSource: (record) => localIconSource(record.id),
        actions: { ...editors, toggleFold } };
    renderGroups(groupsRoot, count, model.server.groups.filter(group => !rareGroups.has(group.id)), model.ui, { ...options, actions: { ...options.actions, makeHeaderDraggable: drag.makeHeaderDraggable } });
    for (const { example, groups, savedCount } of sections) {
        const ids = new Set(example.groups.map(group => group.id));
        renderGroups(groups, savedCount, model.server.groups.filter(group => ids.has(group.id)), { ...model.ui, safeMode: example.safeMode ?? false }, options);
        prefixIds(groups, `design-${example.id}`);
        if (example.id === "drag") {
            groups.querySelector(".group")?.classList.add("dragging");
            groups.querySelectorAll(".group-header")[1]?.classList.add("drag-over");
        }
    }
    editors.syncCreateParentSelect();
}
sort.addEventListener("change", () => { model.setSortMode(sort.value); render(); });
const filters = root.querySelectorAll(".group-filter [data-group-filter]");
for (const button of filters) {
    button.addEventListener("click", () => {
        model.setSafeMode(button.dataset.groupFilter !== "all");
        for (const candidate of filters)
            candidate.setAttribute("aria-pressed", String(candidate === button));
        render();
    });
}
const launches = [
    ["url-edit-dialog", "URL editor", () => {
            const group = editorExample();
            if (!group)
                return;
            editors.showUrlEditor(group.urls[0]);
            required("#url-edit-status").textContent = "Example conflict. Your draft is preserved; try saving again.";
        }],
    ["group-edit-dialog", "Group editor", () => {
            const group = editorExample();
            if (!group)
                return;
            editors.showGroupEditor(group);
            required("#group-edit-status").textContent = "Example validation error. Choose another group name.";
        }],
    ["confirm-dialog", "URL deletion confirmation", (button) => {
            const group = editorExample();
            if (!group)
                return;
            required("#delete-immediately").checked = false;
            void editors.requestDelete(group.urls[0], group.id, button);
        }],
    ["group-delete-dialog", "Group deletion confirmation", () => {
            const group = editorExample();
            if (group)
                void editors.requestGroupDelete(group);
        }],
];
function editorExample() {
    const group = model.server.groups.find(group => group.name !== "default" && group.urls.length > 0);
    if (!group)
        setStatus("Reload to restore the deleted dialog examples.");
    return group;
}
for (const [id, label, open] of launches) {
    const button = h("button", { type: "button" }, [label]);
    button.dataset.designDialog = id;
    button.addEventListener("click", () => open(button));
    required("#design-dialog-actions").append(button);
}
const shellView = createShellView(root);
shellView.showLogin("Example sign-in error. This preview does not authenticate.");
required("#app-view").hidden = false;
required("#session-pending").hidden = false;
shellView.form.addEventListener("submit", event => {
    event.preventDefault();
    shellView.passwordInput.value = "";
    shellView.status.textContent = "Example sign-in error. Nothing was sent or stored.";
});
shellView.logoutButton.addEventListener("click", () => setStatus("Example session ended. This overview remains local."));
required("#export-button").addEventListener("click", () => setStatus("Example export ready.", "is-success"));
const retry = required("#import-retry-button");
retry.hidden = false;
retry.disabled = false;
retry.addEventListener("click", () => setStatus("Example import failed. You can retry without losing this view.", "is-error"));
required("#import-input").addEventListener("change", event => {
    event.target.value = "";
    setStatus("Example import failed. No file contents were read.", "is-error");
});
ready = load();
void ready.then(() => {
    setStatus("Example saved successfully.", "is-success");
    editors.setGroupStatus("Example error. Your current groups are preserved.", "is-error");
});
// Example links retain their real URL presentation without leaving this local
// component page or making a remote request on activation.
function keepExampleNavigationLocal(event) {
    if (event.target instanceof Element && event.target.closest(".url-text a")) {
        event.preventDefault();
        setStatus("Example link selected. Navigation stays on this page.");
    }
}
root.addEventListener("click", keepExampleNavigationLocal);
root.addEventListener("auxclick", keepExampleNavigationLocal);
window.addEventListener("pagehide", () => {
    privateLifetime.invalidate();
    drag.dispose();
    editors.dispose();
    dialogs.dispose();
    theme.dispose();
}, { once: true });
