import { useEffect, useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { connectWithPopup, type OAuthAuthorization } from "./oauthPopup";
import { errorText, requestConnection } from "./request";
import { SharedAccountDialog } from "./SharedAccountDialog";
import type {
  AgentConnections,
  ConnectionConsentGroup,
  ConnectionService,
  ConnectionStatus,
} from "./types";

interface ConnectableGroup {
  group: ConnectionConsentGroup;
  /** In the agent's service order, so the first is the one whose callback the server finishes. */
  candidates: ConnectionService[];
}

/** The groups with two or more services this user can connect with one sign-in, in the order they first appear. */
function connectableGroups(
  agent: AgentConnections,
  statuses: Readonly<Record<string, ConnectionStatus>>,
): ConnectableGroup[] {
  const groups = new Map<string, ConnectableGroup>();
  for (const service of agent.services) {
    const status = statuses[service.provider];
    if (
      !service.consent_group ||
      !service.can_manage ||
      !status ||
      status.connected ||
      !status.can_connect ||
      status.reset_required
    )
      continue;
    const entry = groups.get(service.consent_group.key) ?? {
      group: service.consent_group,
      candidates: [],
    };
    entry.candidates.push(service);
    groups.set(service.consent_group.key, entry);
  }
  return [...groups.values()].filter(({ candidates }) => candidates.length > 1);
}

function ConsentGroupRow({
  agent,
  group,
  candidates,
  onConnectionChange,
}: ConnectableGroup & {
  agent: AgentConnections;
  onConnectionChange: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sharedOpen, setSharedOpen] = useState(false);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
  const agentPath = `/api/connections/agents/${encodeURIComponent(agent.agent_name)}`;

  /** One abandoned by leaving the page or by Cancel changes nothing; any other end reloads the statuses. */
  const connect = async () => {
    const controller = new AbortController();
    const { signal } = controller;
    operation.current = controller;
    setBusy(true);
    setError(null);
    try {
      // The server picks the leading service, which the page may not know yet.
      await connectWithPopup(
        null,
        () =>
          requestConnection<OAuthAuthorization>(
            `${agentPath}/consent-groups/${encodeURIComponent(group.key)}/connect`,
            signal,
            "POST",
          ),
        signal,
        // A popup that closed after connecting is a connection, not a cancel.
        async (lead) =>
          (
            await requestConnection<ConnectionStatus>(
              `${agentPath}/${encodeURIComponent(lead)}/status`,
              signal,
            )
          ).connected,
      );
      if (!signal.aborted) onConnectionChange();
    } catch (cause) {
      if (!signal.aborted) {
        setError(errorText(cause, "Could not connect. Try again."));
        onConnectionChange();
      }
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  };

  return (
    <div className="border-b border-border/60 px-5 py-3 max-sm:px-4">
      {/* The action lines up with the Actions column of the service rows below. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {`${candidates.length} ${group.display_name} services are not connected.`}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              candidates.some((service) => service.is_shared)
                ? setSharedOpen(true)
                : void connect()
            }
          >
            {busy
              ? "Connecting…"
              : `Connect all ${group.display_name} services`}
          </Button>
          {busy && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                operation.current?.abort();
                setBusy(false);
              }}
            >
              Cancel
            </Button>
          )}
        </div>
      </div>
      {error && (
        <Alert variant="destructive" className="mt-2 p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <SharedAccountDialog
        open={sharedOpen}
        agent={agent.agent_display_name}
        provider={group.display_name}
        onCancel={() => setSharedOpen(false)}
        onContinue={() => {
          setSharedOpen(false);
          void connect();
        }}
      />
    </div>
  );
}

/**
 * One Connect all per consent group, for the services of an agent that one
 * sign-in can connect together; it stays out until two of them are waiting.
 */
export function ConsentGroupConnect({
  agent,
  statuses,
  onConnectionChange,
}: {
  agent: AgentConnections;
  /** Each service's loaded status, by provider; a service whose status has not loaded is left out. */
  statuses: Readonly<Record<string, ConnectionStatus>>;
  onConnectionChange: () => void;
}) {
  return (
    <>
      {connectableGroups(agent, statuses).map((entry) => (
        <ConsentGroupRow
          key={entry.group.key}
          agent={agent}
          onConnectionChange={onConnectionChange}
          {...entry}
        />
      ))}
    </>
  );
}
