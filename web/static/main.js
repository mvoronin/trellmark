import * as api from "./api/client.js";
import { createBookmarks } from "./features/bookmarks/index.js";
import { createShell } from "./shell/index.js";
import { createRequestLifetime } from "./shared/request.js";
import { createThemeControls } from "./shared/theme.js";
const privateLifetime = createRequestLifetime();
createThemeControls(document.documentElement);
const shell = createShell(document.body, {
    api, privateLifetime,
    replaceGroups: (groups) => bookmarks.replaceGroups(groups),
    refreshGroups: () => bookmarks.ready(),
    onAuthenticated: () => bookmarks.authenticated(),
    onPrivateInvalidate: () => bookmarks.invalidate(),
    onPrivateClear: () => bookmarks.clear(),
});
const bookmarks = createBookmarks(document.body, {
    api, privateLifetime, dialogs: shell.dialogs, status: shell.setStatus,
    storage: () => localStorage,
});
void shell.start();
