import { exportData as apiExportData, importData as apiImportData, isImportApiError, } from "../api/client.js";
import { h } from "../shared/dom.js";
import { errorMessage } from "../shared/format.js";
import { createRequestLifetime } from "../shared/request.js";
export function createBackupControls(root, options) {
    function required(selector) {
        const element = root.querySelector(selector);
        if (!element)
            throw new Error(`Missing required element: ${selector}`);
        return element;
    }
    const exportButton = required("#export-button");
    const importInput = required("#import-input");
    const importRetryButton = required("#import-retry-button");
    const importLifetime = createRequestLifetime();
    const exportLifetime = createRequestLifetime();
    const listeners = new AbortController();
    const { replaceGroups, refreshGroups, setStatus, privateLifetime } = options;
    let pendingImportFile = null;
    let pendingImportRetryable = false;
    let disposed = false;
    function syncImportControls() {
        importInput.disabled = importLifetime.inFlight;
        importRetryButton.hidden = !pendingImportRetryable;
        importRetryButton.disabled = importLifetime.inFlight || !pendingImportRetryable;
    }
    function setImportRetryState(file, retryable) {
        pendingImportFile = file;
        pendingImportRetryable = file !== null && retryable;
        syncImportControls();
    }
    async function importData(file) {
        if (disposed || importLifetime.inFlight)
            return;
        const ticket = importLifetime.begin();
        const epoch = privateLifetime.capture();
        const isCurrent = () => !disposed
            && privateLifetime.isCurrent(epoch) && importLifetime.isCurrent(ticket);
        setStatus("");
        syncImportControls();
        try {
            // The composition root supplies the existing group-load readiness promise.
            // An earlier read must finish before a successful import replaces its tree.
            await refreshGroups().catch(() => undefined);
            if (!isCurrent())
                return;
            const document = await file.text();
            if (!isCurrent())
                return;
            const data = await apiImportData(document);
            if (!isCurrent())
                return;
            replaceGroups(data.groups);
            importInput.value = "";
            setImportRetryState(null, false);
            setStatus(`Imported ${data.imported}, skipped ${data.skipped}.`, "is-success");
        }
        catch (error) {
            if (!isCurrent())
                return;
            if (isImportApiError(error)) {
                setImportRetryState(file, error.code === "import_conflict" || error.code === "import_failed");
            }
            setStatus(errorMessage(error), "is-error");
        }
        finally {
            if (isCurrent() && importLifetime.finish(ticket))
                syncImportControls();
        }
    }
    async function exportData() {
        if (disposed || exportLifetime.inFlight)
            return;
        const ticket = exportLifetime.begin();
        const epoch = privateLifetime.capture();
        const isCurrent = () => !disposed
            && privateLifetime.isCurrent(epoch) && exportLifetime.isCurrent(ticket);
        setStatus("");
        exportButton.disabled = true;
        try {
            const response = await apiExportData();
            if (!isCurrent())
                return;
            const blob = await response.blob();
            if (!isCurrent())
                return;
            const match = response.headers.get("Content-Disposition")?.match(/filename="?([^"]+)"?/);
            const link = h("a", {
                href: URL.createObjectURL(blob),
                download: match?.[1] || "trellmark-export.json",
            });
            try {
                importInput.ownerDocument.body.append(link);
                link.click();
            }
            finally {
                link.remove();
                URL.revokeObjectURL(link.href);
            }
            setStatus("Exported.", "is-success");
        }
        catch (error) {
            if (isCurrent())
                setStatus(errorMessage(error), "is-error");
        }
        finally {
            if (isCurrent() && exportLifetime.finish(ticket))
                exportButton.disabled = false;
        }
    }
    function invalidate() {
        importLifetime.invalidate();
        exportLifetime.invalidate();
        exportButton.disabled = false;
        syncImportControls();
    }
    function clear() {
        invalidate();
        importInput.value = "";
        setImportRetryState(null, false);
        setStatus("");
    }
    exportButton.addEventListener("click", exportData, { signal: listeners.signal });
    importInput.addEventListener("change", () => {
        if (disposed || importLifetime.inFlight)
            return;
        const [file] = importInput.files ?? [];
        if (file) {
            setImportRetryState(file, false);
            void importData(file);
        }
    }, { signal: listeners.signal });
    importRetryButton.addEventListener("click", () => {
        if (pendingImportFile && pendingImportRetryable)
            void importData(pendingImportFile);
    }, { signal: listeners.signal });
    syncImportControls();
    return {
        invalidate, clear,
        dispose() {
            disposed = true;
            clear();
            listeners.abort();
        },
    };
}
