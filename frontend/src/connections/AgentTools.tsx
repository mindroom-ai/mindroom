import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ConnectionServiceRows } from "./ConnectionServiceRows";
import { ToolExposure, ToolName } from "./ToolRow";
import type { AgentConnections } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

export function AgentTools({
  agent,
  mcp,
  refreshVersion,
  onConnectionChange,
}: {
  agent: AgentConnections;
  mcp: McpSelectionState;
  refreshVersion: number;
  onConnectionChange: () => void;
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
        className="w-full min-w-[640px] text-sm"
      >
        <thead className="text-left text-xs text-muted-foreground">
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
        <tbody>
          {agent.services.map((service) => (
            <ConnectionServiceRows
              key={service.provider}
              agent={agent}
              service={service}
              mcp={mcp}
              refreshVersion={refreshVersion}
              onConnectionChange={onConnectionChange}
            />
          ))}
          {otherTools.length > 0 && (
            <>
              <tr className="border-t border-border/60">
                <td colSpan={4}>
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
                  <tr key={tool.name} className="border-t border-border/60">
                    <th scope="row" className="px-5 py-3 text-left font-normal">
                      <ToolName tool={tool} />
                    </th>
                    <td className="px-4 py-3 text-xs text-muted-foreground">
                      No setup needed
                    </td>
                    <td className="px-4 py-3">
                      <ToolExposure agent={agent} tool={tool} mcp={mcp} />
                    </td>
                    <td />
                  </tr>
                ))}
            </>
          )}
        </tbody>
      </table>
    </div>
  );
}
