// API configuration
// If VITE_API_URL is explicitly set to empty string, use relative URLs (for Docker/production)
// Otherwise use the provided URL or fallback to localhost
const viteApiUrl = (import.meta as any).env?.VITE_API_URL;
export const API_BASE_URL =
  viteApiUrl === ''
    ? '' // Use relative URLs when empty (Docker/production mode)
    : viteApiUrl ?? `http://localhost:${(import.meta as any).env?.VITE_BACKEND_PORT || '8765'}`;

// Export as API_BASE for compatibility
export const API_BASE = API_BASE_URL;

export const API_ENDPOINTS = {
  // Config endpoints
  config: {
    load: `${API_BASE_URL}/api/config/load`,
    save: `${API_BASE_URL}/api/config/save`,
    agents: `${API_BASE_URL}/api/config/agents`,
    teams: `${API_BASE_URL}/api/config/teams`,
    models: `${API_BASE_URL}/api/config/models`,
    roomModels: `${API_BASE_URL}/api/config/room-models`,
  },

  // Matrix operations
  matrix: {
    agentsRooms: `${API_BASE_URL}/api/matrix/agents/rooms`,
    agentRooms: (agentId: string) => `${API_BASE_URL}/api/matrix/agents/${agentId}/rooms`,
    leaveRoom: `${API_BASE_URL}/api/matrix/rooms/leave`,
    leaveRoomsBulk: `${API_BASE_URL}/api/matrix/rooms/leave-bulk`,
  },

  // Knowledge base operations
  knowledge: {
    bases: `${API_BASE_URL}/api/knowledge/bases`,
    files: (baseId: string) =>
      `${API_BASE_URL}/api/knowledge/bases/${encodeURIComponent(baseId)}/files`,
    upload: (baseId: string) =>
      `${API_BASE_URL}/api/knowledge/bases/${encodeURIComponent(baseId)}/upload`,
    deleteFile: (baseId: string, path: string) =>
      `${API_BASE_URL}/api/knowledge/bases/${encodeURIComponent(baseId)}/files/${encodeURIComponent(
        path
      )}`,
    status: (baseId: string) =>
      `${API_BASE_URL}/api/knowledge/bases/${encodeURIComponent(baseId)}/status`,
    reindex: (baseId: string) =>
      `${API_BASE_URL}/api/knowledge/bases/${encodeURIComponent(baseId)}/reindex`,
  },

  // Credentials operations
  credentials: {
    list: `${API_BASE_URL}/api/credentials/list`,
    status: (service: string) =>
      `${API_BASE_URL}/api/credentials/${encodeURIComponent(service)}/status`,
    get: (service: string) => `${API_BASE_URL}/api/credentials/${encodeURIComponent(service)}`,
    set: (service: string) => `${API_BASE_URL}/api/credentials/${encodeURIComponent(service)}`,
    delete: (service: string) => `${API_BASE_URL}/api/credentials/${encodeURIComponent(service)}`,
    test: (service: string) =>
      `${API_BASE_URL}/api/credentials/${encodeURIComponent(service)}/test`,
  },

  // Other endpoints
  tools: `${API_BASE_URL}/api/tools`,
  rooms: `${API_BASE_URL}/api/rooms`,
};

export async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  // Add a timeout to prevent hanging requests
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000); // 30 second timeout

  try {
    const useJsonHeaders = typeof FormData === 'undefined' || !(options?.body instanceof FormData);

    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        ...(useJsonHeaders ? { 'Content-Type': 'application/json' } : {}),
        ...options?.headers,
      },
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      let detail = `API call failed: ${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        if (typeof payload?.detail === 'string') {
          detail = payload.detail;
        }
      } catch {
        // Keep fallback detail text.
      }
      throw new Error(detail);
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return (await response.json()) as T;
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('API call timed out');
    }
    throw error;
  }
}

// Backward-compatible helper for existing call sites.
export async function fetchAPI(url: string, options?: RequestInit): Promise<any> {
  return fetchJSON<any>(url, options);
}
