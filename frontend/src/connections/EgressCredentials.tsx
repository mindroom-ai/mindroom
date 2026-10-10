import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import mindroomLogo from "../../../assets/logo/logo-mark-animated.svgz?url";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  EgressServiceEditor,
  findBroaderGithubService,
} from "./EgressServiceEditor";
import { EgressServiceRows, type ServiceEditing } from "./EgressServiceRows";
import { type RequestErrorMessages, requestConnection } from "./request";
import type {
  AuthoredEgressService,
  EgressCredentialAgent,
  EgressCredentialList,
  EgressCredentialService,
  EgressLogRecord,
} from "./types";

const LOG_LIMIT = 50;
const SERVICE_ERROR_MESSAGES: RequestErrorMessages = {
  forbidden:
    "Only credential managers can change the services of a shared agent.",
  notFound:
    "This agent or service is no longer available. Reload the page to update the list.",
};

type EditorState =
  | { kind: "add" }
  | { kind: "edit"; name: string; service: AuthoredEgressService };

type LogState =
  | { kind: "loading" }
  | { kind: "ready"; records: EgressLogRecord[] }
  | { kind: "error"; message: string };

function servicePath(agentName: string, serviceName: string): string {
  return `/api/connections/egress/agents/${encodeURIComponent(agentName)}/services/${encodeURIComponent(serviceName)}`;
}

function deleteWarning(service: EgressCredentialService): string {
  return service.is_shared
    ? "This deletes the service and its shared key. Everyone who relies on it loses access to it until it is added again."
    : "This deletes the service and its saved key. Your agent loses access to it until you add it again.";
}

