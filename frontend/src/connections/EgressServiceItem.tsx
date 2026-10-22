import { type FormEvent, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { disconnectDescription } from "./accountCopy";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  canEdit,
  limitedNote,
  limitOverlap,
  limitOf,
  limitTarget,
  removeWarning,
  type RowAgent,
  type ServiceEditing,
  servicesUsingAccount,
  sharesAccount,
  unsetNote,
} from "./egressRowText";
import { BusyNote, useLimitActions } from "./EgressLimitRows";
import { ServiceStatus } from "./EgressServiceStatus";
import {
  AgentSees,
  type RowAction,
  RowActionsMenu,
  SERVICE_CELL,
  SERVICE_ROW,
  ServiceHosts,
} from "./EgressServiceTable";
import { coversGithub } from "./githubLimits";
import { connectWithPopup, type OAuthAuthorization } from "./oauthPopup";
import {
  errorText,
  type RequestErrorMessages,
  requestConnection,
} from "./request";
import { SharedAccountDialog } from "./SharedAccountDialog";
import { ItemName } from "./ToolRow";
import type { EgressCredentialService } from "./types";

/** The endpoints one service row talks to. */
export interface ServicePaths {
  secret: string;
  connect: string;
  disconnect: string;
  /** Whether the service's account is connected now, read again from the listing the row came from. */
  accountConnected: (signal: AbortSignal) => Promise<boolean>;
}

