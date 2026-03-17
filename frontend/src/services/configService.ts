import { Agent, Config, TeamEligibilityByAgent } from '@/types/config';

const API_BASE = '/api';

export interface ConfigValidationIssue {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

function isConfigValidationIssue(detail: unknown): detail is ConfigValidationIssue {
  return (
    typeof detail === 'object' &&
    detail !== null &&
    Array.isArray((detail as ConfigValidationIssue).loc) &&
    typeof (detail as ConfigValidationIssue).msg === 'string' &&
    typeof (detail as ConfigValidationIssue).type === 'string'
  );
}

function isConfigValidationIssueList(detail: unknown): detail is ConfigValidationIssue[] {
  return Array.isArray(detail) && detail.every(isConfigValidationIssue);
}

export class ConfigValidationError extends Error {
  readonly issues: ConfigValidationIssue[];

  constructor(issues: ConfigValidationIssue[]) {
    super('Configuration validation failed');
    this.name = 'ConfigValidationError';
    this.issues = issues;
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

export async function loadConfig(): Promise<Config> {
  const response = await fetch(`${API_BASE}/config/load`, {
    method: 'POST',
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error('Authentication required. Please log in to access this instance.');
    }
    if (response.status === 403) {
      throw new Error('Access denied. You do not have permission to access this instance.');
    }
    if (response.status === 500) {
      throw new Error('Server error. Please try again later or contact support.');
    }
    throw new Error(`Failed to load configuration (Error ${response.status})`);
  }

  return response.json();
}

export async function getTeamEligibility(agents: Agent[]): Promise<TeamEligibilityByAgent> {
  const agentsObject = agents.reduce(
    (acc, agent) => {
      const { id, ...rest } = agent;
      acc[id] = rest;
      return acc;
    },
    {} as Record<string, Omit<Agent, 'id'>>
  );

  const response = await fetch(`${API_BASE}/config/team-eligibility`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ agents: agentsObject }),
  });

  if (!response.ok) {
    throw new Error('Failed to derive team eligibility');
  }

  const payload = (await response.json()) as { team_eligibility: TeamEligibilityByAgent };
  return payload.team_eligibility;
}

export async function saveConfig(config: Config): Promise<void> {
  const response = await fetch(`${API_BASE}/config/save`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(config),
  });

  if (!response.ok) {
    const detail = await responseDetail(response);
    if (response.status === 422 && isConfigValidationIssueList(detail)) {
      throw new ConfigValidationError(detail);
    }
    if (typeof detail === 'string' && detail.length > 0) {
      throw new Error(detail);
    }
    throw new Error(`Failed to save configuration (Error ${response.status})`);
  }
}

export async function getAvailableTools(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/tools`);

  if (!response.ok) {
    throw new Error('Failed to fetch available tools');
  }

  return response.json();
}

export async function getAvailableRooms(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/rooms`);

  if (!response.ok) {
    throw new Error('Failed to fetch available rooms');
  }

  return response.json();
}
