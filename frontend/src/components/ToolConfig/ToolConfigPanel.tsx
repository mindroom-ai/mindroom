import { useId, useState } from "react";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useConfigStore } from "@/store/configStore";
import type { ToolFieldSchema } from "@/hooks/useTools";

/**
 * Whose tool entry the panel edits: one agent's, where lazy loading applies
 * unless the tool is a preset or control-plane tool, or the shared
 * defaults.tools, which rejects lazy loading.
 */
export type ToolOverrideTarget =
  | { kind: "agent"; agentId: string; lazyLoading: boolean }
  | { kind: "defaults" };

interface ToolConfigPanelProps {
  target: ToolOverrideTarget;
  toolName: string;
  toolDisplayName?: string;
  /** Dedicated per-agent override fields (curated, e.g. shell). */
  overrideFields?: ToolFieldSchema[] | null;
  /** Global config_fields from the tool metadata (fallback schema). */
  configFields?: ToolFieldSchema[] | null;
}

/** Resolve which schema to use: prefer dedicated overrideFields, fall back to configFields. */
function resolveFields(
  overrideFields?: ToolFieldSchema[] | null,
  configFields?: ToolFieldSchema[] | null,
): ToolFieldSchema[] | null {
  if (overrideFields && overrideFields.length > 0) return overrideFields;
  if (configFields && configFields.length > 0) return configFields;
  return null;
}

function coerceDisplayValue(field: ToolFieldSchema, value: unknown): string {
  if (value == null) return "";
  if (field.type === "boolean") return value ? "true" : "false";
  if (field.type === "string[]") {
    if (Array.isArray(value)) return value.join(", ");
    return String(value);
  }
  return String(value);
}

function coerceEditValue(
  field: ToolFieldSchema,
  value: unknown,
): string | string[] {
  if (field.type === "string[]") {
    if (Array.isArray(value))
      return value.filter((e): e is string => typeof e === "string");
    // The backend reads a string as comma- or newline-separated values.
    if (typeof value === "string")
      return value
        .split(/[,\n]/)
        .map((entry) => entry.trim())
        .filter((entry) => entry.length > 0);
    return [];
  }
  if (field.type === "boolean") {
    if (typeof value === "boolean") return String(value);
    return "";
  }
  if (field.type === "number") {
    if (typeof value === "number") return String(value);
    return "";
  }
  return typeof value === "string" ? value : "";
}

/** Normalize a draft value back to what should be persisted (or null to clear). */
function normalizePersistedValue(
  field: ToolFieldSchema,
  value: string | string[],
): unknown | null {
  if (field.type === "string[]") {
    const values = (Array.isArray(value) ? value : [])
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    return values.length > 0 ? values : null;
  }
  if (field.type === "boolean") {
    if (value === "true") return true;
    if (value === "false") return false;
    return null;
  }
  if (field.type === "number") {
    const trimmed = typeof value === "string" ? value.trim() : "";
    if (trimmed.length === 0) return null;
    const n = Number(trimmed);
    return Number.isNaN(n) ? null : n;
  }
  const trimmed = typeof value === "string" ? value.trim() : "";
  return trimmed.length > 0 ? trimmed : null;
}

type DraftValues = Record<string, string | string[]>;

// An authored value that drops the field's defaults.tools setting.
const INHERIT = "__MINDROOM_INHERIT__";

// Tool entries keep lazy-loading flags beside the override values; a null
// entry in the merge patch below removes the flag.
function lazyFlagPatch(defer: boolean, initial: boolean) {
  return {
    defer: defer ? true : null,
    initial: defer && initial ? true : null,
  };
}

