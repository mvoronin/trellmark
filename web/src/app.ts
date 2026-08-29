import {
  clearSession as apiClearSession,
  createGroup as apiCreateGroup,
  createUrl as apiCreateUrl,
  deleteGroup as apiDeleteGroup,
  deleteUrlById as apiDeleteUrlById,
  editGroup as apiEditGroup,
  editUrl as apiEditUrl,
  exportData as apiExportData,
  importData as apiImportData,
  establishSession as apiEstablishSession,
  getSession as apiGetSession,
  listGroups as apiListGroups,
  login as apiLogin,
  logout as apiLogout,
  moveUrlToGroup as apiMoveUrlToGroup,
  reorderGroups as apiReorderGroups,
  refreshUrlMetadata as apiRefreshUrlMetadata,
  setUrlImportant as apiSetUrlImportant,
  siteIconPath,
  setSessionExpiredHandler,
  type GroupRecord,
  type UrlRecord,
} from "./api.js";


type Theme = "system" | "light" | "dark";
type SortMode = "added-desc" | "added-asc" | "domain";
type GroupFilter = "safe" | "all";
type GroupDeleteAction = "delete" | "move_to_default";
type IconName = "pencil" | "refresh-cw" | "star" | "trash-2";

interface DropTarget {
  groupId: number;
  before: boolean;
}

interface ActiveDrag {
  groupId: number;
  parentId: number | null;
  header: HTMLElement;
  section: HTMLElement;
  pointerId: number;
  startX: number;
  startY: number;
  moving: boolean;
  dropTarget: DropTarget | null;
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return element;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error.";
}

const groupsContainer = requiredElement<HTMLDivElement>("#groups");
const pendingView = requiredElement<HTMLElement>("#session-pending");
const loginView = requiredElement<HTMLElement>("#login-view");
const appView = requiredElement<HTMLDivElement>("#app-view");
const loginForm = requiredElement<HTMLFormElement>("#login-form");
const loginInput = requiredElement<HTMLInputElement>("#login-input");
const passwordInput = requiredElement<HTMLInputElement>("#password-input");
const loginButton = requiredElement<HTMLButtonElement>("#login-button");
const loginStatus = requiredElement<HTMLParagraphElement>("#login-status");
const logoutButton = requiredElement<HTMLButtonElement>("#logout-button");
const form = requiredElement<HTMLFormElement>("#url-form");
const input = requiredElement<HTMLInputElement>("#url-input");
const saveButton = requiredElement<HTMLButtonElement>("#save-button");
const statusMessage = requiredElement<HTMLParagraphElement>("#form-status");
const count = requiredElement<HTMLParagraphElement>("#url-count");
const deleteImmediately = requiredElement<HTMLInputElement>("#delete-immediately");
const confirmDialog = requiredElement<HTMLDialogElement>("#confirm-dialog");
const confirmUrl = requiredElement<HTMLParagraphElement>("#confirm-url");
const sortSelect = requiredElement<HTMLSelectElement>("#sort-select");
const groupForm = requiredElement<HTMLFormElement>("#group-form");
const groupInput = requiredElement<HTMLInputElement>("#group-input");
const groupDomains = requiredElement<HTMLInputElement>("#group-domains");
const groupParent = requiredElement<HTMLSelectElement>("#group-parent");
const groupNsfw = requiredElement<HTMLInputElement>("#group-nsfw");
const groupButton = requiredElement<HTMLButtonElement>("#group-button");
const groupStatus = requiredElement<HTMLParagraphElement>("#group-status");
const exportButton = requiredElement<HTMLButtonElement>("#export-button");
const importInput = requiredElement<HTMLInputElement>("#import-input");
const urlClear = requiredElement<HTMLButtonElement>("#url-clear");
const groupFilterButtons = document.querySelectorAll<HTMLButtonElement>(
  ".group-filter [data-group-filter]",
);
const groupEditDialog = requiredElement<HTMLDialogElement>("#group-edit-dialog");
const groupEditForm = requiredElement<HTMLFormElement>("#group-edit-form");
const groupEditName = requiredElement<HTMLInputElement>("#group-edit-name");
const groupEditNsfw = requiredElement<HTMLInputElement>("#group-edit-nsfw");
const groupEditDomains = requiredElement<HTMLTextAreaElement>("#group-edit-domains");
const groupEditParent = requiredElement<HTMLSelectElement>("#group-edit-parent");
const groupEditStatus = requiredElement<HTMLParagraphElement>("#group-edit-status");
const groupEditCancel = requiredElement<HTMLButtonElement>("#group-edit-cancel");
const groupEditSave = requiredElement<HTMLButtonElement>("#group-edit-save");
const groupDeleteDialog = requiredElement<HTMLDialogElement>("#group-delete-dialog");
const groupDeleteName = requiredElement<HTMLSpanElement>("#group-delete-name");
const groupDeleteSummary = requiredElement<HTMLParagraphElement>(
  "#group-delete-summary",
);
const urlEditDialog = requiredElement<HTMLDialogElement>("#url-edit-dialog");
const urlEditForm = requiredElement<HTMLFormElement>("#url-edit-form");
const urlEditTitle = requiredElement<HTMLInputElement>("#url-edit-title");
const urlEditUrl = requiredElement<HTMLInputElement>("#url-edit-url");
const urlEditStatus = requiredElement<HTMLParagraphElement>("#url-edit-status");
const urlEditCancel = requiredElement<HTMLButtonElement>("#url-edit-cancel");
const urlEditSave = requiredElement<HTMLButtonElement>("#url-edit-save");

