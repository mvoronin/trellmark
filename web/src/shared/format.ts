export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error.";
}

export function hostFor(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatAdded(createdAt: string): string {
  const date = new Date(createdAt);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}
