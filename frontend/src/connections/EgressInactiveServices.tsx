import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  SERVICE_CELL,
  SERVICE_ROW,
  ServiceGroupHeading,
} from "./EgressServiceTable";
import { errorText } from "./request";
import { STACKED } from "./stackedTable";
import { ItemName } from "./ToolRow";
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
    <tr aria-label={`Inactive service ${service.name}`} className={SERVICE_ROW}>
      <th scope="row" className={SERVICE_CELL.name}>
        <ItemName
          names={[service.name]}
          title={<span className="font-mono text-sm">{service.name}</span>}
        >
          <p className="text-xs text-muted-foreground">
            {REASON_TEXT[service.reason]}
          </p>
        </ItemName>
      </th>
      <td className="px-4 py-3 max-sm:hidden" />
      <td data-label="Status" className={SERVICE_CELL.status}>
        <Badge variant="outline" className="font-normal">
          Inactive
        </Badge>
      </td>
      <td className={SERVICE_CELL.actions}>
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
        {error && (
          <Alert variant="destructive" className="mt-2 p-2 text-left">
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
      </td>
    </tr>
  );
}

/**
 * Entries in the agent's own store that the broker ignores, with why, so they
 * do not sit there unseen while still counting toward the caps; a row group
 * of an `EgressServiceTable`.
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
    <tbody aria-label="Inactive services" className={STACKED.body}>
      <ServiceGroupHeading label="Inactive" />
      {services.map((service) => (
        <InactiveServiceRow
          key={service.name}
          service={service}
          disabled={disabled}
          onDelete={onDelete}
        />
      ))}
    </tbody>
  );
}
