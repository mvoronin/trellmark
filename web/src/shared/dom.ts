type Events = Pick<HTMLElementEventMap, "click" | "change" | "input" | "load" | "error">;
type EventCallbacks = { [E in keyof Events]?: (event: Events[E]) => void };

interface CommonProps {
  id?: string;
  className?: string;
  title?: string;
  hidden?: boolean;
  data?: Partial<Record<"depth" | "parentId" | "groupId", string>>;
  aria?: {
    label?: string;
    controls?: string;
    expanded?: boolean;
    pressed?: boolean;
    hidden?: boolean;
  };
  style?: { "--group-hue"?: string };
  on?: EventCallbacks;
}

interface TagProps {
  a: { href?: string; target?: string; rel?: string; download?: string };
  button: { type?: "button" | "submit" | "reset"; disabled?: boolean };
  input: { type?: "text" | "url" | "checkbox" | "password" | "file"; value?: string; checked?: boolean; disabled?: boolean };
  select: { value?: string; disabled?: boolean };
  option: { value?: string; selected?: boolean };
  img: { src?: string; alt?: string; loading?: "eager" | "lazy"; decoding?: "async" | "sync" | "auto" };
}

export type DOMProps<K extends keyof HTMLElementTagNameMap> = CommonProps &
  (K extends keyof TagProps ? TagProps[K] : object);
export type DOMChild = Node | string;

const commonKeys = ["id", "className", "title", "hidden", "data", "aria", "style", "on"];
const tagKeys: Partial<Record<keyof HTMLElementTagNameMap, readonly string[]>> = {
  a: ["href", "target", "rel", "download"],
  button: ["type", "disabled"],
  input: ["type", "value", "checked", "disabled"],
  select: ["value", "disabled"],
  option: ["value", "selected"],
  img: ["src", "alt", "loading", "decoding"],
};

function entries(value: unknown, keys: readonly string[]): [string, unknown][] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError("DOM properties must be an object");
  }
  const result: [string, unknown][] = Object.entries(value);
  for (const [key] of result) {
    if (!keys.includes(key)) throw new TypeError(`Unsupported DOM property: ${key}`);
  }
  return result;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new TypeError("Expected a string DOM property");
  return value;
}

function flag(value: unknown): boolean {
  if (typeof value !== "boolean") throw new TypeError("Expected a boolean DOM property");
  return value;
}

function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  const result = text(value);
  const found = choices.find((item) => item === result);
  if (found === undefined) throw new TypeError(`Unsupported DOM property value: ${result}`);
  return found;
}

// Props are trusted construction instructions, not a URL sanitizer or an HTML
// parser. Keep URL policy at the caller and SVG construction explicitly namespaced.
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props?: DOMProps<NoInfer<K>>,
  children: readonly DOMChild[] = [],
): HTMLElementTagNameMap[K] {
  const properties = entries(props ?? {}, [...commonKeys, ...(tagKeys[tag] ?? [])]);
  const element = document.createElement(tag);
  // Selection properties depend on the option children already being present.
  for (const child of children) {
    element.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  for (const [key, value] of properties) {
    if (value === undefined) continue;
    switch (key) {
      case "id": element.id = text(value); break;
      case "className": element.className = text(value); break;
      case "title": element.title = text(value); break;
      case "hidden": element.hidden = flag(value); break;
      case "data":
        for (const [name, item] of entries(value, ["depth", "parentId", "groupId"])) {
          if (item !== undefined) element.dataset[name] = text(item);
        }
        break;
      case "aria":
        for (const [name, item] of entries(value, ["label", "controls", "expanded", "pressed", "hidden"])) {
          if (item !== undefined) element.setAttribute(`aria-${name}`, name === "label" || name === "controls" ? text(item) : String(flag(item)));
        }
        break;
      case "style":
        for (const [name, item] of entries(value, ["--group-hue"])) {
          if (item !== undefined) element.style.setProperty(name, text(item));
        }
        break;
      case "on":
        for (const [name, callback] of entries(value, ["click", "change", "input", "load", "error"])) {
          if (callback === undefined) continue;
          if (typeof callback !== "function") throw new TypeError("DOM event callbacks must be functions");
          element.addEventListener(name, callback as EventListener);
        }
        break;
      case "href": if (element instanceof HTMLAnchorElement) element.href = text(value); break;
      case "target": if (element instanceof HTMLAnchorElement) element.target = text(value); break;
      case "rel": if (element instanceof HTMLAnchorElement) element.rel = text(value); break;
      case "download": if (element instanceof HTMLAnchorElement) element.download = text(value); break;
      case "value":
        if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLOptionElement) element.value = text(value);
        break;
      case "checked": if (element instanceof HTMLInputElement) element.checked = flag(value); break;
      case "selected": if (element instanceof HTMLOptionElement) element.selected = flag(value); break;
      case "disabled":
        if (element instanceof HTMLInputElement || element instanceof HTMLButtonElement || element instanceof HTMLSelectElement) element.disabled = flag(value);
        break;
      case "type":
        if (element instanceof HTMLButtonElement) element.type = choice(value, ["button", "submit", "reset"]);
        if (element instanceof HTMLInputElement) element.type = choice(value, ["text", "url", "checkbox", "password", "file"]);
        break;
      case "src": if (element instanceof HTMLImageElement) element.src = text(value); break;
      case "alt": if (element instanceof HTMLImageElement) element.alt = text(value); break;
      case "loading": if (element instanceof HTMLImageElement) element.loading = choice(value, ["eager", "lazy"]); break;
      case "decoding": if (element instanceof HTMLImageElement) element.decoding = choice(value, ["async", "sync", "auto"]); break;
    }
  }
  return element;
}
