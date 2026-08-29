import type { components, paths } from "./generated/openapi";

type ResponsesOf<Operation> = Operation extends { responses: infer Responses }
  ? Responses
  : never;
type JsonResponse<
  Operation,
  Status extends keyof ResponsesOf<Operation>,
> = ResponsesOf<Operation>[Status] extends {
  content: { "application/json": infer Body };
}
  ? Body
  : never;
type JsonRequest<Operation> = Operation extends {
  requestBody: { content: { "application/json": infer Body } };
}
  ? Body
  : never;

type ListGroupsOperation = paths["/api/groups"]["get"];
type CreateGroupOperation = paths["/api/groups"]["post"];
type EditGroupOperation = paths["/api/groups/{group_id}"]["patch"];
type DeleteGroupOperation = paths["/api/groups/{group_id}"]["delete"];
type ReorderGroupsOperation = paths["/api/groups/order"]["patch"];
type ImportOperation = paths["/api/import"]["post"];
type CreateUrlOperation = paths["/api/urls"]["post"];
type EditUrlOperation = paths["/api/urls/{url_id}"]["patch"];
type DeleteUrlByIdOperation = paths["/api/urls/{url_id}"]["delete"];
type MoveUrlGroupOperation = paths["/api/urls/{url_id}/group"]["patch"];
type RefreshUrlTitleOperation = paths["/api/urls/{url_id}/refresh-title"]["post"];
type RefreshUrlMetadataOperation =
  paths["/api/urls/{url_id}/refresh-metadata"]["post"];
type SetImportantOperation = paths["/api/urls/{url_id}/important"]["patch"];
type LoginOperation = paths["/api/auth/login"]["post"];
type SessionOperation = paths["/api/auth/session"]["get"];
type LogoutOperation = paths["/api/auth/logout"]["post"];

export type UrlRecord = components["schemas"]["URLRecord"];
export type GroupRecord = components["schemas"]["GroupRecord"];
export type GroupsPayload = JsonResponse<ListGroupsOperation, 200>;
export type ImportPayload = JsonResponse<ImportOperation, 200>;
export type ImportDataPayload = JsonRequest<ImportOperation>;
export type CreateGroupPayload = JsonRequest<CreateGroupOperation>;
export type EditGroupPayload = JsonRequest<EditGroupOperation>;
export type DeleteGroupPayload = JsonRequest<DeleteGroupOperation>;
export type CreateUrlPayload = JsonRequest<CreateUrlOperation>;
export type EditUrlPayload = JsonRequest<EditUrlOperation>;
export type ReorderGroupsPayload = JsonRequest<ReorderGroupsOperation>;
export type MoveUrlGroupPayload = JsonRequest<MoveUrlGroupOperation>;
export type SetImportantPayload = JsonRequest<SetImportantOperation>;
export type LoginPayload = JsonRequest<LoginOperation>;
export type AuthenticatedSession = components["schemas"]["AuthenticatedSessionResponse"];
export type SessionPayload = JsonResponse<SessionOperation, 200>;

interface ErrorPayload {
  error?: string;
}

const exportPath = "/api/export" satisfies keyof paths;
const groupsPath = "/api/groups" satisfies keyof paths;
const groupByIdTemplate = "/api/groups/{group_id}" satisfies keyof paths;
const groupOrderPath = "/api/groups/order" satisfies keyof paths;
const importPath = "/api/import" satisfies keyof paths;
const urlsPath = "/api/urls" satisfies keyof paths;
const urlByIdTemplate = "/api/urls/{url_id}" satisfies keyof paths;
const urlGroupTemplate = "/api/urls/{url_id}/group" satisfies keyof paths;
const urlImportantTemplate = "/api/urls/{url_id}/important" satisfies keyof paths;
const urlRefreshTitleTemplate = "/api/urls/{url_id}/refresh-title" satisfies keyof paths;
const urlRefreshMetadataTemplate =
  "/api/urls/{url_id}/refresh-metadata" satisfies keyof paths;
