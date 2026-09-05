export type Theme = "system" | "light" | "dark";

export function normalizeTheme(value: string | undefined | null): Theme {
  return value === "light" || value === "dark" ? value : "system";
}

export function applyTheme(root: HTMLElement, theme: Theme): void {
  // System follows CSS color-scheme; explicit values pin the owned subtree.
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  for (const button of root.querySelectorAll<HTMLButtonElement>(
    ".theme-toggle [data-theme-value]",
  )) {
    button.setAttribute("aria-pressed", String(button.dataset.themeValue === theme));
  }
}

export function createThemeControls(root: HTMLElement) {
  const controller = new AbortController();
  let theme: Theme = "system";
  try {
    theme = normalizeTheme(localStorage.getItem("theme"));
  } catch {}
  applyTheme(root, theme);
  for (const button of root.querySelectorAll<HTMLButtonElement>(
    ".theme-toggle [data-theme-value]",
  )) {
    button.addEventListener("click", () => {
      const next = normalizeTheme(button.dataset.themeValue);
      try {
        if (next === "system") localStorage.removeItem("theme");
        else localStorage.setItem("theme", next);
      } catch {}
      applyTheme(root, next);
    }, { signal: controller.signal });
  }
  return { dispose: (): void => controller.abort() };
}