export function ToolConfigPanel({
  target,
  toolName,
  toolDisplayName,
  overrideFields,
  configFields,
}: ToolConfigPanelProps) {
  const {
    getAgentToolOverrides,
    updateAgentToolOverrides,
    getDefaultToolOverrides,
    updateDefaultToolOverrides,
  } = useConfigStore();
  const lazyLoading = target.kind === "agent" && target.lazyLoading;
  const fields = resolveFields(overrideFields, configFields);
  const hasFields = fields != null && fields.length > 0;
  const currentOverrides =
    target.kind === "agent"
      ? getAgentToolOverrides(target.agentId, toolName)
      : getDefaultToolOverrides(toolName);
  const deferEnabled = currentOverrides?.defer === true;
  const lazyId = useId();
  const initialEnabled = currentOverrides?.initial === true;

  // What the user typed into each field, such as a blank entry or a cleared
  // input, which the stored value drops. Drafts belong to one tool entry.
  const owner = JSON.stringify([target, toolName]);
  const [draftState, setDraftState] = useState<{
    owner: string;
    drafts: DraftValues;
  }>({ owner, drafts: {} });
  const drafts = draftState.owner === owner ? draftState.drafts : {};

  const title = toolDisplayName ?? toolName;

  /**
   * The value a field shows: its draft while the draft still stands for the
   * stored value, else the stored value, INHERIT for the inherit sentinel, or
   * null when the field is not overridden.
   */
  const fieldValue = (field: ToolFieldSchema) => {
    const stored = currentOverrides?.[field.name] ?? null;
    if (stored === INHERIT) return INHERIT;
    const draft = drafts[field.name];
    if (
      draft !== undefined &&
      JSON.stringify(normalizePersistedValue(field, draft)) ===
        JSON.stringify(stored)
    ) {
      return draft;
    }
    return stored == null ? null : coerceEditValue(field, stored);
  };

  const isCustomized =
    deferEnabled || (fields ?? []).some((field) => fieldValue(field) !== null);

  // Send a merge patch: null removes a key, and keys the patch does not name
  // stay as authored.
  const commitOverrides = (patch: Record<string, unknown>) => {
    if (target.kind === "agent") {
      updateAgentToolOverrides(target.agentId, toolName, patch);
    } else {
      updateDefaultToolOverrides(toolName, patch);
    }
  };

  const setLazyLoading = (defer: boolean, initial: boolean) =>
    commitOverrides(lazyFlagPatch(defer, initial));

  const setFieldDraft = (
    field: ToolFieldSchema,
    value: string | string[] | undefined,
  ) => {
    const { [field.name]: _previous, ...rest } = drafts;
    setDraftState({
      owner,
      drafts: value === undefined ? rest : { ...rest, [field.name]: value },
    });
    commitOverrides({
      [field.name]:
        value === undefined ? null : normalizePersistedValue(field, value),
    });
  };

  const toggleField = (field: ToolFieldSchema, checked: boolean) =>
    // Ticking starts from the tool default.
    setFieldDraft(
      field,
      checked ? coerceEditValue(field, field.default) : undefined,
    );

  const renderFieldInput = (
    field: ToolFieldSchema,
    value: string | string[] | null,
  ) => {
    const isEnabled = value !== null;
    const draftValue = value ?? (field.type === "string[]" ? [] : "");
    const defaultValue = field.default;
    const fieldId = `override-${field.name}`;

    if (field.type === "string[]") {
      const items = isEnabled
        ? Array.isArray(draftValue)
          ? draftValue
          : []
        : [];
      const defaultItems = Array.isArray(defaultValue) ? defaultValue : [];

      return (
        <div className="space-y-2">
          {!isEnabled && defaultItems.length > 0 && (
            <div className="text-xs text-muted-foreground italic">
              Default: {defaultItems.join(", ")}
            </div>
          )}
          {!isEnabled && defaultItems.length === 0 && (
            <div className="text-xs text-muted-foreground italic">
              No default
            </div>
          )}
          {isEnabled && (
            <>
              {items.length === 0 && (
                <div className="text-xs text-muted-foreground">
                  No values. Add one to create an override.
                </div>
              )}
              {items.map((item, index) => (
                <div
                  key={`${field.name}-${index}`}
                  className="flex items-center gap-2"
                >
                  <Input
                    value={item}
                    placeholder={field.placeholder ?? field.label}
                    onChange={(event) => {
                      const nextItems = [...items];
                      nextItems[index] = event.target.value;
                      setFieldDraft(field, nextItems);
                    }}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={`Remove ${field.label} value ${index + 1}`}
                    onClick={() => {
                      setFieldDraft(
                        field,
                        items.filter((_, i) => i !== index),
                      );
                    }}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setFieldDraft(field, [...items, ""])}
              >
                <Plus className="mr-2 h-4 w-4" />
                Add value
              </Button>
            </>
          )}
        </div>
      );
    }

    if (field.type === "boolean") {
      return (
        <div>
          {isEnabled ? (
            <div className="flex items-center space-x-2">
              <Checkbox
                id={fieldId}
                checked={draftValue === "true"}
                onCheckedChange={(checked) =>
                  setFieldDraft(field, checked ? "true" : "false")
                }
              />
              <Label htmlFor={fieldId} className="cursor-pointer text-sm">
                {draftValue === "true" ? "Enabled" : "Disabled"}
              </Label>
            </div>
          ) : (
            <div className="text-xs text-muted-foreground italic">
              {defaultValue != null
                ? `Default: ${defaultValue ? "Enabled" : "Disabled"}`
                : "No default"}
            </div>
          )}
        </div>
      );
    }

    if (field.type === "select" && field.options) {
      const displayValue = typeof draftValue === "string" ? draftValue : "";
      return (
        <div>
          {isEnabled ? (
            <Select
              value={displayValue || (field.default as string | undefined)}
              onValueChange={(val) => setFieldDraft(field, val)}
            >
              <SelectTrigger id={fieldId}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {field.options.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <div className="text-xs text-muted-foreground italic">
              {defaultValue != null
                ? `Default: ${
                    field.options.find((o) => o.value === String(defaultValue))
                      ?.label ?? String(defaultValue)
                  }`
                : "No default"}
            </div>
          )}
        </div>
      );
    }

    // text, password, number, url
    const displayValue = typeof draftValue === "string" ? draftValue : "";
    return (
      <div>
        {isEnabled ? (
          <Input
            id={fieldId}
            type={
              field.type === "password"
                ? "password"
                : field.type === "number"
                  ? "number"
                  : field.type === "url"
                    ? "url"
                    : "text"
            }
            value={displayValue}
            placeholder={field.placeholder ?? field.label}
            onChange={(e) => {
              setFieldDraft(field, e.target.value);
            }}
            min={
              field.type === "number"
                ? (field.validation?.min as number)
                : undefined
            }
            max={
              field.type === "number"
                ? (field.validation?.max as number)
                : undefined
            }
          />
        ) : (
          <div className="text-xs text-muted-foreground italic">
            {defaultValue != null
              ? `Default: ${
                  field.type === "password"
                    ? "••••••••"
                    : coerceDisplayValue(field, defaultValue)
                }`
              : "No default"}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="rounded-lg border bg-background/80 p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">
            {title} —{" "}
            {target.kind === "agent"
              ? "Per-Agent Settings"
              : "Default Settings"}
          </div>
          {hasFields && (
            <div className="text-xs text-muted-foreground">
              {target.kind === "agent"
                ? "Toggle fields to override the tool default for this agent."
                : "Toggle fields to override the tool default for every agent that includes default tools."}
            </div>
          )}
        </div>
        {isCustomized && <Badge variant="secondary">Customized</Badge>}
      </div>

      {lazyLoading && (
        <div className="mb-4 space-y-2 border-b pb-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id={`${lazyId}-defer`}
              checked={deferEnabled}
              onCheckedChange={(checked) =>
                setLazyLoading(checked === true, initialEnabled)
              }
            />
            <Label
              htmlFor={`${lazyId}-defer`}
              className="cursor-pointer text-sm"
            >
              Defer until searched
            </Label>
          </div>
          <p className="pl-6 text-xs text-muted-foreground">
            Hide this tool's definitions until the agent searches for them.
          </p>
          <div className="flex items-center gap-2 pl-6">
            <Checkbox
              id={`${lazyId}-initial`}
              checked={initialEnabled}
              disabled={!deferEnabled}
              onCheckedChange={(checked) =>
                setLazyLoading(deferEnabled, checked === true)
              }
            />
            <Label
              htmlFor={`${lazyId}-initial`}
              className={`cursor-pointer text-sm ${
                deferEnabled ? "" : "text-muted-foreground"
              }`}
            >
              Send on every request
            </Label>
          </div>
        </div>
      )}

      {!hasFields && (
        <p className="text-sm text-muted-foreground">
          {lazyLoading
            ? "This tool has no other settings."
            : "No settings available for this tool."}
        </p>
      )}

      <div className="space-y-4">
        {(fields ?? []).map((field) => {
          const value = fieldValue(field);
          const isEnabled = value !== null;

          return (
            <div key={field.name} className="space-y-1.5">
              <div className="flex items-center gap-2">
                <Checkbox
                  id={`toggle-${field.name}`}
                  checked={isEnabled}
                  onCheckedChange={(checked) =>
                    toggleField(field, checked === true)
                  }
                  aria-label={`Override ${field.label}`}
                />
                <Label
                  htmlFor={`toggle-${field.name}`}
                  className={`cursor-pointer text-sm font-medium ${
                    !isEnabled ? "text-muted-foreground" : ""
                  }`}
                >
                  {field.label}
                </Label>
                {isEnabled && (
                  <Badge variant="outline" className="text-[11px]">
                    Overridden
                  </Badge>
                )}
              </div>
              {field.description && (
                <p className="pl-6 text-xs text-muted-foreground">
                  {field.description}
                </p>
              )}
              <div className="pl-6">
                {value === INHERIT ? (
                  <div className="text-xs text-muted-foreground italic">
                    Set to {INHERIT}: the saved tool setting or the tool default
                    applies.
                  </div>
                ) : (
                  renderFieldInput(field, value)
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