const urlIconTemplate = "/api/urls/{url_id}/icon" satisfies keyof paths;
const loginPath = "/api/auth/login" satisfies keyof paths;
const sessionPath = "/api/auth/session" satisfies keyof paths;
const logoutPath = "/api/auth/logout" satisfies keyof paths;

let csrfToken: string | null = null;
let sessionExpiredHandler: (() => void) | null = null;
let expiryTransitionSent = false;

export class SessionExpiredError extends Error {}

export function setSessionExpiredHandler(handler: () => void): void {
  sessionExpiredHandler = handler;
}

export function establishSession(token: string): void {
  csrfToken = token;
  expiryTransitionSent = false;
}

export function clearSession(): void {
  csrfToken = null;
}

function expiredSessionError(): SessionExpiredError {
  clearSession();
  if (!expiryTransitionSent) {
    expiryTransitionSent = true;
    sessionExpiredHandler?.();
  }
  return new SessionExpiredError("Your session has expired.");
}

function urlByIdPath(urlId: number): string {
  return urlByIdTemplate.replace("{url_id}", String(urlId));
}

function groupByIdPath(groupId: number): string {
  return groupByIdTemplate.replace("{group_id}", String(groupId));
}

function urlGroupPath(urlId: number): string {
  return urlGroupTemplate.replace("{url_id}", String(urlId));
}

function urlImportantPath(urlId: number): string {
  return urlImportantTemplate.replace("{url_id}", String(urlId));
}

function urlRefreshTitlePath(urlId: number): string {
  return urlRefreshTitleTemplate.replace("{url_id}", String(urlId));
}

function urlRefreshMetadataPath(urlId: number): string {
  return urlRefreshMetadataTemplate.replace("{url_id}", String(urlId));
}

export function siteIconPath(urlId: number): string {
  return urlIconTemplate.replace("{url_id}", String(urlId));
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object" || !("error" in payload)) {
    return fallback;
  }

  const { error } = payload as ErrorPayload;
  return typeof error === "string" ? error : fallback;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

async function jsonRequest<ResponseBody>(
  path: string,
  init: RequestInit,
  fallbackError: string,
  authenticationRequired = true,
): Promise<ResponseBody> {
  const response = await fetch(path, { ...init, credentials: "same-origin" });
  const payload = await readJson(response);

  if (!response.ok) {
    if (response.status === 401 && authenticationRequired) {
      throw expiredSessionError();
    }
    throw new Error(errorMessage(payload, fallbackError));
  }
  return payload as ResponseBody;
}

function jsonInit(method: "POST" | "PATCH" | "DELETE", body: unknown): RequestInit {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (csrfToken !== null) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  return {
    method,
    headers,
    body: JSON.stringify(body),
  };
}

function unsafeInit(method: "POST" | "PATCH" | "DELETE"): RequestInit {
  const headers: Record<string, string> = {};
  if (csrfToken !== null) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  return { method, headers };
}

export async function getSession(): Promise<SessionPayload> {
  return jsonRequest<SessionPayload>(
    sessionPath,
    { method: "GET" },
    "Could not check your session.",
    false,
  );
}

export async function login(body: LoginPayload): Promise<AuthenticatedSession> {
  const session = await jsonRequest<AuthenticatedSession>(
    loginPath,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    "Could not log in.",
    false,
  );
  establishSession(session.csrf_token);
  return session;
}

export async function logout(): Promise<JsonResponse<LogoutOperation, 200>> {
  const response = await jsonRequest<JsonResponse<LogoutOperation, 200>>(
    logoutPath,
    unsafeInit("POST"),
    "Could not log out.",
    false,
  );
  clearSession();
  return response;
}

export async function listGroups(): Promise<GroupsPayload> {
  return jsonRequest<GroupsPayload>(
    groupsPath,
    { method: "GET" },
    "Could not load URLs.",
  );
}

export async function exportData(): Promise<Response> {
  const response = await fetch(exportPath, { credentials: "same-origin" });
  if (response.status === 401) {
    throw expiredSessionError();
  }
  if (!response.ok) {
    throw new Error(errorMessage(await readJson(response), "Could not export URLs."));
  }
  return response;
}