// Build an <svg> that references a vendored Lucide symbol in the sprite. Kept as
// DOM (createElementNS) rather than parsed markup strings.
// Color/size come from the .icon CSS via currentColor.
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon icon-${name}`);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `/static/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

// Theme: "system" follows the OS (no data-theme attribute, color-scheme does
// the work); "light"/"dark" pin it via the attribute and persist in storage.
const themeButtons = document.querySelectorAll<HTMLButtonElement>(
  ".theme-toggle [data-theme-value]",
);

function normalizeTheme(value: string | undefined | null): Theme {
  return value === "light" || value === "dark" ? value : "system";
}

function applyTheme(theme: Theme): void {
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
    theme = "system";
  }
  for (const button of themeButtons) {
    const active = button.dataset.themeValue === theme;
    button.setAttribute("aria-pressed", String(active));
  }
}

function storedTheme(): Theme {
  try {
    return normalizeTheme(localStorage.getItem("theme"));
  } catch {
    return "system";
  }
}

for (const button of themeButtons) {
  button.addEventListener("click", () => {
    const theme = normalizeTheme(button.dataset.themeValue);
    try {
      if (theme === "system") {
        localStorage.removeItem("theme");
      } else {
        localStorage.setItem("theme", theme);
      }
    } catch {}
    applyTheme(theme);
  });
}

applyTheme(storedTheme());

