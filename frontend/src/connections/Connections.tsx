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
              Manage accounts and choose which tools your MCP clients can use.
            </p>
          </div>
        </header>
        {mcp.selection?.enabled && (
          <p className="text-sm text-muted-foreground">
            MCP access applies to every connected MCP client for your account.
            Select all compatible tools for an agent, or expand it to choose
            individual tools.
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
      </div>
    </main>
  );
}
