export function createStatusView(root) {
    const status = root.querySelector("#form-status");
    if (!status)
        throw new Error("Missing form status");
    return {
        setStatus(message, state = "") {
            status.textContent = message;
            status.className = state;
        },
    };
}
export function createShellView(root) {
    function required(owner, selector) {
        const element = owner.querySelector(selector);
        if (!element)
            throw new Error(`Missing required element: ${selector}`);
        return element;
    }
    const pending = required(root, "#session-pending");
    const login = required(root, "#login-view");
    const app = required(root, "#app-view");
    const toolbar = required(app, ".app-header");
    const form = required(login, "#login-form");
    const loginInput = required(form, "#login-input");
    const passwordInput = required(form, "#password-input");
    const loginButton = required(form, "#login-button");
    const status = required(form, "#login-status");
    const logoutButton = required(toolbar, "#logout-button");
    return {
        form, loginInput, passwordInput, loginButton, logoutButton, status, toolbar,
        showLogin(message) {
            pending.hidden = true;
            app.hidden = true;
            login.hidden = false;
            status.textContent = message;
            passwordInput.value = "";
            loginButton.disabled = false;
            logoutButton.disabled = false;
        },
        showAuthenticated() {
            logoutButton.disabled = false;
            status.textContent = "";
            passwordInput.value = "";
            pending.hidden = true;
            login.hidden = true;
            app.hidden = false;
        },
    };
}
