import { displayUrl } from "../shared/format.js";

export function createDialogs(root: ParentNode) {
  const containers = new Map(
    Array.from(root.querySelectorAll<HTMLDialogElement>("dialog"), (dialog) => [dialog.id, dialog]),
  );
  const pending = new Map<string, (result: string) => void>();
  const listeners = new AbortController();
  let disposed = false;
  function container(id: string): HTMLDialogElement {
    const dialog = containers.get(id);
    if (!dialog) throw new Error(`Missing dialog: ${id}`);
    return dialog;
  }
  function settle(id: string, result: string): void {
    const resolve = pending.get(id);
    pending.delete(id);
    resolve?.(result);
  }
  for (const [id, dialog] of containers) {
    dialog.addEventListener("close", () => {
      if (!dialog.open) settle(id, dialog.returnValue);
    }, { signal: listeners.signal });
  }
  function open(id: string): Promise<string> {
    const dialog = container(id);
    if (disposed || dialog.open) return Promise.resolve("");
    // A native close event may still be queued when a caller opens again.
    settle(id, dialog.returnValue);
    dialog.returnValue = "";
    const result = new Promise<string>((resolve) => pending.set(id, resolve));
    dialog.showModal();
    return result;
  }
  function close(id: string, result = ""): void {
    const dialog = container(id);
    if (dialog.open) dialog.close(result);
    settle(id, result);
  }
  function clear(): void {
    for (const id of containers.keys()) close(id);
    const label = root.querySelector("#confirm-url");
    if (label) label.textContent = "";
  }
  return {
    open, close, clear,
    isOpen: (id: string): boolean => container(id).open,
    on(id: string, event: "close" | "cancel", callback: () => void): () => void {
      const dialog = container(id);
      dialog.addEventListener(event, callback, { signal: listeners.signal });
      return () => dialog.removeEventListener(event, callback);
    },
    async confirmDeletion(url: string): Promise<boolean> {
      if (disposed || container("confirm-dialog").open) return false;
      const label = container("confirm-dialog").querySelector("#confirm-url");
      if (!label) throw new Error("Missing confirmation label");
      label.textContent = displayUrl(url);
      return await open("confirm-dialog") === "delete";
    },
    dispose(): void {
      disposed = true;
      clear();
      listeners.abort();
    },
  };
}
