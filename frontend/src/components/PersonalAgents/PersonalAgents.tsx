import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ChevronDown, UserRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription } from "@/components/ui/card";
import { API_ENDPOINTS, fetchJSON } from "@/lib/api";
import { pluralize } from "@/lib/utils";
import { useConfigStore } from "@/store/configStore";

export interface PersonalAgent {
  template: string;
  user_id: string;
  agent_name: string;
  matrix_user_id: string;
  display_name: string | null;
  pending: boolean;
  template_exists: boolean;
}

function agentStatus(agent: PersonalAgent) {
  if (!agent.template_exists)
    return { label: "Template removed", variant: "destructive" } as const;
  if (agent.pending) return { label: "Pending", variant: "outline" } as const;
  return { label: "Active", variant: "secondary" } as const;
}

/** Read-only list of the agents generated from personal_agents templates, which the Agents list leaves out. */
export function PersonalAgents() {
  const { config, committedGeneration } = useConfigStore();
  const query = useQuery({
    queryKey: ["personal-agents", committedGeneration],
    queryFn: () =>
      fetchJSON<PersonalAgent[]>(API_ENDPOINTS.config.personalAgents),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const agents = query.data ?? [];
  const hasTemplates = Object.keys(config?.personal_agents ?? {}).length > 0;
  // A failed request keeps the card so its error shows; roster entries can outlive every template.
  if (!hasTemplates && !query.isError && agents.length === 0) return null;

  return (
    <Card>
      <details className="group">
        <summary className="flex min-h-12 cursor-pointer items-center justify-between gap-3 rounded-lg px-4 py-3 hover:bg-secondary">
          <span className="flex items-center gap-2 text-base font-semibold">
            <UserRound className="h-5 w-5" />
            Personal agents
          </span>
          <span className="flex items-center gap-2 text-sm text-muted-foreground">
            {pluralize(agents.length, "agent")}
            <ChevronDown
              aria-hidden="true"
              className="h-4 w-4 transition-transform group-open:rotate-180"
            />
          </span>
        </summary>
        <CardContent className="space-y-3 pt-0">
          <CardDescription>
            One agent per onboarded user, generated from a template. Edit the
            template and per-user overrides in{" "}
            <Link
              to="/settings"
              className="font-medium underline underline-offset-2"
            >
              Settings → Personal rooms
            </Link>
            .
          </CardDescription>
          {query.error && (
            <p className="text-sm text-destructive">{query.error.message}</p>
          )}
          <ul className="max-h-64 divide-y overflow-y-auto">
            {agents.map((agent) => {
              const status = agentStatus(agent);
              return (
                <li
                  key={`${agent.template}:${agent.user_id}`}
                  className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
                >
                  <div className="min-w-0">
                    <p className="font-medium">
                      {agent.display_name ?? agent.agent_name}{" "}
                      <span className="font-mono text-xs text-muted-foreground">
                        {agent.matrix_user_id}
                      </span>
                    </p>
                    <p className="text-muted-foreground">
                      Owner <span className="font-mono">{agent.user_id}</span>,
                      template{" "}
                      <span className="font-mono">{agent.template}</span>
                    </p>
                  </div>
                  <Badge variant={status.variant}>{status.label}</Badge>
                </li>
              );
            })}
          </ul>
        </CardContent>
      </details>
    </Card>
  );
}
