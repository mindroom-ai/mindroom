import { type FormEvent, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { connectWithPopup, type OAuthAuthorization } from "./oauthPopup";
import { type RequestErrorMessages, requestConnection } from "./request";
import type { EgressCredentialService } from "./types";

type AccountAction = "connect" | "disconnect";

/** The endpoints one service row talks to. */
interface ServicePaths {
  secret: string;
  connect: string;
  disconnect: string;
}

function statusLabel(service: EgressCredentialService): string {
  const oauth = service.oauth;
  if (oauth) {
    if (service.active_source === "key") return "Using API key";
    if (service.active_source === "oauth")
      return oauth.account_label
        ? `Connected as ${oauth.account_label}`
        : "Connected";
    return "Not set";
  }
  if (!service.configured) return "Not set";
  const updated = service.updated_at ? new Date(service.updated_at) : null;
  return updated && !Number.isNaN(updated.getTime())
    ? `Updated ${updated.toLocaleDateString()}`
    : "Set";
}

function removeWarning(service: EgressCredentialService): string {
  if (service.is_global)
    return "This deletes the global key, which every agent without a worker scope shares. Those agents lose access to this service until a key is set again.";
  if (service.is_shared === null)
    return "This deletes the saved key. The agent loses access to this service until a key is set again.";
  // A shared agent's key, or for an agent without a worker scope the global
  // key, so other agents may rely on it too.
  if (service.is_shared)
    return "This deletes the shared key. Everyone who relies on it loses access to this service until a key is set again.";
  return "This deletes your saved key. Your agent loses access to this service until you set a key again.";
}

function EgressServiceRow({
  paths,
  service,
  onChanged,
  errorMessages,
}: {
  paths: ServicePaths;
  service: EgressCredentialService;
  onChanged: () => void;
  errorMessages?: RequestErrorMessages;
}) {
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<
    "save" | "remove" | "connect" | "disconnect" | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"key" | "account" | null>(null);
  const operation = useRef<AbortController | null>(null);
  const keyLabel = `${service.display_name} API key`;
  const oauth = service.oauth;
  // Rows with an account keep the key behind a toggle until a key is set.
  const showKeyControls = !oauth || service.key_configured || editing;
  // Requester-scoped accounts belong to the requester, so the server lets any
  // eligible user manage them even without key rights.
  const canResetAccount =
    oauth !== null &&
    (oauth.connected || oauth.reset_required) &&
    (service.can_manage || oauth.can_connect);

  useEffect(() => () => operation.current?.abort(), []);

  const closeEditor = () => {
    setEditing(false);
    setSecret("");
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!secret.trim() || busy) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy("save");
    setError(null);
    try {
      await requestConnection<void>(
        paths.secret,
        controller.signal,
        "PUT",
        { secret },
        errorMessages,
      );
      if (controller.signal.aborted) return;
      closeEditor();
      onChanged();
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not save the key. Try again.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  const remove = async () => {
    setConfirm(null);
    const controller = new AbortController();
    operation.current = controller;
    setBusy("remove");
    setError(null);
    try {
      await requestConnection<void>(
        paths.secret,
        controller.signal,
        "DELETE",
        undefined,
        errorMessages,
      );
      if (!controller.signal.aborted) onChanged();
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not remove the key. Try again.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  const connect = async () => {
    if (!oauth) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy("connect");
    setError(null);
    try {
      await connectWithPopup(
        oauth.provider,
        () =>
          requestConnection<OAuthAuthorization>(
            paths.connect,
            controller.signal,
            "POST",
            {},
            errorMessages,
          ),
        controller.signal,
      );
      if (!controller.signal.aborted) onChanged();
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
    setConfirm(null);
    const controller = new AbortController();
    operation.current = controller;
    setBusy("disconnect");
    setError(null);
    try {
      await requestConnection<void>(
        paths.disconnect,
        controller.signal,
        "POST",
        {},
        errorMessages,
      );
      if (!controller.signal.aborted) onChanged();
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
    <li
      aria-label={service.display_name}
      className="border-t border-border/60 px-5 py-3 first:border-t-0"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <span className="font-medium">{service.display_name}</span>
          <p
            className="max-w-md truncate text-xs text-muted-foreground"
            title={service.description}
          >
            {service.description}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Badge
            variant={service.configured ? "secondary" : "outline"}
            className="font-normal"
          >
            {statusLabel(service)}
          </Badge>
          {oauth?.service_account && (
            <span className="text-xs text-muted-foreground">
              Uses a shared service account
            </span>
          )}
          {oauth?.unavailable_reason === "shared_worker" && (
            <span className="text-xs text-muted-foreground">
              Personal accounts are not used on shared agents; add an API key or
              ask an administrator
            </span>
          )}
          {oauth?.shared_worker_opt_in && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              Everyone using this agent can act with the connected account until
              its access expires
            </span>
          )}
          {!service.can_manage && (
            <span className="text-xs text-muted-foreground">
              {oauth?.can_connect
                ? "API key managed by credential managers"
                : "Managed by credential managers"}
            </span>
          )}
          {oauth?.can_connect && !oauth.connected && (
            <Button
              size="sm"
              disabled={busy !== null}
              aria-label={`${oauth.reset_required ? "Reconnect" : "Connect"} ${oauth.display_name}`}
              onClick={() => void connect()}
            >
              {busy === "connect"
                ? "Connecting…"
                : `${oauth.reset_required ? "Reconnect" : "Connect"} ${oauth.display_name}`}
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
          {oauth && canResetAccount && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy !== null}
              aria-label={
                oauth.connected
                  ? `Disconnect ${oauth.display_name}`
                  : `Reset ${oauth.display_name} connection`
              }
              onClick={() => setConfirm("account")}
            >
              {busy === "disconnect"
                ? "Disconnecting…"
                : oauth.connected
                  ? "Disconnect"
                  : "Reset connection"}
            </Button>
          )}
          {oauth && service.can_manage && !showKeyControls && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => {
                setError(null);
                setEditing(true);
              }}
            >
              Use an API key instead
            </Button>
          )}
          {service.can_manage && showKeyControls && !editing && (
            <>
              <Button
                size="sm"
                variant="outline"
                disabled={busy !== null}
                aria-label={`${service.key_configured ? "Replace" : "Set"} ${keyLabel}`}
                onClick={() => {
                  setError(null);
                  setEditing(true);
                }}
              >
                {service.key_configured ? "Replace" : "Set"}
              </Button>
              {service.key_configured && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy !== null}
                  aria-label={`Remove ${keyLabel}`}
                  onClick={() => setConfirm("key")}
                >
                  {busy === "remove" ? "Removing…" : "Remove"}
                </Button>
              )}
            </>
          )}
        </div>
      </div>
      {service.can_manage && editing && (
        <form
          className="mt-3 flex flex-wrap items-center gap-2"
          onSubmit={(event) => void save(event)}
        >
          <Input
            type="password"
            autoComplete="off"
            aria-label={keyLabel}
            placeholder="Paste the key"
            className="h-9 max-w-sm flex-1"
            value={secret}
            disabled={busy !== null}
            onChange={(event) => setSecret(event.target.value)}
          />
          <Button
            type="submit"
            size="sm"
            disabled={busy !== null || !secret.trim()}
          >
            {busy === "save" ? (
              <>
                <Loader2
                  className="mr-2 h-3.5 w-3.5 animate-spin"
                  aria-hidden="true"
                />
                Saving…
              </>
            ) : (
              "Save"
            )}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy !== null}
            onClick={closeEditor}
          >
            Cancel
          </Button>
        </form>
      )}
      {error && (
        <Alert variant="destructive" className="mt-3 p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <Dialog
        open={confirm === "key"}
        onOpenChange={(open) => !open && setConfirm(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {keyLabel}?</DialogTitle>
            <DialogDescription>{removeWarning(service)}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void remove()}>
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {oauth && (
        <Dialog
          open={confirm === "account"}
          onOpenChange={(open) => !open && setConfirm(null)}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {oauth.connected ? "Disconnect" : "Reset"} {oauth.display_name}?
              </DialogTitle>
              <DialogDescription>
                This removes the saved {oauth.display_name} connection. Agents
                lose access through it until an account is connected again.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirm(null)}>
                Cancel
              </Button>
              <Button variant="destructive" onClick={() => void disconnect()}>
                {oauth.connected ? "Disconnect" : "Reset connection"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </li>
  );
}

/** Where one service's secret is written and its account is connected. */
type ServiceTarget =
  | {
      agentName: string;
      secretPath?: undefined;
      accountPath?: undefined;
    }
  | {
      agentName?: undefined;
      secretPath: (serviceName: string) => string;
      accountPath: (serviceName: string, action: AccountAction) => string;
    };

function servicePathsFor(
  target: ServiceTarget,
  serviceName: string,
): ServicePaths {
  if (target.secretPath)
    return {
      secret: target.secretPath(serviceName),
      connect: target.accountPath(serviceName, "connect"),
      disconnect: target.accountPath(serviceName, "disconnect"),
    };
  const base = `/api/connections/egress/agents/${encodeURIComponent(target.agentName)}/${encodeURIComponent(serviceName)}`;
  return {
    secret: base,
    connect: `${base}/connect`,
    disconnect: `${base}/disconnect`,
  };
}

/**
 * Accounts and API keys the egress broker injects into outbound requests.
 *
 * Rows target one agent's personal connections API by default. Pass
 * `secretPath` and `accountPath` instead of `agentName` to use another
 * endpoint, such as the dashboard's.
 */
export function EgressServiceRows({
  services,
  onChanged,
  errorMessages,
  ...target
}: ServiceTarget & {
  services: EgressCredentialService[];
  onChanged: () => void;
  /** Wording for 403 and 404 responses outside the Connections portal. */
  errorMessages?: RequestErrorMessages;
}) {
  return (
    <ul>
      {services.map((service) => (
        <EgressServiceRow
          key={service.name}
          paths={servicePathsFor(target, service.name)}
          service={service}
          onChanged={onChanged}
          errorMessages={errorMessages}
        />
      ))}
    </ul>
  );
}
