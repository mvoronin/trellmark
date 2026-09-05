import type { GroupRecord } from "../api/client.js";
import type { RequestLifetime } from "../shared/request.js";
import { createBackupControls } from "./backup.js";
import { createDialogs } from "./dialogs.js";
import { createShellSession, type ShellSessionOptions } from "./session.js";
import { createStatusView } from "./view.js";

export { createDialogs } from "./dialogs.js";
export { createShellView, createStatusView } from "./view.js";

export interface ShellOptions {
  api: ShellSessionOptions["api"];
  privateLifetime: RequestLifetime;
  replaceGroups(groups: readonly GroupRecord[]): void;
  refreshGroups(): Promise<void>;
  onAuthenticated(): void;
  onPrivateClear(): void;
  onPrivateInvalidate(): void;
}

export function createShell(root: ParentNode, options: ShellOptions) {
  const { setStatus } = createStatusView(root);
  const dialogs = createDialogs(root);
  const backup = createBackupControls(root, { ...options, setStatus });
  let disposed = false;

  function invalidate(): void {
    options.privateLifetime.invalidate();
    backup.invalidate();
    options.onPrivateInvalidate();
  }

  function clear(): void {
    // Invalidate all owners before closing dialogs can settle pending actions.
    invalidate();
    dialogs.clear();
    options.onPrivateClear();
    backup.clear();
    setStatus("");
  }

  const session = createShellSession(root, {
    api: options.api,
    onAuthenticated: options.onAuthenticated,
    onPrivateClear: clear,
    onPrivateInvalidate: invalidate,
    onError: (message) => setStatus(message, "is-error"),
  });

  return {
    dialogs, setStatus, start: session.start,
    dispose(): void {
      if (disposed) return;
      disposed = true;
      session.dispose();
      clear();
      backup.dispose();
      dialogs.dispose();
    },
  };
}
