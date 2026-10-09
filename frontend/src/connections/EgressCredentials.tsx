import { useCallback, useEffect, useRef, useState } from "react";
import mindroomLogo from "../../../assets/logo/logo-mark-animated.svgz?url";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EgressServiceRows } from "./EgressServiceRows";
import { requestConnection } from "./request";
import type { EgressCredentialAgent, EgressCredentialList } from "./types";

/** Personal page to set the API keys the egress broker injects for each agent. */
export function EgressCredentials() {
  const [agents, setAgents] = useState<EgressCredentialAgent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const data = await requestConnection<EgressCredentialList>(
        "/api/connections/egress",
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setAgents(data.agents);
      setError(null);
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not load your API keys. Reload this page to try again.",
        );
    }
  }, []);

  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-4xl space-y-6">
        <header className="flex items-start gap-4">
          <img src={mindroomLogo} alt="" className="h-12 w-12 shrink-0" />
          <div className="space-y-1.5">
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Your agents' API keys
            </h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              Keys are stored on the MindRoom server and added to outgoing
              requests for the services below. Your agents never see them, and
              saved keys cannot be read back.
            </p>
          </div>
        </header>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!agents && !error && <p role="status">Loading your API keys…</p>}
        {agents?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No agents with shell or python access are available to you.
          </p>
        )}
        {agents?.map((agent) => (
          <section
            key={agent.agent_name}
            aria-labelledby={`egress-agent-${agent.agent_name}`}
            className="overflow-hidden rounded-2xl border bg-card shadow-sm"
          >
            <div className="flex items-center gap-2 border-b px-5 py-4">
              <h2
                id={`egress-agent-${agent.agent_name}`}
                className="font-semibold"
              >
                {agent.agent_display_name}
              </h2>
              {agent.services.some((service) => service.is_shared) && (
                <Badge variant="secondary" className="font-normal">
                  Shared
                </Badge>
              )}
            </div>
            {agent.services.length ? (
              <EgressServiceRows
                agentName={agent.agent_name}
                services={agent.services}
                onChanged={() => void load()}
              />
            ) : (
              <p className="px-5 py-4 text-sm text-muted-foreground">
                No services are configured yet.
              </p>
            )}
          </section>
        ))}
      </div>
    </main>
  );
}
