import { createRequestLifetime } from "../shared/request.js";
const exportPath = "/api/export";
const groupsPath = "/api/groups";
const groupByIdTemplate = "/api/groups/{group_id}";
const groupOrderPath = "/api/groups/order";
const importPath = "/api/import";
const urlsPath = "/api/urls";
const urlByIdTemplate = "/api/urls/{url_id}";
const urlGroupTemplate = "/api/urls/{url_id}/group";
const urlImportantTemplate = "/api/urls/{url_id}/important";
const urlRefreshTitleTemplate = "/api/urls/{url_id}/refresh-title";
const urlRefreshMetadataTemplate = "/api/urls/{url_id}/refresh-metadata";
const urlIconTemplate = "/api/urls/{url_id}/icon";
const loginPath = "/api/auth/login";
const sessionPath = "/api/auth/session";
const logoutPath = "/api/auth/logout";
let csrfToken = null;
let sessionExpiredHandler = null;
let expiryTransitionSent = false;
const sessionLifetime = createRequestLifetime();
export class SessionExpiredError extends Error {
}
export class ImportApiError extends Error {
    status;
    payload;
    code;
    constructor(status, payload) {
        super(payload.error);
        this.status = status;
        this.payload = payload;
        this.name = "ImportApiError";
        this.code = payload.code;
    }
}
export function isImportApiError(error) {
    return error instanceof ImportApiError;
}
export function setSessionExpiredHandler(handler) {
    sessionExpiredHandler = handler;
    return () => {
        if (sessionExpiredHandler === handler)
            sessionExpiredHandler = null;
    };
}
export function establishSession(token) {
    sessionLifetime.invalidate();
    csrfToken = token;
    expiryTransitionSent = false;
}
export function clearSession() {
    sessionLifetime.invalidate();
    csrfToken = null;
}
function expiredSessionError(ticket) {
    // Check ownership here, before global CSRF state or the UI callback changes.
    // Caller guards run too late to protect a newly established session.
    if (sessionLifetime.isCurrent(ticket)) {
        clearSession();
        if (!expiryTransitionSent) {
            expiryTransitionSent = true;
            sessionExpiredHandler?.();
        }
    }
    return new SessionExpiredError("Your session has expired.");
}
function urlByIdPath(urlId) {
    return urlByIdTemplate.replace("{url_id}", String(urlId));
}
function groupByIdPath(groupId) {
    return groupByIdTemplate.replace("{group_id}", String(groupId));
}
function urlGroupPath(urlId) {
    return urlGroupTemplate.replace("{url_id}", String(urlId));
}
function urlImportantPath(urlId) {
    return urlImportantTemplate.replace("{url_id}", String(urlId));
}
function urlRefreshTitlePath(urlId) {
    return urlRefreshTitleTemplate.replace("{url_id}", String(urlId));
}
function urlRefreshMetadataPath(urlId) {
    return urlRefreshMetadataTemplate.replace("{url_id}", String(urlId));
}
export function siteIconPath(urlId) {
    return urlIconTemplate.replace("{url_id}", String(urlId));
}
function errorMessage(payload, fallback) {
    if (!payload || typeof payload !== "object" || !("error" in payload)) {
        return fallback;
    }
    const { error } = payload;
    return typeof error === "string" ? error : fallback;
}
async function readJson(response) {
    try {
        return await response.json();
    }
    catch {
        return {};
    }
}
async function jsonRequest(path, init, fallbackError, authenticationRequired = true, errorFactory) {
    const ticket = sessionLifetime.capture();
    const response = await fetch(path, { ...init, credentials: "same-origin" });
    const payload = await readJson(response);
    if (!response.ok) {
        if (response.status === 401 && authenticationRequired) {
            throw expiredSessionError(ticket);
        }
        if (errorFactory) {
            throw errorFactory(response.status, payload, fallbackError);
        }
        throw new Error(errorMessage(payload, fallbackError));
    }
    return payload;
}
function hasImportErrorCode(payload, code) {
    return (payload !== null &&
        typeof payload === "object" &&
        "error" in payload &&
        typeof payload.error === "string" &&
        "code" in payload &&
        payload.code === code);
}
function importError(status, payload, fallback) {
    if (status === 409 &&
        hasImportErrorCode(payload, "import_conflict")) {
        return new ImportApiError(status, payload);
    }
    if (status === 422 && hasImportErrorCode(payload, "invalid_import")) {
        return new ImportApiError(status, payload);
    }
    if (status === 500 && hasImportErrorCode(payload, "import_failed")) {
        return new ImportApiError(status, payload);
    }
    return new Error(errorMessage(payload, fallback));
}
function jsonInit(method, body) {
    const headers = { "Content-Type": "application/json" };
    if (csrfToken !== null) {
        headers["X-CSRF-Token"] = csrfToken;
    }
    return {
        method,
        headers,
        body: JSON.stringify(body),
    };
}
function unsafeInit(method) {
    const headers = {};
    if (csrfToken !== null) {
        headers["X-CSRF-Token"] = csrfToken;
    }
    return { method, headers };
}
export async function getSession() {
    const ticket = sessionLifetime.begin();
    try {
        const session = await jsonRequest(sessionPath, { method: "GET" }, "Could not check your session.", false);
        if (sessionLifetime.isCurrent(ticket)) {
            if (session.authenticated)
                establishSession(session.csrf_token);
            else
                clearSession();
        }
        return session;
    }
    finally {
        sessionLifetime.finish(ticket);
    }
}
export async function login(body) {
    const ticket = sessionLifetime.begin();
    try {
        const session = await jsonRequest(loginPath, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        }, "Could not log in.", false);
        if (sessionLifetime.isCurrent(ticket))
            establishSession(session.csrf_token);
        return session;
    }
    finally {
        sessionLifetime.finish(ticket);
    }
}
export async function logout() {
    const ticket = sessionLifetime.begin();
    try {
        const response = await jsonRequest(logoutPath, unsafeInit("POST"), "Could not log out.", false);
        if (sessionLifetime.isCurrent(ticket))
            clearSession();
        return response;
    }
    finally {
        sessionLifetime.finish(ticket);
    }
}
export async function listGroups() {
    return jsonRequest(groupsPath, { method: "GET" }, "Could not load URLs.");
}
export async function exportData() {
    const ticket = sessionLifetime.capture();
    const response = await fetch(exportPath, { credentials: "same-origin" });
    if (response.status === 401) {
        throw expiredSessionError(ticket);
    }
    if (!response.ok) {
        throw new Error(errorMessage(await readJson(response), "Could not export URLs."));
    }
    return response;
}
export async function importData(body) {
    return jsonRequest(importPath, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...(csrfToken === null ? {} : { "X-CSRF-Token": csrfToken }),
        },
        body: typeof body === "string" ? body : JSON.stringify(body),
    }, "Could not import URLs.", true, importError);
}
export async function createGroup(body) {
    return jsonRequest(groupsPath, jsonInit("POST", body), "Could not create group.");
}
export async function editGroup(groupId, body) {
    return jsonRequest(groupByIdPath(groupId), jsonInit("PATCH", body), "Could not update group.");
}
export async function deleteGroup(groupId, body) {
    return jsonRequest(groupByIdPath(groupId), jsonInit("DELETE", body), "Could not delete group.");
}
export async function createUrl(body) {
    return jsonRequest(urlsPath, jsonInit("POST", body), "Could not save URL.");
}
export async function editUrl(urlId, body) {
    return jsonRequest(urlByIdPath(urlId), jsonInit("PATCH", body), "Could not update URL.");
}
export async function refreshUrlTitle(urlId) {
    return jsonRequest(urlRefreshTitlePath(urlId), unsafeInit("POST"), "Could not refresh page title.");
}
export async function refreshUrlMetadata(urlId) {
    return jsonRequest(urlRefreshMetadataPath(urlId), unsafeInit("POST"), "Could not refresh page metadata.");
}
export async function reorderGroups(body) {
    return jsonRequest(groupOrderPath, jsonInit("PATCH", body), "Could not reorder groups.");
}
export async function deleteUrlById(urlId, groupId) {
    return jsonRequest(`${urlByIdPath(urlId)}?${new URLSearchParams({ group_id: String(groupId) })}`, unsafeInit("DELETE"), "Could not delete URL.");
}
export async function moveUrlToGroup(urlId, body) {
    return jsonRequest(urlGroupPath(urlId), jsonInit("PATCH", body), "Could not move URL.");
}
export async function setUrlImportant(urlId, body) {
    return jsonRequest(urlImportantPath(urlId), jsonInit("PATCH", body), "Could not update URL.");
}
