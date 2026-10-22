import type { RowAgent, ServiceEditing } from "./egressRowText";
import { LimitRow } from "./EgressLimitRows";
import { EgressServiceItem, type ServicePaths } from "./EgressServiceItem";
import { ServiceGroupHeading } from "./EgressServiceTable";
import { type RequestErrorMessages, requestConnection } from "./request";
import { STACKED } from "./stackedTable";
import type { EgressCredentialList, EgressCredentialService } from "./types";

type AccountAction = "connect" | "disconnect";

/** Where one service's secret is written and its account is connected. */
type ServiceTarget =
  | {
      agentName: string;
      secretPath?: undefined;
      accountPath?: undefined;
      servicesPath?: undefined;
    }
  | {
      agentName?: undefined;
      secretPath: (serviceName: string) => string;
      accountPath: (serviceName: string, action: AccountAction) => string;
      /** The listing these services came from, `{services: [...]}`, read again when a login popup closes without answering. */
      servicesPath: string;
    };

const EGRESS_LISTING_PATH = "/api/connections/egress";

/** Where the personal page writes a service's API key. */
export const keyPath = (agentName: string, serviceName: string) =>
  `${EGRESS_LISTING_PATH}/agents/${encodeURIComponent(agentName)}/${encodeURIComponent(serviceName)}`;

/** Where the personal page reads and writes one of its own services. */
export const servicePath = (agentName: string, serviceName: string) =>
  `${EGRESS_LISTING_PATH}/agents/${encodeURIComponent(agentName)}/services/${encodeURIComponent(serviceName)}`;

const accountConnectedIn = (
  services: readonly EgressCredentialService[] | undefined,
  serviceName: string,
) =>
  services?.find((service) => service.name === serviceName)?.oauth
    ?.connected === true;

// The egress listings say whether each service's account is connected, for any
// agent and provider; a provider's own status route only knows the agent's tools.
function servicePathsFor(
  target: ServiceTarget,
  serviceName: string,
): ServicePaths {
  if (target.secretPath) {
    const { servicesPath } = target;
    return {
      secret: target.secretPath(serviceName),
      connect: target.accountPath(serviceName, "connect"),
      disconnect: target.accountPath(serviceName, "disconnect"),
      accountConnected: async (signal) =>
        accountConnectedIn(
          (
            await requestConnection<{ services: EgressCredentialService[] }>(
              servicesPath,
              signal,
            )
          ).services,
          serviceName,
        ),
    };
  }
  const { agentName } = target;
  const base = keyPath(agentName, serviceName);
  return {
    secret: base,
    connect: `${base}/connect`,
    disconnect: `${base}/disconnect`,
    accountConnected: async (signal) =>
      accountConnectedIn(
        (
          await requestConnection<EgressCredentialList>(
            EGRESS_LISTING_PATH,
            signal,
          )
        ).agents.find((agent) => agent.agent_name === agentName)?.services,
        serviceName,
      ),
  };
}

/**
 * Accounts and API keys the egress broker injects into outbound requests, as
 * one row group of an `EgressServiceTable`.
 *
 * Rows target one agent's personal connections API by default. Pass
 * `secretPath`, `accountPath`, and `servicesPath` instead of `agentName` to
 * use another endpoint, such as the dashboard's.
 */
export function EgressServiceRows({
  label,
  services,
  onChanged,
  errorMessages,
  serviceEditing,
  agent,
  hideKeyActions = false,
  ...target
}: ServiceTarget & {
  /** Names the group and heads its rows, when the table has several. */
  label?: string;
  services: EgressCredentialService[];
  onChanged: () => void;
  /** Wording for 403 and 404 responses outside the Connections portal. */
  errorMessages?: RequestErrorMessages;
  /** Edit and delete controls for the services the viewer defines. */
  serviceEditing?: ServiceEditing;
  /** The agent the rows belong to, for the wording that depends on it. */
  agent: RowAgent;
  /** Leaves out what sets, removes, or connects a key or account, for rows no agent uses. */
  hideKeyActions?: boolean;
}) {
  return (
    <tbody aria-label={label} className={STACKED.body}>
      {label && <ServiceGroupHeading label={label} />}
      {services.map((service) =>
        service.restrict_only ? (
          <LimitRow
            key={service.name}
            service={service}
            serviceEditing={serviceEditing}
          />
        ) : (
          <EgressServiceItem
            key={service.name}
            paths={servicePathsFor(target, service.name)}
            service={service}
            onChanged={onChanged}
            errorMessages={errorMessages}
            serviceEditing={serviceEditing}
            agent={agent}
            hideKeyActions={hideKeyActions}
          />
        ),
      )}
    </tbody>
  );
}
