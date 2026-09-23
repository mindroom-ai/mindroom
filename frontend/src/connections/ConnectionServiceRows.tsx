import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { connectWithPopup, type OAuthAuthorization } from "./oauthPopup";
import { requestConnection } from "./request";
import { ToolName, ToolExposure } from "./ToolRow";
import type {
  AgentConnections,
  ConnectionService,
  ConnectionStatus,
} from "./types";
import type { McpSelectionState } from "./useMcpSelection";

/** One account status and operation lifecycle, even when several tools share it. */
export function ConnectionServiceRows({
  agent,
  service,
  mcp,
  refreshVersion,
  onConnectionChange,
}: {
  agent: AgentConnections;
  service: ConnectionService;
  mcp: McpSelectionState;
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

  const tools = agent.tools.filter(
    (tool) => tool.provider === service.provider,
  );
  return (
    <>
      {tools.map((tool, index) => (
        <Fragment key={tool.name}>
          <tr className="border-t border-border/60">
            <th scope="row" className="px-5 py-3 text-left font-normal">
              <ToolName tool={tool} />
            </th>
            {index === 0 && (
              <td rowSpan={tools.length} className="px-4 py-3 align-top">
                {loading ? (
                  <span
                    role="status"
                    className="flex items-center gap-2 text-sm text-muted-foreground"
                  >
                    <Loader2
                      className="h-3.5 w-3.5 animate-spin"
                      aria-hidden="true"
                    />
                    Checking connection…
                  </span>
                ) : (
                  status && (
                    <>
                      <span className="flex items-center gap-1.5 text-sm">
                        {status.connected && (
                          <CheckCircle2
                            className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400"
                            aria-hidden="true"
                          />
                        )}
                        {!service.can_manage
                          ? status.connected
                            ? "Shared connection configured"
                            : "Shared connection unavailable"
                          : status.reset_required
                            ? "Reset required"
                            : status.connected
                              ? "Connected"
                              : "Not connected"}
                      </span>
                      {status.account_label && (
                        <p className="mt-1 max-w-56 break-all text-xs text-muted-foreground">
                          {status.account_label}
                        </p>
                      )}
                    </>
                  )
                )}
                {error && (
                  <Alert variant="destructive" className="mt-2 p-2">
                    <AlertDescription>{error}</AlertDescription>
                  </Alert>
                )}
              </td>
            )}
            <td className="px-4 py-3">
              <ToolExposure agent={agent} tool={tool} mcp={mcp} />
            </td>
            {index === 0 && (
              <td
                rowSpan={tools.length}
                className="px-5 py-3 text-right align-top"
              >
                {!loading && !status && (
                  <Button variant="outline" onClick={() => void loadStatus()}>
                    Retry status
                  </Button>
                )}
                {!loading && status && !service.can_manage && (
                  <span className="text-xs text-muted-foreground">
                    Managed by credential managers
                  </span>
                )}
                {!loading && status && service.can_manage && (
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    {status.reset_required ? (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        aria-label={`Reset ${service.display_name} connection`}
                        onClick={() => setConfirmOpen(true)}
                      >
                        Reset connection
                      </Button>
                    ) : status.connected ? (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy !== null}
                        aria-label={`Disconnect ${service.display_name}`}
                        onClick={() => setConfirmOpen(true)}
                      >
                        Disconnect
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        disabled={busy !== null || !status.can_connect}
                        aria-label={`Connect ${service.display_name}`}
                        onClick={() => void connect()}
                      >
                        {busy === "connect" ? "Connecting…" : "Connect"}
                      </Button>
                    )}
                    {busy === "connect" && (
                      <Button
                        size="sm"
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
                      <Button
                        variant="outline"
                        onClick={() => setConfirmOpen(false)}
                      >
                        Cancel
                      </Button>
                      <Button
                        variant="destructive"
                        onClick={() => void disconnect()}
                      >
                        {status?.reset_required
                          ? "Reset connection"
                          : "Disconnect"}
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              </td>
            )}
          </tr>
        </Fragment>
      ))}
    </>
  );
}
