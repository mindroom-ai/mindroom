import { useMemo } from "react";
import { Save, SlidersHorizontal } from "lucide-react";

import { showSaveFailureToastIfNeeded } from "@/components/shared";
import {
  ReferenceOptionsProvider,
  SchemaField,
  SchemaSection,
  SchemaUnavailable,
} from "@/components/SchemaForm";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { useTools } from "@/hooks/useTools";
import { fieldLabel } from "@/lib/configSchema";
import { readConfigRoot, useConfigStore } from "@/store/configStore";

import { DefaultToolSettings } from "./DefaultToolSettings";

// Roots edited on their own dashboard pages.
const PAGE_OWNED_ROOTS = new Set([
  "agents",
  "teams",
  "rooms",
  "room_models",
  "models",
  "memory",
  "knowledge_bases",
  "calls",
]);

// Roots whose listed keys another page edits; Settings shows the rest.
const PARTLY_OWNED_ROOTS: Record<
  string,
  { definition: string; owned: readonly string[] }
> = {
  voice: {
    definition: "VoiceConfig",
    owned: ["enabled", "visible_router_echo", "stt", "intelligence"],
  },
  room_defaults: { definition: "RoomDefaultsConfig", owned: ["admins"] },
};

export function Settings() {
  const { config, isDirty, isLoading, saveConfig, updateConfigValue } =
    useConfigStore();
  const { schema: root, error, retry } = useConfigSchema();
  const { tools } = useTools();
  const { toast } = useToast();
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
      {root != null && (
        <Card className="min-h-0 overflow-y-auto">
          <CardContent className="space-y-6 pt-6">
            <ReferenceOptionsProvider options={referenceOptions}>
              {Object.keys(root.properties!)
                .filter((key) => !PAGE_OWNED_ROOTS.has(key))
                .map((key) => {
                  const value =
                    config == null ? undefined : readConfigRoot(config, key);
                  const partly = PARTLY_OWNED_ROOTS[key];
                  return partly == null ? (
                    <SchemaField
                      key={key}
                      name={key}
                      schema={root.properties![key]}
                      root={root}
                      value={value}
                      path={[key]}
                      onChange={(next) => updateConfigValue([key], next)}
                    />
                  ) : (
                    <SchemaSection
                      key={key}
                      title={fieldLabel(key)}
                      definition={partly.definition}
                      value={value}
                      path={[key]}
                      exclude={partly.owned}
                      onFieldChange={(field, next) =>
                        updateConfigValue([key, field], next)
                      }
                    />
                  );
                })}
              <DefaultToolSettings tools={tools} />
            </ReferenceOptionsProvider>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
