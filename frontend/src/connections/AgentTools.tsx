import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ConnectionServiceRows } from "./ConnectionServiceRows";
import { STACKED, STACKED_CELL, STACKED_LABEL } from "./stackedTable";
import { ToolExposure, ToolName } from "./ToolRow";
import type { AgentConnections, ConnectionStatus } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

export function AgentTools({
  agent,
  mcp,
  refreshVersion,
  onConnectionChange,
  onAccountStatus,
}: {
  agent: AgentConnections;
  mcp: McpSelectionState;
  refreshVersion: number;
  onConnectionChange: () => void;
  /** Told each time one of the agent's account statuses loads. */
  onAccountStatus?: (
    agentName: string,
    provider: string,
    status: ConnectionStatus,
  ) => void;
}) {
  const [otherOpen, setOtherOpen] = useState(false);
  const otherTools = agent.tools.filter((tool) => tool.provider === null);
  if (!agent.tools.length)
    return (
      <p className="px-6 py-5 text-sm text-muted-foreground">
        No tools are available for this agent yet.
      </p>
    );
  return (
    <div className="overflow-x-auto">
      <table
        aria-label={`${agent.agent_display_name} tools`}
        className={`w-full text-sm sm:min-w-[640px] ${STACKED.table}`}
      >
        <thead
          className={`text-left text-xs text-muted-foreground ${STACKED.head}`}
        >
          <tr>
            <th scope="col" className="w-[44%] px-5 py-3 font-medium">
              Tool
            </th>
            <th scope="col" className="w-[25%] px-4 py-3 font-medium">
              Connection
            </th>
            <th scope="col" className="w-[15%] px-4 py-3 font-medium">
              MCP gateway
            </th>
            <th scope="col" className="px-5 py-3 text-right font-medium">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className={STACKED.body}>
          {agent.services.map((service) => (
            <ConnectionServiceRows
              key={service.provider}
              agent={agent}
              service={service}
              mcp={mcp}
              refreshVersion={refreshVersion}
              onConnectionChange={onConnectionChange}
              onStatus={onAccountStatus}
            />
          ))}
          {otherTools.length > 0 && (
            <>
              <tr className={`border-t border-border/60 ${STACKED.row}`}>
                <td colSpan={4} className={STACKED_CELL}>
                  <button
                    type="button"
                    className="flex w-full items-center gap-2 px-5 py-3 text-left text-sm hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    aria-expanded={otherOpen}
                    aria-label={`Other tools for ${agent.agent_display_name}`}
                    onClick={() => setOtherOpen((open) => !open)}
                  >
                    {otherOpen ? (
                      <ChevronDown className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <ChevronRight className="h-4 w-4" aria-hidden="true" />
                    )}
                    <span className="font-medium">Other tools</span>
                    <span className="rounded-md bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                      {otherTools.length}
                    </span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      No additional setup
                    </span>
                  </button>
                </td>
              </tr>
              {otherOpen &&
                otherTools.map((tool) => (
                  <tr
                    key={tool.name}
                    className={`border-t border-border/60 ${STACKED.row} max-sm:py-2`}
                  >
                    <th
                      scope="row"
                      className={`px-5 py-3 text-left font-normal ${STACKED_CELL} max-sm:px-4 max-sm:py-1`}
                    >
                      <ToolName tool={tool} />
                    </th>
                    <td
                      data-label="Connection"
                      className={`px-4 py-3 text-xs text-muted-foreground ${STACKED_CELL} ${STACKED_LABEL} max-sm:py-1`}
                    >
                      No setup needed
                    </td>
                    <td
                      data-label="MCP gateway"
                      className={`px-4 py-3 ${STACKED_CELL} ${STACKED_LABEL} max-sm:py-1`}
                    >
                      <ToolExposure agent={agent} tool={tool} mcp={mcp} />
                    </td>
                    <td className="max-sm:hidden" />
                  </tr>
                ))}
            </>
          )}
        </tbody>
      </table>
    </div>
  );
}
