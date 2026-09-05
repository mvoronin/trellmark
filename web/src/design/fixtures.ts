import type * as client from "../api/client.js";

export function sampleUrl(id: number, title: string | null = "A quiet reading list"): client.UrlRecord {
  return { id, title, url: `https://example.test/reading/${id}`,
    created_at: "2026-01-15T12:00:00Z", important: false, version: 1 };
}

export function sampleGroup(id: number, name: string, urls: client.UrlRecord[] = []): client.GroupRecord {
  return { id, name, urls, children: [], depth: 1, parent_id: null,
    position: id, domains: [], nsfw: false };
}

export function initialGroups(): client.GroupRecord[] {
  return [sampleGroup(1, "Reading", [sampleUrl(1)]), sampleGroup(2, "default")];
}

export interface DesignExample {
  id: string;
  title: string;
  description: string;
  groups: client.GroupRecord[];
  safeMode?: boolean;
  folded?: number[];
}

export function rareExamples(): DesignExample[] {
  const long = sampleUrl(11, "Unbroken".repeat(35));
  const icons = sampleGroup(12, "Site icons", [sampleUrl(120, "No site icon"),
    sampleUrl(121, "Locally failed icon"), sampleUrl(122, "Loaded local icon")]);
  const important = sampleUrl(130, "Important bookmark");
  important.important = true;
  const privateGroup = sampleGroup(14, "NSFW example", [sampleUrl(140, "Filtered reading")]);
  privateGroup.nsfw = true;
  const safeGroup = sampleGroup(15, "Safe reading", [sampleUrl(150)]);
  const leaf = sampleGroup(19, "Third depth", [sampleUrl(190, "Deeply nested bookmark")]);
  Object.assign(leaf, { depth: 3, parent_id: 18 });
  const child = sampleGroup(18, "Second depth");
  Object.assign(child, { depth: 2, parent_id: 17, children: [leaf] });
  const parent = sampleGroup(17, "First depth");
  parent.children = [child];
  const filterGroups = [safeGroup, privateGroup];
  return [
    { id: "empty", title: "Empty group", description: "No saved URLs; group controls remain available.", groups: [sampleGroup(10, "Empty example")] },
    { id: "long-title", title: "Unbroken long title", description: "Long text uses the application's real row and wrapping rules.", groups: [sampleGroup(11, "Long title", [long])] },
    { id: "icons", title: "Missing, failed and loaded icons", description: "Local empty and invalid resources exercise the real image error states.", groups: [icons] },
    { id: "important", title: "Important on and off", description: "Toggle either star to change only these examples.", groups: [sampleGroup(13, "Priority", [important, sampleUrl(131, "Ordinary bookmark")])] },
    { id: "safe", title: "Safe filter", description: "The NSFW group is omitted from this view of the same example data.", groups: filterGroups, safeMode: true },
    { id: "all", title: "All groups", description: "The NSFW group is shown alongside safe content.", groups: filterGroups, safeMode: false },
    { id: "hierarchy", title: "All three hierarchy depths", description: "Nested groups retain their real controls and indentation.", groups: [parent] },
    { id: "folding", title: "Folded and unfolded", description: "Both starting states are shown together. Each toggle works.", groups: [sampleGroup(20, "Folded example", [sampleUrl(200)]), sampleGroup(21, "Unfolded example", [sampleUrl(210)])], folded: [20] },
    { id: "drag", title: "Drag placeholder and drop indicator", description: "The real dragged-group and target states. Drag the interactive Reading header above to try reordering.", groups: [sampleGroup(22, "Dragged group", [sampleUrl(220)]), sampleGroup(23, "Drop target", [sampleUrl(230)])] },
  ];
}

export function localIconSource(id: number): string {
  if (id === 120) return "/static/design/no-icon.svg";
  if (id === 121) return "/static/design/frame.css";
  // A tiny synthetic icon is self-contained; it neither fetches a real site
  // nor mistakes the hidden application SVG sprite for a visible thumbnail.
  return "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="#377785"/><path d="M6 4h8v12l-4-3-4 3z" fill="white"/></svg>');
}

