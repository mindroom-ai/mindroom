// Leaf widgets for schema-driven config forms. Nothing here renders nested
// schema nodes, so SchemaField.tsx can compose these without import cycles.
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import yaml from "js-yaml";
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { ReferenceOptions, SchemaNode } from "@/lib/configSchema";

export type SchemaPath = Array<string | number>;

type Presence = "inline" | "toggle" | "tri";

export const DEFAULT_OPTION = "__default__";
export const NONE_OPTION = "__none__";
const SELECT_CLASS =
  "glass-control h-10 w-full px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50";

// Nullable fields whose default is null only need "set or not"; nullable
// fields with another default, or none reported, also need an explicit null.
export function presenceMode(node: SchemaNode, required: boolean): Presence {
  if (!node.nullable) {
    return "inline";
  }
  if (!required && node.hasDefault && node.defaultValue === null) {
    return "toggle";
  }
  return "tri";
}

/** Lowercase a label for use mid-sentence, keeping leading acronyms. */
export function inlineLabel(label: string): string {
  return /^[A-Z]{2}/.test(label)
    ? label
    : label.charAt(0).toLowerCase() + label.slice(1);
}

function displayScalar(value: unknown): string {
  if (typeof value === "boolean") {
    return value ? "on" : "off";
  }
  return String(value);
}

function defaultSummary(node: SchemaNode): string | null {
  const value = node.defaultValue;
  if (!node.hasDefault || value == null || typeof value === "object") {
    return null;
  }
  if (typeof value === "string" && value === "") {
    return null;
  }
  return displayScalar(value);
}

export function helperText(node: SchemaNode): string | undefined {
  const summary = defaultSummary(node);
  const description = node.description?.trim();
  if (summary == null) {
    return description || undefined;
  }
  if (!description) {
    return `Default: ${summary}`;
  }
  const separator = /[.!?]$/.test(description) ? " " : ". ";
  return `${description}${separator}Default: ${summary}`;
}

export function ResetButton({
  label,
  onReset,
}: {
  label: string;
  onReset: () => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-7 px-2 text-xs text-muted-foreground"
      aria-label={`Reset ${label}`}
      onClick={onReset}
    >
      <RotateCcw className="mr-1 h-3 w-3" />
      Reset
    </Button>
  );
}

