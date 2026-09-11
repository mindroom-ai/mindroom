import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentConnections } from "./types";
import { requestConnection } from "./request";

interface McpSelection {
  enabled: boolean;
  agents: Record<string, string[] | null>;
}

const selectionPath = "/api/connections/mcp/selection";

/** Keep one saved selection for every client, independently of service status. */
export function useMcpSelection() {
  const [selection, setSelection] = useState<McpSelection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const result = await requestConnection<McpSelection>(
        selectionPath,
        controller.signal,
      );
      if (!controller.signal.aborted) setSelection(result);
    } catch {
      if (!controller.signal.aborted)
        setError("Could not load MCP selection. Try again.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    return () => {
      request.current?.abort();
      mutation.current?.abort();
    };
  }, [reload]);

  const save = async (agents: McpSelection["agents"]) => {
    if (!selection?.enabled || mutation.current || loading || error) return;
    const controller = new AbortController();
    mutation.current = controller;
    setSaving(true);
    try {
      const result = await requestConnection<McpSelection>(
        selectionPath,
        controller.signal,
        "POST",
        { agents },
      );
      if (!controller.signal.aborted) setSelection(result);
    } catch {
      if (!controller.signal.aborted)
        setError(
          "Could not save MCP selection. Reload selection to check the saved state.",
        );
    } finally {
      mutation.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  };

  const selectedTools = (agent: string) =>
    selection && Object.prototype.hasOwnProperty.call(selection.agents, agent)
      ? selection.agents[agent]
      : undefined;

  const toggle = (agent: string, checked: boolean) => {
    if (!selection) return;
    const agents = { ...selection.agents };
    if (checked) return save({ ...agents, [agent]: null });
    delete agents[agent];
    return save(agents);
  };

  const toggleTool = (
    agent: AgentConnections,
    tool: string,
    checked: boolean,
  ) => {
    if (!selection) return;
    const agents = { ...selection.agents };
    const current = selectedTools(agent.agent_name);
    const tools = new Set(
      current === null
        ? agent.tools
            .filter((item) => !item.requires_room_context)
            .map((item) => item.name)
        : (current ?? []),
    );
    if (checked) tools.add(tool);
    else tools.delete(tool);
    if (tools.size) return save({ ...agents, [agent.agent_name]: [...tools] });
    delete agents[agent.agent_name];
    return save(agents);
  };

  return {
    selection,
    selectedTools,
    error,
    loading,
    saving,
    reload,
    toggle,
    toggleTool,
  };
}

export type McpSelectionState = ReturnType<typeof useMcpSelection>;
