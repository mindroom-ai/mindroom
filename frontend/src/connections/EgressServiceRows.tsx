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
import { requestConnection } from "./request";
import type { EgressCredentialService } from "./types";

function statusLabel(service: EgressCredentialService): string {
  if (!service.configured) return "Not set";
  const updated = service.updated_at ? new Date(service.updated_at) : null;
  return updated && !Number.isNaN(updated.getTime())
    ? `Updated ${updated.toLocaleDateString()}`
    : "Set";
}

function EgressServiceRow({
  agentName,
  service,
  onChanged,
}: {
  agentName: string;
  service: EgressCredentialService;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<"save" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const path = `/api/connections/egress/agents/${encodeURIComponent(agentName)}/${encodeURIComponent(service.name)}`;
  const keyLabel = `${service.display_name} API key`;

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
      await requestConnection<void>(path, controller.signal, "PUT", {
        secret,
      });
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
    setConfirmOpen(false);
    const controller = new AbortController();
    operation.current = controller;
    setBusy("remove");
    setError(null);
    try {
      await requestConnection<void>(path, controller.signal, "DELETE");
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
          {!service.can_manage && (
            <span className="text-xs text-muted-foreground">
              Managed by credential managers
            </span>
          )}
          {service.can_manage && !editing && (
            <>
              <Button
                size="sm"
                variant="outline"
                disabled={busy !== null}
                aria-label={`${service.configured ? "Replace" : "Set"} ${keyLabel}`}
                onClick={() => {
                  setError(null);
                  setEditing(true);
                }}
              >
                {service.configured ? "Replace" : "Set"}
              </Button>
              {service.configured && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy !== null}
                  aria-label={`Remove ${keyLabel}`}
                  onClick={() => setConfirmOpen(true)}
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
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {keyLabel}?</DialogTitle>
            <DialogDescription>
              {service.is_shared
                ? "This deletes the shared key. Everyone using this agent loses access to this service until a key is set again."
                : "This deletes your saved key. Your agent loses access to this service until you set a key again."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void remove()}>
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}

/** API keys the egress broker injects into one agent's outbound requests. */
export function EgressServiceRows({
  agentName,
  services,
  onChanged,
}: {
  agentName: string;
  services: EgressCredentialService[];
  onChanged: () => void;
}) {
  return (
    <ul>
      {services.map((service) => (
        <EgressServiceRow
          key={service.name}
          agentName={agentName}
          service={service}
          onChanged={onChanged}
        />
      ))}
    </ul>
  );
}
