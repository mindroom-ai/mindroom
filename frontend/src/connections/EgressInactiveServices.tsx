import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "./ConfirmDialog";
import { errorText } from "./request";
import type { EgressInactiveReason, EgressInactiveService } from "./types";

const REASON_TEXT: Record<EgressInactiveReason, string> = {
  shadowed:
    "Not in use: your administrator provides a service with this name, and theirs takes precedence.",
  invalid: "Not in use: this entry is no longer valid, so it is ignored.",
};

const DELETE_TEXT: Record<EgressInactiveReason, string> = {
  shadowed:
    "This removes your own entry. The administrator's service with this name stays, and so does the key saved for it.",
  invalid: "This removes the invalid entry and any key saved under its name.",
};

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
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      await onDelete?.(service.name);
    } catch (cause) {
      setError(errorText(cause, "Could not delete the entry. Try again."));
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
            {REASON_TEXT[service.reason]}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
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
        <ConfirmDialog
          open={confirming}
          title={`Delete inactive ${service.name}?`}
          description={DELETE_TEXT[service.reason]}
          confirmLabel="Delete"
          onCancel={() => setConfirming(false)}
          onConfirm={() => void remove()}
        />
      )}
    </li>
  );
}

/**
 * Entries in the agent's own store that the broker ignores, with why, so they
 * do not sit there unseen while still counting toward the caps.
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