export async function importData(
  body: string | ImportDataPayload,
): Promise<ImportPayload> {
  return jsonRequest<ImportPayload>(
    importPath,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(csrfToken === null ? {} : { "X-CSRF-Token": csrfToken }),
      },
      body: typeof body === "string" ? body : JSON.stringify(body),
    },
    "Could not import URLs.",
  );
}

export async function createGroup(
  body: CreateGroupPayload,
): Promise<JsonResponse<CreateGroupOperation, 201>> {
  return jsonRequest<JsonResponse<CreateGroupOperation, 201>>(
    groupsPath,
    jsonInit("POST", body),
    "Could not create group.",
  );
}

export async function editGroup(
  groupId: number,
  body: EditGroupPayload,
): Promise<JsonResponse<EditGroupOperation, 200>> {
  return jsonRequest<JsonResponse<EditGroupOperation, 200>>(
    groupByIdPath(groupId),
    jsonInit("PATCH", body),
    "Could not update group.",
  );
}

export async function deleteGroup(
  groupId: number,
  body: DeleteGroupPayload,
): Promise<JsonResponse<DeleteGroupOperation, 200>> {
  return jsonRequest<JsonResponse<DeleteGroupOperation, 200>>(
    groupByIdPath(groupId),
    jsonInit("DELETE", body),
    "Could not delete group.",
  );
}

export async function createUrl(
  body: CreateUrlPayload,
): Promise<JsonResponse<CreateUrlOperation, 201>> {
  return jsonRequest<JsonResponse<CreateUrlOperation, 201>>(
    urlsPath,
    jsonInit("POST", body),
    "Could not save URL.",
  );
}

export async function editUrl(
  urlId: number,
  body: EditUrlPayload,
): Promise<JsonResponse<EditUrlOperation, 200>> {
  return jsonRequest<JsonResponse<EditUrlOperation, 200>>(
    urlByIdPath(urlId),
    jsonInit("PATCH", body),
    "Could not update URL.",
  );
}

export async function refreshUrlTitle(
  urlId: number,
): Promise<JsonResponse<RefreshUrlTitleOperation, 200>> {
  return jsonRequest<JsonResponse<RefreshUrlTitleOperation, 200>>(
    urlRefreshTitlePath(urlId),
    unsafeInit("POST"),
    "Could not refresh page title.",
  );
}

export async function refreshUrlMetadata(
  urlId: number,
): Promise<JsonResponse<RefreshUrlMetadataOperation, 200>> {
  return jsonRequest<JsonResponse<RefreshUrlMetadataOperation, 200>>(
    urlRefreshMetadataPath(urlId),
    unsafeInit("POST"),
    "Could not refresh page metadata.",
  );
}

export async function reorderGroups(
  body: ReorderGroupsPayload,
): Promise<JsonResponse<ReorderGroupsOperation, 200>> {
  return jsonRequest<JsonResponse<ReorderGroupsOperation, 200>>(
    groupOrderPath,
    jsonInit("PATCH", body),
    "Could not reorder groups.",
  );
}

export async function deleteUrlById(
  urlId: number,
  groupId: number,
): Promise<JsonResponse<DeleteUrlByIdOperation, 200>> {
  return jsonRequest<JsonResponse<DeleteUrlByIdOperation, 200>>(
    `${urlByIdPath(urlId)}?${new URLSearchParams({ group_id: String(groupId) })}`,
    unsafeInit("DELETE"),
    "Could not delete URL.",
  );
}

export async function moveUrlToGroup(
  urlId: number,
  body: MoveUrlGroupPayload,
): Promise<JsonResponse<MoveUrlGroupOperation, 200>> {
  return jsonRequest<JsonResponse<MoveUrlGroupOperation, 200>>(
    urlGroupPath(urlId),
    jsonInit("PATCH", body),
    "Could not move URL.",
  );
}

export async function setUrlImportant(
  urlId: number,
  body: SetImportantPayload,
): Promise<JsonResponse<SetImportantOperation, 200>> {
  return jsonRequest<JsonResponse<SetImportantOperation, 200>>(
    urlImportantPath(urlId),
    jsonInit("PATCH", body),
    "Could not update URL.",
  );
}
