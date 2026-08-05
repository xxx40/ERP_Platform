import type {
  AnswerFeedback,
  ChatResponse,
  ConversationDetailResponse,
  ConversationListResponse,
  FeedbackPayload,
  HealthResponse,
  ConnectorSnapshot,
  PlatformConfigStatus,
  PlatformConfigVersion,
  PlatformModule,
  PlatformCapability,
  TraceResponse,
  WorkflowRun,
  GraphTopology,
  HarnessStatus,
  EvaluationRun,
  BusinessDatasetSnapshot,
  HttpToolCatalogSnapshot,
  PlatformProviderStatus,
  PlatformSecret,
  GovernedDataSource,
  SemanticModelRecord,
  SemanticModelVersion,
  AuthorizationContext,
} from "./types";
import {
  authenticationMode,
  authorizationHeaders,
  renewAuthentication,
} from "./auth";

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8001";

// Call sites keep a normal fetch shape; authentication is resolved at request time.
const IDENTITY_HEADERS: Record<string, string> = {};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  allowRenew = true,
): Promise<Response> {
  const headers = new Headers(init.headers);
  const identityHeaders = await authorizationHeaders();
  Object.entries(identityHeaders).forEach(([name, value]) => headers.set(name, value));
  const response = await globalThis.fetch(input, { ...init, headers });
  if (response.status === 401 && authenticationMode === "oidc") {
    if (allowRenew && await renewAuthentication()) {
      return apiFetch(input, init, false);
    }
    window.dispatchEvent(new CustomEvent("erp-authentication-required"));
  }
  return response;
}

const fetch = apiFetch;

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail ?? "";
    } catch {
      // Keep the status-only fallback for non-JSON gateway failures.
    }
    const fallback = response.status === 401
      ? "登录状态已失效，请重新登录"
      : response.status === 403
        ? "当前账号没有执行此操作的权限"
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(detail || fallback, response.status);
  }
  return (await response.json()) as T;
}

export async function listPlatformModules(): Promise<{ count: number; items: PlatformModule[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/modules`, { headers: IDENTITY_HEADERS }));
}

export async function listPlatformCapabilities(): Promise<{ count: number; items: PlatformCapability[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/capabilities`, { headers: IDENTITY_HEADERS }));
}


export async function listPlatformGraphs(): Promise<{ count: number; items: GraphTopology[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/graphs`, { headers: IDENTITY_HEADERS }));
}

export async function getPlatformConfigStatus(): Promise<PlatformConfigStatus> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/config/status`, { headers: IDENTITY_HEADERS }));
}

export async function listPlatformConfigVersions(): Promise<{ count: number; items: PlatformConfigVersion[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/config/versions`, { headers: IDENTITY_HEADERS }));
}

export async function getPlatformHarness(): Promise<HarnessStatus> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/harness`, { headers: IDENTITY_HEADERS }));
}

export async function listPlatformEvaluations(): Promise<{ count: number; items: EvaluationRun[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/evaluations`, { headers: IDENTITY_HEADERS }));
}

export async function validatePlatformConfig(pluginEnabled: Record<string, boolean>): Promise<PlatformConfigStatus> {
  return platformConfigAction("validate", pluginEnabled);
}

export async function publishPlatformConfig(pluginEnabled: Record<string, boolean>): Promise<PlatformConfigStatus> {
  return platformConfigAction("publish", pluginEnabled);
}

async function platformConfigAction(
  action: "validate" | "publish",
  pluginEnabled: Record<string, boolean>,
): Promise<PlatformConfigStatus> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/config/${action}`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify({ plugin_enabled: pluginEnabled }),
  }));
}

export async function rollbackPlatformConfig(): Promise<PlatformConfigStatus> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/config/rollback`, {
    method: "POST",
    headers: IDENTITY_HEADERS,
  }));
}

export async function getPlatformConnectors(): Promise<ConnectorSnapshot> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/connectors`, { headers: IDENTITY_HEADERS }));
}

export async function testPlatformConnector(connectorId: string): Promise<{ connector_id: string; ready: boolean }> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/connectors/${encodeURIComponent(connectorId)}/test`,
    { method: "POST", headers: IDENTITY_HEADERS },
  ));
}

export async function configurePlatformConnectors(
  action: "validate" | "publish" | "rollback",
  payload?: Record<string, unknown>,
): Promise<ConnectorSnapshot> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/connectors/config/${action}`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  }));
}

export async function getPlatformDatasets(): Promise<BusinessDatasetSnapshot> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/datasets`, { headers: IDENTITY_HEADERS }));
}

export async function configurePlatformDatasets(
  action: "validate" | "publish" | "rollback",
  payload?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/datasets/config/${action}`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  }));
}

export async function previewPlatformDataset(datasetId: string): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/datasets/${encodeURIComponent(datasetId)}/preview`,
    { method: "POST", headers: IDENTITY_HEADERS },
  ));
}

export async function introspectPlatformConnector(connectorId: string): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/connectors/${encodeURIComponent(connectorId)}/introspect`,
    { headers: IDENTITY_HEADERS },
  ));
}

export async function getPlatformProviders(): Promise<{ items: PlatformProviderStatus[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/providers`, { headers: IDENTITY_HEADERS }));
}

export async function listPlatformSecrets(): Promise<{ count: number; items: PlatformSecret[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/secrets`, { headers: IDENTITY_HEADERS }));
}