function hostFor(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function parseDomains(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((domain) => domain.trim())
    .filter((domain) => domain !== "");
}

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatAdded(createdAt: string): string {
  const date = new Date(createdAt);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}

// The API returns groups ordered by position, each holding URLs in insertion
// order (oldest first). Keep the last loaded set so the sort control can
// re-render each group without a round trip.
let groups: GroupRecord[] = [];
let groupFilter: GroupFilter = "safe";
let initialGroupsLoad: Promise<void> = Promise.resolve();

// Fold state is a browser-only preference: the set of folded group ids, kept
// in localStorage. Groups absent from the set render unfolded, so newly created
// groups start open.
const FOLDED_KEY = "foldedGroups";

function loadFoldedGroupIds(): Set<number> {
  try {
    const ids: unknown = JSON.parse(localStorage.getItem(FOLDED_KEY) ?? "[]");
    return new Set(
      Array.isArray(ids) ? ids.filter((id): id is number => Number.isInteger(id)) : [],
    );
  } catch {
    return new Set();
  }
}

function saveFoldedGroupIds(): void {
  try {
    localStorage.setItem(FOLDED_KEY, JSON.stringify([...folded]));
  } catch {}
}

const folded = loadFoldedGroupIds();

function sortUrls(urls: UrlRecord[]): UrlRecord[] {
  const mode = sortSelect.value as SortMode;
  if (mode === "domain") {
    return [...urls].sort((a, b) =>
      hostFor(a.url).localeCompare(hostFor(b.url)) ||
      a.created_at.localeCompare(b.created_at)
    );
  }
  if (mode === "added-desc") {
    return [...urls].reverse();
  }
  return urls; // added-asc: oldest first, the order the API returns
}

interface TreeGroup {
  group: GroupRecord;
  depth: number;
}

// The server owns the tree. This bounded walk only presents that tree in DOM
// order and protects rendering if a malformed response contains deeper nodes.
function treeGroups(source: GroupRecord[] = groups): TreeGroup[] {
  const result: TreeGroup[] = [];
  const walk = (nodes: GroupRecord[], depth: number): void => {
    if (depth > 3) {
      return;
    }
    for (const group of nodes) {
      result.push({ group, depth });
      walk(group.children, depth + 1);
    }
  };
  walk(source, 1);
  return result;
}

function optionLabel(group: GroupRecord, depth: number): string {
  return `${"\u2007\u2007".repeat(Math.max(0, depth - 1))}${depth > 1 ? "— " : ""}${group.name}`;
}

function fillGroupSelect(
  select: HTMLSelectElement,
  candidates: TreeGroup[],
  selectedId: number | null,
): void {
  const root = document.createElement("option");
  root.value = "";
  root.textContent = "Root";
  select.replaceChildren(root);
  for (const { group, depth } of candidates) {
    const option = document.createElement("option");
    option.value = String(group.id);
    option.textContent = optionLabel(group, depth);
    select.append(option);
  }
  // An id with no matching option leaves the select on selectedIndex -1, where
  // it renders blank and reads back as "" — indistinguishable from a deliberate
  // "Root". Callers check for that state instead of trusting `.value`.
  select.value = selectedId === null ? "" : String(selectedId);
}

function syncCreateParentSelect(): void {
  const pending = groupParent.value === "" ? null : Number(groupParent.value);
  fillGroupSelect(
    groupParent,
    treeGroups().filter(({ depth }) => depth < 3),
    pending,
  );
  if (groupParent.selectedIndex === -1) {
    // The pending parent is gone, or can no longer hold a child. Falling back to
    // the root is harmless for a group that does not exist yet, and showing
    // "Root" beats leaving the control blank.
    groupParent.value = "";
  }
}

function renderGroups(nextGroups: GroupRecord[]): void {
  groups = nextGroups;
  const visibleUrlIds = new Set<number>();
  groupsContainer.replaceChildren();

  for (const group of groups) {
    if (groupFilter === "all" || !group.nsfw) {
      groupsContainer.append(renderGroup(group, 1, visibleUrlIds));
    }
  }
  count.textContent = `${visibleUrlIds.size} saved`;
  syncCreateParentSelect();
}

// Hues for the group edge marker. The stylesheet pins lightness and chroma for
// every accent, so hue is the only thing that varies here and no group's marker
// outweighs another's. Keyed on the group id rather than the render index so a
// group keeps its color when the list is filtered or reordered.
const GROUP_HUES = [27, 75, 135, 245];

function renderGroup(
  group: GroupRecord,
  depth: number,
  visibleUrlIds: Set<number>,
): HTMLElement {
  const section = document.createElement("section");
  section.className = "group";
  section.dataset.depth = String(depth);
  section.dataset.parentId = group.parent_id === null ? "" : String(group.parent_id);
  section.style.setProperty(
    "--group-hue",
    String(GROUP_HUES[group.id % GROUP_HUES.length]),
  );

  const header = document.createElement("div");
  header.className = "group-header";

  const name = document.createElement("h2");
  name.className = "group-name";
  name.textContent = group.name;

  const groupCount = document.createElement("span");
  groupCount.className = "group-count";
  groupCount.textContent = String(group.urls.length);

  const domains = document.createElement("span");
  domains.className = "group-domains";
  domains.textContent = group.domains.join(", ");
  domains.title = group.domains.join("\n");

  const nsfwBadge = document.createElement("span");
  nsfwBadge.className = "group-nsfw-badge";
  nsfwBadge.textContent = "NSFW";

  const content = document.createElement("div");
  content.className = "group-content";
  content.id = `group-content-${group.id}`;

  const urls = document.createElement("ul");
  urls.className = "group-urls";

  const isFolded = folded.has(group.id);
  content.hidden = isFolded;

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "fold-toggle";
  toggle.setAttribute("aria-label", `Toggle ${group.name}`);
  toggle.setAttribute("aria-expanded", String(!isFolded));
  toggle.setAttribute("aria-controls", content.id);
  toggle.addEventListener("click", () => toggleFold(group.id, toggle, content));

  makeHeaderDraggable(header, section, group.id, group.parent_id);

  header.append(toggle, name);
  if (group.nsfw) {
    header.append(nsfwBadge);
  }
  header.append(groupCount);
  if (group.domains.length > 0) {
    header.append(domains);
  }

  if (!isDefaultGroup(group)) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "group-action group-edit";
    edit.setAttribute("aria-label", `Edit ${group.name}`);
    edit.append(icon("pencil"));
    edit.addEventListener("click", () => showGroupEditor(group));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "group-action group-delete";
    remove.setAttribute("aria-label", `Delete group ${group.name}`);
    remove.append(icon("trash-2"));
    remove.addEventListener("click", () => requestGroupDelete(group));

    const actions = document.createElement("div");
    actions.className = "group-actions";
    actions.append(edit, remove);
    header.append(actions);
  }

  if (group.urls.length === 0) {
    const empty = document.createElement("li");
    empty.className = "group-empty";
    empty.textContent = "No URLs yet.";
    urls.append(empty);
  } else {
    for (const record of sortUrls(group.urls)) {
      visibleUrlIds.add(record.id);
      urls.append(renderUrlItem(record, group));
    }
  }

  content.append(urls);
  const visibleChildren = depth < 3
    ? group.children.filter((child) => groupFilter === "all" || !child.nsfw)
    : [];
  if (visibleChildren.length > 0) {
    const children = document.createElement("div");
    children.className = "group-children";
    for (const child of visibleChildren) {
      children.append(renderGroup(child, depth + 1, visibleUrlIds));
    }
    content.append(children);
  }

  section.append(header, content);
  return section;
}

