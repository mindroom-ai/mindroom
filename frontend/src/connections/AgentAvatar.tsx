import { useState } from "react";
import { Users, UserRound } from "lucide-react";
import type { AgentConnections } from "./types";

export function AgentAvatar({ agent }: { agent: AgentConnections }) {
  const [failed, setFailed] = useState(false);
  const Icon = agent.is_shared ? Users : UserRound;
  return (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-primary/5 text-primary">
      {failed ? (
        <Icon className="h-4 w-4" aria-hidden="true" />
      ) : (
        <img
          src={`/api/connections/agents/${encodeURIComponent(agent.agent_name)}/avatar`}
          alt=""
          loading="lazy"
          className="h-full w-full object-cover"
          onError={() => setFailed(true)}
        />
      )}
    </span>
  );
}
