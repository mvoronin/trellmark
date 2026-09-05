import {
  exportData as apiExportData,
  importData as apiImportData,
  isImportApiError,
  type GroupRecord,
} from "../api/client.js";
import { h } from "../shared/dom.js";
import { errorMessage } from "../shared/format.js";
import { createRequestLifetime, type RequestLifetime } from "../shared/request.js";

export interface BackupOptions {
  replaceGroups(groups: readonly GroupRecord[]): void;
  refreshGroups(): Promise<void>;
  setStatus(message: string, state?: string): void;
  privateLifetime: RequestLifetime;
}

export function createBackupControls(root: ParentNode, options: BackupOptions) {
  function required<T extends Element>(selector: string): T {
    const element = root.querySelector<T>(selector);
    if (!element) throw new Error(`Missing required element: ${selector}`);
    return element;
  }
  const exportButton = required<HTMLButtonElement>("#export-button");
  const importInput = required<HTMLInputElement>("#import-input");
  const importRetryButton = required<HTMLButtonElement>("#import-retry-button");
  const importLifetime = createRequestLifetime();
  const exportLifetime = createRequestLifetime();
  const listeners = new AbortController();
  const { replaceGroups, refreshGroups, setStatus, privateLifetime } = options;
  let pendingImportFile: File | null = null;
  let pendingImportRetryable = false;
  let disposed = false;

  function syncImportControls(): void {
    importInput.disabled = importLifetime.inFlight;
    importRetryButton.hidden = !pendingImportRetryable;
    importRetryButton.disabled = importLifetime.inFlight || !pendingImportRetryable;
  }

  function setImportRetryState(file: File | null, retryable: boolean): void {
    pendingImportFile = file;
    pendingImportRetryable = file !== null && retryable;
    syncImportControls();
  }

  async function importData(file: File): Promise<void> {
    if (disposed || importLifetime.inFlight) return;
    const ticket = importLifetime.begin();
    const epoch = privateLifetime.capture();
    const isCurrent = (): boolean => !disposed
      && privateLifetime.isCurrent(epoch) && importLifetime.isCurrent(ticket);
    setStatus("");
    syncImportControls();
    try {
      // The composition root supplies the existing group-load readiness promise.
      // An earlier read must finish before a successful import replaces its tree.
      await refreshGroups().catch(() => undefined);
      if (!isCurrent()) return;
      const document = await file.text();
      if (!isCurrent()) return;
      const data = await apiImportData(document);
      if (!isCurrent()) return;
      replaceGroups(data.groups);
      importInput.value = "";
      setImportRetryState(null, false);
      setStatus(`Imported ${data.imported}, skipped ${data.skipped}.`, "is-success");
    } catch (error) {
      if (!isCurrent()) return;
      if (isImportApiError(error)) {
        setImportRetryState(
          file,
          error.code === "import_conflict" || error.code === "import_failed",
        );
      }
      setStatus(errorMessage(error), "is-error");
    } finally {
      if (isCurrent() && importLifetime.finish(ticket)) syncImportControls();
    }
  }

  async function exportData(): Promise<void> {
    if (disposed || exportLifetime.inFlight) return;
    const ticket = exportLifetime.begin();
    const epoch = privateLifetime.capture();
    const isCurrent = (): boolean => !disposed
      && privateLifetime.isCurrent(epoch) && exportLifetime.isCurrent(ticket);
    setStatus("");
    exportButton.disabled = true;
    try {
      const response = await apiExportData();
      if (!isCurrent()) return;
      const blob = await response.blob();
      if (!isCurrent()) return;
      const match = response.headers.get("Content-Disposition")?.match(/filename="?([^"]+)"?/);
      const link = h("a", {
        href: URL.createObjectURL(blob),
        download: match?.[1] || "trellmark-export.json",
      });
      try {
        importInput.ownerDocument.body.append(link);
        link.click();
      } finally {
        link.remove();
        URL.revokeObjectURL(link.href);
      }
      setStatus("Exported.", "is-success");
    } catch (error) {
      if (isCurrent()) setStatus(errorMessage(error), "is-error");
    } finally {
      if (isCurrent() && exportLifetime.finish(ticket)) exportButton.disabled = false;
    }
  }

  function invalidate(): void {
    importLifetime.invalidate();
    exportLifetime.invalidate();
    exportButton.disabled = false;
    syncImportControls();
  }

  function clear(): void {
    invalidate();
    importInput.value = "";
    setImportRetryState(null, false);
    setStatus("");
  }

  exportButton.addEventListener("click", exportData, { signal: listeners.signal });
  importInput.addEventListener("change", () => {
    if (disposed || importLifetime.inFlight) return;
    const [file] = importInput.files ?? [];
    if (file) {
      setImportRetryState(file, false);
      void importData(file);
    }
  }, { signal: listeners.signal });
  importRetryButton.addEventListener("click", () => {
    if (pendingImportFile && pendingImportRetryable) void importData(pendingImportFile);
  }, { signal: listeners.signal });
  syncImportControls();

  return {
    invalidate, clear,
    dispose(): void {
      disposed = true;
      clear();
      listeners.abort();
    },
  };
}
