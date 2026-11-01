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
  /** The tool's agent-only fields, which only an agent's entry may set. */
  overrideFields?: ToolFieldSchema[] | null;
  /** The tool's config_fields, and the secrets the panel points to the Tools page. */
  configFields?: ToolFieldSchema[] | null;
}

/**
 * The fields a tool entry may set, as the backend merges them: the
 * configFields, then the agent-only fields, without those the backend refuses
 * in a tool entry. A same-name agent-only field replaces its config twin in
 * place, because a Map keeps a key's first position when it is set again.
 */
function resolveFields(
  overrideFields?: ToolFieldSchema[] | null,
  configFields?: ToolFieldSchema[] | null,
): ToolFieldSchema[] {
  const fields = new Map(
    [...(configFields ?? []), ...(overrideFields ?? [])].map((field) => [
      field.name,
      field,
    ]),
  );
  return [...fields.values()].filter(
    (field) => field.authored_override !== false,
  );
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
  if (field.type === "boolean") return String(value === true);
  if (field.type === "number") {
    if (typeof value === "number") return String(value);
    return "";
  }
  // A text field whose list form the backend also accepts reads it as the
  // comma-separated text.
  if (Array.isArray(value)) return value.join(", ");
  return typeof value === "string" ? value : "";
}

/**
 * The value a ticked field stores, which is the value it shows: a list stores
 * its non-blank entries, even none, and a boolean the state of its box. A blank
 * input shows no value and stores none (null clears the key), because a stored
 * blank string would replace the tool's saved setting or default.
 */
function normalizePersistedValue(
  field: ToolFieldSchema,
  value: string | string[],
): unknown | null {
  if (field.type === "string[]") {
    return (Array.isArray(value) ? value : [])
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
  }
  if (field.type === "boolean") return value === "true";
  if (field.type === "number") {
    const trimmed = typeof value === "string" ? value.trim() : "";
    if (trimmed.length === 0) return null;
    const n = Number(trimmed);
    return Number.isNaN(n) ? null : n;
  }
  const trimmed = typeof value === "string" ? value.trim() : "";
  return trimmed.length > 0 ? trimmed : null;
}

type DraftValues = Partial<Record<string, string | string[]>>;

// An authored value that drops the field's defaults.tools setting.
const INHERIT = "__MINDROOM_INHERIT__";

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
  const hasFields = fields.length > 0;
  const secretLabels = (configFields ?? [])
    .filter((field) => field.type === "password")
    .map((field) => field.label);
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
    deferEnabled || fields.some((field) => fieldValue(field) !== null);

  // Send a merge patch: null removes a key, and keys the patch does not name
  // stay as authored.
  const commitOverrides = (patch: Record<string, unknown>) => {
    if (target.kind === "agent") {
      updateAgentToolOverrides(target.agentId, toolName, patch);
    } else {
      updateDefaultToolOverrides(toolName, patch);
    }
  };

  // Tool entries keep lazy-loading flags beside the override values; a null
  // flag removes it.
  const setLazyLoading = (defer: boolean, initial: boolean) =>
    commitOverrides({
      defer: defer ? true : null,
      initial: defer && initial ? true : null,
    });

  const setFieldDraft = (
    field: ToolFieldSchema,
    value: string | string[] | undefined,
  ) => {
    setDraftState({ owner, drafts: { ...drafts, [field.name]: value } });
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
    const text = typeof value === "string" ? value : "";
    const defaultValue = field.default;
    const fieldId = `override-${field.name}`;

    if (field.type === "string[]") {
      const items = Array.isArray(value) ? value : [];
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
                <div className="text-xs text-muted-foreground">No values.</div>
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
                checked={text === "true"}
                onCheckedChange={(checked) =>
                  setFieldDraft(field, checked ? "true" : "false")
                }
              />
              <Label htmlFor={fieldId} className="cursor-pointer text-sm">
                {text === "true" ? "Enabled" : "Disabled"}
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
      return (
        <div>
          {isEnabled ? (
            <Select
              value={text || (field.default as string | undefined)}
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

    // text, number, url
    return (
      <div>
        {isEnabled ? (
          <Input
            id={fieldId}
            type={
              field.type === "number" || field.type === "url"
                ? field.type
                : "text"
            }
            value={text}
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
              ? `Default: ${String(defaultValue)}`
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
        {fields.map((field) => {
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

      {secretLabels.length > 0 && (
        <p className="mt-4 text-xs text-muted-foreground">
          Secrets are set on the Tools page: {secretLabels.join(", ")}.
        </p>
      )}
    </div>
  );
}
