export function createStatusView(root: ParentNode) {
  const status = root.querySelector<HTMLElement>("#form-status");
  if (!status) throw new Error("Missing form status");
  return {
    setStatus(message: string, state = ""): void {
      status.textContent = message;
      status.className = state;
    },
  };
}

export function createShellView(root: ParentNode) {
  function required<T extends Element>(owner: ParentNode, selector: string): T {
    const element = owner.querySelector<T>(selector);
    if (!element) throw new Error(`Missing required element: ${selector}`);
    return element;
  }
  const pending = required<HTMLElement>(root, "#session-pending");
  const login = required<HTMLElement>(root, "#login-view");
  const app = required<HTMLElement>(root, "#app-view");
  const toolbar = required<HTMLElement>(app, ".app-header");
  const form = required<HTMLFormElement>(login, "#login-form");
  const loginInput = required<HTMLInputElement>(form, "#login-input");
  const passwordInput = required<HTMLInputElement>(form, "#password-input");
  const loginButton = required<HTMLButtonElement>(form, "#login-button");
  const status = required<HTMLElement>(form, "#login-status");
  const logoutButton = required<HTMLButtonElement>(toolbar, "#logout-button");
  return {
    form, loginInput, passwordInput, loginButton, logoutButton, status, toolbar,
    showLogin(message: string): void {
      pending.hidden = true;
      app.hidden = true;
      login.hidden = false;
      status.textContent = message;
      passwordInput.value = "";
      loginButton.disabled = false;
      logoutButton.disabled = false;
    },
    showAuthenticated(): void {
      logoutButton.disabled = false;
      status.textContent = "";
      passwordInput.value = "";
      pending.hidden = true;
      login.hidden = true;
      app.hidden = false;
    },
  };
}
