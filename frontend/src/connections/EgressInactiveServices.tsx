import { useState } from "react";
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
import type { EgressInactiveService } from "./types";

// A reason this page does not know, from a newer server, still reads sensibly.
const REASON_TEXT: Record<string, string> = {
  shadowed:
    "Not in use: your administrator provides a service with this name, and theirs takes precedence.",
  invalid: "Not in use: this entry is no longer valid, so it is ignored.",
};
const REASON_FALLBACK = "Not in use: the broker ignores this entry.";

const DELETE_TEXT: Record<string, string> = {
  shadowed:
    "This removes your own entry. The administrator's service with this name stays, and so does the key saved for it.",
  invalid: "This removes the invalid entry and any key saved under its name.",
};
const DELETE_FALLBACK = "This removes the entry.";

function InactiveServiceRow({
  service,
  disabled,
  onDelete,
}: {
  service: EgressInactiveService;
  disabled: boolean;
  onDelete?: (name: string) => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    if (!onDelete) return;
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      await onDelete(service.name);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Could not delete the entry. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <li
      aria-label={`Inactive service ${service.name}`}
      className="border-t border-border/60 px-5 py-3 first:border-t-0"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <span className="font-mono text-sm">{service.name}</span>
          <p className="text-xs text-muted-foreground">
            {REASON_TEXT[service.reason] ?? REASON_FALLBACK}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-normal">
            Inactive
          </Badge>
          {onDelete && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy || disabled}
              aria-label={`Delete inactive service ${service.name}`}
              onClick={() => setConfirming(true)}
            >
              {busy ? "Deleting…" : "Delete"}
            </Button>
          )}
        </div>
      </div>
      {error && (
        <Alert variant="destructive" className="mt-3 p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {onDelete && (
        <Dialog
          open={confirming}
          onOpenChange={(open) => !open && setConfirming(false)}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Delete inactive {service.name}?</DialogTitle>
              <DialogDescription>
                {DELETE_TEXT[service.reason] ?? DELETE_FALLBACK}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button variant="destructive" onClick={() => void remove()}>
                Delete
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </li>
  );
}

/**
 * Entries in the agent's own store that the broker ignores, with why, so they
 * do not sit there unseen while still counting toward the limits.
 *
 * Without `onDelete` the entries are listed only: the viewer cannot manage them.
 */
export function InactiveServices({
  services,
  disabled,
  onDelete,
}: {
  services: EgressInactiveService[];
  /** Turns Delete off while something else is being written. */
  disabled: boolean;
  /** Rejects with an Error whose message the row shows. */
  onDelete?: (name: string) => Promise<void>;
}) {
  if (services.length === 0) return null;
  return (
    <section
      aria-label="Inactive services"
      className="border-t border-border/60 bg-muted/10"
    >
      <h3 className="px-5 pt-3 text-xs font-medium text-muted-foreground">
        Inactive
      </h3>
      <ul>
        {services.map((service) => (
          <InactiveServiceRow
            key={service.name}
            service={service}
            disabled={disabled}
            onDelete={onDelete}
          />
        ))}
      </ul>
    </section>
  );
}