function isDefaultGroup(group: GroupRecord): boolean {
  return group.name.toLocaleLowerCase() === "default";
}

function toggleFold(
  groupId: number,
  toggle: HTMLButtonElement,
  content: HTMLDivElement,
): void {
  if (folded.has(groupId)) {
    folded.delete(groupId);
  } else {
    folded.add(groupId);
  }

  const isFolded = folded.has(groupId);
  content.hidden = isFolded;
  toggle.setAttribute("aria-expanded", String(!isFolded));
  saveFoldedGroupIds();
}

// Reorder groups by dragging a title line. This uses Pointer Events rather than
// the HTML5 drag-and-drop API so a single path works for mouse, touch (Android),
// and pen. The header carries touch-action: none so a touch-drag reorders
// instead of scrolling the page.
const DRAG_THRESHOLD = 6; // px of movement before a press becomes a drag
let activeDrag: ActiveDrag | null = null;

function makeHeaderDraggable(
  header: HTMLElement,
  section: HTMLElement,
  groupId: number,
  parentId: number | null,
): void {
  header.dataset.groupId = String(groupId);
  header.dataset.parentId = parentId === null ? "" : String(parentId);
  header.addEventListener("pointerdown", (event) => {
    // Only the primary button/finger, and never when the press starts on the
    // fold toggle (that stays a plain click).
    if (activeDrag || event.button !== 0 || !event.isPrimary) {
      return;
    }
    if (event.target instanceof Element && event.target.closest("button")) {
      return;
    }

    activeDrag = {
      groupId,
      parentId,
      header,
      section,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moving: false,
      dropTarget: null,
    };
    header.setPointerCapture(event.pointerId);
    header.addEventListener("pointermove", onDragMove);
    header.addEventListener("pointerup", onDragEnd);
    header.addEventListener("pointercancel", onDragCancel);
  });
}

function onDragMove(event: PointerEvent): void {
  if (!activeDrag || event.pointerId !== activeDrag.pointerId) {
    return;
  }

  if (!activeDrag.moving) {
    const distance = Math.hypot(
      event.clientX - activeDrag.startX,
      event.clientY - activeDrag.startY,
    );
    if (distance < DRAG_THRESHOLD) {
      return;
    }
    activeDrag.moving = true;
    activeDrag.section.classList.add("dragging");
  }

  event.preventDefault();
  updateDropTarget(event.clientX, event.clientY);
}

function updateDropTarget(x: number, y: number): void {
  if (!activeDrag) {
    return;
  }
  const drag = activeDrag;
  clearDropIndicators();

  const element = document.elementFromPoint(x, y);
  const targetHeader = element?.closest<HTMLElement>(".group-header") ?? null;
  if (!targetHeader || targetHeader === drag.header) {
    drag.dropTarget = null;
    return;
  }

  const targetParentId = targetHeader.dataset.parentId === ""
    ? null
    : Number(targetHeader.dataset.parentId);
  if (targetParentId !== drag.parentId) {
    drag.dropTarget = null;
    return;
  }

  const rect = targetHeader.getBoundingClientRect();
  const before = y < rect.top + rect.height / 2;
  drag.dropTarget = { groupId: Number(targetHeader.dataset.groupId), before };
  targetHeader.classList.add("drag-over");
}

function onDragEnd(event: PointerEvent): void {
  if (!activeDrag || event.pointerId !== activeDrag.pointerId) {
    return;
  }

  const { moving, groupId, parentId, dropTarget } = activeDrag;
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
  if (!activeDrag || event.pointerId !== activeDrag.pointerId) {
    return;
  }
  finishDrag();
}

