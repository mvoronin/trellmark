import { errorMessage } from "../shared/format.js";
import { createRequestLifetime } from "../shared/request.js";
import { createShellView } from "./view.js";
export function createShellSession(root, options) {
    const view = createShellView(root);
    const lifetime = createRequestLifetime();
    const controller = new AbortController();
    let disposed = false;
    const { api } = options;
    function clear(message = "") {
        if (disposed)
            return;
        lifetime.invalidate();
        api.clearSession();
        options.onPrivateClear();
        view.showLogin(message);
        const ticket = lifetime.capture();
        queueMicrotask(() => {
            if (!disposed && lifetime.isCurrent(ticket))
                view.loginInput.focus();
        });
    }
    function authenticated() {
        view.showAuthenticated();
        options.onAuthenticated();
    }
    const unsubscribe = api.setSessionExpiredHandler(() => {
        if (disposed)
            return;
        clear("Your session expired. Log in again.");
        options.onError("Your session has expired.");
    });
    view.form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const ticket = lifetime.begin();
        options.onPrivateInvalidate();
        view.status.textContent = "";
        view.loginButton.disabled = true;
        try {
            await api.login({
                login: view.loginInput.value,
                password: view.passwordInput.value,
            });
            if (!lifetime.isCurrent(ticket))
                return;
            authenticated();
        }
        catch (error) {
            if (!lifetime.isCurrent(ticket))
                return;
            view.status.textContent = errorMessage(error);
            view.passwordInput.value = "";
            view.passwordInput.focus();
        }
        finally {
            if (lifetime.finish(ticket))
                view.loginButton.disabled = false;
        }
    }, { signal: controller.signal });
    view.logoutButton.addEventListener("click", async () => {
        const ticket = lifetime.begin();
        // A failed logout leaves this session active, so pending private actions
        // still own their controls and results. Successful clear (or expiry)
        // invalidates them before removing private state.
        view.logoutButton.disabled = true;
        try {
            await api.logout();
            if (!lifetime.isCurrent(ticket))
                return;
            clear();
        }
        catch (error) {
            if (lifetime.isCurrent(ticket))
                options.onError(errorMessage(error));
        }
        finally {
            if (lifetime.finish(ticket))
                view.logoutButton.disabled = false;
        }
    }, { signal: controller.signal });
    return {
        clear,
        async start() {
            if (disposed)
                return;
            const ticket = lifetime.begin();
            try {
                const session = await api.getSession();
                if (!lifetime.isCurrent(ticket))
                    return;
                if (session.authenticated)
                    authenticated();
                else
                    clear();
            }
            catch (error) {
                if (lifetime.isCurrent(ticket))
                    clear(errorMessage(error));
            }
            finally {
                lifetime.finish(ticket);
            }
        },
        dispose() {
            if (disposed)
                return;
            disposed = true;
            lifetime.invalidate();
            controller.abort();
            // Unregister only our callback; a newer owner may already be installed.
            unsubscribe();
        },
    };
}
