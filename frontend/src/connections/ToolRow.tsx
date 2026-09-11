import { Checkbox } from "@/components/ui/checkbox";
import { ConnectionIcon } from "./ConnectionIcon";
import type { AgentConnections, ConnectionTool } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

export function ToolName({ tool }: { tool: ConnectionTool }) {
  return (
    <div className="flex items-center gap-3">
      <ConnectionIcon
        names={[tool.name, tool.display_name]}
        iconName={tool.icon}
        className="h-8 w-8 rounded-lg [&_svg]:h-4 [&_svg]:w-4"
      />
      <div className="min-w-0">
        <span className="font-medium">{tool.display_name}</span>
        <p
          className="mt-0.5 max-w-md truncate text-xs text-muted-foreground"
          title={tool.description}
        >
          {tool.description}
        </p>
      </div>
    </div>
  );
}

export function ToolExposure({
  agent,
  tool,
  mcp,
}: {
  agent: AgentConnections;
  tool: ConnectionTool;
  mcp: McpSelectionState;
}) {
  if (tool.requires_room_context)
    return <span className="text-xs text-muted-foreground">MindRoom only</span>;
  if (!mcp.selection?.enabled)
    return <span className="text-muted-foreground">—</span>;
  const selected = mcp.selectedTools(agent.agent_name);
  return (
    <Checkbox
      aria-label={`Expose ${tool.display_name} for ${agent.agent_display_name} through MCP`}
      checked={selected === null || selected?.includes(tool.name) === true}
      disabled={mcp.loading || mcp.saving || mcp.error !== null}
      onCheckedChange={(checked) =>
        void mcp.toggleTool(agent, tool.name, checked === true)
      }
    />
  );
}
