import { displayUrl } from "../shared/format.js";
export function createDialogs(root) {
    const containers = new Map(Array.from(root.querySelectorAll("dialog"), (dialog) => [dialog.id, dialog]));
    const pending = new Map();
    const listeners = new AbortController();
    let disposed = false;
    function container(id) {
        const dialog = containers.get(id);
        if (!dialog)
            throw new Error(`Missing dialog: ${id}`);
        return dialog;
    }
    function settle(id, result) {
        const resolve = pending.get(id);
        pending.delete(id);
        resolve?.(result);
    }
    for (const [id, dialog] of containers) {
        dialog.addEventListener("close", () => {
            if (!dialog.open)
                settle(id, dialog.returnValue);
        }, { signal: listeners.signal });
    }
    function open(id) {
        const dialog = container(id);
        if (disposed || dialog.open)
            return Promise.resolve("");
        // A native close event may still be queued when a caller opens again.
        settle(id, dialog.returnValue);
        dialog.returnValue = "";
        const result = new Promise((resolve) => pending.set(id, resolve));
        dialog.showModal();
        return result;
    }
    function close(id, result = "") {
        const dialog = container(id);
        if (dialog.open)
            dialog.close(result);
        settle(id, result);
    }
    function clear() {
        for (const id of containers.keys())
            close(id);
        const label = root.querySelector("#confirm-url");
        if (label)
            label.textContent = "";
    }
    return {
        open, close, clear,
        isOpen: (id) => container(id).open,
        on(id, event, callback) {
            const dialog = container(id);
            dialog.addEventListener(event, callback, { signal: listeners.signal });
            return () => dialog.removeEventListener(event, callback);
        },
        async confirmDeletion(url) {
            if (disposed || container("confirm-dialog").open)
                return false;
            const label = container("confirm-dialog").querySelector("#confirm-url");
            if (!label)
                throw new Error("Missing confirmation label");
            label.textContent = displayUrl(url);
            return await open("confirm-dialog") === "delete";
        },
        dispose() {
            disposed = true;
            clear();
            listeners.abort();
        },
    };
}