function finishDrag(): void {
  if (!activeDrag) {
    return;
  }
  const { header, section, pointerId } = activeDrag;
  header.removeEventListener("pointermove", onDragMove);
  header.removeEventListener("pointerup", onDragEnd);
  header.removeEventListener("pointercancel", onDragCancel);
  if (header.hasPointerCapture(pointerId)) {
    header.releasePointerCapture(pointerId);
  }
  section.classList.remove("dragging");
  clearDropIndicators();
  activeDrag = null;
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
  const current = treeGroups()
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
  setStatus("");

  try {
    const data = await apiReorderGroups({
      parent_id: parentId,
      group_ids: orderedIds,
    });
    renderGroups(data.groups);
    setStatus("Reordered.", "is-success");
  } catch (error) {
    setStatus(errorMessage(error), "is-error");
    // Drop the optimistic move and show the server's current order.
    loadGroups().catch((loadError: unknown) =>
      setStatus(errorMessage(loadError), "is-error"),
    );
  }
}

function renderUrlItem(record: UrlRecord, currentGroup: GroupRecord): HTMLLIElement {
  const { id, url, title, created_at, important } = record;
  const currentGroupId = currentGroup.id;
  const displayedUrl = displayUrl(url);

  const item = document.createElement("li");
  item.className = "url-item";

  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = title || displayUrl(url);

  const meta = document.createElement("span");
  const added = formatAdded(created_at);
  meta.textContent = added ? `${hostFor(url)} · ${added}` : hostFor(url);

  const text = document.createElement("div");
  text.className = "url-text";
  text.append(link, meta);

  const main = document.createElement("div");
  main.className = "url-main";
  main.append(renderSiteIcon(id), text);

  const importantToggle = document.createElement("button");
  importantToggle.type = "button";
  importantToggle.className = "important-toggle";
  if (important) {
    importantToggle.classList.add("is-important");
  }
  importantToggle.setAttribute("aria-pressed", String(important));
  importantToggle.setAttribute(
    "aria-label",
    `Mark ${displayedUrl} important in ${currentGroup.name}`,
  );
  importantToggle.append(icon("star"));
  importantToggle.addEventListener("click", () =>
    toggleImportant(id, !important, importantToggle)
  );

  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "url-action edit-url-button";
  edit.setAttribute(
    "aria-label",
    `Edit ${displayedUrl} in ${currentGroup.name}`,
  );
  edit.append(icon("pencil"));
  edit.addEventListener("click", () => showUrlEditor(record));

  const refreshMetadata = document.createElement("button");
  refreshMetadata.type = "button";
  refreshMetadata.className = "url-action refresh-metadata-button";
  refreshMetadata.setAttribute(
    "aria-label",
    `Refresh title and icon for ${displayedUrl} in ${currentGroup.name}`,
  );
  refreshMetadata.append(icon("refresh-cw"));
  refreshMetadata.addEventListener("click", () =>
    refreshMetadataForUrl(record, refreshMetadata)
  );

  const move = document.createElement("select");
  move.className = "move-select";
  move.setAttribute(
    "aria-label",
    `Move ${displayedUrl} from ${currentGroup.name}`,
  );
  for (const { group, depth } of treeGroups()) {
    const option = document.createElement("option");
    option.value = String(group.id);
    option.textContent = optionLabel(group, depth);
    move.append(option);
  }
  move.value = String(currentGroupId);
  move.addEventListener("change", () =>
    moveUrl(id, currentGroupId, Number(move.value), move)
  );

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "delete-button";
  remove.append(icon("trash-2"));
  remove.setAttribute(
    "aria-label",
    `Delete ${displayedUrl} from ${currentGroup.name}`,
  );
  remove.addEventListener("click", () =>
    requestDelete(record, currentGroupId, remove)
  );

  const controls = document.createElement("div");
  controls.className = "url-controls";
  controls.append(importantToggle, edit, refreshMetadata, move, remove);

  item.append(main, controls);
  return item;
}

function renderSiteIcon(urlId: number): HTMLSpanElement {
  const frame = document.createElement("span");
  frame.className = "site-icon";
  frame.setAttribute("aria-hidden", "true");

  const placeholder = document.createElement("span");
  placeholder.className = "site-icon-placeholder";

  const image = document.createElement("img");
  image.className = "site-icon-image";
  image.src = siteIconPath(urlId);
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("load", () => frame.classList.add("is-loaded"));
  image.addEventListener("error", () => {
    image.hidden = true;
    frame.classList.add("is-unavailable");
  });

  frame.append(placeholder, image);
  return frame;
}

sortSelect.addEventListener("change", () => renderGroups(groups));

for (const button of groupFilterButtons) {
  button.addEventListener("click", () => {
    groupFilter = button.dataset.groupFilter === "all" ? "all" : "safe";
    for (const candidate of groupFilterButtons) {
      candidate.setAttribute(
        "aria-pressed",
        String(candidate.dataset.groupFilter === groupFilter),
      );
    }
    renderGroups(groups);
  });
}

function setStatus(message: string, state = ""): void {
  statusMessage.textContent = message;
  statusMessage.className = state;
}

