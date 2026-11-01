import type { WorkerScope } from "@/types/config";

export const API_ENDPOINTS = {
  // Config endpoints
  config: {
    schema: "/api/config/schema",
    personalAgents: "/api/config/personal-agents",
  },

  // Matrix operations
  matrix: {
    agentsRooms: "/api/matrix/agents/rooms",
    leaveRoomsBulk: "/api/matrix/rooms/leave-bulk",
  },

  // Knowledge base operations
  knowledge: {
    files: (baseId: string) =>
      `/api/knowledge/bases/${encodeURIComponent(baseId)}/files`,
    upload: (baseId: string) =>
      `/api/knowledge/bases/${encodeURIComponent(baseId)}/upload`,
    deleteFile: (baseId: string, path: string) =>
      `/api/knowledge/bases/${encodeURIComponent(baseId)}/files/${encodeURIComponent(path)}`,
    status: (baseId: string) =>
      `/api/knowledge/bases/${encodeURIComponent(baseId)}/status`,
    reindex: (baseId: string) =>
      `/api/knowledge/bases/${encodeURIComponent(baseId)}/reindex`,
  },

  // Credentials operations
  credentials: {
    list: "/api/credentials/list",
    status: (service: string) =>
      `/api/credentials/${encodeURIComponent(service)}/status`,
    get: (service: string) => `/api/credentials/${encodeURIComponent(service)}`,
    set: (service: string) => `/api/credentials/${encodeURIComponent(service)}`,
    delete: (service: string) =>
      `/api/credentials/${encodeURIComponent(service)}`,
    test: (service: string) =>
      `/api/credentials/${encodeURIComponent(service)}/test`,
  },

  // Egress broker operations
  egressBroker: {
    services: "/api/egress-broker/services",
    secret: (service: string) =>
      `/api/egress-broker/services/${encodeURIComponent(service)}/secret`,
    account: (
      service: string,
      action: "connect" | "disconnect" | "oauth-reuse",
    ) => `/api/egress-broker/services/${encodeURIComponent(service)}/${action}`,
    logs: "/api/egress-broker/logs",
    presets: "/api/egress-broker/presets",
    caPem: "/api/egress-broker/ca.pem",
  },

  // Other endpoints
  tools: "/api/tools",
};

export function withQueryParams(
  url: string,
  params: Record<string, string | null | undefined>,
): string {
  const parsed = new URL(url, "http://localhost");

  Object.entries(params).forEach(([key, value]) => {
    if (value == null || value === "") {
      parsed.searchParams.delete(key);
      return;
    }
    parsed.searchParams.set(key, value);
  });

  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

export function withAgentName(url: string, agentName?: string | null): string {
  return withQueryParams(url, { agent_name: agentName });
}

export function withAgentExecutionScope(
  url: string,
  agentName?: string | null,
  executionScope?: WorkerScope | null,
): string {
  const executionScopeQuery =
    agentName != null && executionScope === null
      ? "unscoped"
      : (executionScope ?? null);
  return withQueryParams(url, {
    agent_name: agentName,
    execution_scope: executionScopeQuery,
  });
}

function stringField(
  payload: Record<string, unknown>,
  field: string,
): string | null {
  const value = payload[field];
  return typeof value === "string" && value.trim() ? value : null;
}

function apiErrorMessageFromPayload(
  payload: unknown,
  fallback: string,
): string {
  if (payload == null || typeof payload !== "object") {
    return fallback;
  }

  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }

  if (detail == null || typeof detail !== "object" || Array.isArray(detail)) {
    return fallback;
  }

  const detailRecord = detail as Record<string, unknown>;
  return (
    stringField(detailRecord, "last_error") ??
    stringField(detailRecord, "message") ??
    stringField(detailRecord, "error") ??
    stringField(detailRecord, "detail") ??
    fallback
  );
}

/** The error message of a failed response, or `fallback` when its body has none. */
export async function apiErrorMessageFromResponse(
  response: Response,
  fallback: string,
): Promise<string> {
  return apiErrorMessageFromPayload(
    await response.json().catch(() => null),
    fallback,
  );
}

export async function fetchJSON<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  // Add a timeout to prevent hanging requests
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000); // 30 second timeout

  try {
    const useJsonHeaders =
      typeof FormData === "undefined" || !(options?.body instanceof FormData);

    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        ...(useJsonHeaders ? { "Content-Type": "application/json" } : {}),
        ...options?.headers,
      },
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      throw new Error(
        await apiErrorMessageFromResponse(
          response,
          `API call failed: ${response.status} ${response.statusText}`,
        ),
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return (await response.json()) as T;
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("API call timed out");
    }
    throw error;
  }
}
