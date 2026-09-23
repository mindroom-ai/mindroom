import { useMemo, useState } from "react";
import { Save, SlidersHorizontal } from "lucide-react";

import { showSaveFailureToastIfNeeded } from "@/components/shared";
import {
  ReferenceOptionsProvider,
  SchemaField,
  SchemaFields,
} from "@/components/SchemaForm";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { useTools } from "@/hooks/useTools";
import {
  classifySchemaNode,
  fieldLabel,
  rootPropertySchema,
  setObjectKey,
  type JsonSchema,
} from "@/lib/configSchema";
import { cn } from "@/lib/utils";
import { readConfigRoot, useConfigStore } from "@/store/configStore";

import {
  resolveSettingsSections,
  type SettingsEntry,
} from "./settingsSections";

function SettingsEntryView({
  entry,
  root,
  value,
  showHeading,
  onChange,
}: {
  entry: SettingsEntry;
  root: JsonSchema;
  value: unknown;
  showHeading: boolean;
  onChange: (next: unknown) => void;
}) {
  const schema = rootPropertySchema(root, entry.root);
  const node = classifySchemaNode(schema, root);
  // Plain object roots render their keys directly instead of one nested block.
  if (entry.keys != null || (node.kind === "object" && !node.nullable)) {
    const fields = (
      <SchemaFields
        schema={schema}
        root={root}
        value={value}
        path={[entry.root]}
        include={entry.keys}
        onFieldChange={(key, next) => onChange(setObjectKey(value, key, next))}
      />
    );
    if (!showHeading || entry.keys != null) {
      return fields;
    }
    return (
      <div className="space-y-4">
        <div>
          <h3 className="text-sm font-semibold">{fieldLabel(entry.root)}</h3>
          {node.description && (
            <p className="mt-1 text-xs text-muted-foreground">
              {node.description}
            </p>
          )}
        </div>
        {fields}
      </div>
    );
  }
  return (
    <SchemaField
      name={entry.root}
      schema={schema}
      root={root}
      value={value}
      path={[entry.root]}
      onChange={onChange}
    />
  );
}

export function Settings() {
  const { config, isDirty, isLoading, saveConfig, updateConfigRoot } =
    useConfigStore();
  const { schema: root, error } = useConfigSchema();
  const { tools } = useTools();
  const { toast } = useToast();
  const sections = useMemo(
    () => (root == null ? [] : resolveSettingsSections(root)),
    [root],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const active =
    sections.find((section) => section.id === selectedId) ?? sections[0];
  const referenceOptions = useMemo(
    () => ({ tool: tools.map((tool) => tool.name) }),
    [tools],
  );

  const handleSave = async () => {
    const result = await saveConfig();
    if (
      showSaveFailureToastIfNeeded(result, {
        staleMessage: "Save was superseded by newer settings edits.",
        fallbackMessage: "Failed to save settings.",
      })
    ) {
      return;
    }
    toast({
      title: "Settings Saved",
      description: "Your configuration has been updated.",
    });
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-5 w-5 text-primary" />
          <div>
            <h2 className="text-lg font-semibold">Settings</h2>
            <p className="text-sm text-muted-foreground">
              Every other option in config.yaml, described by the running
              backend.
            </p>
          </div>
        </div>
        <Button onClick={handleSave} disabled={!isDirty || isLoading}>
          <Save className="mr-2 h-4 w-4" />
          Save
        </Button>
      </div>

      {error != null && (
        <p className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
          Settings are unavailable: {error}
        </p>
      )}
      {root != null && active != null && (
        <div className="grid min-h-0 flex-1 gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
          <nav aria-label="Settings sections" className="hidden md:block">
            <ul className="space-y-1">
              {sections.map((section) => (
                <li key={section.id}>
                  <button
                    type="button"
                    aria-current={section.id === active.id ? "page" : undefined}
                    className={cn(
                      "w-full rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-muted",
                      section.id === active.id && "bg-muted font-medium",
                    )}
                    onClick={() => setSelectedId(section.id)}
                  >
                    {section.title}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <select
            aria-label="Settings section"
            className="glass-control h-10 px-3 text-sm md:hidden"
            value={active.id}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            {sections.map((section) => (
              <option key={section.id} value={section.id}>
                {section.title}
              </option>
            ))}
          </select>
          <Card className="min-h-0 overflow-y-auto">
            <CardHeader>
              <CardTitle>{active.title}</CardTitle>
              <CardDescription>{active.description}</CardDescription>
            </CardHeader>
            <CardContent>
              <ReferenceOptionsProvider options={referenceOptions}>
                <div className="space-y-8">
                  {active.entries.map((entry) => (
                    <SettingsEntryView
                      key={`${entry.root}:${entry.keys?.join(",") ?? "*"}`}
                      entry={entry}
                      root={root}
                      showHeading={active.entries.length > 1}
                      value={
                        config == null
                          ? undefined
                          : readConfigRoot(config, entry.root)
                      }
                      onChange={(next) => updateConfigRoot(entry.root, next)}
                    />
                  ))}
                </div>
              </ReferenceOptionsProvider>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