export async function createPlatformSecret(name: string, value: string): Promise<PlatformSecret> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/secrets`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify({ name, value }),
  }));
}

export async function deletePlatformSecret(secretId: string): Promise<{ deleted: boolean }> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/secrets/${encodeURIComponent(secretId)}`,
    { method: "DELETE", headers: IDENTITY_HEADERS },
  ));
}

export async function listGovernedDataSources(): Promise<{ count: number; items: GovernedDataSource[] }> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/data-sources`, { headers: IDENTITY_HEADERS }));
}

export async function createGovernedDataSource(payload: Record<string, unknown>): Promise<GovernedDataSource> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/data-sources`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
}

export async function dataSourceAction(
  connectorId: string,
  action: "test" | "submit" | "approve" | "reject" | "disable",
  payload?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/data-sources/${encodeURIComponent(connectorId)}/${action}`,
    {
      method: "POST",
      headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    },
  ));
}

export async function introspectGovernedDataSource(connectorId: string): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/data-sources/${encodeURIComponent(connectorId)}/introspect`,
    { headers: IDENTITY_HEADERS },
  ));
}

export async function rotateGovernedDataSourceSecret(
  connectorId: string,
  payload: Record<string, unknown>,
): Promise<GovernedDataSource & { rotation: { verified: boolean; dataset_rebuilt: boolean } }> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/data-sources/${encodeURIComponent(connectorId)}/rotate-secret`,
    {
      method: "POST",
      headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ));
}

export async function createSemanticModel(payload: Record<string, unknown>): Promise<SemanticModelRecord> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/semantic-models`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
}

export async function listSemanticModels(): Promise<{ count: number; items: SemanticModelRecord[] }> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/semantic-models`,
    { headers: IDENTITY_HEADERS },
  ));
}

export async function createSemanticModelVersion(
  modelId: string,
  logicalModel: Record<string, unknown>,
): Promise<SemanticModelRecord> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/semantic-models/${encodeURIComponent(modelId)}/versions`,
    {
      method: "POST",
      headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({ logical_model: logicalModel }),
    },
  ));
}

export async function listSemanticModelVersions(
  modelId: string,
): Promise<{ count: number; items: SemanticModelVersion[] }> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/semantic-models/${encodeURIComponent(modelId)}/versions`,
    { headers: IDENTITY_HEADERS },
  ));
}

export async function semanticModelAction(
  modelId: string,
  action: "validate" | "preview" | "publish" | "rollback",
  payload?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return parseResponse(await fetch(
    `${API_BASE_URL}/api/v1/platform/semantic-models/${encodeURIComponent(modelId)}/${action}`,
    {
      method: "POST",
      headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    },
  ));
}

export async function getPlatformHttpTools(): Promise<HttpToolCatalogSnapshot> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/http-tools`, { headers: IDENTITY_HEADERS }));
}

export async function configurePlatformHttpTools(
  action: "validate" | "publish" | "rollback",
  payload?: Record<string, unknown>,
): Promise<HttpToolCatalogSnapshot> {
  return parseResponse(await fetch(`${API_BASE_URL}/api/v1/platform/http-tools/config/${action}`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  }));
}

export async function sendChat(message: string, sessionId?: string): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/chat`, {
    method: "POST",
    headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId || null }),
  });
  return parseResponse<ChatResponse>(response);
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/health`, {
    headers: IDENTITY_HEADERS,
  });
  return parseResponse<HealthResponse>(response);
}

export async function getAuthorizationContext(): Promise<AuthorizationContext> {
  const response = await fetch(`${API_BASE_URL}/api/v1/auth/context`, {
    headers: IDENTITY_HEADERS,
  });
  return parseResponse<AuthorizationContext>(response);
}

export async function listConversations(
  limit = 100,
  offset = 0,
): Promise<ConversationListResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations?limit=${encodeURIComponent(String(limit))}&offset=${encodeURIComponent(String(offset))}`,
    { headers: IDENTITY_HEADERS },
  );
  return parseResponse<ConversationListResponse>(response);
}

export async function getConversation(sessionId: string): Promise<ConversationDetailResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations/${encodeURIComponent(sessionId)}`,
    { headers: IDENTITY_HEADERS },
  );
  return parseResponse<ConversationDetailResponse>(response);
}

export async function deleteConversation(sessionId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations/${encodeURIComponent(sessionId)}`,
    { method: "DELETE", headers: IDENTITY_HEADERS },
  );
  if (!response.ok) await parseResponse<never>(response);
}

export async function getTrace(requestId: string): Promise<TraceResponse> {
  const response = await fetch(`${API_BASE_URL}/api/v1/traces/${encodeURIComponent(requestId)}`, {
    headers: IDENTITY_HEADERS,
  });
  return parseResponse<TraceResponse>(response);
}

export async function getWorkflowRun(requestId: string): Promise<WorkflowRun> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/platform/workflow-runs/${encodeURIComponent(requestId)}`,
    { headers: IDENTITY_HEADERS },
  );
  return parseResponse<WorkflowRun>(response);
}

export async function submitFeedback(
  requestId: string,
  payload: FeedbackPayload,
): Promise<AnswerFeedback> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/feedback/${encodeURIComponent(requestId)}`,
    {
      method: "PUT",
      headers: { ...IDENTITY_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  return parseResponse<AnswerFeedback>(response);
}
