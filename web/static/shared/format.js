export function errorMessage(error) {
    return error instanceof Error ? error.message : "Unexpected error.";
}
export function hostFor(url) {
    try {
        return new URL(url).host;
    }
    catch {
        return url;
    }
}
export function displayUrl(url) {
    return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}
const dateFormat = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
});
export function formatAdded(createdAt) {
    const date = new Date(createdAt);
    return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}
