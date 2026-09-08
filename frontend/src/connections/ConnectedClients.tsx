import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { requestConnection } from "./request";

interface ConnectedClient {
  id: string;
  client_name: string;
  redirect_uri: string | null;
  created_at: number | null;
  last_used_at: number | null;
  idle_expires_at: number;
  expires_at: number;
}

interface ConnectedClientList {
  enabled: boolean;
  clients: ConnectedClient[];
  next_cursor?: string | null;
}

const clientsPath = "/api/connections/mcp/clients";

function clientHost(redirectUri: string | null): string | null {
  if (!redirectUri) return null;
  try {
    return new URL(redirectUri).host || null;
  } catch {
    return null;
  }
}

function DateValue({ timestamp }: { timestamp: number }) {
  const date = new Date(timestamp * 1000);
  return <time dateTime={date.toISOString()}>{date.toLocaleString()}</time>;
}

function mergeClients(
  current: ConnectedClient[],
  incoming: ConnectedClient[],
): ConnectedClient[] {
  const byId = new Map(current.map((client) => [client.id, client]));
  for (const client of incoming) byId.set(client.id, client);
  return [...byId.values()];
}

/**
 * Render the connected-clients section, or return `null` when the gateway is disabled.
 */
export function ConnectedClients() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [clients, setClients] = useState<ConnectedClient[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasAuthoritativeState, setHasAuthoritativeState] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const listRequest = useRef<AbortController | null>(null);
  const operation = useRef<AbortController | null>(null);

  const loadClients = useCallback(
    async (cursor: string | null, append: boolean) => {
      listRequest.current?.abort();
      const controller = new AbortController();
      listRequest.current = controller;
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);
      const path = cursor
        ? `${clientsPath}?cursor=${encodeURIComponent(cursor)}`
        : clientsPath;
      try {
        const result = await requestConnection<ConnectedClientList>(
          path,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setEnabled(result.enabled);
        setHasAuthoritativeState(true);
        setClients((current) =>
          append ? mergeClients(current, result.clients) : result.clients,
        );
        setNextCursor(result.next_cursor ?? null);
      } catch {
        if (!controller.signal.aborted) {
          setEnabled(true);
          setError(
            append
              ? "Could not load more connected clients. Try again."
              : "Could not load connected clients. Try again.",
          );
        }
      } finally {
        if (!controller.signal.aborted) {
          if (append) setLoadingMore(false);
          else setLoading(false);
        }
      }
    },
    [],
  );

  const reload = useCallback(() => loadClients(null, false), [loadClients]);

  useEffect(() => {
    void reload();
    return () => {
      listRequest.current?.abort();
      operation.current?.abort();
    };
  }, [reload]);

  const disconnect = async (client: ConnectedClient | null) => {
    if (client === null) setConfirmOpen(false);
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setBusy(client?.id ?? "all");
    setError(null);
    try {
      await requestConnection(
        client
          ? `${clientsPath}/${encodeURIComponent(client.id)}/revoke`
          : `${clientsPath}/revoke-all`,
        controller.signal,
        "POST",
      );
      if (!controller.signal.aborted) {
        setClients((current) =>
          client
            ? current.filter((currentClient) => currentClient.id !== client.id)
            : [],
        );
        if (client === null) setNextCursor(null);
        setHasAuthoritativeState(true);
        await reload();
      }
    } catch {
      if (!controller.signal.aborted)
        setError(
          client
            ? "Could not disconnect this client. Try again."
            : "Could not disconnect clients. Try again.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  if (enabled === false) return null;

  return (
    <section className="space-y-4" aria-labelledby="connected-clients-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2
            id="connected-clients-heading"
            className="text-2xl font-semibold tracking-tight"
          >
            Connected clients
          </h2>
          <p className="text-sm text-muted-foreground">
            Manage apps that can use your personal tools.
          </p>
        </div>
        {clients.length > 0 && (
          <Button
            variant="outline"
            disabled={busy !== null}
            onClick={() => setConfirmOpen(true)}
          >
            {busy === "all" ? "Disconnecting…" : "Disconnect all"}
          </Button>
        )}
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {loading && (
        <Card>
          <CardContent className="pt-6">
            <p
              role="status"
              className="flex items-center gap-2 text-sm text-muted-foreground"
            >
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              Loading connected clients…
            </p>
          </CardContent>
        </Card>
      )}
      {!loading &&
        !error &&
        !nextCursor &&
        hasAuthoritativeState &&
        clients.length === 0 && (
          <Card>
            <CardContent className="pt-6 text-muted-foreground">
              No clients are connected.
            </CardContent>
          </Card>
        )}
      <div className="space-y-3">
        {clients.map((client) => {
          const host = clientHost(client.redirect_uri);
          return (
            <Card key={client.id}>
              <CardContent className="flex flex-col gap-5 pt-6 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0 space-y-3">
                  <div>
                    <h3 className="break-words text-lg font-semibold">
                      {client.client_name}
                    </h3>
                    <p className="break-all text-sm text-muted-foreground">
                      {host ?? "Client address unavailable"}
                    </p>
                  </div>
                  <dl className="grid gap-3 text-sm sm:grid-cols-3">
                    <div>
                      <dt className="text-muted-foreground">Last used</dt>
                      <dd>
                        {client.last_used_at === null ? (
                          client.created_at === null ? (
                            "Unknown"
                          ) : (
                            "Never used"
                          )
                        ) : (
                          <DateValue timestamp={client.last_used_at} />
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">
                        Unused access expires
                      </dt>
                      <dd>
                        <DateValue timestamp={client.idle_expires_at} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">
                        Authorization expires
                      </dt>
                      <dd>
                        <DateValue timestamp={client.expires_at} />
                      </dd>
                    </div>
                  </dl>
                </div>
                <Button
                  className="shrink-0 self-start sm:self-center"
                  variant="outline"
                  disabled={busy !== null}
                  aria-label={`Disconnect ${client.client_name}${
                    host ? ` from ${host}` : ""
                  }`}
                  onClick={() => void disconnect(client)}
                >
                  {busy === client.id ? "Disconnecting…" : "Disconnect"}
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
      {nextCursor && (
        <Button
          variant="outline"
          disabled={loadingMore || busy !== null}
          onClick={() => void loadClients(nextCursor, true)}
        >
          {loadingMore ? "Loading…" : "Load more"}
        </Button>
      )}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect all clients?</DialogTitle>
            <DialogDescription>
              This removes access for all connected clients. Your connected
              service accounts will stay connected.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void disconnect(null)}>
              Disconnect all clients
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
