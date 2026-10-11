import { Users, UserRound } from "lucide-react";
import { AvatarImage } from "@/components/shared/AvatarImage";
import type { AgentConnections } from "./types";

export function AgentAvatar({ agent }: { agent: AgentConnections }) {
  const Icon = agent.is_shared ? Users : UserRound;
  return (
    <AvatarImage
      src={`/api/connections/agents/${encodeURIComponent(agent.agent_name)}/avatar`}
      fallback={<Icon className="h-4 w-4" aria-hidden="true" />}
    />
  );
}
