import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Download, RefreshCw } from "lucide-react";
import { EgressServiceRows } from "@/connections/EgressServiceRows";
import type { RequestErrorMessages } from "@/connections/request";
import type { EgressCredentialService } from "@/connections/types";
import {
  API_ENDPOINTS,
  fetchJSON,
  withAgentName,
  withQueryParams,
} from "@/lib/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useConfigStore } from "@/store/configStore";
import {
  type Agent,
  type Config,
  resolveEffectiveDefaultTools,
} from "@/types/config";

const NOT_RUNNING_MESSAGE =
  "Egress broker is not running. Set MINDROOM_EGRESS_BROKER_PORT and MINDROOM_EGRESS_BROKER_URL.";
const LOG_ERROR_MESSAGE = "Could not load the request log. Try again.";
const LOG_TIMEOUT_MS = 30_000;
const GLOBAL_OPTION = "Global (unscoped agents)";
// Brokered traffic only exists for agents that can run commands.
const EGRESS_TOOLS = ["shell", "python"];
const SECRET_ERROR_MESSAGES: RequestErrorMessages = {
  forbidden: "You are not allowed to change egress keys.",
  notFound:
    "This service is no longer configured. Reload the page to update the list.",
};

interface BrokerService {
  name: string;
  display_name: string | null;
  description: string;
  configured: boolean;
  updated_at: string | null;
}

interface AuditRecord {
  at: string;
  agent_name: string | null;
  method: string;
  host: string;
  path: string;
  service: string | null;
  status: number;
  duration_ms: number;
}

interface LogFilters {
  agent: string;
  host: string;
  service: string;
}

type LogState =
  | { kind: "loading" }
  | { kind: "ready"; records: AuditRecord[] }
  | { kind: "not-running" }
  | { kind: "error" };

const LOG_COLUMNS = [
  "Time",
  "Agent",
  "Method",
  "Host",
  "Path",
  "Service",
  "Status",
  "Duration",
];

const NO_FILTERS: LogFilters = { agent: "", host: "", service: "" };

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

function canUseEgress(agent: Agent, defaults: Config["defaults"]): boolean {
  const inherited =
    agent.include_default_tools === false
      ? []
      : resolveEffectiveDefaultTools(defaults);
  return [...agent.tools, ...inherited].some((tool) =>
    EGRESS_TOOLS.includes(tool),
  );
}

// Keys for `user` and `user_agent` agents belong to each requester, so the
// admin API rejects them; their users set keys on the personal egress page.
function isRequesterScoped(
  agent: Agent,
  defaults: Config["defaults"],
): boolean {
  if (agent.private != null) return true;
  const scope = agent.worker_scope ?? defaults?.worker_scope ?? null;
  return scope === "user" || scope === "user_agent";
}

function ServiceSection({ agentName }: { agentName: string | null }) {
  const [services, setServices] = useState<EgressCredentialService[] | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(0);

  const load = useCallback(async () => {
    const request = ++latest.current;
    try {
      const payload = await fetchJSON<{ services: BrokerService[] }>(
        withAgentName(API_ENDPOINTS.egressBroker.services, agentName),
      );
      if (request !== latest.current) return;
      // The dashboard operator manages every service. Without an agent the
      // keys are the global ones; for a selected agent the API does not report
      // its scope, so the wording stays neutral.
      setServices(
        payload.services.map((service) => ({
          ...service,
          display_name: service.display_name ?? service.name,
          is_shared: agentName === null ? true : null,
          is_global: agentName === null,
          can_manage: true,
        })),
      );
      setError(null);
    } catch (cause) {
      if (request !== latest.current) return;
      setError(errorMessage(cause, "Could not load the egress services."));
    }
  }, [agentName]);

  useEffect(() => {
    void load();
    return () => {
      latest.current += 1;
    };
  }, [load]);

  return (
    <section aria-labelledby="egress-services-heading" className="space-y-2">
      <h3 id="egress-services-heading" className="text-sm font-medium">
        Service keys
      </h3>
      {error && (
        <Alert variant="destructive" className="p-2">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {!services && !error && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading services...
        </p>
      )}
      {services?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No egress services are configured yet. Add them under{" "}
          <code>egress_broker.services</code> in the config.
        </p>
      )}
      {services && services.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border/60">
          <EgressServiceRows
            secretPath={(serviceName) =>
              withAgentName(
                API_ENDPOINTS.egressBroker.secret(serviceName),
                agentName,
              )
            }
            services={services}
            onChanged={() => void load()}
            errorMessages={SECRET_ERROR_MESSAGES}
          />
        </div>
      )}
    </section>
  );
}