export function NativeSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id?: string;
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <select
      id={id}
      aria-label={label}
      className={SELECT_CLASS}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export function FieldChrome({
  label,
  htmlFor,
  helper,
  error,
  actions,
  children,
}: {
  label: string;
  htmlFor?: string;
  helper?: string;
  error?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <Label htmlFor={htmlFor} className="text-sm font-medium">
            {label}
          </Label>
          {helper && (
            <p className="mt-1 text-xs text-muted-foreground">{helper}</p>
          )}
        </div>
        {actions}
      </div>
      {children}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

export function NestedBlock({ children }: { children: ReactNode }) {
  return (
    <div className="border-l-2 border-border/60 pl-4 pt-1">{children}</div>
  );
}

/** Open while collapsed content has a validation error, so it is never hidden. */
export function useOpenOnError(defaultOpen: boolean, hasError: boolean) {
  const [open, setOpen] = useState(defaultOpen || hasError);
  useEffect(() => {
    if (hasError) {
      setOpen(true);
    }
  }, [hasError]);
  return [open, setOpen] as const;
}

export function ObjectDisclosure({
  label,
  helper,
  error,
  hasNestedError,
  actions,
  defaultOpen,
  children,
}: {
  label: string;
  helper?: string;
  error?: string;
  hasNestedError: boolean;
  actions?: ReactNode;
  defaultOpen: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useOpenOnError(defaultOpen, hasNestedError);
  const Icon = open ? ChevronDown : ChevronRight;
  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between gap-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-start gap-1 text-left"
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
        >
          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0">
            <span className="block text-sm font-medium">{label}</span>
            {helper && (
              <span className="mt-1 block text-xs text-muted-foreground">
                {helper}
              </span>
            )}
          </span>
        </button>
        {actions}
      </div>
      {open && <NestedBlock>{children}</NestedBlock>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

export function Suggestions({
  id,
  options,
}: {
  id: string;
  options: string[];
}) {
  return (
    <datalist id={id}>
      {options.map((option) => (
        <option key={option} value={option} />
      ))}
    </datalist>
  );
}

export function ScalarInput({
  id,
  label,
  node,
  value,
  required,
  presence,
  references,
  onChange,
}: {
  id: string;
  label: string;
  node: SchemaNode;
  value: unknown;
  required: boolean;
  presence: Presence;
  references: ReferenceOptions;
  onChange: (next: unknown) => void;
}) {
  const reference = node.hint.reference;
  const selectOptions =
    node.kind === "enum"
      ? node.options.map(String)
      : node.kind === "string" &&
          (reference === "model" || reference === "agent")
        ? references[reference]
        : null;

  if (selectOptions != null) {
    const summary = defaultSummary(node);
    const current =
      value === undefined
        ? DEFAULT_OPTION
        : value === null
          ? NONE_OPTION
          : String(value);
    const values =
      selectOptions.includes(current) ||
      current === DEFAULT_OPTION ||
      current === NONE_OPTION
        ? selectOptions
        : [current, ...selectOptions];
    const options = [
      ...(required
        ? []
        : [
            {
              value: DEFAULT_OPTION,
              label: summary ? `Default (${summary})` : "Not set",
            },
          ]),
      ...(presence === "tri" ? [{ value: NONE_OPTION, label: "None" }] : []),
      ...values.map((option) => ({ value: option, label: option })),
    ];
    return (
      <NativeSelect
        id={id}
        label={label}
        value={current}
        options={options}
        onChange={(next) => {
          if (next === DEFAULT_OPTION) {
            onChange(undefined);
          } else if (next === NONE_OPTION) {
            onChange(null);
          } else {
            onChange(
              node.options.find((option) => String(option) === next) ?? next,
            );
          }
        }}
      />
    );
  }

  // Clearing only restores the default when the default is empty as well;
  // otherwise an empty string is a real value (for example "no welcome").
  const clearedRemovesKey =
    !required &&
    (!node.hasDefault || node.defaultValue == null || node.defaultValue === "");
  const isNull = value === null;
  const control =
    node.kind === "number" ? (
      <NumberInput
        id={id}
        node={node}
        value={typeof value === "number" ? value : undefined}
        disabled={isNull}
        onChange={onChange}
      />
    ) : (
      <TextInput
        id={id}
        node={node}
        value={typeof value === "string" ? value : ""}
        disabled={isNull}
        suggestions={
          reference === "room" || reference === "tool"
            ? references[reference]
            : undefined
        }
        onChange={(text) =>
          onChange(text === "" && clearedRemovesKey ? undefined : text)
        }
      />
    );

  if (presence !== "tri") {
    return control;
  }
  return (
    <div className="space-y-2">
      {control}
      <div className="flex items-center gap-2">
        <Checkbox
          id={`${id}-none`}
          checked={isNull}
          onCheckedChange={(next) => onChange(next === true ? null : undefined)}
        />
        <Label htmlFor={`${id}-none`} className="cursor-pointer text-xs">
          Set to none
        </Label>
      </div>
    </div>
  );
}

function NumberInput({
  id,
  node,
  value,
  disabled,
  onChange,
}: {
  id: string;
  node: SchemaNode;
  value: number | undefined;
  disabled: boolean;
  onChange: (next: number | undefined) => void;
}) {
  const external = value === undefined ? "" : String(value);
  const [draft, setDraft] = useState(external);
  useEffect(() => {
    setDraft((current) => (Number(current) === value ? current : external));
  }, [external, value]);
  const summary = defaultSummary(node);

  return (
    <Input
      id={id}
      type="number"
      inputMode="decimal"
      value={draft}
      disabled={disabled}
      placeholder={summary == null ? undefined : `Default: ${summary}`}
      // Exclusive bounds have no HTML equivalent; the backend enforces them.
      min={node.schema.minimum}
      max={node.schema.maximum}
      step={node.integer ? 1 : "any"}
      onChange={(event) => {
        const text = event.target.value;
        setDraft(text);
        if (text.trim() === "") {
          onChange(undefined);
          return;
        }
        const parsed = Number(text);
        if (!Number.isNaN(parsed)) {
          onChange(parsed);
        }
      }}
    />
  );
}

function TextInput({
  id,
  node,
  value,
  disabled,
  suggestions,
  onChange,
}: {
  id: string;
  node: SchemaNode;
  value: string;
  disabled: boolean;
  suggestions?: string[];
  onChange: (text: string) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const listId = `${id}-suggestions`;
  const summary = defaultSummary(node);
  const placeholder = summary == null ? undefined : `Default: ${summary}`;

  if (node.hint.multiline) {
    return (
      <Textarea
        id={id}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        rows={4}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  if (node.hint.secret) {
    return (
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type={revealed ? "text" : "password"}
          autoComplete="off"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={revealed ? "Hide value" : "Show value"}
          onClick={() => setRevealed((current) => !current)}
        >
          {revealed ? (
            <EyeOff className="h-4 w-4" />
          ) : (
            <Eye className="h-4 w-4" />
          )}
        </Button>
      </div>
    );
  }
  return (
    <>
      <Input
        id={id}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        list={suggestions ? listId : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {suggestions && <Suggestions id={listId} options={suggestions} />}
    </>
  );
}

export function StringListEditor({
  label,
  values,
  suggestions,
  onChange,
}: {
  label: string;
  values: string[];
  suggestions?: string[];
  onChange: (values: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const listId = useId();
  // Lists such as command arguments are ordered and may repeat values, so
  // entries are addressed by position; the backend validates uniqueness.
  const add = () => {
    const trimmed = draft.trim();
    if (trimmed === "") {
      return;
    }
    onChange([...values, trimmed]);
    setDraft("");
  };
  const move = (index: number, offset: number) => {
    const next = [...values];
    const [item] = next.splice(index, 1);
    next.splice(index + offset, 0, item);
    onChange(next);
  };
  const available = suggestions?.filter((option) => !values.includes(option));

  return (
    <div className="space-y-2">
      {values.length > 0 && (
        <ul className="space-y-1">
          {values.map((item, index) => (
            <li
              key={index}
              className="flex items-center gap-1 rounded-md border border-border/60 py-0.5 pl-3 pr-1"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-xs">
                {item}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Move ${item} up`}
                disabled={index === 0}
                onClick={() => move(index, -1)}
              >
                <ArrowUp className="h-3 w-3" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Move ${item} down`}
                disabled={index === values.length - 1}
                onClick={() => move(index, 1)}
              >
                <ArrowDown className="h-3 w-3" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Remove ${item}`}
                onClick={() => onChange(values.filter((_, i) => i !== index))}
              >
                <X className="h-3 w-3" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-2">
        <Input
          aria-label={`New ${inlineLabel(label)} entry`}
          value={draft}
          list={available ? listId : undefined}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        {available && <Suggestions id={listId} options={available} />}
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`Add ${inlineLabel(label)} entry`}
          disabled={draft.trim() === ""}
          onClick={add}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

/** Editable mapping key; commits a rename on blur or Enter when the new key is free. */
export function MapKeyInput({
  entryKey,
  taken,
  onRename,
}: {
  entryKey: string;
  taken: string[];
  onRename: (next: string) => void;
}) {
  const [draft, setDraft] = useState(entryKey);
  useEffect(() => {
    setDraft(entryKey);
  }, [entryKey]);
  const commit = () => {
    const next = draft.trim();
    if (next !== "" && next !== entryKey && !taken.includes(next)) {
      onRename(next);
    } else {
      setDraft(entryKey);
    }
  };
  return (
    <Input
      aria-label={`Rename ${entryKey}`}
      className="h-8 max-w-xs font-mono text-sm"
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commit();
        }
      }}
    />
  );
}

function dumpYaml(value: unknown): string {
  return value === undefined ? "" : yaml.dump(value);
}

export function YamlEditor({
  label,
  value,
  onChange,
}: {
  label: string;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const serialized = useMemo(() => dumpYaml(value), [value]);
  const [draft, setDraft] = useState(serialized);
  const [parseError, setParseError] = useState<string | null>(null);
  useEffect(() => {
    setDraft(serialized);
    setParseError(null);
  }, [serialized]);

  return (
    <div className="space-y-1">
      <Textarea
        aria-label={`${label} YAML`}
        className="font-mono text-xs"
        rows={Math.min(12, Math.max(3, draft.split("\n").length))}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          if (draft === serialized) {
            return;
          }
          try {
            onChange(draft.trim() === "" ? undefined : yaml.load(draft));
            setParseError(null);
          } catch (error) {
            setParseError(
              error instanceof Error ? error.message : "Invalid YAML",
            );
          }
        }}
      />
      <p className="text-xs text-muted-foreground">
        Free-form YAML, applied when the field loses focus.
      </p>
      {parseError && <p className="text-xs text-destructive">{parseError}</p>}
    </div>
  );
}
