export function normalizeTheme(value) {
    return value === "light" || value === "dark" ? value : "system";
}
export function applyTheme(root, theme) {
    // System follows CSS color-scheme; explicit values pin the owned subtree.
    if (theme === "system")
        delete root.dataset.theme;
    else
        root.dataset.theme = theme;
    for (const button of root.querySelectorAll(".theme-toggle [data-theme-value]")) {
        button.setAttribute("aria-pressed", String(button.dataset.themeValue === theme));
    }
}
export function createThemeControls(root) {
    const controller = new AbortController();
    let theme = "system";
    try {
        theme = normalizeTheme(localStorage.getItem("theme"));
    }
    catch { }
    applyTheme(root, theme);
    for (const button of root.querySelectorAll(".theme-toggle [data-theme-value]")) {
        button.addEventListener("click", () => {
            const next = normalizeTheme(button.dataset.themeValue);
            try {
                if (next === "system")
                    localStorage.removeItem("theme");
                else
                    localStorage.setItem("theme", next);
            }
            catch { }
            applyTheme(root, next);
        }, { signal: controller.signal });
    }
    return { dispose: () => controller.abort() };
}