export function EgressServiceItem({
  paths,
  service,
  onChanged,
  errorMessages,
  serviceEditing,
  agent,
  hideKeyActions,
}: {
  paths: ServicePaths;
  service: EgressCredentialService;
  onChanged: () => void;
  errorMessages?: RequestErrorMessages;
  serviceEditing: ServiceEditing;
  agent: RowAgent;
  hideKeyActions: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<
    "save" | "remove" | "connect" | "disconnect" | "delete" | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<
    "key" | "account" | "service" | "shared" | null
  >(null);
  const operation = useRef<AbortController | null>(null);
  const keyLabel = `${service.display_name} API key`;
  const oauth = service.oauth;
  // Rows with an account keep the key behind a toggle until a key is set.
  const showKeyControls = !oauth || service.key_configured || editing;
  const canManageKey = service.can_manage && !hideKeyActions;
  const keyInsteadLabel = oauth?.connected
    ? "Use an API key instead of the account"
    : "Use an API key instead";
  // Requester-scoped accounts belong to the requester, so the server lets any
  // eligible user manage them even without key rights.
  const canResetAccount =
    oauth !== null &&
    !hideKeyActions &&
    (oauth.connected || oauth.reset_required) &&
    (service.can_manage || oauth.can_connect);

  // A limit of the viewer's that narrows this administrator's service.
  const limit = limitOf(service, agent.services);
  // Another service of the viewer's with rules on the limit's hosts.
  const overlap =
    limit &&
    limitOverlap(
      limit.rules.map((rule) => rule.host),
      agent.services,
    );
  // A service that already has a limit changes or removes it instead of getting another.
  const canLimit =
    limit === null &&
    serviceEditing.onLimitToRepositories !== undefined &&
    service.source === "config" &&
    service.can_manage &&
    coversGithub(service.rules);
  // Everyone using a shared agent acts as the account connected there.
  const asksAboutSharedAccount = sharesAccount(agent, oauth);
  const limitedText = limitedNote(service);
  const unsetText = unsetNote(service);
  const limitActions = useLimitActions(
    limit,
    service.display_name,
    serviceEditing,
    setError,
  );

  useEffect(() => () => operation.current?.abort(), []);

  const openKeyForm = () => {
    setError(null);
    setEditing(true);
  };
  const closeEditor = () => {
    setEditing(false);
    setSecret("");
  };

  /**
   * Runs one key or account action: the row is busy and shows its error, a
   * saved key closes the key form, and the page reloads the row after it. An
   * action abandoned by leaving the page changes nothing.
   */
  const run = async (
    kind: "save" | "remove" | "connect" | "disconnect",
    failure: string,
    action: (signal: AbortSignal) => Promise<unknown>,
  ) => {
    setConfirm(null);
    const controller = new AbortController();
    operation.current = controller;
    setBusy(kind);
    setError(null);
    try {
      await action(controller.signal);
      if (controller.signal.aborted) return;
      if (kind === "save") closeEditor();
      onChanged();
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause, failure));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };
  const write =
    (path: string, method: string, body?: object) => (signal: AbortSignal) =>
      requestConnection<void>(path, signal, method, body, errorMessages);

  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!secret.trim() || busy) return;
    void run(
      "save",
      "Could not save the key. Try again.",
      write(paths.secret, "PUT", { secret }),
    );
  };

  const deleteService = async () => {
    setConfirm(null);
    setBusy("delete");
    setError(null);
    try {
      await serviceEditing.onDelete(service);
    } catch (cause) {
      setError(errorText(cause, "Could not delete the service. Try again."));
    } finally {
      setBusy(null);
    }
  };

  const connect = () => {
    // A second click on Continue, while the first connect is under way, starts nothing.
    if (!oauth || busy !== null) return;
    void run("connect", "Could not connect. Try again.", (signal) =>
      connectWithPopup(
        oauth.provider,
        () =>
          requestConnection<OAuthAuthorization>(
            paths.connect,
            signal,
            "POST",
            {},
            errorMessages,
          ),
        signal,
        // A popup that closed after connecting is a connection, not a cancel.
        () => paths.accountConnected(signal),
      ),
    );
  };

  const name = service.display_name;
  // Connecting reads the saved connection first, so an unreadable one needs a reset before.
  const canConnect =
    oauth !== null &&
    !hideKeyActions &&
    oauth.can_connect &&
    !oauth.connected &&
    !oauth.reset_required;
  const keyActions = canManageKey && service.key_configured && !editing;
  // A key is the main action when there is no account to connect instead.
  const setKeyFirst =
    canManageKey &&
    !service.key_configured &&
    !editing &&
    (!oauth || (!canConnect && !oauth.connected && !oauth.reset_required));
  const editable = canEdit(serviceEditing, service);
  // Everything but the action that makes the service work, which is a button of its own.
  const actions: RowAction[] = [
    ...(oauth && canManageKey && !showKeyControls && !setKeyFirst
      ? [
          {
            label: keyInsteadLabel,
            ariaLabel: `${keyInsteadLabel} for ${name}`,
            onSelect: openKeyForm,
          },
        ]
      : []),
    ...(keyActions
      ? [
          {
            label: "Replace key",
            ariaLabel: `Replace key for ${name}`,
            onSelect: openKeyForm,
          },
        ]
      : []),
    ...(canLimit && serviceEditing.onLimitToRepositories
      ? [
          {
            label: "Limit to repositories",
            ariaLabel: `Limit to repositories for ${name}`,
            disabled: serviceEditing.disabled || serviceEditing.limitDisabled,
            onSelect: () => serviceEditing.onLimitToRepositories?.(service),
          },
        ]
      : []),
    ...limitActions.actions,
    ...(editable
      ? [
          {
            label: "Edit service",
            ariaLabel: `Edit service for ${name}`,
            disabled:
              serviceEditing.disabled ||
              serviceEditing.editingName === service.name,
            onSelect: () => serviceEditing.onEdit(service),
          },
        ]
      : []),
    ...(keyActions
      ? [
          {
            label: "Remove key",
            ariaLabel: `Remove key for ${name}`,
            destructive: true,
            onSelect: () => setConfirm("key"),
          },
        ]
      : []),
    ...(oauth?.connected && canResetAccount
      ? [
          {
            label: "Disconnect",
            ariaLabel: `Disconnect for ${name}`,
            destructive: true,
            onSelect: () => setConfirm("account"),
          },
        ]
      : []),
    ...(editable
      ? [
          {
            label: "Delete service",
            ariaLabel: `Delete service for ${name}`,
            disabled: serviceEditing.disabled,
            destructive: true,
            onSelect: () => setConfirm("service"),
          },
        ]
      : []),
  ];
  const busyNote =
    busy === "remove" || limitActions.busy
      ? "Removing…"
      : busy === "disconnect"
        ? "Disconnecting…"
        : busy === "delete"
          ? "Deleting…"
          : null;

  return (
    <tr aria-label={service.display_name} className={SERVICE_ROW}>
      {/* What the service is, what the agent sees, and the limit that narrows it. */}
      <th scope="row" className={SERVICE_CELL.name}>
        <ItemName
          names={[
            service.name,
            service.display_name,
            ...(oauth ? [oauth.provider] : []),
          ]}
          title={service.display_name}
          description={service.description}
        >
          <AgentSees service={service} />
          {limitedText && (
            <p className="text-xs text-muted-foreground">{limitedText}</p>
          )}
          {limit && (
            <p className="text-xs text-muted-foreground">
              {overlap
                ? `Limited to ${limitTarget(limit)}. ${overlap}`
                : `Limited to ${limitTarget(limit)}`}
            </p>
          )}
        </ItemName>
      </th>
      <td data-label="Hosts" className={SERVICE_CELL.hosts}>
        <ServiceHosts service={service} />
      </td>
      <td data-label="Status" className={SERVICE_CELL.status}>
        <div className="flex flex-col items-start gap-1">
          <ServiceStatus
            service={service}
            agent={agent}
            serviceEditing={serviceEditing}
          />
          {unsetText && (
            <span className="text-xs text-muted-foreground">{unsetText}</span>
          )}
        </div>
      </td>
      <td className={SERVICE_CELL.actions}>
        <div className="flex items-center gap-1 max-sm:justify-start sm:justify-end">
          {busyNote ? (
            <BusyNote>{busyNote}</BusyNote>
          ) : canConnect ? (
            <>
              <Button
                size="sm"
                disabled={busy !== null}
                aria-label={`Connect ${oauth.display_name} for ${name}`}
                onClick={() =>
                  asksAboutSharedAccount ? setConfirm("shared") : connect()
                }
              >
                {busy === "connect"
                  ? "Connecting…"
                  : // The row names the service; only another account needs naming.
                    oauth.display_name === name
                    ? "Connect"
                    : `Connect ${oauth.display_name}`}
              </Button>
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
            </>
          ) : oauth?.reset_required && canResetAccount ? (
            <Button
              size="sm"
              variant="outline"
              disabled={busy !== null}
              aria-label={`Reset connection for ${name}`}
              onClick={() => setConfirm("account")}
            >
              Reset connection
            </Button>
          ) : (
            setKeyFirst && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy !== null}
                aria-label={`Set key for ${name}`}
                onClick={openKeyForm}
              >
                Set key
              </Button>
            )
          )}
          <RowActionsMenu
            service={name}
            actions={actions}
            disabled={busy !== null || limitActions.busy}
          />
        </div>
        {canManageKey && editing && (
          <form
            className="mt-2 flex flex-wrap items-center gap-2 sm:flex-nowrap"
            onSubmit={save}
          >
            <Input
              type="password"
              autoComplete="off"
              aria-label={keyLabel}
              placeholder="Paste the key"
              autoFocus
              className="h-9 min-w-0 flex-1 max-sm:basis-full"
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
          <Alert variant="destructive" className="mt-2 p-2 text-left">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {limitActions.dialog}
        <ConfirmDialog
          open={confirm === "key"}
          title={`Remove ${keyLabel}?`}
          description={removeWarning(service)}
          confirmLabel="Remove"
          onCancel={() => setConfirm(null)}
          onConfirm={() =>
            void run(
              "remove",
              "Could not remove the key. Try again.",
              write(paths.secret, "DELETE"),
            )
          }
        />
        {editable && (
          <ConfirmDialog
            open={confirm === "service"}
            title={`Delete ${service.display_name}?`}
            description={serviceEditing.deleteWarning(service)}
            confirmLabel="Delete"
            onCancel={() => setConfirm(null)}
            onConfirm={() => void deleteService()}
          />
        )}
        {oauth && (
          <SharedAccountDialog
            open={confirm === "shared"}
            agent={agent.displayName}
            provider={oauth.display_name}
            onCancel={() => setConfirm(null)}
            onContinue={() => {
              setConfirm(null);
              connect();
            }}
          />
        )}
        {oauth && (
          <ConfirmDialog
            open={confirm === "account"}
            title={`${oauth.connected ? "Disconnect" : "Reset"} ${oauth.display_name}?`}
            description={disconnectDescription(
              oauth.display_name,
              agent.displayName,
              servicesUsingAccount(agent.services, oauth.provider),
            )}
            confirmLabel={oauth.connected ? "Disconnect" : "Reset connection"}
            onCancel={() => setConfirm(null)}
            onConfirm={() =>
              void run(
                "disconnect",
                "Could not disconnect. Try again.",
                write(paths.disconnect, "POST"),
              )
            }
          />
        )}
      </td>
    </tr>
  );
}
