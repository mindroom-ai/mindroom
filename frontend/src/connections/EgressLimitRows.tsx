import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "./ConfirmDialog";
import { canEdit, hostsNote, type ServiceEditing } from "./egressRowText";
import { errorText } from "./request";
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
  const hosts = hostsNote(service, "Limits requests to");
  return (
    <li
      aria-label={service.display_name}
      className="border-t border-border/60 px-5 py-3 first:border-t-0"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <span className="font-medium">{service.display_name}</span>
          {service.description && (
            <p
              className="max-w-md truncate text-xs text-muted-foreground"
              title={service.description}
            >
              {service.description}
            </p>
          )}
          {hosts && (
            <p className="break-words text-xs text-muted-foreground">{hosts}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 max-sm:justify-start sm:justify-end">
          <Badge variant="outline" className="font-normal">
            Limit only
          </Badge>
          {canEdit(serviceEditing, service) && (
            <LimitActions
              limit={service}
              label={service.display_name}
              serviceEditing={serviceEditing}
              onError={setError}
            />
          )}
        </div>
      </div>
      {error && (
        <Alert variant="destructive" className="mt-3 p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
    </li>
  );
}