/** The caller's own recent brokered requests for one agent, loaded when the table is opened. */
function RecentRequests({ agentName }: { agentName: string }) {
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState<LogState>({ kind: "loading" });
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLog({ kind: "loading" });
    try {
      const data = await requestConnection<{ records: EgressLogRecord[] }>(
        `/api/connections/egress/logs?agent_name=${encodeURIComponent(agentName)}&limit=${LOG_LIMIT}`,
        controller.signal,
      );
      if (!controller.signal.aborted)
        setLog({ kind: "ready", records: data.records });
    } catch (cause) {
      if (!controller.signal.aborted)
        setLog({
          kind: "error",
          message:
            cause instanceof Error
              ? cause.message
              : "Could not load your recent requests. Try again.",
        });
    }
  }, [agentName]);

  useEffect(() => () => request.current?.abort(), []);

  const toggle = () => {
    if (!open) void load();
    setOpen(!open);
  };

  return (
    <div className="border-t border-border/60">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`egress-log-${agentName}`}
        onClick={toggle}
        className="flex w-full items-center justify-between gap-3 px-5 py-3 text-left text-sm font-medium hover:bg-foreground/[0.035] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        Recent requests
        <ChevronDown
          aria-hidden="true"
          className={`h-4 w-4 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div id={`egress-log-${agentName}`} className="space-y-2 px-5 pb-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              Your last {LOG_LIMIT} requests through this agent's sandbox,
              newest first. Only the method, host, and path are kept, never what
              was sent.
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={log.kind === "loading"}
              onClick={() => void load()}
            >
              <RefreshCw className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
              Refresh
            </Button>
          </div>
          {log.kind === "loading" && (
            <p role="status" className="text-sm text-muted-foreground">
              Loading recent requests…
            </p>
          )}
          {log.kind === "error" && (
            <Alert variant="destructive" className="p-2">
              <AlertDescription>{log.message}</AlertDescription>
            </Alert>
          )}
          {log.kind === "ready" && log.records.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No requests logged yet.
            </p>
          )}
          {log.kind === "ready" && log.records.length > 0 && (
            <div className="overflow-x-auto rounded-md border border-border/60">
              <table
                aria-label={`Recent requests for ${agentName}`}
                className="w-full text-left text-sm"
              >
                <thead className="bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    {[
                      "Time",
                      "Method",
                      "Host",
                      "Path",
                      "Service",
                      "Status",
                    ].map((column) => (
                      <th key={column} className="px-3 py-2 font-medium">
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {log.records.map((record, index) => {
                    const refusal =
                      record.code ??
                      (record.kind === "denied" ? "refused" : null);
                    return (
                      <tr
                        // The log has no unique ids and can repeat identical requests.
                        key={`${record.at}-${index}`}
                        className="border-t border-border/60"
                      >
                        <td className="whitespace-nowrap px-3 py-2">
                          {new Date(record.at).toLocaleString()}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {record.method}
                        </td>
                        <td className="px-3 py-2">{record.host}</td>
                        <td
                          className="max-w-xs truncate px-3 py-2 font-mono text-xs"
                          title={record.path}
                        >
                          {record.path || "-"}
                        </td>
                        <td className="px-3 py-2">{record.service ?? "-"}</td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <Badge
                            variant={
                              record.status >= 400 ? "destructive" : "secondary"
                            }
                            className="font-normal"
                          >
                            {record.status}
                          </Badge>
                          {refusal && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              {refusal}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One agent's services with their editor and request log. */
function AgentSection({
  agent,
  onChanged,
}: {
  agent: EgressCredentialAgent;
  onChanged: () => void;
}) {
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  const isShared = agent.services.some((service) => service.is_shared);
  // An agent without services has no flag to read, so the server decides.
  const mayWrite =
    agent.services.length === 0 ||
    agent.services.some((service) => service.can_manage);

  useEffect(() => () => operation.current?.abort(), []);

  const newOperation = () => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    return controller;
  };

  const edit = async (service: EgressCredentialService) => {
    const controller = newOperation();
    setError(null);
    try {
      const authored = await requestConnection<AuthoredEgressService>(
        servicePath(agent.agent_name, service.name),
        controller.signal,
        "GET",
        {},
        SERVICE_ERROR_MESSAGES,
      );
      if (!controller.signal.aborted)
        setEditor({ kind: "edit", name: service.name, service: authored });
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not load the service. Try again.",
        );
    }
  };

  const save = async (name: string, service: AuthoredEgressService) => {
    const controller = newOperation();
    await requestConnection<void>(
      servicePath(agent.agent_name, name),
      controller.signal,
      "PUT",
      service,
      SERVICE_ERROR_MESSAGES,
    );
    if (controller.signal.aborted) return;
    setEditor(null);
    onChanged();
  };

  const remove = async (name: string) => {
    const controller = newOperation();
    await requestConnection<void>(
      servicePath(agent.agent_name, name),
      controller.signal,
      "DELETE",
      undefined,
      SERVICE_ERROR_MESSAGES,
    );
    if (controller.signal.aborted) return;
    setEditor((current) =>
      current?.kind === "edit" && current.name === name ? null : current,
    );
    onChanged();
  };

  const serviceEditing: ServiceEditing = {
    editableSource: "user",
    onEdit: (service) => void edit(service),
    onDelete: (service) => remove(service.name),
    deleteWarning,
    labels: { config: "Added by your administrator" },
  };

  return (
    <section
      aria-labelledby={`egress-agent-${agent.agent_name}`}
      className="overflow-hidden rounded-2xl border bg-card shadow-sm"
    >
      <div className="flex items-center gap-2 border-b px-5 py-4">
        <h2 id={`egress-agent-${agent.agent_name}`} className="font-semibold">
          {agent.agent_display_name}
        </h2>
        {isShared && (
          <Badge variant="secondary" className="font-normal">
            Shared
          </Badge>
        )}
        {mayWrite && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={editor !== null}
            aria-label={`Add service for ${agent.agent_display_name}`}
            onClick={() => {
              setError(null);
              setEditor({ kind: "add" });
            }}
          >
            Add service
          </Button>
        )}
      </div>
      {error && (
        <Alert variant="destructive" className="m-4 w-auto p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {editor && (
        <div className="border-b border-border/60">
          <EgressServiceEditor
            key={editor.kind === "add" ? "add" : `edit:${editor.name}`}
            context="personal"
            sharedTarget={isShared}
            name={editor.kind === "edit" ? editor.name : undefined}
            service={editor.kind === "edit" ? editor.service : null}
            takenNames={agent.services.map((service) => service.name)}
            broaderGithubService={findBroaderGithubService(
              agent.services,
              editor.kind === "edit" ? editor.name : undefined,
            )}
            onSave={save}
            onDelete={
              editor.kind === "edit" ? () => remove(editor.name) : undefined
            }
            onCancel={() => setEditor(null)}
          />
        </div>
      )}
      {agent.services.length ? (
        <EgressServiceRows
          agentName={agent.agent_name}
          services={agent.services}
          onChanged={onChanged}
          serviceEditing={serviceEditing}
        />
      ) : (
        <p className="px-5 py-4 text-sm text-muted-foreground">
          No services are configured yet.
        </p>
      )}
      <RecentRequests agentName={agent.agent_name} />
    </section>
  );
}

/** Personal page to connect accounts or set the API keys the egress broker injects for each agent. */
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
              Connect an account or paste an API key for the services below, or
              add a service of your own. Accounts and keys are stored on the
              MindRoom server and added to outgoing requests. Your agents never
              see them, and saved keys cannot be read back.
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
          <AgentSection
            key={agent.agent_name}
            agent={agent}
            onChanged={() => void load()}
          />
        ))}
      </div>
    </main>
  );
}