function LogTable({ records }: { records: AuditRecord[] }) {
  if (records.length === 0)
    return (
      <p className="text-sm text-muted-foreground">No requests logged yet.</p>
    );
  return (
    <div className="overflow-x-auto rounded-md border border-border/60">
      <table aria-label="Request log" className="w-full text-left text-sm">
        <thead className="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            {LOG_COLUMNS.map((column) => (
              <th key={column} className="px-3 py-2 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {records.map((record, index) => (
            <tr
              // The log has no unique ids and can repeat identical requests.
              key={`${record.at}-${index}`}
              className="border-t border-border/60"
            >
              <td className="whitespace-nowrap px-3 py-2">
                {new Date(record.at).toLocaleString()}
              </td>
              <td className="px-3 py-2">{record.agent_name ?? "-"}</td>
              <td className="px-3 py-2 font-mono text-xs">{record.method}</td>
              <td className="px-3 py-2">{record.host}</td>
              <td
                className="max-w-xs truncate px-3 py-2 font-mono text-xs"
                title={record.path}
              >
                {record.path}
              </td>
              <td className="px-3 py-2">{record.service ?? "-"}</td>
              <td className="px-3 py-2">
                <Badge
                  variant={record.status >= 400 ? "destructive" : "secondary"}
                  className="font-normal"
                >
                  {record.status}
                </Badge>
              </td>
              <td className="whitespace-nowrap px-3 py-2">
                {record.duration_ms} ms
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LogSection() {
  const [filters, setFilters] = useState<LogFilters>(NO_FILTERS);
  const [log, setLog] = useState<LogState>({ kind: "loading" });
  const operation = useRef<AbortController | null>(null);

  const load = useCallback(async (applied: LogFilters) => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    // A newer request or an unmount replaces `operation.current`; a timeout
    // only aborts the controller, so it still reaches the error state.
    const superseded = () => operation.current !== controller;
    const timeout = setTimeout(() => controller.abort(), LOG_TIMEOUT_MS);
    setLog({ kind: "loading" });
    try {
      const response = await fetch(
        withQueryParams(API_ENDPOINTS.egressBroker.logs, {
          agent_name: applied.agent.trim(),
          host: applied.host.trim(),
          service: applied.service.trim(),
        }),
        { credentials: "same-origin", signal: controller.signal },
      );
      if (superseded()) return;
      if (response.status === 409) {
        setLog({ kind: "not-running" });
        return;
      }
      if (!response.ok) {
        setLog({ kind: "error" });
        return;
      }
      const payload = (await response.json()) as { records: AuditRecord[] };
      if (!superseded()) setLog({ kind: "ready", records: payload.records });
    } catch {
      if (!superseded()) setLog({ kind: "error" });
    } finally {
      clearTimeout(timeout);
    }
  }, []);

  useEffect(() => {
    void load(NO_FILTERS);
    return () => {
      operation.current?.abort();
      operation.current = null;
    };
  }, [load]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void load(filters);
  };

  const filterInput = (field: keyof LogFilters, label: string) => (
    <Input
      aria-label={`Filter by ${label.toLowerCase()}`}
      placeholder={label}
      className="h-9 w-40"
      value={filters[field]}
      onChange={(event) =>
        setFilters((previous) => ({ ...previous, [field]: event.target.value }))
      }
    />
  );

  return (
    <section aria-labelledby="egress-log-heading" className="space-y-2">
      <h3 id="egress-log-heading" className="text-sm font-medium">
        Request log
      </h3>
      <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
        {filterInput("agent", "Agent")}
        {filterInput("host", "Host")}
        {filterInput("service", "Service")}
        <Button
          type="submit"
          size="sm"
          variant="outline"
          disabled={log.kind === "loading"}
        >
          <RefreshCw className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
          Refresh
        </Button>
      </form>
      {log.kind === "loading" && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading request log...
        </p>
      )}
      {log.kind === "not-running" && (
        <Alert className="p-2">
          <AlertDescription>{NOT_RUNNING_MESSAGE}</AlertDescription>
        </Alert>
      )}
      {log.kind === "error" && (
        <Alert variant="destructive" className="p-2">
          <AlertDescription>{LOG_ERROR_MESSAGE}</AlertDescription>
        </Alert>
      )}
      {log.kind === "ready" && <LogTable records={log.records} />}
      {/* The CA only exists while the broker runs, so the link would 409. */}
      {log.kind === "not-running" ? (
        <Button size="sm" variant="outline" disabled>
          <Download className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
          Download CA certificate
        </Button>
      ) : (
        <Button asChild size="sm" variant="outline">
          <a href={API_ENDPOINTS.egressBroker.caPem} download>
            <Download className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
            Download CA certificate
          </a>
        </Button>
      )}
    </section>
  );
}

/** Egress broker secrets for the global scope or one agent, plus its request log. */
export function EgressBroker() {
  const agents = useConfigStore((state) => state.agents);
  const defaults = useConfigStore((state) => state.config?.defaults);
  const [selected, setSelected] = useState<string | null>(null);

  const commandAgents = useMemo(
    () => agents.filter((agent) => canUseEgress(agent, defaults)),
    [agents, defaults],
  );
  const eligibleAgents = useMemo(
    () => commandAgents.filter((agent) => !isRequesterScoped(agent, defaults)),
    [commandAgents, defaults],
  );
  const hidesRequesterScopedAgents =
    eligibleAgents.length < commandAgents.length;
  // An agent that disappears from the config falls back to the global scope.
  const agentName = eligibleAgents.some((agent) => agent.id === selected)
    ? selected
    : null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-xl">Egress broker</CardTitle>
        <CardDescription>
          API keys the broker injects into outbound requests from agent workers.
          Keys are write-only and never shown again.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="egress-secret-target" className="text-sm font-medium">
            Secrets for
          </label>
          <select
            id="egress-secret-target"
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={agentName ?? ""}
            onChange={(event) => setSelected(event.target.value || null)}
          >
            <option value="">{GLOBAL_OPTION}</option>
            {eligibleAgents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.display_name || agent.id}
              </option>
            ))}
          </select>
          {hidesRequesterScopedAgents && (
            <p className="w-full text-xs text-muted-foreground">
              Agents with user or user_agent scope are not listed: each user
              sets their own keys on the personal egress page.
            </p>
          )}
        </div>
        {/* Keyed so an unsaved secret never carries over to another target. */}
        <ServiceSection
          key={agentName === null ? "global" : `agent:${agentName}`}
          agentName={agentName}
        />
        <LogSection />
      </CardContent>
    </Card>
  );
}