function setGroupStatus(message: string, state = ""): void {
  groupStatus.textContent = message;
  groupStatus.className = state;
}

function downloadFilename(contentDisposition: string | null): string {
  const match = contentDisposition?.match(/filename="?([^"]+)"?/);
  return match?.[1] || "trellmark-export.json";
}

async function exportData(): Promise<void> {
  setStatus("");
  exportButton.disabled = true;

  try {
    const response = await apiExportData();
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = downloadFilename(response.headers.get("Content-Disposition"));
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    setStatus("Exported.", "is-success");
  } catch (error) {
    setStatus(errorMessage(error), "is-error");
  } finally {
    exportButton.disabled = false;
  }
}

async function importData(file: File): Promise<void> {
  setStatus("");
  importInput.disabled = true;

  try {
    await initialGroupsLoad.catch(() => undefined);
    const data = await apiImportData(await file.text());
    renderGroups(data.groups);
    setStatus(`Imported ${data.imported}, skipped ${data.skipped}.`, "is-success");
  } catch (error) {
    setStatus(errorMessage(error), "is-error");
  } finally {
    importInput.value = "";
    importInput.disabled = false;
  }
}

exportButton.addEventListener("click", exportData);
importInput.addEventListener("change", () => {
  const [file] = importInput.files ?? [];
  if (file) {
    importData(file);
  }
});

async function loadGroups(): Promise<void> {
  const data = await apiListGroups();
  renderGroups(data.groups);
}

function clearPrivateState(): void {
  groups = [];
  activeDrag = null;
  editingGroup = null;
  editingUrl = null;
  urlEditRequestSerial += 1;
  urlEditRequestInFlight = false;
  groupsContainer.replaceChildren();
  count.textContent = "0 saved";
  statusMessage.textContent = "";
  groupStatus.textContent = "";
  for (const dialog of [
    confirmDialog,
    groupEditDialog,
    urlEditDialog,
    groupDeleteDialog,
  ]) {
    dialog.returnValue = "";
    if (dialog.open) {
      dialog.close();
    }
  }
  form.reset();
  groupForm.reset();
  groupEditForm.reset();
  urlEditForm.reset();
  importInput.value = "";
  fillGroupSelect(groupParent, [], null);
  fillGroupSelect(groupEditParent, [], null);
  confirmUrl.textContent = "";
  groupEditStatus.textContent = "";
  urlEditStatus.textContent = "";
  groupDeleteName.textContent = "";
  groupDeleteSummary.textContent = "";
}

function showLogin(message = ""): void {
  apiClearSession();
  clearPrivateState();
  pendingView.hidden = true;
  appView.hidden = true;
  loginView.hidden = false;
  loginStatus.textContent = message;
  passwordInput.value = "";
  loginButton.disabled = false;
  queueMicrotask(() => loginInput.focus());
}

function showAuthenticated(csrfToken: string): void {
  apiEstablishSession(csrfToken);
  loginStatus.textContent = "";
  setStatus("");
  setGroupStatus("");
  passwordInput.value = "";
  pendingView.hidden = true;
  loginView.hidden = true;
  appView.hidden = false;
  initialGroupsLoad = loadGroups();
  initialGroupsLoad.catch((error: unknown) => {
    setStatus(errorMessage(error), "is-error");
  });
  queueMicrotask(() => input.focus());
}

