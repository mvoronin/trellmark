import { h } from "../../shared/dom.js";
import { icon } from "../../shared/icons.js";
import { displayUrl, formatAdded, hostFor } from "../../shared/format.js";
import { sortUrls, treeGroups, visibleGroups } from "./model.js";
export function renderGroups(root, count, groups, ui, options) {
    const visibleUrlIds = new Set();
    root.replaceChildren();
    for (const group of visibleGroups(groups, ui.safeMode)) {
        root.append(renderGroup(group, 1, visibleUrlIds, ui, groups, options));
    }
    count.textContent = `${visibleUrlIds.size} saved`;
}
export function optionLabel(group, depth) {
    return `${"\u2007\u2007".repeat(Math.max(0, depth - 1))}${depth > 1 ? "— " : ""}${group.name}`;
}
// Hues for the group edge marker. The stylesheet pins lightness and chroma for
// every accent, so hue is the only thing that varies here and no group's marker
// outweighs another's. Keyed on the group id rather than the render index so a
// group keeps its color when the list is filtered or reordered.
const GROUP_HUES = [27, 75, 135, 245];
export function renderGroup(group, depth, visibleUrlIds, ui, groups, options) {
    const section = h("section", {
        className: "group",
        data: {
            depth: String(depth),
            parentId: group.parent_id === null ? "" : String(group.parent_id),
        },
        style: { "--group-hue": String(GROUP_HUES[group.id % GROUP_HUES.length]) },
    });
    const header = h("div", {
        className: "group-header",
        data: { groupId: String(group.id), parentId: group.parent_id === null ? "" : String(group.parent_id) },
    });
    const name = h("h2", { className: "group-name" }, [group.name]);
    const groupCount = h("span", { className: "group-count" }, [String(group.urls.length)]);
    const domains = h("span", {
        className: "group-domains",
        title: group.domains.join("\n"),
    }, [group.domains.join(", ")]);
    const nsfwBadge = h("span", { className: "group-nsfw-badge" }, ["NSFW"]);
    const content = h("div", { className: "group-content", id: `group-content-${group.id}` });
    const urls = h("ul", { className: "group-urls" });
    const isFolded = ui.foldedGroupIds.has(group.id);
    content.hidden = isFolded;
    const toggle = h("button", {
        type: "button",
        className: "fold-toggle",
        aria: { label: `Toggle ${group.name}`, expanded: !isFolded, controls: content.id },
    });
    toggle.addEventListener("click", () => options.actions.toggleFold?.(group.id, toggle, content));
    options.actions.makeHeaderDraggable?.(header, section, group.id, group.parent_id);
    header.append(toggle, name);
    if (group.nsfw) {
        header.append(nsfwBadge);
    }
    header.append(groupCount);
    if (group.domains.length > 0) {
        header.append(domains);
    }
    if (!isDefaultGroup(group)) {
        const edit = h("button", {
            type: "button", className: "group-action group-edit", aria: { label: `Edit ${group.name}` },
        }, [icon("pencil")]);
        edit.addEventListener("click", () => options.actions.showGroupEditor?.(group));
        const remove = h("button", {
            type: "button", className: "group-action group-delete", aria: { label: `Delete group ${group.name}` },
        }, [icon("trash-2")]);
        remove.addEventListener("click", () => options.actions.requestGroupDelete?.(group));
        const actions = h("div", { className: "group-actions" }, [edit, remove]);
        header.append(actions);
    }
    if (group.urls.length === 0) {
        const empty = h("li", { className: "group-empty" }, ["No URLs yet."]);
        urls.append(empty);
    }
    else {
        for (const record of sortUrls(group.urls, ui.sortMode)) {
            visibleUrlIds.add(record.id);
            urls.append(renderUrlItem(record, group, groups, options));
        }
    }
    content.append(urls);
    const visibleChildren = depth < 3
        ? visibleGroups(group.children, ui.safeMode)
        : [];
    if (visibleChildren.length > 0) {
        const children = h("div", { className: "group-children" });
        for (const child of visibleChildren) {
            children.append(renderGroup(child, depth + 1, visibleUrlIds, ui, groups, options));
        }
        content.append(children);
    }
    section.append(header, content);
    return section;
}
function isDefaultGroup(group) {
    return group.name.toLocaleLowerCase() === "default";
}
export function renderUrlItem(record, currentGroup, groups, options) {
    const { id, url, title, created_at, important } = record;
    const currentGroupId = currentGroup.id;
    const displayedUrl = displayUrl(url);
    const item = h("li", { className: "url-item" });
    const link = h("a", {
        href: url,
        target: "_blank",
        rel: "noopener noreferrer",
    }, [title || displayUrl(url)]);
    const added = formatAdded(created_at);
    const meta = h("span", {}, [added ? `${hostFor(url)} · ${added}` : hostFor(url)]);
    const text = h("div", { className: "url-text" }, [link, meta]);
    const main = h("div", { className: "url-main" }, [renderSiteIcon(record, options.iconSource), text]);
    const importantToggle = h("button", {
        type: "button",
        className: "important-toggle",
        aria: { pressed: important, label: `Mark ${displayedUrl} important in ${currentGroup.name}` },
    }, [icon("star")]);
    if (important) {
        importantToggle.classList.add("is-important");
    }
    importantToggle.addEventListener("click", () => options.actions.toggleImportant?.(id, !important, importantToggle));
    const edit = h("button", {
        type: "button", className: "url-action edit-url-button",
        aria: { label: `Edit ${displayedUrl} in ${currentGroup.name}` },
    }, [icon("pencil")]);
    edit.addEventListener("click", () => options.actions.showUrlEditor?.(record));
    const refreshMetadata = h("button", {
        type: "button", className: "url-action refresh-metadata-button",
        aria: { label: `Refresh title and icon for ${displayedUrl} in ${currentGroup.name}` },
    }, [icon("refresh-cw")]);
    refreshMetadata.addEventListener("click", () => options.actions.refreshMetadataForUrl?.(record, refreshMetadata));
    const move = h("select", {
        className: "move-select", aria: { label: `Move ${displayedUrl} from ${currentGroup.name}` },
    });
    for (const { group, depth } of treeGroups(groups)) {
        const option = h("option", { value: String(group.id) }, [optionLabel(group, depth)]);
        move.append(option);
    }
    move.value = String(currentGroupId);
    move.addEventListener("change", () => options.actions.moveUrl?.(id, currentGroupId, Number(move.value), move));
    const remove = h("button", {
        type: "button", className: "delete-button",
        aria: { label: `Delete ${displayedUrl} from ${currentGroup.name}` },
    }, [icon("trash-2")]);
    remove.addEventListener("click", () => options.actions.requestDelete?.(record, currentGroupId, remove));
    const controls = h("div", { className: "url-controls" }, [importantToggle, edit, refreshMetadata, move, remove]);
    item.append(main, controls);
    return item;
}
export function renderSiteIcon(record, iconSource) {
    const frame = h("span", { className: "site-icon", aria: { hidden: true } });
    const placeholder = h("span", { className: "site-icon-placeholder" });
    const image = h("img", {
        className: "site-icon-image", src: iconSource(record), alt: "", loading: "lazy", decoding: "async",
    });
    image.addEventListener("load", () => frame.classList.add("is-loaded"));
    image.addEventListener("error", () => {
        image.hidden = true;
        frame.classList.add("is-unavailable");
    });
    frame.append(placeholder, image);
    return frame;
}
