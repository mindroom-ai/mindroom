import { Badge } from "@/components/ui/badge";
import { personalAccountRefusal, personalSandboxRefusal } from "./accountCopy";
import {
  type RowAgent,
  type ServiceEditing,
  sharesAccount,
  statusLabel,
} from "./egressRowText";
import type { EgressCredentialService } from "./types";

/** The row's status badge and the notes about the account it uses, in the order they are shown. */
export function ServiceStatus({
  service,
  agent,
  serviceEditing,
}: {
  service: EgressCredentialService;
  agent: RowAgent;
  serviceEditing: ServiceEditing;
}) {
  const oauth = service.oauth;
  const sourceLabel = serviceEditing.labels?.[service.source];
  // Once a key is saved there is nothing left to ask for, so the hint goes.
  const refusalShown =
    oauth?.unavailable_reason === "shared_sandbox" && !service.key_configured;
  return (
    <>
      <Badge
        variant={service.configured ? "secondary" : "outline"}
        className="max-w-full font-normal"
      >
        {statusLabel(service)}
      </Badge>
      {/* Also beside an account only its tools use: the one Agent Keys would use once allowed. */}
      {service.active_source !== "key" && oauth?.account_label && (
        <span className="break-all text-xs">{oauth.account_label}</span>
      )}
      {service.active_source === "oauth" && sharesAccount(agent, oauth) && (
        <span className="text-xs text-muted-foreground">
          Shared with everyone using {agent.displayName}
        </span>
      )}
      {service.active_source === "key" && oauth?.connected && (
        <span className="text-xs text-muted-foreground">
          Your connected {oauth.display_name} account is kept for other
          services.
        </span>
      )}
      {sourceLabel && (
        <Badge variant="outline" className="font-normal">
          {sourceLabel}
        </Badge>
      )}
      {/* A service account serves the provider's tools, but the broker never injects it. */}
      {oauth?.service_account && service.active_source !== "key" && (
        <span className="text-xs text-muted-foreground">
          Agent Keys cannot use the shared service account, so{" "}
          {service.active_source === "oauth"
            ? "this uses the account connected earlier."
            : "this service needs a key."}
        </span>
      )}
      {refusalShown && (
        <span className="text-xs text-muted-foreground">
          {agent.shared
            ? personalAccountRefusal(
                oauth.display_name,
                service.can_manage,
                agent.managers ?? [],
              )
            : personalSandboxRefusal(oauth.display_name)}
        </span>
      )}
      {oauth?.shared_worker_opt_in && (
        <span className="text-xs text-amber-600 dark:text-amber-400">
          Everyone using this agent can act with the connected account until its
          access expires
        </span>
      )}
    </>
  );
}
