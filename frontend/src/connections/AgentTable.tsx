import { Fragment, useCallback, useMemo, useState } from "react";
import {
  type ColumnDef,
  type ExpandedState,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { sharedAgentNote } from "./accountCopy";
import { AgentTools } from "./AgentTools";
import { AgentAvatar } from "./AgentAvatar";
import { EgressServiceRows } from "./EgressServiceRows";
import { EgressServiceTable } from "./EgressServiceTable";
import { STACKED, STACKED_CELL, STACKED_LABEL } from "./stackedTable";
import type { AgentConnections, ConnectionStatus } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

/** Whether each agent's account is connected, by agent and provider, once its status has loaded. */
type ConnectedAccounts = Record<string, boolean>;

type AgentTableRow = AgentConnections & {
  mcp: McpSelectionState;
  accounts: ConnectedAccounts;
};

const accountKey = (agentName: string, provider: string) =>
  `${agentName}/${provider}`;

/**
 * How many of an agent's accounts and keys are set, out of all of them: its
 * connectable services plus its egress services. A service counts as connected
 * by the catalog, until its status loads (when the agent is opened) and says
 * otherwise, as after a connect or disconnect, and again once the catalog is
 * read anew.
 */
function accountCounts(agent: AgentTableRow) {
  // A service that only limits paths is neither an account nor a key.
  const egress = (agent.egress_services ?? []).filter(
    (service) => !service.restrict_only,
  );
  const connectedServices = agent.services.filter(
    (service) =>
      agent.accounts[accountKey(agent.agent_name, service.provider)] ??
      service.connected,
  );
  return {
    set:
      egress.filter((service) => service.configured).length +
      connectedServices.length,
    total: agent.services.length + egress.length,
  };
}

/** The agent's services and tools by name, for the search box. */
function searchTerms(agent: AgentConnections): string[] {
  return [
    agent.agent_name,
    agent.agent_display_name,
    ...agent.tools.flatMap((tool) => [tool.name, tool.display_name]),
    ...agent.services.flatMap((service) => [
      service.provider,
      service.display_name,
    ]),
    ...(agent.egress_services ?? []).flatMap((service) => [
      service.name,
      service.display_name,
      ...service.rules.map((rule) => rule.host),
    ]),
  ];
}

const columns: ColumnDef<AgentTableRow>[] = [
  {
    id: "agent",
    header: "Agent",
    accessorFn: (agent) => agent.agent_display_name,
    cell: ({ row }) => {
      const agent = row.original;
      return (
        <button
          type="button"
          className="flex w-full items-center gap-3 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={`${row.getIsExpanded() ? "Collapse" : "Expand"} ${agent.agent_display_name}`}
          aria-expanded={row.getIsExpanded()}
          aria-controls={
            row.getIsExpanded() ? `agent-tools-${agent.agent_name}` : undefined
          }
          onClick={row.getToggleExpandedHandler()}
        >
          {row.getIsExpanded() ? (
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
          <AgentAvatar key={agent.agent_name} agent={agent} />
          <span id={`agent-${agent.agent_name}`} className="font-medium">
            {agent.agent_display_name}
          </span>
        </button>
      );
    },
  },
  {
    id: "type",
    header: "Type",
    cell: ({ row }) => (
      <Badge variant="secondary" className="font-normal">
        {row.original.is_shared ? "Shared" : "Personal"}
      </Badge>
    ),
  },
  {
    id: "tools",
    header: "Tools & connections",
    cell: ({ row }) => {
      const { set, total } = accountCounts(row.original);
      const count = row.original.tools.length;
      const tools = `${count} ${count === 1 ? "tool" : "tools"}`;
      // An agent that shows no keys, as while Agent Keys is off, counts only accounts.
      const counted = Array.isArray(row.original.egress_services)
        ? total === 1
          ? "account or key"
          : "accounts and keys"
        : total === 1
          ? "account"
          : "accounts";
      return (
        <span className="text-muted-foreground">
          {/* An agent with no accounts or keys to set only counts its tools. */}
          {total === 0 ? tools : `${tools} / ${set} of ${total} ${counted} set`}
        </span>
      );
    },
  },
  {
    id: "mcp",
    header: "MCP gateway",
    cell: ({ row }) => {
      const mcp = row.original.mcp;
      if (!row.original.can_use)
        return (
          <span className="text-xs text-muted-foreground">
            Credential management only
          </span>
        );
      if (!mcp.selection?.enabled)
        return <span className="text-muted-foreground">—</span>;
      if (!row.original.mcp_available)
        return (
          <span
            className="text-xs text-muted-foreground"
            title="Runs tools with shared credentials, so it is not exposed through MCP"
          >
            MindRoom only
          </span>
        );
      const choice = mcp.selectedTools(row.original.agent_name);
      return (
        <label className="inline-flex items-center gap-2.5 whitespace-nowrap">
          <Checkbox
            aria-label={`Expose ${row.original.agent_display_name} through MCP`}
            checked={
              choice === null ? true : choice?.length ? "indeterminate" : false
            }
            disabled={mcp.loading || mcp.saving || mcp.error !== null}
            onCheckedChange={(checked) =>
              void mcp.toggle(row.original.agent_name, checked === true)
            }
          />
          <span className="text-xs text-muted-foreground">
            {choice === null
              ? "All tools"
              : choice?.length
                ? `${choice.length} selected`
                : "Off"}
          </span>
        </label>
      );
    },
  },
];

/** An agent's brokered API keys and accounts, when the viewer may use its egress credentials. */
function AgentEgress({
  agent,
  onChanged,
}: {
  agent: AgentConnections;
  onChanged: () => void;
}) {
  const services = agent.egress_services;
  // An array, even an empty one, marks an agent that can use egress credentials.
  if (!Array.isArray(services)) return null;
  // The catalog gives the agent's scope only through its services, which all share it.
  const shared = services.some((service) => service.is_shared);
  // Nobody who can change none of an agent's services manages it; with no services there is no telling.
  const viewOnly =
    services.length > 0 && services.every((service) => !service.can_manage);
  return (
    <div className="border-t border-border/60">
      <div className="flex flex-wrap items-center justify-between gap-2 px-5 pt-4">
        <h3 className="text-xs font-medium text-muted-foreground">
          API keys and accounts
        </h3>
        <a
          href={`/connections/egress#egress-agent-${agent.agent_name}`}
          className="text-xs font-medium text-primary underline-offset-4 hover:underline"
        >
          {viewOnly ? "View services" : "Manage services"}
        </a>
      </div>
      {shared && (
        <p className="px-5 pt-2 text-xs text-muted-foreground">
          {sharedAgentNote(
            services.some((service) => service.can_manage),
            agent.managers,
          )}
        </p>
      )}
      {services.length ? (
        <EgressServiceTable
          label={`API keys and accounts for ${agent.agent_display_name}`}
        >
          <EgressServiceRows
            agentName={agent.agent_name}
            services={services}
            onChanged={onChanged}
            agent={{
              displayName: agent.agent_display_name,
              shared,
              managers: agent.managers,
              services,
            }}
          />
        </EgressServiceTable>
      ) : (
        <p className="px-5 py-4 text-sm text-muted-foreground">
          No services are set up for this agent yet.
        </p>
      )}
    </div>
  );
}

export function AgentTable({
  agents,
  mcp,
  refreshVersion,
  onConnectionChange,
  onEgressChange,
}: {
  agents: AgentConnections[];
  mcp: McpSelectionState;
  refreshVersion: number;
  onConnectionChange: () => void;
  onEgressChange: () => void;
}) {
  const [expanded, setExpanded] = useState<ExpandedState>({});
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState("all");
  const [accounts, setAccounts] = useState<ConnectedAccounts>({});
  // Statuses count only until the catalog arrives again: an agent that is
  // closed cannot load its status again, so a fresh catalog replaces them,
  // and a status that loads after it still corrects it.
  const [accountsCatalog, setAccountsCatalog] = useState(agents);
  if (accountsCatalog !== agents) {
    setAccountsCatalog(agents);
    setAccounts({});
  }
  const onAccountStatus = useCallback(
    (agentName: string, provider: string, status: ConnectionStatus) =>
      setAccounts((known) =>
        known[accountKey(agentName, provider)] === status.connected
          ? known
          : { ...known, [accountKey(agentName, provider)]: status.connected },
      ),
    [],
  );
  const data = useMemo(
    () =>
      agents
        .filter((agent) =>
          scope === "personal"
            ? !agent.is_shared
            : scope === "shared"
              ? agent.is_shared
              : scope === "exposed"
                ? Object.prototype.hasOwnProperty.call(
                    mcp.selection?.agents ?? {},
                    agent.agent_name,
                  )
                : true,
        )
        .map((agent) => ({ ...agent, mcp, accounts })),
    [agents, scope, mcp, accounts],
  );
  const table = useReactTable({
    data,
    columns,
    state: { expanded, globalFilter: search },
    onExpandedChange: setExpanded,
    onGlobalFilterChange: setSearch,
    getRowId: (agent) => `connection:${agent.agent_name}`,
    getRowCanExpand: () => true,
    autoResetPageIndex: false,
    getCoreRowModel: getCoreRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    globalFilterFn: (row, _column, value: string) => {
      const needle = value.toLowerCase().trim();
      return searchTerms(row.original).some((name) =>
        name.toLowerCase().includes(needle),
      );
    },
  });
  return (
    <section
      className="overflow-hidden rounded-2xl border bg-card shadow-sm"
      aria-label="Agent connections"
    >
      <div className="flex flex-col gap-4 border-b px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">Agents</h2>
          <span className="rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground">
            {agents.length}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative flex-1 max-sm:basis-full sm:w-80">
            <Search
              className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              type="search"
              aria-label="Search agents, tools, or services"
              placeholder="Search agents, tools, services…"
              className="h-9 pl-9"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <select
            aria-label="Filter agents"
            value={scope}
            onChange={(event) => setScope(event.target.value)}
            className="h-9 rounded-lg border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="all">All agents</option>
            <option value="personal">Personal</option>
            <option value="shared">Shared</option>
            {mcp.selection?.enabled && (
              <option value="exposed">On MCP gateway</option>
            )}
          </select>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table
          aria-label="Agents"
          className={`w-full text-sm sm:min-w-[680px] ${STACKED.table}`}
        >
          <thead
            className={`bg-muted/30 text-left text-xs text-muted-foreground ${STACKED.head}`}
          >
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header) => (
                  <th
                    key={header.id}
                    scope="col"
                    className="px-5 py-3 font-medium"
                  >
                    {flexRender(
                      header.column.columnDef.header,
                      header.getContext(),
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody className={STACKED.body}>
            {table.getRowModel().rows.map((row) => (
              <Fragment key={row.id}>
                <tr
                  className={`border-t transition-colors hover:bg-muted/30 ${STACKED.row} max-sm:px-0 max-sm:py-2 ${row.getIsExpanded() ? "bg-muted/30" : ""}`}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      data-label={
                        cell.column.id === "agent"
                          ? undefined
                          : String(cell.column.columnDef.header)
                      }
                      className={`px-5 py-4 ${STACKED_CELL} max-sm:px-4 max-sm:py-1.5 ${cell.column.id === "agent" ? "" : STACKED_LABEL}`}
                    >
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </td>
                  ))}
                </tr>
                {row.getIsExpanded() && (
                  <tr className={STACKED.row}>
                    <td
                      colSpan={columns.length}
                      className={`border-t border-border/60 p-0 ${STACKED_CELL}`}
                    >
                      <section
                        id={`agent-tools-${row.original.agent_name}`}
                        aria-labelledby={`agent-${row.original.agent_name}`}
                        className="border-l-2 border-primary/30 bg-background"
                      >
                        <AgentTools
                          agent={row.original}
                          mcp={mcp}
                          refreshVersion={refreshVersion}
                          onConnectionChange={onConnectionChange}
                          onAccountStatus={onAccountStatus}
                        />
                        <AgentEgress
                          agent={row.original}
                          onChanged={onEgressChange}
                        />
                      </section>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {!table.getRowModel().rows.length && (
              <tr className={STACKED.row}>
                <td
                  colSpan={columns.length}
                  className={`px-5 py-12 text-center text-muted-foreground ${STACKED_CELL}`}
                >
                  No agents match your search.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="border-t px-5 py-3 text-xs text-muted-foreground">
        {table.getRowModel().rows.length} of {agents.length} agents · Expand an
        agent to manage its connections and tools.
      </p>
    </section>
  );
}
