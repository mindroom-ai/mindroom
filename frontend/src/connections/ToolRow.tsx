import type { ReactNode } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { ConnectionIcon } from "./ConnectionIcon";
import type { AgentConnections, ConnectionTool } from "./types";
import type { McpSelectionState } from "./useMcpSelection";

/** A tool's or service's icon, name, and description, with any notes below them. */
export function ItemName({
  names,
  iconName,
  title,
  description,
  children,
}: {
  /** Names to pick the icon by, when `iconName` names none. */
  names: string[];
  iconName?: string | null;
  title: ReactNode;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-3">
      <ConnectionIcon
        names={names}
        iconName={iconName}
        className="h-8 w-8 rounded-lg [&_svg]:h-4 [&_svg]:w-4"
      />
      <div className="min-w-0 space-y-0.5">
        {/* A block, so the gap below it applies. */}
        <span className="block font-medium wrap-anywhere">{title}</span>
        {description && (
          <p
            className="max-w-md truncate text-xs text-muted-foreground"
            title={description}
          >
            {description}
          </p>
        )}
        {children}
      </div>
    </div>
  );
}

const STATUS_DOT = {
  set: "bg-primary",
  attention: "bg-amber-600 dark:bg-amber-500",
  unset: "border border-muted-foreground/70",
} as const;

/** A state as a dot and its label: set or connected, waiting on someone, or not set. */
export function StatusLabel({
  tone,
  children,
}: {
  tone: keyof typeof STATUS_DOT;
  children: ReactNode;
}) {
  return (
    <span className="inline-flex items-start gap-2 text-sm tabular-nums">
      <span
        aria-hidden="true"
        className={`mt-[0.45em] h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[tone]}`}
      />
      <span>{children}</span>
    </span>
  );
}

export function ToolName({ tool }: { tool: ConnectionTool }) {
  return (
    <ItemName
      names={[tool.name, tool.display_name]}
      iconName={tool.icon}
      title={tool.display_name}
      description={tool.description}
    />
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
  if (!agent.can_use)
    return (
      <span className="text-xs text-muted-foreground">No tool access</span>
    );
  if (!agent.mcp_available || !tool.mcp_available)
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
