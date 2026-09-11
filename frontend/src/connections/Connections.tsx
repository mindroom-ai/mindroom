import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2, Plug } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ConnectedClients } from "./ConnectedClients";
import { connectWithPopup, type OAuthAuthorization } from "./oauthPopup";
import { requestConnection } from "./request";
import { useMcpSelection } from "./useMcpSelection";

interface ConnectionService {
  provider: string;
  is_shared: boolean;
  display_name: string;
  description: string;
  tools: string[];
}

interface AgentConnections {
  agent_name: string;
  agent_display_name: string;
  is_shared: boolean;
  services: ConnectionService[];
  tools: ConnectionTool[];
}

interface ConnectionTool {
  name: string;
  display_name: string;
  description: string;
  provider: string | null;
  requires_room_context: boolean;
}

interface ConnectionList {
  agents: AgentConnections[];
}

interface ConnectionStatus {
  provider: string;
  connected: boolean;
  can_connect: boolean;
  reset_required: boolean;
  account_label: string | null;
}

function ConnectionCard({
  agent,
  service,
  refreshVersion,
  onConnectionChange,
}: {
  agent: AgentConnections;
  service: ConnectionService;
  refreshVersion: number;
  onConnectionChange: () => void;
}) {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const statusRequest = useRef<AbortController | null>(null);
  const operation = useRef<AbortController | null>(null);
  const basePath = `/api/connections/agents/${encodeURIComponent(agent.agent_name)}/${encodeURIComponent(service.provider)}`;

  const loadStatus = useCallback(async () => {
    statusRequest.current?.abort();
    const controller = new AbortController();
    statusRequest.current = controller;
    setLoading(true);
    setError(null);
    try {
      const next = await requestConnection<ConnectionStatus>(
        `${basePath}/status`,
        controller.signal,
      );
      if (!controller.signal.aborted) setStatus(next);
    } catch {
      if (!controller.signal.aborted) {
        setStatus(null);
        setError("Could not load connection status. Try again.");
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [basePath]);

  useEffect(() => {
    void loadStatus();
    return () => statusRequest.current?.abort();
  }, [loadStatus, refreshVersion]);

  useEffect(() => () => operation.current?.abort(), []);

  const connect = async () => {
    const controller = new AbortController();
    operation.current = controller;
    setBusy("connect");
    setError(null);
    try {
      await connectWithPopup(
        service.provider,
        () =>
          requestConnection<OAuthAuthorization>(
            `${basePath}/connect`,
            controller.signal,
            "POST",
          ),
        controller.signal,
      );
      if (!controller.signal.aborted) onConnectionChange();
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not connect. Try again.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  const disconnect = async () => {
    setConfirmOpen(false);
    setBusy("disconnect");
    setError(null);
    const controller = new AbortController();
    operation.current = controller;
    try {
      await requestConnection(
        `${basePath}/disconnect`,
        controller.signal,
        "POST",
      );
      if (!controller.signal.aborted) onConnectionChange();
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not disconnect. Try again.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <CardTitle className="text-lg">{service.display_name}</CardTitle>
          {!loading && status && (
            <Badge variant={status.connected ? "default" : "secondary"}>
              {status.connected ? "Connected" : "Not connected"}
            </Badge>
          )}
        </div>
        <CardDescription>{service.description}</CardDescription>
        <div className="flex flex-wrap gap-2">
          {service.tools.map((name) => {
            const tool = agent.tools.find((item) => item.name === name);
            return (
              <Badge key={name} variant="outline">
                {tool?.display_name ?? name}
                {tool?.requires_room_context ? " · MindRoom only" : ""}
              </Badge>
            );
          })}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && (
          <p
            role="status"
            className="flex items-center gap-2 text-sm text-muted-foreground"
          >
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Checking connection…
          </p>
        )}
        {!loading && status?.account_label && (
          <p className="flex items-center gap-2 break-all text-sm">
            <CheckCircle2
              className="h-4 w-4 shrink-0 text-primary"
              aria-hidden="true"
            />
            {status.account_label}
          </p>
        )}
        {status?.reset_required && (
          <p className="text-sm text-muted-foreground">
            This connection needs to be reset before you can connect again.
          </p>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!loading && !status && (
          <Button variant="outline" onClick={() => void loadStatus()}>
            Retry status
          </Button>
        )}
        {!loading && status && (
          <div className="flex flex-wrap gap-2">
            {status.reset_required ? (
              <Button
                variant="outline"
                disabled={busy !== null}
                aria-label={`Reset ${service.display_name} connection`}
                onClick={() => setConfirmOpen(true)}
              >
                Reset connection
              </Button>
            ) : status.connected ? (
              <Button
                variant="outline"
                disabled={busy !== null}
                aria-label={`Disconnect ${service.display_name}`}
                onClick={() => setConfirmOpen(true)}
              >
                Disconnect
              </Button>
            ) : (
              <Button
                disabled={busy !== null || !status.can_connect}
                aria-label={`Connect ${service.display_name}`}
                onClick={() => void connect()}
              >
                {busy === "connect" ? "Connecting…" : "Connect"}
              </Button>
            )}
            {busy === "connect" && (
              <Button
                variant="ghost"
                onClick={() => {
                  operation.current?.abort();
                  setBusy(null);
                }}
              >
                Cancel
              </Button>
            )}
            {!status.connected &&
              !status.can_connect &&
              !status.reset_required && (
                <p className="w-full text-sm text-muted-foreground">
                  This service is not ready to connect yet.
                </p>
              )}
          </div>
        )}
      </CardContent>
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {status?.reset_required ? "Reset" : "Disconnect"}{" "}
              {service.display_name}?
            </DialogTitle>
            <DialogDescription>
              {service.is_shared
                ? `This removes the saved connection used by ${agent.agent_display_name}. Anyone using this connection will lose access until you connect again.`
                : "This removes your saved connection. Your assistant will lose access until you connect again."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void disconnect()}>
              {status?.reset_required ? "Reset connection" : "Disconnect"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

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
    <main className="min-h-screen bg-background px-5 py-12 sm:py-16">
      <div className="mx-auto max-w-3xl space-y-8">
        <header className="space-y-3">
          <Plug className="h-8 w-8 text-primary" aria-hidden="true" />
          <h1 className="text-3xl font-semibold tracking-tight">
            Your connections
          </h1>
          <p className="text-muted-foreground">
            Connect services for your personal assistant and shared agents you
            manage, and choose which tools your MCP clients can use.
          </p>
        </header>
        <ConnectedClients />
        {mcp.selection?.enabled && (
          <p className="text-sm text-muted-foreground">
            Expose an agent below to make its compatible tools available to
            every connected MCP client. Your selection only affects your
            clients.
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
        {connections?.agents.map((agent) => (
          <section
            key={agent.agent_name}
            aria-labelledby={`agent-${agent.agent_name}`}
            className="space-y-4"
          >
            <div className="flex items-center gap-3">
              <h2
                id={`agent-${agent.agent_name}`}
                className="text-xl font-semibold"
              >
                {agent.agent_display_name}
              </h2>
              <Badge variant="secondary">
                {agent.is_shared ? "Shared agent" : "Personal agent"}
              </Badge>
            </div>
            {mcp.selection?.enabled && (
              <label className="flex items-center gap-3 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-primary"
                  aria-label={`Expose ${agent.agent_display_name} through MCP`}
                  checked={mcp.selection.selected_agents.includes(
                    agent.agent_name,
                  )}
                  disabled={mcp.loading || mcp.saving || mcp.error !== null}
                  onChange={(event) =>
                    void mcp.toggle(agent.agent_name, event.target.checked)
                  }
                />
                Expose through MCP
              </label>
            )}
            {agent.services.length === 0 && agent.tools.length === 0 && (
              <Card>
                <CardContent className="pt-6 text-muted-foreground">
                  No tools are available for this agent yet.
                </CardContent>
              </Card>
            )}
            <div className="grid gap-5 sm:grid-cols-2">
              {agent.services.map((service) => (
                <ConnectionCard
                  key={service.provider}
                  agent={agent}
                  service={service}
                  refreshVersion={refreshVersion}
                  onConnectionChange={refreshConnections}
                />
              ))}
              {agent.tools
                .filter((tool) => tool.provider === null)
                .map((tool) => (
                  <Card key={tool.name}>
                    <CardHeader>
                      <div className="flex items-start justify-between gap-3">
                        <CardTitle className="text-lg">
                          {tool.display_name}
                        </CardTitle>
                        {tool.requires_room_context && (
                          <Badge variant="secondary">MindRoom only</Badge>
                        )}
                      </div>
                      <CardDescription>{tool.description}</CardDescription>
                    </CardHeader>
                  </Card>
                ))}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}
