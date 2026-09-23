import { useMemo, useState } from "react";
import { Save, SlidersHorizontal } from "lucide-react";

import { showSaveFailureToastIfNeeded } from "@/components/shared";
import {
  NativeSelect,
  ReferenceOptionsProvider,
  SchemaField,
  SchemaFields,
  SchemaUnavailable,
} from "@/components/SchemaForm";
import { Badge } from "@/components/ui/badge";
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
  type ConfigPath,
  type JsonSchema,
} from "@/lib/configSchema";
import { getConfigValidationIssues } from "@/lib/configValidation";
import { cn } from "@/lib/utils";
import { readConfigRoot, useConfigStore } from "@/store/configStore";

import { DefaultToolSettings } from "./DefaultToolSettings";
import {
  resolveSettingsSections,
  sectionHasIssue,
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
  onChange: (path: ConfigPath, next: unknown) => void;
}) {
  const schema = root.properties![entry.root];
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
        showOwnError
        onFieldChange={(key, next) => onChange([entry.root, key], next)}
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
      onChange={(next) => onChange([entry.root], next)}
    />
  );
}

export function Settings() {
  const {
    config,
    diagnostics,
    isDirty,
    isLoading,
    saveConfig,
    updateConfigValue,
  } = useConfigStore();
  const { schema: root, error, retry } = useConfigSchema();
  const { tools } = useTools();
  const { toast } = useToast();
  const sections = useMemo(
    () => (root == null ? [] : resolveSettingsSections(root)),
    [root],
  );
  const sectionsWithIssues = useMemo(() => {
    const issues = getConfigValidationIssues(diagnostics);
    return new Set(
      sections
        .filter((section) => sectionHasIssue(section, issues))
        .map((section) => section.id),
    );
  }, [sections, diagnostics]);
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
        <SchemaUnavailable subject="Settings" error={error} onRetry={retry} />
      )}
      {root == null && error == null && (
        <p className="text-sm text-muted-foreground">Loading settings...</p>
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
                    <span className="flex items-center justify-between gap-2">
                      {section.title}
                      {sectionsWithIssues.has(section.id) && (
                        <Badge variant="destructive">Needs attention</Badge>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <NativeSelect
            label="Settings section"
            className="md:hidden"
            value={active.id}
            options={sections.map((section) => ({
              value: section.id,
              label: sectionsWithIssues.has(section.id)
                ? `${section.title} (needs attention)`
                : section.title,
            }))}
            onChange={setSelectedId}
          />
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
                      onChange={updateConfigValue}
                    />
                  ))}
                  {active.id === "tools" && (
                    <DefaultToolSettings tools={tools} />
                  )}
                </div>
              </ReferenceOptionsProvider>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
