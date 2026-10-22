import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  PASSED_THROUGH,
  deleteServiceWarning,
  passThroughCaption,
  personalScopeLine,
  removeLimitWarning,
  sharedAgentNote,
} from "./accountCopy";
import { AgentAvatar } from "./AgentAvatar";
import { breakAfterUnderscores } from "./breakAfterUnderscores";
import { ConnectionsHeader } from "./ConnectionsHeader";
import { InactiveServices } from "./EgressInactiveServices";
import { DiscardEditsNotice, EgressServiceEditor } from "./EgressServiceEditor";
import { configPlaceholderOwners } from "./egressServiceNotes";
import type { RowAgent, ServiceEditing } from "./egressRowText";
import { EgressServiceRows, keyPath, servicePath } from "./EgressServiceRows";
import { EgressServiceTable } from "./EgressServiceTable";
import {
  errorText,
  type RequestErrorMessages,
  requestConnection,
} from "./request";
import { STACKED, STACKED_CELL, STACKED_LABEL } from "./stackedTable";
import type {
  AuthoredEgressService,
  EgressCredentialAgent,
  EgressCredentialList,
  EgressCredentialService,
  EgressLogRecord,
  EgressPresetCatalog,
} from "./types";

const LOG_LIMIT = 50;
/** How many of the newest requests the table shows before Show all. */
const LOG_PREVIEW = 10;
const SERVICE_ERROR_MESSAGES: RequestErrorMessages = {
  forbidden: "You are not allowed to change the services of this agent.",
  // The server says why, such as that a shared agent needs a credential manager.
  forbiddenDetail: true,
  notFound:
    "This agent or service is no longer available. Reload the page to update the list.",
};

type EditorState = (
  | {
      kind: "add";
      /** Add a limit on GitHub repositories, which has no key of its own. */
      restrictOnly?: boolean;
    }
  | { kind: "edit"; name: string; service: AuthoredEgressService }
) & {
  /**
   * Another service's Edit, pressed while this editor had unsaved changes. It
   * lives here so that closing the editor drops it.
   */
  pendingEdit?: EgressCredentialService;
};

type LogState =
  | { kind: "loading" }
  | { kind: "ready"; records: EgressLogRecord[] }
  | { kind: "error"; message: string };

function loadPresets(signal: AbortSignal): Promise<EgressPresetCatalog> {
  return requestConnection<EgressPresetCatalog>(
    "/api/connections/egress/presets",
    signal,
  );
}

/**
 * Recent brokered requests for one agent, loaded when the table is opened: the
 * caller's own, or with `everyone` (a shared or unscoped agent the caller
 * manages) every requester's, with a User column. `shared` marks a shared or
 * unscoped agent, whose credential managers can see the requests too.
 */
