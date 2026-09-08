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

interface ConnectionService {
  provider: string;
  display_name: string;
  description: string;
  tools: string[];
}

interface ConnectionList {
  agent_display_name: string;
  services: ConnectionService[];
}

interface ConnectionStatus {
  provider: string;
  connected: boolean;
  can_connect: boolean;
  reset_required: boolean;
  account_label: string | null;
}

function ConnectionCard({ service }: { service: ConnectionService }) {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const statusRequest = useRef<AbortController | null>(null);
  const operation = useRef<AbortController | null>(null);
  const basePath = `/api/connections/${encodeURIComponent(service.provider)}`;

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
    return () => {
      statusRequest.current?.abort();
      operation.current?.abort();
    };
  }, [loadStatus]);

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
      if (!controller.signal.aborted) await loadStatus();
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
      if (!controller.signal.aborted) await loadStatus();
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
              This removes your saved connection. Your assistant will lose
              access until you connect again.
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
  const [connections, setConnections] = useState<ConnectionList | null>(null);
  const [error, setError] = useState<string | null>(null);
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
            Connect the services your personal assistant can use for you.
          </p>
          {connections && (
            <p className="text-sm font-medium">
              {connections.agent_display_name}
            </p>
          )}
        </header>
        <ConnectedClients />
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!connections && !error && (
          <p role="status">Loading your connections…</p>
        )}
        {connections?.services.length === 0 && (
          <Card>
            <CardContent className="pt-6 text-muted-foreground">
              No services are available for your assistant yet.
            </CardContent>
          </Card>
        )}
        <div className="grid gap-5 sm:grid-cols-2">
          {connections?.services.map((service) => (
            <ConnectionCard key={service.provider} service={service} />
          ))}
        </div>
      </div>
    </main>
  );
}
