import { useEffect, useState } from "react";
import { Plug } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConnectedClients } from "./ConnectedClients";
import { AgentTable } from "./AgentTable";
import { requestConnection } from "./request";
import { useMcpSelection } from "./useMcpSelection";
import type { ConnectionList } from "./types";

export function Connections() {
  const gatewayUrl =
    "'" + window.location.origin.replace(/'/g, "'\\''") + "/mcp'";
  const mcp = useMcpSelection();
  const [connections, setConnections] = useState<ConnectionList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const refreshConnections = () => setRefreshVersion((version) => version + 1);
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
            cause instanceof Error
              ? cause.message
              : "Could not load connections. Reload this page to try again.",
          );
      });
    return () => controller.abort();
  }, []);

  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex items-start gap-4">
          <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border bg-background shadow-sm">
            <Plug className="h-5 w-5 text-primary" aria-hidden="true" />
          </span>
          <div className="space-y-1.5">
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Your connections
            </h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              See each agent’s tools and connect the accounts they need.
            </p>
          </div>
        </header>
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
        {!connections && !error && (
          <p role="status">Loading your connections…</p>
        )}
        {connections && (
          <AgentTable
            agents={connections.agents}
            mcp={mcp}
            refreshVersion={refreshVersion}
            onConnectionChange={refreshConnections}
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
