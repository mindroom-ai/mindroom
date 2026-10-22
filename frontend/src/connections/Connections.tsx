import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConnectedClients } from "./ConnectedClients";
import { ConnectionsHeader } from "./ConnectionsHeader";
import { AgentTable } from "./AgentTable";
import { errorText, requestConnection } from "./request";
import { useMcpSelection } from "./useMcpSelection";
import type { AgentState, ConnectionList, ConnectionStatus } from "./types";

/** How many agents' states load at once. */
const STATE_REQUESTS = 6;
/** An agent whose state cannot load shows nothing connected and no keys, as an unreadable service or store does. */
const UNAVAILABLE: AgentState = { connected: {}, egress_services: null };

/**
 * Load every agent's state after the catalog, a few at a time, and merge each
 * into its agent as it arrives. An account change reloads them all, and each
 * agent keeps its previous state until the new one arrives. A status that a
 * card loads corrects its account until the agent's state arrives again.
 */
function useAgentStates(
  catalog: ConnectionList | null,
  refreshVersion: number,
) {
  const [states, setStates] = useState<Record<string, AgentState>>({});
  useEffect(() => {
    if (!catalog) return;
    const controller = new AbortController();
    const queue = catalog.agents.map((agent) => agent.agent_name);
    const loadNext = async (): Promise<void> => {
      const name = queue.shift();
      if (name === undefined || controller.signal.aborted) return;
      const state = await requestConnection<AgentState>(
        `/api/connections/agents/${encodeURIComponent(name)}/state`,
        controller.signal,
      ).catch(() => UNAVAILABLE);
      if (!controller.signal.aborted)
        setStates((known) => ({ ...known, [name]: state }));
      return loadNext();
    };
    for (let started = 0; started < STATE_REQUESTS; started += 1)
      void loadNext();
    return () => controller.abort();
  }, [catalog, refreshVersion]);
  const onAccountStatus = useCallback(
    (agentName: string, provider: string, status: ConnectionStatus) =>
      setStates((known) => {
        const state = known[agentName];
        if (!state || state.connected[provider] === status.connected)
          return known;
        const connected = { ...state.connected, [provider]: status.connected };
        return { ...known, [agentName]: { ...state, connected } };
      }),
    [],
  );
  const agents = useMemo(
    () =>
      catalog?.agents.map((agent) => {
        const state = states[agent.agent_name];
        if (!state) return agent;
        return {
          ...agent,
          services: agent.services.map((service) => ({
            ...service,
            connected: state.connected[service.provider] ?? false,
          })),
          egress_services: state.egress_services,
        };
      }) ?? null,
    [catalog, states],
  );
  return { agents, onAccountStatus };
}

export function Connections() {
  const gatewayUrl =
    "'" + window.location.origin.replace(/'/g, "'\\''") + "/mcp'";
  const mcp = useMcpSelection();
  const [connections, setConnections] = useState<ConnectionList | null>(null);
  const [error, setError] = useState<string | null>(null);
  // An account change reloads every agent's state, whose API key counts can
  // depend on the same account, and the tool statuses together.
  const [refreshVersion, setRefreshVersion] = useState(0);
  const refresh = () => setRefreshVersion((version) => version + 1);
  const { agents, onAccountStatus } = useAgentStates(
    connections,
    refreshVersion,
  );
  useEffect(() => {
    const controller = new AbortController();
    void requestConnection<ConnectionList>(
      "/api/connections",
      controller.signal,
    )
      .then((data) => {
        if (!controller.signal.aborted) setConnections(data);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(
            errorText(
              cause,
              "Could not load connections. Reload this page to try again.",
            ),
          );
      });
    return () => controller.abort();
  }, []);

  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl space-y-6">
        <ConnectionsHeader current="agents" title="Your MindRoom connections">
          See each agent’s tools and connect the accounts they need.
        </ConnectionsHeader>
        {mcp.selection?.enabled && (
          <p className="text-sm text-muted-foreground">
            The MCP gateway column controls access from external apps. It does
            not change which tools your agents can use in MindRoom.
          </p>
        )}
        {mcp.loading && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading MCP selection…
          </p>
        )}
        {mcp.selection?.unavailable_reason === "account_required" && (
          <Alert>
            <AlertDescription>
              MCP access isn't enabled for your account. You can still manage
              your MindRoom connections below. Ask an administrator to enable
              access from external apps.
            </AlertDescription>
          </Alert>
        )}
        {mcp.error && (
          <Alert variant="destructive">
            <AlertDescription>{mcp.error}</AlertDescription>
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              disabled={mcp.loading || mcp.saving}
              onClick={() => void mcp.reload()}
            >
              Reload selection
            </Button>
          </Alert>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!agents && !error && <p role="status">Loading your connections…</p>}
        {agents && (
          <AgentTable
            agents={agents}
            mcp={mcp}
            refreshVersion={refreshVersion}
            onConnectionChange={refresh}
            onAccountStatus={onAccountStatus}
          />
        )}
        <ConnectedClients />
        {mcp.selection?.enabled && (
          <footer className="space-y-3 border-t pt-5 text-xs text-muted-foreground">
            <div className="space-y-1">
              <h2 className="text-sm font-medium text-foreground">
                MindRoom MCP gateway
              </h2>
              <p>
                Use your selected agent tools outside MindRoom, in Claude Code,
                Codex, or another MCP client. Your selection applies to every
                connected MCP client for your account.
              </p>
            </div>
            {[
              [
                "Claude Code",
                `claude mcp add --transport http --scope user mindroom ${gatewayUrl}`,
              ],
              ["Codex", `codex mcp add mindroom --url ${gatewayUrl}`],
            ].map(([name, command]) => (
              <div
                key={name}
                className="grid gap-1.5 sm:grid-cols-[6rem_minmax(0,1fr)] sm:items-center sm:gap-3"
              >
                <span className="font-medium">{name}</span>
                <code className="block overflow-x-auto whitespace-nowrap rounded-md bg-muted/60 px-3 py-2 text-foreground">
                  {command}
                </code>
              </div>
            ))}
            <p>
              Run a command in your terminal, then sign in using{" "}
              <code>/mcp</code> in Claude Code or{" "}
              <code>codex mcp login mindroom</code> in Codex.
            </p>
          </footer>
        )}
      </div>
    </main>
  );
}