// This adapter owns disposable demonstration data only. It never imports or
// calls the production transport, authentication or external metadata services.
export function createFixtureApi(seed = initialGroups()) {
  const groups = structuredClone(seed);
  let nextId = 1000;
  const walk = (nodes = groups): client.GroupRecord[] => nodes.flatMap(group => [group, ...walk(group.children)]);
  const payload = (): client.GroupsPayload => ({ groups: structuredClone(groups) });
  const findGroup = (id: number): client.GroupRecord => {
    const group = walk().find(group => group.id === id);
    if (!group) throw new Error("This example group no longer exists. Reload to reset.");
    return group;
  };
  const findUrl = (id: number): client.UrlRecord => {
    const url = walk().flatMap(group => group.urls).find(url => url.id === id);
    if (!url) throw new Error("This example URL no longer exists. Reload to reset.");
    return url;
  };
  const siblings = (parentId: number | null): client.GroupRecord[] => parentId === null ? groups : findGroup(parentId).children;
  function safeUrl(value: string): string {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("Use an HTTP or HTTPS example URL.");
    return url.href;
  }
  const api = {
    async listGroups() { return payload(); },
    siteIconPath: localIconSource,
    async setUrlImportant(id: number, body: client.SetImportantPayload) {
      const url = findUrl(id);
      url.important = body.important;
      return { ...payload(), url: structuredClone(url) };
    },
    async editUrl(id: number, body: client.EditUrlPayload) {
      const url = findUrl(id);
      if (body.version !== url.version) throw new Error("This URL was changed. Reload and try again.");
      const address = safeUrl(body.url ?? url.url);
      Object.assign(url, { url: address, title: body.title === undefined ? url.title : body.title || null, version: url.version + 1 });
      return { ...payload(), url: structuredClone(url) };
    },
    async createUrl(body: client.CreateUrlPayload) {
      const url = sampleUrl(nextId++, null);
      url.url = safeUrl(body.url);
      findGroup(2).urls.push(url);
      return { ...payload(), url: structuredClone(url), urls: structuredClone(walk().flatMap(group => group.urls)) };
    },
    async refreshUrlMetadata(id: number) {
      const url = findUrl(id);
      return { ...payload(), url: structuredClone(url), title_updated: false, icon_updated: false };
    },
    async deleteUrlById(id: number, groupId: number) {
      const group = findGroup(groupId);
      const url = findUrl(id);
      group.urls = group.urls.filter(url => url.id !== id);
      return { ...payload(), url: structuredClone(url), urls: structuredClone(walk().flatMap(group => group.urls)) };
    },
    async moveUrlToGroup(id: number, body: client.MoveUrlGroupPayload) {
      const url = findUrl(id);
      const source = body.source_group_id == null
        ? walk().find(group => group.urls.some(url => url.id === id))!
        : findGroup(body.source_group_id);
      const target = findGroup(body.group_id);
      source.urls = source.urls.filter(url => url.id !== id);
      if (!target.urls.some(url => url.id === id)) target.urls.push(url);
      return { ...payload(), url: structuredClone(url), group_id: target.id, source_group_id: source.id };
    },
    async createGroup(body: client.CreateGroupPayload) {
      const parent = body.parent_id == null ? null : findGroup(body.parent_id);
      const name = body.name.trim();
      if (!name) throw new Error("Enter a group name.");
      if (parent?.depth === 3) throw new Error("Examples support three group depths.");
      const group = sampleGroup(nextId++, name);
      Object.assign(group, { parent_id: parent?.id ?? null, depth: (parent?.depth ?? 0) + 1,
        domains: body.domains ?? [], nsfw: body.nsfw ?? false });
      siblings(group.parent_id).push(group);
      return { ...payload(), group: structuredClone(group) };
    },
    async editGroup(id: number, body: client.EditGroupPayload) {
      const group = findGroup(id);
      if (group.name === "default") throw new Error("The default group is protected.");
      const name = (body.name ?? group.name).trim();
      if (!name) throw new Error("Enter a group name.");
      const parentId = body.parent_id ?? null;
      const parent = parentId === null ? null : findGroup(parentId);
      const descendants = walk([group]);
      const height = Math.max(...descendants.map(node => node.depth)) - group.depth + 1;
      if (descendants.some(node => node.id === parentId) || (parent?.depth ?? 0) + height > 3) {
        throw new Error("Choose a parent outside this subtree and within three depths.");
      }
      if (parentId !== group.parent_id) {
        const source = siblings(group.parent_id);
        source.splice(source.indexOf(group), 1);
        const change = (parent?.depth ?? 0) + 1 - group.depth;
        for (const node of descendants) node.depth += change;
        group.parent_id = parentId;
        siblings(parentId).push(group);
      }
      Object.assign(group, { name, domains: body.domains ?? group.domains, nsfw: body.nsfw ?? group.nsfw });
      return { ...payload(), group: structuredClone(group) };
    },
    async deleteGroup(id: number, body: client.DeleteGroupPayload) {
      const group = findGroup(id);
      if (group.name === "default") throw new Error("The default group is protected.");
      const urls = walk([group]).flatMap(node => node.urls);
      const source = siblings(group.parent_id);
      source.splice(source.indexOf(group), 1);
      if (body.url_action === "move_to_default") findGroup(2).urls.push(...urls);
      return { ...payload(), group_id: id, url_action: body.url_action, deleted: body.url_action === "delete" ? urls.length : 0,
        moved: body.url_action === "move_to_default" ? urls.length : 0 };
    },
    async reorderGroups(body: client.ReorderGroupsPayload) {
      const source = siblings(body.parent_id ?? null);
      const ordered = body.group_ids.map(id => findGroup(id));
      source.splice(0, source.length, ...ordered);
      return payload();
    },
  };
  return api;
}