setSessionExpiredHandler(() => {
  showLogin("Your session expired. Log in again.");
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginStatus.textContent = "";
  loginButton.disabled = true;
  try {
    const session = await apiLogin({
      login: loginInput.value,
      password: passwordInput.value,
    });
    showAuthenticated(session.csrf_token);
  } catch (error) {
    loginStatus.textContent = errorMessage(error);
    passwordInput.value = "";
    passwordInput.focus();
  } finally {
    loginButton.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  logoutButton.disabled = true;
  try {
    await apiLogout();
    showLogin();
  } catch (error) {
    setStatus(errorMessage(error), "is-error");
  } finally {
    logoutButton.disabled = false;
  }
});

async function resolveInitialSession(): Promise<void> {
  try {
    const session = await apiGetSession();
    if (session.authenticated) {
      showAuthenticated(session.csrf_token);
    } else {
      showLogin();
    }
  } catch (error) {
    showLogin(errorMessage(error));
  }
}

let editingGroup: GroupRecord | null = null;

function subtreeIds(group: GroupRecord): Set<number> {
  const ids = new Set<number>();
  const walk = (node: GroupRecord, remainingDepth: number): void => {
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

function subtreeHeight(group: GroupRecord, remainingDepth = 2): number {
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
function editParentCandidates(group: GroupRecord): TreeGroup[] {
  const excluded = subtreeIds(group);
  const height = subtreeHeight(group);
  return treeGroups().filter(
    ({ group: candidate, depth }) =>
      !excluded.has(candidate.id)
      && (candidate.id === group.parent_id || depth + height <= 3),
  );
}

// selectedIndex -1 means no option matched the group's current parent, so the
// blank control carries no intent: keep the group where it is rather than
// reading its empty value as a deliberate move to the root.
function editedParentId(group: GroupRecord): number | null {
  if (groupEditParent.selectedIndex === -1) {
    return group.parent_id;
  }
  return groupEditParent.value === "" ? null : Number(groupEditParent.value);
}

function showGroupEditor(group: GroupRecord): void {
  editingGroup = group;
  groupEditName.value = group.name;
  groupEditNsfw.checked = group.nsfw;
  groupEditDomains.value = group.domains.join("\n");
  fillGroupSelect(
    groupEditParent,
    editParentCandidates(group),
    group.parent_id,
  );
  groupEditStatus.textContent = "";
  groupEditDialog.showModal();
  groupEditName.focus();
  groupEditName.select();
}

groupEditCancel.addEventListener("click", () => groupEditDialog.close("cancel"));
groupEditDialog.addEventListener("close", () => {
  editingGroup = null;
});

groupEditForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editingGroup) {
    return;
  }

  groupEditStatus.textContent = "";
  groupEditSave.disabled = true;
  try {
    const data = await apiEditGroup(editingGroup.id, {
      name: groupEditName.value,
      nsfw: groupEditNsfw.checked,
      domains: parseDomains(groupEditDomains.value),
      parent_id: editedParentId(editingGroup),
    });
    renderGroups(data.groups);
    groupEditDialog.close("saved");
    setGroupStatus("Group updated.", "is-success");
  } catch (error) {
    groupEditStatus.textContent = errorMessage(error);
    await loadGroups().catch(() => undefined);
  } finally {
    groupEditSave.disabled = false;
  }
});

let editingUrl: UrlRecord | null = null;
let urlEditRequestSerial = 0;
let urlEditRequestInFlight = false;

function showUrlEditor(record: UrlRecord): void {
  urlEditRequestSerial += 1;
  urlEditRequestInFlight = false;
  editingUrl = record;
  urlEditTitle.value = record.title ?? "";
  urlEditUrl.value = record.url;
  urlEditStatus.textContent = "";
  urlEditSave.disabled = false;
  urlEditDialog.showModal();
  urlEditTitle.focus();
  urlEditTitle.select();
}

urlEditCancel.addEventListener("click", () => urlEditDialog.close("cancel"));
urlEditDialog.addEventListener("close", () => {
  urlEditRequestSerial += 1;
  urlEditRequestInFlight = false;
  editingUrl = null;
});

function isCurrentUrlEditRequest(requestSerial: number, urlId: number): boolean {
  return (
    requestSerial === urlEditRequestSerial &&
    editingUrl?.id === urlId &&
    urlEditDialog.open
  );
}

urlEditForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editingUrl || urlEditRequestInFlight) {
    return;
  }

  const record = editingUrl;
  const requestSerial = ++urlEditRequestSerial;
  urlEditRequestInFlight = true;
  setStatus("");
  urlEditStatus.textContent = "";
  urlEditSave.disabled = true;
  try {
    const data = await apiEditUrl(record.id, {
      title: urlEditTitle.value,
      url: urlEditUrl.value,
      version: record.version,
    });
    if (!isCurrentUrlEditRequest(requestSerial, record.id)) {
      return;
    }
    renderGroups(data.groups);
    urlEditDialog.close("saved");
    setStatus("URL updated.", "is-success");
  } catch (error) {
    if (!isCurrentUrlEditRequest(requestSerial, record.id)) {
      return;
    }
    urlEditStatus.textContent = errorMessage(error);
  } finally {
    if (isCurrentUrlEditRequest(requestSerial, record.id)) {
      urlEditRequestInFlight = false;
      urlEditSave.disabled = false;
    }
  }
});

async function refreshMetadataForUrl(
  record: UrlRecord,
  button: HTMLButtonElement,
): Promise<void> {
  setStatus("Refreshing title and icon…");
  button.disabled = true;

  try {
    const data = await apiRefreshUrlMetadata(record.id);
    renderGroups(data.groups);
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
    button.disabled = false;
    setStatus(errorMessage(error), "is-error");
  }
}