function RecentRequests({
  agentName,
  shared,
  everyone,
}: {
  agentName: string;
  shared: boolean;
  everyone: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState<LogState>({ kind: "loading" });
  const [showAll, setShowAll] = useState(false);
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
          message: errorText(
            cause,
            "Could not load your recent requests. Try again.",
          ),
        });
    }
  }, [agentName]);

  useEffect(() => () => request.current?.abort(), []);

  const toggle = () => {
    if (!open) void load();
    setOpen(!open);
  };

  // The newest come first, so the first few are the ones to look at.
  const records = log.kind === "ready" ? log.records : [];
  const shownRecords = showAll ? records : records.slice(0, LOG_PREVIEW);

  return (
    <div className="border-t border-border/60">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? `egress-log-${agentName}` : undefined}
        onClick={toggle}
        className="flex w-full items-center justify-between gap-3 px-5 py-3 text-left text-sm font-medium hover:bg-foreground/[0.035] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        Recent requests
        <ChevronDown
          aria-hidden="true"
          className={`h-4 w-4 text-muted-foreground transition-transform motion-reduce:transition-none ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div id={`egress-log-${agentName}`} className="space-y-2 px-5 pb-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              {everyone
                ? `The last ${LOG_LIMIT} requests through this agent's sandbox, from everyone who uses it, newest first.`
                : `Your last ${LOG_LIMIT} requests through this agent's sandbox, newest first.`}{" "}
              Only the method, host, and path are kept, never what was sent.
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
                className={`w-full text-left text-sm ${STACKED.table}`}
              >
                <caption className="px-3 py-2 text-left text-xs text-muted-foreground max-sm:block">
                  {passThroughCaption(shared, everyone)}
                </caption>
                <thead
                  className={`bg-muted/40 text-xs text-muted-foreground ${STACKED.head}`}
                >
                  <tr>
                    {[
                      "Time",
                      ...(everyone ? ["User"] : []),
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
                <tbody className={STACKED.body}>
                  {shownRecords.map((record, index) => {
                    const refusal =
                      record.code ??
                      (record.kind === "denied" ? "refused" : null);
                    // A tunnel the broker only relayed carries no key; a refused one never opened.
                    const passedThrough =
                      record.method === "CONNECT" &&
                      record.service === null &&
                      refusal === null;
                    const cell = `px-3 py-2 ${STACKED_CELL} ${STACKED_LABEL} max-sm:py-1`;
                    return (
                      <tr
                        // The log has no unique ids and can repeat identical requests.
                        key={`${record.at}-${index}`}
                        className={`border-t border-border/60 ${STACKED.row} max-sm:py-2`}
                      >
                        <td
                          data-label="Time"
                          className={`whitespace-nowrap ${cell}`}
                        >
                          {new Date(record.at).toLocaleString()}
                        </td>
                        {everyone && (
                          <td data-label="User" className={`break-all ${cell}`}>
                            {record.requester_id ?? "-"}
                          </td>
                        )}
                        <td
                          data-label="Method"
                          className={`font-mono text-xs ${cell}`}
                        >
                          {record.method}
                        </td>
                        <td data-label="Host" className={cell}>
                          {record.host}
                        </td>
                        <td
                          data-label="Path"
                          className={`font-mono text-xs sm:max-w-[14rem] sm:truncate ${cell}`}
                          title={record.path}
                        >
                          {record.path || "-"}
                        </td>
                        <td data-label="Service" className={cell}>
                          {passedThrough
                            ? PASSED_THROUGH
                            : (record.service ?? "-")}
                        </td>
                        <td data-label="Status" className={cell}>
                          <Badge
                            variant={
                              record.status >= 400 ? "destructive" : "secondary"
                            }
                            className="font-normal"
                          >
                            {record.status}
                          </Badge>
                          {refusal && (
                            <div className="mt-1 text-xs text-muted-foreground">
                              {breakAfterUnderscores(refusal)}
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {records.length > LOG_PREVIEW && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setShowAll(!showAll)}
            >
              {showAll
                ? `Show latest ${LOG_PREVIEW}`
                : `Show all (${records.length})`}
            </Button>
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
  const [editorDirty, setEditorDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => () => operation.current?.abort(), []);

  const newOperation = () => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    return controller;
  };

  const setPendingEdit = (pendingEdit?: EgressCredentialService) =>
    setEditor((current) => current && { ...current, pendingEdit });

  const edit = async (service: EgressCredentialService) => {
    setPendingEdit(undefined);
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
        setError(errorText(cause, "Could not load the service. Try again."));
    }
  };

  // Writes are not interrupted by another action: the rows stay disabled
  // meanwhile. Resolves to whether the write still counts, which it does not
  // once another action took its place.
  const write = async (path: string, method: string, body?: object) => {
    const controller = newOperation();
    setSaving(true);
    try {
      await requestConnection<void>(
        path,
        controller.signal,
        method,
        body,
        SERVICE_ERROR_MESSAGES,
      );
    } finally {
      setSaving(false);
    }
    return !controller.signal.aborted;
  };

  // The editor stays open after the service is written: it saves the key next
  // and closes through `onClose`, so a key that fails can be reported there.
  const save = async (name: string, service: AuthoredEgressService) => {
    if (await write(servicePath(agent.agent_name, name), "PUT", service))
      onChanged();
  };

  const saveKey = async (secret: string, name: string) => {
    if (await write(keyPath(agent.agent_name, name), "PUT", { secret }))
      onChanged();
  };

  const remove = async (name: string) => {
    if (!(await write(servicePath(agent.agent_name, name), "DELETE"))) return;
    setEditor((current) =>
      current?.kind === "edit" && current.name === name ? null : current,
    );
    onChanged();
  };

  // Opening another service's editor would throw away what is typed in this one.
  const requestEdit = (service: EgressCredentialService) => {
    if (editor !== null && editorDirty) setPendingEdit(service);
    else void edit(service);
  };

  const pendingEdit = editor?.pendingEdit;
  const serviceEditing: ServiceEditing = {
    editableSource: "user",
    disabled: saving,
    editingName: editor?.kind === "edit" ? editor.name : null,
    onEdit: requestEdit,
    onDelete: (service) => remove(service.name),
    // Deleting a limit gives back what it narrowed instead.
    deleteWarning: (service) =>
      (service.restrict_only ? removeLimitWarning : deleteServiceWarning)(
        agent.scope,
        agent.agent_display_name,
        agent.scope_agents,
      ),
    onLimitToRepositories: () => {
      setError(null);
      setEditor({ kind: "add", restrictOnly: true });
    },
    limitDisabled: editor !== null,
  };
  const rowAgent: RowAgent = {
    displayName: agent.agent_display_name,
    shared: agent.shared,
    managers: agent.managers,
    services: agent.services,
  };
  const scopeLine = agent.shared
    ? sharedAgentNote(agent.can_manage, agent.managers)
    : personalScopeLine(agent.scope);
  const groups = [
    {
      label: "From your administrator",
      services: agent.services.filter((service) => service.source === "config"),
    },
    {
      label: agent.shared ? "Added by its credential managers" : "Added by you",
      services: agent.services.filter((service) => service.source !== "config"),
    },
  ].filter((group) => group.services.length > 0);

  return (
    <section
      id={`egress-agent-panel-${agent.agent_name}`}
      aria-labelledby={`egress-agent-${agent.agent_name}`}
      className="border-l-2 border-primary/30 bg-background"
    >
      {(scopeLine || agent.can_manage) && (
        <div className="flex flex-wrap items-center gap-3 px-5 py-3">
          {scopeLine && (
            <p className="text-sm text-muted-foreground">{scopeLine}</p>
          )}
          {agent.can_manage && (
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
      )}
      {error && (
        <Alert variant="destructive" className="m-4 w-auto p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {pendingEdit && (
        <DiscardEditsNotice
          onDiscard={() => void edit(pendingEdit)}
          onKeep={() => setPendingEdit(undefined)}
        />
      )}
      {editor && (
        <div className="border-y border-border/60">
          <EgressServiceEditor
            key={editor.kind === "add" ? "add" : `edit:${editor.name}`}
            context="personal"
            sharedTarget={agent.shared}
            name={editor.kind === "edit" ? editor.name : undefined}
            service={editor.kind === "edit" ? editor.service : null}
            initialRestrictOnly={
              editor.kind === "add" && editor.restrictOnly === true
            }
            takenNames={agent.services.map((service) => service.name)}
            configPlaceholders={configPlaceholderOwners(agent.services)}
            agentServices={agent.services}
            scope={agent.scope}
            agentName={agent.agent_display_name}
            scopeAgents={agent.scope_agents}
            loadPresets={loadPresets}
            onDirtyChange={setEditorDirty}
            onSave={save}
            saveKey={saveKey}
            onDelete={
              editor.kind === "edit" ? () => remove(editor.name) : undefined
            }
            onClose={() => setEditor(null)}
          />
        </div>
      )}
      {agent.services.length || agent.inactive_services.length ? (
        <EgressServiceTable label={`Services for ${agent.agent_display_name}`}>
          {groups.map((group) => (
            <EgressServiceRows
              key={group.label}
              label={group.label}
              agentName={agent.agent_name}
              services={group.services}
              onChanged={onChanged}
              serviceEditing={serviceEditing}
              agent={rowAgent}
            />
          ))}
          <InactiveServices
            services={agent.inactive_services}
            disabled={saving}
            onDelete={agent.can_manage ? remove : undefined}
          />
        </EgressServiceTable>
      ) : (
        <p className="border-t border-border/60 px-5 py-4 text-sm text-muted-foreground">
          No services are configured yet.
        </p>
      )}
      <RecentRequests
        agentName={agent.agent_name}
        shared={agent.shared}
        everyone={agent.shared && agent.can_manage}
      />
    </section>
  );
}

/** How many of an agent's keys and accounts are set, out of all it can have; a limit has neither. */
function keyCounts(agent: EgressCredentialAgent) {
  const services = agent.services.filter((service) => !service.restrict_only);
  return {
    set: services.filter((service) => service.configured).length,
    total: services.length,
  };
}

/** The agent the page was opened for, from a `#egress-agent-<name>` link such as Manage services on the Agents tab. */
function linkedAgent(): string | null {
  const prefix = "#egress-agent-";
  const { hash } = window.location;
  return hash.startsWith(prefix)
    ? decodeURIComponent(hash.slice(prefix.length))
    : null;
}

/** One agent's row, with its services below it while it is open. */
function AgentRow({
  agent,
  open,
  onToggle,
  onChanged,
}: {
  agent: EgressCredentialAgent;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const { set, total } = keyCounts(agent);
  return (
    <>
      <tr
        className={`border-t transition-colors hover:bg-muted/30 ${STACKED.row} max-sm:px-0 max-sm:py-2 ${open ? "bg-muted/30" : ""}`}
      >
        <td className={`px-5 py-4 ${STACKED_CELL} max-sm:px-4 max-sm:py-1.5`}>
          <button
            type="button"
            className="flex w-full items-center gap-3 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`${open ? "Collapse" : "Expand"} ${agent.agent_display_name}`}
            aria-expanded={open}
            aria-controls={`egress-agent-panel-${agent.agent_name}`}
            onClick={onToggle}
          >
            {open ? (
              <ChevronDown
                className="h-4 w-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            ) : (
              <ChevronRight
                className="h-4 w-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            )}
            <AgentAvatar
              agent={{ agent_name: agent.agent_name, is_shared: agent.shared }}
            />
            <span
              id={`egress-agent-${agent.agent_name}`}
              className="font-medium"
            >
              {agent.agent_display_name}
            </span>
          </button>
        </td>
        <td
          data-label="Type"
          className={`px-5 py-4 ${STACKED_CELL} ${STACKED_LABEL} max-sm:px-4 max-sm:py-1.5`}
        >
          <Badge variant="secondary" className="font-normal">
            {agent.shared ? "Shared" : "Personal"}
          </Badge>
        </td>
        <td
          data-label="Keys and accounts"
          className={`px-5 py-4 text-muted-foreground ${STACKED_CELL} ${STACKED_LABEL} max-sm:px-4 max-sm:py-1.5`}
        >
          {total === 0 ? "No services yet" : `${set} of ${total} set`}
        </td>
      </tr>
      {/* Hidden rather than removed, so closing the row keeps an open editor
          and lets a write under way finish. */}
      <tr className={STACKED.row} hidden={!open}>
        <td
          colSpan={3}
          className={`border-t border-border/60 p-0 ${STACKED_CELL}`}
        >
          <AgentSection agent={agent} onChanged={onChanged} />
        </td>
      </tr>
    </>
  );
}

/** Personal page to connect accounts or set the API keys the egress broker injects for each agent. */
export function EgressCredentials() {
  const [agents, setAgents] = useState<EgressCredentialAgent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(() => {
    const linked = linkedAgent();
    return new Set(linked ? [linked] : []);
  });
  const request = useRef<AbortController | null>(null);
  const loaded = agents !== null;

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
          errorText(
            cause,
            "Could not load your API keys. Reload this page to try again.",
          ),
        );
    }
  }, []);

  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  // The linked agent's row exists only once the listing is in, after the browser looked for it.
  useEffect(() => {
    const linked = linkedAgent();
    if (loaded && linked)
      document.getElementById(`egress-agent-${linked}`)?.scrollIntoView();
  }, [loaded]);

  const toggle = (agentName: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(agentName)) next.add(agentName);
      return next;
    });

  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl space-y-6">
        <ConnectionsHeader
          current="keys"
          title="Agent Keys"
          howItWorks="Connect an account or paste an API key for the services below, or add a service of your own. MindRoom stores them and adds them to your agents' requests to the matching websites. Your agents never get the keys themselves, but a website can send a key back in its response (an echo service does), so only add keys for sites you trust. Saved keys cannot be read back."
        >
          Connect accounts or add API keys; MindRoom adds them to your agents'
          requests and your agents never get the keys themselves.
        </ConnectionsHeader>
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
        {agents && agents.length > 0 && (
          <div className="overflow-hidden rounded-2xl border bg-card shadow-sm">
            <div className="flex items-center gap-2 border-b px-5 py-4">
              <h2 className="font-semibold">Agents</h2>
              <span className="rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                {agents.length}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table
                aria-label="Agents"
                className={`w-full text-sm sm:min-w-[680px] ${STACKED.table}`}
              >
                <thead
                  className={`bg-muted/30 text-left text-xs text-muted-foreground ${STACKED.head}`}
                >
                  <tr>
                    {["Agent", "Type", "Keys and accounts"].map((column) => (
                      <th
                        key={column}
                        scope="col"
                        className="px-5 py-3 font-medium"
                      >
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className={STACKED.body}>
                  {agents.map((agent) => (
                    <AgentRow
                      key={agent.agent_name}
                      agent={agent}
                      open={open.has(agent.agent_name)}
                      onToggle={() => toggle(agent.agent_name)}
                      onChanged={() => void load()}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <p className="border-t px-5 py-3 text-xs text-muted-foreground">
              Expand an agent to manage its keys and accounts.
            </p>
          </div>
        )}
      </div>
    </main>
  );
}
