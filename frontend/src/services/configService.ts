import { Agent, AgentPoliciesByAgent, Config } from "@/types/config";
import { ConfigValidationIssue } from "@/lib/configValidation";
import type { ToolEntry } from "@/lib/toolEntry";

export type RawAgentConfig = Omit<Agent, "id" | "tools"> & {
  tools: ToolEntry[];
};

export type RawDefaultsConfig = Omit<Config["defaults"], "tools"> & {
  tools?: ToolEntry[];
};

export type RawConfig = Omit<Config, "agents" | "defaults"> & {
  agents: Record<string, RawAgentConfig>;
  defaults: RawDefaultsConfig;
};

export type ConfigSavePayload = RawConfig;

const API_BASE = "/api";
const CONFIG_GENERATION_HEADER = "x-mindroom-config-generation";

function isConfigValidationIssue(
  detail: unknown,
): detail is ConfigValidationIssue {
  return (
    typeof detail === "object" &&
    detail !== null &&
    Array.isArray((detail as ConfigValidationIssue).loc) &&
    typeof (detail as ConfigValidationIssue).msg === "string" &&
    typeof (detail as ConfigValidationIssue).type === "string"
  );
}

function isConfigValidationIssueList(
  detail: unknown,
): detail is ConfigValidationIssue[] {
  return Array.isArray(detail) && detail.every(isConfigValidationIssue);
}

export class ConfigValidationError extends Error {
  readonly issues: ConfigValidationIssue[];

  constructor(issues: ConfigValidationIssue[]) {
    super("Configuration validation failed");
    this.name = "ConfigValidationError";
    this.issues = issues;
  }
}

export class ConfigStaleError extends Error {
  constructor(
    message = "Configuration changed while request was in progress. Retry the operation.",
  ) {
    super(message);
    this.name = "ConfigStaleError";
  }
}

async function responseDetail(response: Response): Promise<unknown> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    return payload.detail;
  } catch {
    return null;
  }
}

function responseGeneration(
  response: Response,
  fallbackGeneration: number,
): number {
  const headerValue =
    typeof response.headers?.get === "function"
      ? response.headers.get(CONFIG_GENERATION_HEADER)
      : null;
  const parsed =
    headerValue == null || headerValue.trim() === ""
      ? Number.NaN
      : Number.parseInt(headerValue, 10);
  return Number.isFinite(parsed) ? parsed : fallbackGeneration;
}

export async function loadConfig(): Promise<{
  config: RawConfig;
  generation: number;
}> {
  const response = await fetch(`${API_BASE}/config/load`, {
    method: "POST",
  });

  if (!response.ok) {
    const detail = await responseDetail(response);
    if (response.status === 422 && isConfigValidationIssueList(detail)) {
      throw new ConfigValidationError(detail);
    }
    if (response.status === 401) {
      throw new Error(
        "Authentication required. Please log in to access this instance.",
      );
    }
    if (response.status === 403) {
      throw new Error(
        "Access denied. You do not have permission to access this instance.",
      );
    }
    if (response.status === 500) {
      throw new Error(
        "Server error. Please try again later or contact support.",
      );
    }
    throw new Error(`Failed to load configuration (Error ${response.status})`);
  }

  return {
    config: (await response.json()) as RawConfig,
    generation: responseGeneration(response, 0),
  };
}

export async function getAgentPolicies(
  config: Pick<Config, "defaults"> | null | undefined,
  agents: Agent[],
): Promise<AgentPoliciesByAgent> {
  const agentsObject = agents.reduce(
    (acc, agent) => {
      const { id, ...rest } = agent;
      acc[id] = rest;
      return acc;
    },
    {} as Record<string, Omit<Agent, "id">>,
  );

  const response = await fetch(`${API_BASE}/config/agent-policies`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      defaults: config?.defaults ?? {},
      agents: agentsObject,
    }),
  });

  if (!response.ok) {
    throw new Error("Failed to derive agent policies");
  }

  const payload = (await response.json()) as {
    agent_policies: AgentPoliciesByAgent;
  };
  return payload.agent_policies;
}

export async function saveConfig(
  config: ConfigSavePayload,
  generation: number,
): Promise<{ generation: number }> {
  const response = await fetch(`${API_BASE}/config/save`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      [CONFIG_GENERATION_HEADER]: String(generation),
    },
    body: JSON.stringify(config),
  });

  if (!response.ok) {
    const detail = await responseDetail(response);
    if (response.status === 409) {
      throw new ConfigStaleError(
        typeof detail === "string" && detail.length > 0 ? detail : undefined,
      );
    }
    if (response.status === 422 && isConfigValidationIssueList(detail)) {
      throw new ConfigValidationError(detail);
    }
    if (typeof detail === "string" && detail.length > 0) {
      throw new Error(detail);
    }
    throw new Error(`Failed to save configuration (Error ${response.status})`);
  }

  return { generation: responseGeneration(response, generation + 1) };
}

export async function loadRawConfigSource(): Promise<{
  source: string;
  generation: number;
}> {
  const response = await fetch(`${API_BASE}/config/raw`);

  if (!response.ok) {
    const detail = await responseDetail(response);
    if (typeof detail === "string" && detail.length > 0) {
      throw new Error(detail);
    }
    throw new Error(
      `Failed to load raw configuration (Error ${response.status})`,
    );
  }

  const payload = (await response.json()) as { source: string };
  return {
    source: payload.source,
    generation: responseGeneration(response, 0),
  };
}

export async function saveRawConfigSource(
  source: string,
  generation: number,
): Promise<{ generation: number }> {
  const response = await fetch(`${API_BASE}/config/raw`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      [CONFIG_GENERATION_HEADER]: String(generation),
    },
    body: JSON.stringify({ source }),
  });

  if (!response.ok) {
    const detail = await responseDetail(response);
    if (response.status === 409) {
      throw new ConfigStaleError(
        typeof detail === "string" && detail.length > 0 ? detail : undefined,
      );
    }
    if (response.status === 422 && isConfigValidationIssueList(detail)) {
      throw new ConfigValidationError(detail);
    }
    if (typeof detail === "string" && detail.length > 0) {
      throw new Error(detail);
    }
    throw new Error(
      `Failed to save raw configuration (Error ${response.status})`,
    );
  }

  return { generation: responseGeneration(response, generation + 1) };
}

export async function getAvailableTools(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/tools`);

  if (!response.ok) {
    throw new Error("Failed to fetch available tools");
  }

  return response.json();
}

export async function getAvailableRooms(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/rooms`);

  if (!response.ok) {
    throw new Error("Failed to fetch available rooms");
  }

  return response.json();
}
