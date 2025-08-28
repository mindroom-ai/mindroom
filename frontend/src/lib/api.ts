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

  // Google setup
  google: {
    status: `${API_BASE_URL}/api/auth/google/status`,
    connect: `${API_BASE_URL}/api/auth/google/connect`,
    disconnect: `${API_BASE_URL}/api/auth/google/disconnect`,
    callback: `${API_BASE_URL}/api/auth/google/callback`,
    setup: {
      checkPrerequisites: `${API_BASE_URL}/api/setup/google/check-prerequisites`,
      createProject: `${API_BASE_URL}/api/setup/google/create-project`,
      enableApis: `${API_BASE_URL}/api/setup/google/enable-apis`,
      startOauth: `${API_BASE_URL}/api/setup/google/start-oauth-setup`,
      complete: `${API_BASE_URL}/api/setup/google/complete-setup`,
      quickScript: `${API_BASE_URL}/api/setup/google/quick-setup-script`,
    },
  },

  // Simple mode
  simple: {
    status: `${API_BASE_URL}/api/simple/mode/status`,
    toggle: `${API_BASE_URL}/api/simple/mode/toggle`,
  },

  // Matrix operations
  matrix: {
    agentsRooms: `${API_BASE_URL}/api/matrix/agents/rooms`,
    agentRooms: (agentId: string) => `${API_BASE_URL}/api/matrix/agents/${agentId}/rooms`,
    leaveRoom: `${API_BASE_URL}/api/matrix/rooms/leave`,
    leaveRoomsBulk: `${API_BASE_URL}/api/matrix/rooms/leave-bulk`,
  },

  // Other endpoints
  tools: `${API_BASE_URL}/api/tools`,
  rooms: `${API_BASE_URL}/api/rooms`,
  testModel: `${API_BASE_URL}/api/test/model`,
  encryptKey: `${API_BASE_URL}/api/keys/encrypt`,
};

// Helper function to make API calls
export async function fetchAPI(url: string, options?: RequestInit) {
  // Add a timeout to prevent hanging requests
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000); // 30 second timeout

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      throw new Error(`API call failed: ${response.status} ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('API call timed out');
    }
    throw error;
  }
}
