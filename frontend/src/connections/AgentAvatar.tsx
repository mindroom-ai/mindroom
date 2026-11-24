import { Users, UserRound } from "lucide-react";
import { AvatarImage } from "@/components/shared/AvatarImage";
import type { AgentConnections } from "./types";

export function AgentAvatar({
  agent,
}: {
  agent: Pick<AgentConnections, "agent_name" | "is_shared">;
}) {
  const Icon = agent.is_shared ? Users : UserRound;
  return (
    <AvatarImage
      src={`/api/connections/agents/${encodeURIComponent(agent.agent_name)}/avatar`}
      fallback={<Icon className="h-4 w-4" aria-hidden="true" />}
    />
  );
}

/**
 * An agent's avatar and name, with whether it is shared beneath the name.
 * `nameId` lets a panel be labelled by the name, and `${nameId}-type` lets
 * the button around it be described by the type.
 */
export function AgentIdentity({
  agent,
  displayName,
  nameId,
}: {
  agent: Pick<AgentConnections, "agent_name" | "is_shared">;
  displayName: string;
  nameId: string;
}) {
  return (
    <>
      <AgentAvatar key={agent.agent_name} agent={agent} />
      <span className="min-w-0">
        <span id={nameId} className="block font-medium">
          {displayName}
        </span>
        <span
          id={`${nameId}-type`}
          className="block text-xs text-muted-foreground"
        >
          {agent.is_shared ? "Shared" : "Personal"}
        </span>
      </span>
    </>
  );
}
