import type { GroupRecord, UrlRecord } from "../../api/client.js";
import { hostFor } from "../../shared/format.js";

type ReadonlyData<T> = T extends readonly (infer Item)[]
  ? readonly ReadonlyData<Item>[]
  : T extends object ? { readonly [Key in keyof T]: ReadonlyData<T[Key]> } : T;

export type BookmarkGroup = ReadonlyData<GroupRecord>;
export type BookmarkUrl = ReadonlyData<UrlRecord>;
export type SortMode = "added-desc" | "added-asc" | "domain";

export interface BookmarksServerState {
  readonly groups: readonly BookmarkGroup[];
}

export interface BookmarksUIState {
  readonly sortMode: SortMode;
  readonly safeMode: boolean;
  readonly foldedGroupIds: ReadonlySet<number>;
  readonly editingGroup: BookmarkGroup | null;
  readonly editingUrl: BookmarkUrl | null;
  readonly activeDrag: ActiveDrag | null;
  readonly createParentId: number | null;
}

export interface DropTarget {
  readonly groupId: number;
  readonly before: boolean;
}

export interface ActiveDrag {
  readonly groupId: number;
  readonly parentId: number | null;
  readonly header: HTMLElement;
  readonly section: HTMLElement;
  readonly pointerId: number;
  readonly startX: number;
  readonly startY: number;
  readonly moving: boolean;
  readonly dropTarget: DropTarget | null;
}

export interface BookmarkStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface BookmarksModel {
  readonly server: BookmarksServerState;
  readonly ui: BookmarksUIState;
  replaceGroups(groups: readonly BookmarkGroup[]): void;
  clearGroups(): void;
  setSortMode(sortMode: SortMode): void;
  setSafeMode(safeMode: boolean): void;
  foldGroup(groupId: number): void;
  unfoldGroup(groupId: number): void;
  openGroupEditor(group: BookmarkGroup): void;
  closeGroupEditor(): void;
  openUrlEditor(url: BookmarkUrl): void;
  closeUrlEditor(): void;
  startDrag(drag: Omit<ActiveDrag, "moving" | "dropTarget">): void;
  moveDrag(): void;
  setDropTarget(dropTarget: DropTarget | null): void;
  finishDrag(): void;
  setCreateParentId(createParentId: number | null): void;
}

const FOLDED_KEY = "foldedGroups";

function loadFoldedGroupIds(storage?: () => BookmarkStorage): Set<number> {
  try {
    const ids: unknown = JSON.parse(storage?.().getItem(FOLDED_KEY) ?? "[]");
    return new Set(
      Array.isArray(ids) ? ids.filter((id): id is number => Number.isInteger(id)) : [],
    );
  } catch {
    return new Set();
  }
}

export function createBookmarksModel(storage?: () => BookmarkStorage): BookmarksModel {
  let server: BookmarksServerState = { groups: [] };
  let ui: BookmarksUIState = {
    sortMode: "added-desc", safeMode: true, foldedGroupIds: loadFoldedGroupIds(storage),
    editingGroup: null, editingUrl: null, activeDrag: null, createParentId: null,
  };
  const setFolded = (groupId: number, folded: boolean): void => {
    const foldedGroupIds = new Set(ui.foldedGroupIds);
    if (folded) foldedGroupIds.add(groupId);
    else foldedGroupIds.delete(groupId);
    ui = { ...ui, foldedGroupIds };
    try {
      storage?.().setItem(FOLDED_KEY, JSON.stringify([...foldedGroupIds]));
    } catch {} // Browser preference persistence is optional; the UI still changes.
  };
  return {
    get server(): BookmarksServerState { return server; },
    get ui(): BookmarksUIState { return ui; },
    // Async callers retain their private-state capture checks before replacement;
    // request ticket mechanics belong to the following lifetime extraction.
    replaceGroups(groups: readonly BookmarkGroup[]): void {
      server = { groups };
      if (!treeGroups(groups).some(({ group, depth }) =>
        group.id === ui.createParentId && depth < 3
      )) ui = { ...ui, createParentId: null };
    },
    clearGroups(): void {
      server = { groups: [] };
      ui = { ...ui, editingGroup: null, editingUrl: null, activeDrag: null, createParentId: null };
    },
    setSortMode(sortMode: SortMode): void { ui = { ...ui, sortMode }; },
    setSafeMode(safeMode: boolean): void { ui = { ...ui, safeMode }; },
    foldGroup(groupId: number): void { setFolded(groupId, true); },
    unfoldGroup(groupId: number): void { setFolded(groupId, false); },
    openGroupEditor(editingGroup: BookmarkGroup): void { ui = { ...ui, editingGroup }; },
    closeGroupEditor(): void { ui = { ...ui, editingGroup: null }; },
    openUrlEditor(editingUrl: BookmarkUrl): void { ui = { ...ui, editingUrl }; },
    closeUrlEditor(): void { ui = { ...ui, editingUrl: null }; },
    startDrag(drag: Omit<ActiveDrag, "moving" | "dropTarget">): void {
      ui = { ...ui, activeDrag: { ...drag, moving: false, dropTarget: null } };
    },
    moveDrag(): void {
      if (ui.activeDrag) ui = { ...ui, activeDrag: { ...ui.activeDrag, moving: true } };
    },
    setDropTarget(dropTarget: DropTarget | null): void {
      if (ui.activeDrag) ui = { ...ui, activeDrag: { ...ui.activeDrag, dropTarget } };
    },
    finishDrag(): void { ui = { ...ui, activeDrag: null }; },
    setCreateParentId(createParentId: number | null): void { ui = { ...ui, createParentId }; },
  };
}

export function sortUrls(urls: readonly BookmarkUrl[], mode: SortMode): readonly BookmarkUrl[] {
  if (mode === "domain") {
    return [...urls].sort((a, b) =>
      hostFor(a.url).localeCompare(hostFor(b.url)) ||
      a.created_at.localeCompare(b.created_at)
    );
  }
  if (mode === "added-desc") {
    return [...urls].reverse();
  }
  return urls; // added-asc: preserve the authoritative insertion order.
}

export function visibleGroups(
  groups: readonly BookmarkGroup[], safeMode: boolean,
): readonly BookmarkGroup[] {
  return safeMode ? groups.filter((group) => !group.nsfw) : groups;
}

export interface TreeGroup {
  readonly group: BookmarkGroup;
  readonly depth: number;
}

// The server owns hierarchy. This projection only bounds presentation depth.
export function treeGroups(source: readonly BookmarkGroup[]): TreeGroup[] {
  const result: TreeGroup[] = [];
  const walk = (nodes: readonly BookmarkGroup[], depth: number): void => {
    if (depth > 3) return;
    for (const group of nodes) {
      result.push({ group, depth });
      walk(group.children, depth + 1);
    }
  };
  walk(source, 1);
  return result;
}
