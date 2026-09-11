import { useCallback, useEffect, useRef, useState } from "react";
import { requestConnection } from "./request";

interface McpSelection {
  enabled: boolean;
  selected_agents: string[];
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

  const toggle = async (agent: string, checked: boolean) => {
    if (!selection?.enabled || mutation.current || loading || error) return;
    const controller = new AbortController();
    mutation.current = controller;
    setSaving(true);
    const agents = checked
      ? [...selection.selected_agents, agent]
      : selection.selected_agents.filter((name) => name !== agent);
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

  return { selection, error, loading, saving, reload, toggle };
}
