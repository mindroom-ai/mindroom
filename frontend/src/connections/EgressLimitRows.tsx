import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "./ConfirmDialog";
import { canEdit, type ServiceEditing } from "./egressRowText";
import { SERVICE_CELL, SERVICE_ROW, ServiceHosts } from "./EgressServiceTable";
import { errorText } from "./request";
import { ItemName } from "./ToolRow";
import type { EgressCredentialService } from "./types";

/**
 * Change and Remove for a limit, with the question before it goes. `label`
 * names what the buttons are for. The page turns every limit action off while
 * one is being written (`serviceEditing.disabled`), so a limit shown in two
 * rows cannot be removed twice.
 */
export function LimitActions({
  limit,
  label,
  serviceEditing,
  onError,
}: {
  limit: EgressCredentialService;
  label: string;
  serviceEditing: ServiceEditing;
  onError: (message: string | null) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const disabled = busy || serviceEditing.disabled === true;

  const remove = async () => {
    setConfirming(false);
    setBusy(true);
    onError(null);
    try {
      await serviceEditing.onDelete(limit);
    } catch (cause) {
      onError(errorText(cause, "Could not remove the limit. Try again."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        disabled={disabled || serviceEditing.editingName === limit.name}
        aria-label={`Change limit for ${label}`}
        onClick={() => serviceEditing.onEdit(limit)}
      >
        Change limit
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={disabled}
        aria-label={`Remove limit for ${label}`}
        onClick={() => setConfirming(true)}
      >
        {busy ? "Removing…" : "Remove limit"}
      </Button>
      <ConfirmDialog
        open={confirming}
        title={`Remove the limit on ${label}?`}
        description={serviceEditing.deleteWarning(limit)}
        confirmLabel="Remove limit"
        onCancel={() => setConfirming(false)}
        onConfirm={() => void remove()}
      />
    </>
  );
}

/** A service that only limits paths: no key, account, or status, just what it limits and how to change that. */
export function LimitRow({
  service,
  serviceEditing,
}: {
  service: EgressCredentialService;
  serviceEditing?: ServiceEditing;
}) {
  const [error, setError] = useState<string | null>(null);
  return (
    <tr aria-label={service.display_name} className={SERVICE_ROW}>
      <th scope="row" className={SERVICE_CELL.name}>
        <ItemName
          names={[service.name, service.display_name]}
          title={service.display_name}
          description={service.description}
        />
      </th>
      <td data-label="Hosts" className={SERVICE_CELL.hosts}>
        <ServiceHosts service={service} />
      </td>
      <td data-label="Status" className={SERVICE_CELL.status}>
        <Badge variant="outline" className="font-normal">
          Limit only
        </Badge>
      </td>
      <td className={SERVICE_CELL.actions}>
        {canEdit(serviceEditing, service) && (
          <div className="flex flex-wrap items-center gap-2 max-sm:justify-start sm:justify-end">
            <LimitActions
              limit={service}
              label={service.display_name}
              serviceEditing={serviceEditing}
              onError={setError}
            />
          </div>
        )}
        {error && (
          <Alert variant="destructive" className="mt-2 p-2 text-left">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
      </td>
    </tr>
  );
}
