import { useState } from "react";

import { NativeSelect } from "@/components/SchemaForm";
import { ToolConfigPanel } from "@/components/ToolConfig/ToolConfigPanel";
import type { ToolInfo } from "@/hooks/useTools";
import { useConfigStore } from "@/store/configStore";

/** Per-tool overrides for the tools every agent inherits from defaults.tools. */
export function DefaultToolSettings({ tools }: { tools: ToolInfo[] }) {
  const { config } = useConfigStore();
  const names = config?.defaults?.tools ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const toolName =
    selected != null && names.includes(selected) ? selected : names[0];
  if (toolName == null) {
    return null;
  }
  const info = tools.find((tool) => tool.name === toolName);

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">Default tool settings</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Override tool options for every agent that includes the default tools.
        </p>
      </div>
      <NativeSelect
        label="Default tool"
        value={toolName}
        options={names.map((name) => ({
          value: name,
          label: tools.find((tool) => tool.name === name)?.display_name ?? name,
        }))}
        onChange={setSelected}
      />
      <ToolConfigPanel
        target={{ kind: "defaults" }}
        toolName={toolName}
        toolDisplayName={info?.display_name}
        overrideFields={info?.agent_override_fields ?? null}
        configFields={info?.config_fields ?? null}
      />
    </div>
  );
}
