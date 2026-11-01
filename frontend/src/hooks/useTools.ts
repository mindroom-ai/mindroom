import { useMemo } from "react";
import { API_ENDPOINTS, fetchJSON, withAgentExecutionScope } from "@/lib/api";
import type { WorkerScope } from "@/types/config";
import { useFetchData } from "./useFetchData";

export interface ToolFieldSchema {
  name: string;
  label: string;
  type:
    | "boolean"
    | "number"
    | "password"
    | "select"
    | "string[]"
    | "text"
    | "url";
  required?: boolean;
  default?: unknown;
  placeholder?: string | null;
  description?: string | null;
  requiredWhenFieldChanges?: string | null;
  options?: Array<{ label: string; value: string }> | null;
  validation?: Record<string, unknown> | null;
  /** False when a tool entry may not set the field, such as a password field, whose secret the Tools page saves. */
  authored_override?: boolean;
}

export interface ToolInfo {
  name: string;
  display_name: string;
  description: string;
  category: string;
  status: string;
  setup_type: string;
  default_execution_target?: string | null;
  icon: string | null;
  icon_color: string | null;
  config_fields: ToolFieldSchema[] | null;
  agent_override_fields?: ToolFieldSchema[] | null;
  dependencies: string[] | null;
  auth_provider?: string | null;
  docs_url?: string | null;
  helper_text?: string | null;
  function_names?: string[] | null;
  dashboard_configuration_supported?: boolean;
  execution_scope_supported?: boolean;
  /** Whether an agent's entry may set defer/initial; presets and control-plane tools may not. */
  lazy_loading_supported?: boolean;
  oauth_fallback_fields?: string[];
  manual_auth_configured?: boolean;
  environment_auth_configured?: boolean;
}

export interface ToolsResponse {
  tools: ToolInfo[];
  status_authoritative?: boolean;
}

const DEFAULT_RESPONSE: ToolsResponse = {
  tools: [],
  status_authoritative: true,
};

export function useTools(
  agentName?: string | null,
  executionScope?: WorkerScope | null,
) {
  const fetcher = useMemo(
    () => async () =>
      fetchJSON<ToolsResponse>(
        withAgentExecutionScope(API_ENDPOINTS.tools, agentName, executionScope),
      ),
    [agentName, executionScope],
  );
  const { data: response, ...rest } = useFetchData(fetcher, DEFAULT_RESPONSE);
  return {
    tools: response.tools,
    statusAuthoritative: response.status_authoritative ?? true,
    ...rest,
  };
}

// Map a backend tool to the fields of its Integrations card
export function mapToolToIntegration(tool: ToolInfo) {
  const setup_type: "oauth" | "api_key" | "special" | "none" =
    tool.setup_type === "oauth" ||
    tool.setup_type === "api_key" ||
    tool.setup_type === "special"
      ? tool.setup_type
      : "none";
  // For tools that require configuration, "available" means they are configured.
  const status: "connected" | "not_connected" | "available" =
    tool.status === "requires_config"
      ? "not_connected"
      : tool.status === "available" && setup_type !== "none"
        ? "connected"
        : "available";

  return {
    id: tool.name,
    name: tool.display_name,
    description: tool.description,
    category: tool.category,
    status,
    setup_type,
    config_fields: tool.config_fields,
    docs_url: tool.docs_url,
    helper_text: tool.helper_text,
    oauth_fallback_fields: tool.oauth_fallback_fields,
    manual_auth_configured: tool.manual_auth_configured,
    environment_auth_configured: tool.environment_auth_configured,
  };
}