function confirmGroupDeletion(group: GroupRecord): Promise<GroupDeleteAction | null> {
  groupDeleteName.textContent = group.name;
  const linkCount = group.urls.length;
  groupDeleteSummary.textContent = linkCount === 0
    ? "This group is empty."
    : `Choose whether to preserve its ${linkCount} saved ${linkCount === 1 ? "link" : "links"}.`;
  groupDeleteDialog.returnValue = "";
  groupDeleteDialog.showModal();
  return new Promise<GroupDeleteAction | null>((resolve) => {
    groupDeleteDialog.addEventListener(
      "close",
      () => {
        const action = groupDeleteDialog.returnValue;
        resolve(
          action === "delete" || action === "move_to_default" ? action : null,
        );
      },
      { once: true },
    );
  });
}

async function requestGroupDelete(group: GroupRecord): Promise<void> {
  const action = await confirmGroupDeletion(group);
  if (!action) {
    return;
  }

  setGroupStatus("");
  try {
    const data = await apiDeleteGroup(group.id, { url_action: action });
    folded.delete(group.id);
    saveFoldedGroupIds();
    renderGroups(data.groups);
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
    setGroupStatus(errorMessage(error), "is-error");
    await loadGroups().catch(() => undefined);
  }
}

// Skip the prompt when "Delete immediately" is on; otherwise show the modal
// and only delete if the user confirms. The dialog's returnValue is "delete"
// for the Delete button and "cancel"/empty for Cancel or Escape.
function confirmDeletion(url: string): Promise<boolean> {
  confirmUrl.textContent = displayUrl(url);
  confirmDialog.showModal();
  return new Promise<boolean>((resolve) => {
    confirmDialog.addEventListener(
      "close",
      () => resolve(confirmDialog.returnValue === "delete"),
      { once: true },
    );
  });
}

async function requestDelete(
  record: UrlRecord,
  groupId: number,
  button: HTMLButtonElement,
): Promise<void> {
  if (!deleteImmediately.checked && !(await confirmDeletion(record.url))) {
    return;
  }
  deleteUrl(record.id, groupId, button);
}

async function deleteUrl(
  id: number,
  groupId: number,
  button: HTMLButtonElement,
): Promise<void> {
  setStatus("");
  button.disabled = true;

  try {
    const data = await apiDeleteUrlById(id, groupId);
    renderGroups(data.groups);
    setStatus("Deleted.", "is-success");
  } catch (error) {
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
  setStatus("");
  select.disabled = true;

  try {
    const data = await apiMoveUrlToGroup(id, {
      group_id: targetGroupId,
      source_group_id: sourceGroupId,
    });
    renderGroups(data.groups);
    setStatus("Moved.", "is-success");
  } catch (error) {
    // Re-render from the last known state so the select snaps back.
    renderGroups(groups);
    setStatus(errorMessage(error), "is-error");
  }
}

async function toggleImportant(
  id: number,
  important: boolean,
  button: HTMLButtonElement,
): Promise<void> {
  setStatus("");
  button.disabled = true;

  try {
    const data = await apiSetUrlImportant(id, { important });
    renderGroups(data.groups);
    setStatus("Updated.", "is-success");
  } catch (error) {
    // Re-render from the last known state so the toggle snaps back.
    renderGroups(groups);
    setStatus(errorMessage(error), "is-error");
  }
}

// The in-field clear button only shows while there is something to clear.
function syncUrlClear(): void {
  urlClear.hidden = input.value === "";
}

input.addEventListener("input", syncUrlClear);
urlClear.addEventListener("click", () => {
  input.value = "";
  syncUrlClear();
  input.focus();
});
syncUrlClear();

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  setStatus("");
  saveButton.disabled = true;

  try {
    await initialGroupsLoad.catch(() => undefined);
    const data = await apiCreateUrl({ url: input.value });
    input.value = "";
    syncUrlClear();
    renderGroups(data.groups);
    setStatus("Saved.", "is-success");
  } catch (error) {
    setStatus(errorMessage(error), "is-error");
  } finally {
    saveButton.disabled = false;
    input.focus();
  }
});

groupForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  setGroupStatus("");
  groupButton.disabled = true;

  try {
    await initialGroupsLoad.catch(() => undefined);
    const data = await apiCreateGroup({
      name: groupInput.value,
      nsfw: groupNsfw.checked,
      domains: parseDomains(groupDomains.value),
      parent_id: groupParent.value === "" ? null : Number(groupParent.value),
    });
    groupInput.value = "";
    groupDomains.value = "";
    groupNsfw.checked = false;
    groupParent.value = "";
    renderGroups(data.groups);
    setGroupStatus("Group added.", "is-success");
  } catch (error) {
    setGroupStatus(errorMessage(error), "is-error");
    await loadGroups().catch(() => undefined);
  } finally {
    groupButton.disabled = false;
    groupInput.focus();
  }
});

resolveInitialSession();
