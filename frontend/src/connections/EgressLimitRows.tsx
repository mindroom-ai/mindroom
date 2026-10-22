import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "./ConfirmDialog";
import { canEdit, type ServiceEditing } from "./egressRowText";
import {
  type RowAction,
  RowActionsMenu,
  SERVICE_CELL,
  SERVICE_ROW,
  ServiceHosts,
} from "./EgressServiceTable";
import { errorText } from "./request";
import { ItemName } from "./ToolRow";
import type { EgressCredentialService } from "./types";

/**
 * Change limit and Remove limit as row actions, with the question before it
 * goes, for a limit the viewer can edit; none for any other or for `null`.
 * `label` names what the actions are for. The page turns every limit action
 * off while one is being written (`serviceEditing.disabled`), so a limit shown
 * in two rows cannot be removed twice.
 */
export function useLimitActions(
  limit: EgressCredentialService | null,
  label: string,
  serviceEditing: ServiceEditing,
  onError: (message: string | null) => void,
) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!limit || !canEdit(serviceEditing, limit))
    return { actions: [], dialog: null, busy: false };
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

  const actions: RowAction[] = [
    {
      label: "Change limit",
      ariaLabel: `Change limit for ${label}`,
      disabled: disabled || serviceEditing.editingName === limit.name,
      onSelect: () => serviceEditing.onEdit(limit),
    },
    {
      label: "Remove limit",
      ariaLabel: `Remove limit for ${label}`,
      disabled,
      destructive: true,
      onSelect: () => setConfirming(true),
    },
  ];
  const dialog = (
    <ConfirmDialog
      open={confirming}
      title={`Remove the limit on ${label}?`}
      description={serviceEditing.deleteWarning(limit)}
      confirmLabel="Remove limit"
      onCancel={() => setConfirming(false)}
      onConfirm={() => void remove()}
    />
  );
  return { actions, dialog, busy };
}

/** What a row shows in place of its actions while one of them runs. */
export function BusyNote({ children }: { children: string }) {
  return (
    <span role="status" className="text-xs text-muted-foreground">
      {children}
    </span>
  );
}

/** A service that only limits paths: no key, account, or status, just what it limits and how to change that. */
export function LimitRow({
  service,
  serviceEditing,
}: {
  service: EgressCredentialService;
  serviceEditing: ServiceEditing;
}) {
  const [error, setError] = useState<string | null>(null);
  const limitActions = useLimitActions(
    service,
    service.display_name,
    serviceEditing,
    setError,
  );
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
        {limitActions.busy ? (
          <BusyNote>Removing…</BusyNote>
        ) : (
          <RowActionsMenu
            service={service.display_name}
            actions={limitActions.actions}
          />
        )}
        {limitActions.dialog}
        {error && (
          <Alert variant="destructive" className="mt-2 p-2 text-left">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
      </td>
    </tr>
  );
}
