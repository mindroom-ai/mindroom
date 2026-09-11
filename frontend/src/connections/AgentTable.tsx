import { Fragment, useMemo, useState } from "react";
import {
  type ColumnDef,
  type ExpandedState,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  ChevronDown,
  ChevronRight,
  Search,
  Users,
  UserRound,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { AgentTools } from "./AgentTools";
import type { AgentConnections } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

type AgentTableRow = AgentConnections & { mcp: McpSelectionState };

const columns: ColumnDef<AgentTableRow>[] = [
  {
    id: "agent",
    header: "Agent",
    accessorFn: (agent) => agent.agent_display_name,
    cell: ({ row }) => {
      const agent = row.original;
      const Icon = agent.is_shared ? Users : UserRound;
      return (
        <button
          type="button"
          className="flex w-full items-center gap-3 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={`${row.getIsExpanded() ? "Collapse" : "Expand"} ${agent.agent_display_name}`}
          aria-expanded={row.getIsExpanded()}
          aria-controls={`agent-tools-${agent.agent_name}`}
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
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary/5 text-primary">
            <Icon className="h-4 w-4" aria-hidden="true" />
          </span>
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
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        {row.original.tools.length} tools
        <span className="mx-2 text-border">/</span>
        {row.original.services.length} connections
      </span>
    ),
  },
  {
    id: "mcp",
    header: "MCP access",
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

export function AgentTable({
  agents,
  mcp,
  refreshVersion,
  onConnectionChange,
}: {
  agents: AgentConnections[];
  mcp: McpSelectionState;
  refreshVersion: number;
  onConnectionChange: () => void;
}) {
  const [expanded, setExpanded] = useState<ExpandedState>({});
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState("all");
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
        .map((agent) => ({ ...agent, mcp })),
    [agents, scope, mcp],
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
      const agent = row.original;
      return [
        agent.agent_name,
        agent.agent_display_name,
        ...agent.tools.flatMap((tool) => [tool.name, tool.display_name]),
      ].some((name) => name.toLowerCase().includes(value.toLowerCase().trim()));
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
          <div className="relative flex-1 sm:w-64">
            <Search
              className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              type="search"
              aria-label="Search agents or tools"
              placeholder="Search agents or tools…"
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
              <option value="exposed">MCP enabled</option>
            )}
          </select>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table aria-label="Agents" className="w-full min-w-[680px] text-sm">
          <thead className="bg-muted/30 text-left text-xs text-muted-foreground">
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
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <Fragment key={row.id}>
                <tr
                  className={`border-t transition-colors hover:bg-muted/30 ${row.getIsExpanded() ? "bg-muted/30" : ""}`}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="px-5 py-4">
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </td>
                  ))}
                </tr>
                {row.getIsExpanded() && (
                  <tr>
                    <td
                      colSpan={columns.length}
                      className="border-t border-border/60 p-0"
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
                        />
                      </section>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {!table.getRowModel().rows.length && (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-5 py-12 text-center text-muted-foreground"
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
