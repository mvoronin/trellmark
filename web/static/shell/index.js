import { createBackupControls } from "./backup.js";
import { createDialogs } from "./dialogs.js";
import { createShellSession } from "./session.js";
import { createStatusView } from "./view.js";
export { createDialogs } from "./dialogs.js";
export { createShellView, createStatusView } from "./view.js";
export function createShell(root, options) {
    const { setStatus } = createStatusView(root);
    const dialogs = createDialogs(root);
    const backup = createBackupControls(root, { ...options, setStatus });
    let disposed = false;
    function invalidate() {
        options.privateLifetime.invalidate();
        backup.invalidate();
        options.onPrivateInvalidate();
    }
    function clear() {
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
        dispose() {
            if (disposed)
                return;
            disposed = true;
            session.dispose();
            clear();
            backup.dispose();
            dialogs.dispose();
        },
    };
}
