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
  Trash2,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useScopedConfigValidation } from "@/hooks/useScopedConfigValidation";
import {
  classifySchemaNode,
  fieldLabel,
  hasEmptyDefault,
  initialValue,
  isPlainObject,
  matchUnionVariant,
  objectProperties,
  resolveSchema,
  setObjectKey,
  type JsonSchema,
  type ReferenceKind,
  type SchemaNode,
} from "@/lib/configSchema";
import { cn } from "@/lib/utils";

import { useReferenceOptions } from "./references";

export type SchemaPath = Array<string | number>;

const DEFAULT_OPTION = "__default__";
const NONE_OPTION = "__none__";
const SELECT_CLASS =
  "glass-control h-10 w-full px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50";

export interface SchemaFieldsProps {
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  /** Called with undefined to remove the key. */
  onFieldChange: (key: string, next: unknown) => void;
  path: SchemaPath;
  /** Keys rendered elsewhere, typically by a hand-built editor. */
  exclude?: readonly string[];
  /** Restrict and order the rendered keys. */
  include?: readonly string[];
}

export interface SchemaFieldProps {
  name: string;
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  /** Called with undefined to remove the key. */
  onChange: (next: unknown) => void;
  path: SchemaPath;
  required?: boolean;
  label?: string;
}

/** Property keys of an object schema that a form with these filters renders. */
export function schemaFieldKeys(
  schema: JsonSchema,
  root: JsonSchema,
  {
    exclude = [],
    include,
  }: { exclude?: readonly string[]; include?: readonly string[] } = {},
): string[] {
  const keys = objectProperties(schema, root).map(([key]) => key);
  const ordered =
    include == null ? keys : include.filter((key) => keys.includes(key));
  return ordered.filter((key) => !exclude.includes(key));
}

export function SchemaFields({
  schema,
  root,
  value,
  onFieldChange,
  path,
  exclude,
  include,
}: SchemaFieldsProps) {
  const resolved = resolveSchema(schema, root);
  const required = new Set(resolved.required ?? []);
  const record = isPlainObject(value) ? value : {};
  const keys = schemaFieldKeys(resolved, root, { exclude, include });

  return (
    <div className="space-y-5">
      {keys.map((key) => (
        <SchemaField
          key={key}
          name={key}
          schema={resolved.properties![key]}
          root={root}
          value={record[key]}
          required={required.has(key)}
          path={[...path, key]}
          onChange={(next) => onFieldChange(key, next)}
        />
      ))}
    </div>
  );
}

type Presence = "inline" | "toggle" | "tri";

// Nullable fields whose default is null only need "set or not"; nullable
// fields with another default (or a default factory) also need an explicit null.
function presenceMode(node: SchemaNode, required: boolean): Presence {
  if (!node.nullable) {
    return "inline";
  }
  if (!required && node.hasDefault && node.defaultValue === null) {
    return "toggle";
  }
  return "tri";
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

function helperText(node: SchemaNode): string | undefined {
  const summary = defaultSummary(node);
  const parts = [node.description, summary ? `Default: ${summary}.` : null];
  const text = parts.filter(Boolean).join(" ");
  return text || undefined;
}

function ResetButton({
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

function NativeSelect({
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

function FieldChrome({
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

export function SchemaField({
  name,
  schema,
  root,
  value,
  onChange,
  path,
  required = false,
  label: labelOverride,
}: SchemaFieldProps) {
  const node = classifySchemaNode(schema, root);
  const references = useReferenceOptions();
  const errorForPath = useScopedConfigValidation(path);
  const error = errorForPath([], true);
  const id = useId();
  const label = labelOverride ?? fieldLabel(name);
  const presence = presenceMode(node, required);
  const canReset = !required && value !== undefined;
  const reset = canReset ? (
    <ResetButton label={label} onReset={() => onChange(undefined)} />
  ) : null;

  if (node.kind === "const") {
    return null;
  }

  if (node.kind === "boolean") {
    if (presence === "inline") {
      const checked =
        typeof value === "boolean" ? value : node.defaultValue === true;
      return (
        <div className="space-y-1">
          <div className="flex items-start gap-3">
            <Checkbox
              id={id}
              checked={checked}
              onCheckedChange={(next) => onChange(next === true)}
              className="mt-0.5"
            />
            <div className="min-w-0 flex-1">
              <Label
                htmlFor={id}
                className="cursor-pointer text-sm font-medium"
              >
                {label}
              </Label>
              {node.description && (
                <p className="mt-1 text-xs text-muted-foreground">
                  {node.description}
                </p>
              )}
            </div>
            {reset}
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      );
    }
    const options = [
      { value: DEFAULT_OPTION, label: "Default" },
      { value: "true", label: "On" },
      { value: "false", label: "Off" },
      ...(presence === "tri" ? [{ value: NONE_OPTION, label: "None" }] : []),
    ];
    const current =
      value === undefined
        ? DEFAULT_OPTION
        : value === null
          ? NONE_OPTION
          : String(value);
    return (
      <FieldChrome
        label={label}
        htmlFor={id}
        helper={helperText(node)}
        error={error}
      >
        <NativeSelect
          id={id}
          label={label}
          value={current}
          options={options}
          onChange={(next) =>
            onChange(
              next === DEFAULT_OPTION
                ? undefined
                : next === NONE_OPTION
                  ? null
                  : next === "true",
            )
          }
        />
      </FieldChrome>
    );
  }

  if (
    node.kind === "number" ||
    node.kind === "string" ||
    node.kind === "enum"
  ) {
    return (
      <FieldChrome
        label={label}
        htmlFor={id}
        helper={helperText(node)}
        error={error}
        actions={reset}
      >
        <ScalarInput
          id={id}
          label={label}
          node={node}
          value={value}
          required={required}
          presence={presence}
          references={references}
          onChange={onChange}
        />
      </FieldChrome>
    );
  }

  const editor = (
    <ValueEditor
      name={name}
      label={label}
      node={node}
      root={root}
      value={value}
      path={path}
      onChange={onChange}
    />
  );

  if (presence === "inline") {
    if (node.kind === "object") {
      return (
        <ObjectDisclosure
          label={label}
          helper={node.description}
          error={error}
          actions={reset}
          defaultOpen={isPlainObject(value) && Object.keys(value).length > 0}
        >
          {editor}
        </ObjectDisclosure>
      );
    }
    return (
      <FieldChrome
        label={label}
        helper={node.description}
        error={error}
        actions={reset}
      >
        {editor}
      </FieldChrome>
    );
  }

  const enabled = value !== undefined && value !== null;
  const enable = () => onChange(initialValue(node, root, references));
  const presenceControl =
    presence === "toggle" ? (
      <div className="flex items-center gap-2">
        <Checkbox
          id={id}
          checked={enabled}
          onCheckedChange={(next) =>
            next === true ? enable() : onChange(undefined)
          }
        />
        <Label htmlFor={id} className="cursor-pointer text-sm">
          Configure {label.toLowerCase()}
        </Label>
      </div>
    ) : (
      <NativeSelect
        id={id}
        label={label}
        value={
          value === undefined
            ? DEFAULT_OPTION
            : value === null
              ? NONE_OPTION
              : "custom"
        }
        options={[
          ...(required ? [] : [{ value: DEFAULT_OPTION, label: "Default" }]),
          { value: NONE_OPTION, label: "None" },
          { value: "custom", label: "Custom" },
        ]}
        onChange={(next) => {
          if (next === DEFAULT_OPTION) {
            onChange(undefined);
          } else if (next === NONE_OPTION) {
            onChange(null);
          } else if (!enabled) {
            enable();
          }
        }}
      />
    );

  return (
    <FieldChrome label={label} helper={node.description} error={error}>
      {presenceControl}
      {enabled && <NestedBlock>{editor}</NestedBlock>}
    </FieldChrome>
  );
}

function NestedBlock({ children }: { children: ReactNode }) {
  return (
    <div className="border-l-2 border-border/60 pl-4 pt-1">{children}</div>
  );
}

function ObjectDisclosure({
  label,
  helper,
  error,
  actions,
  defaultOpen,
  children,
}: {
  label: string;
  helper?: string;
  error?: string;
  actions?: ReactNode;
  defaultOpen: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
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

function ScalarInput({
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
  references: Record<ReferenceKind, string[]>;
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

  const isNull = value === null;
  const control =
    node.kind === "number" ? (
      <NumberInput
        id={id}
        node={node}
        value={typeof value === "number" ? value : undefined}
        disabled={isNull}
        onChange={(next) => onChange(next)}
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
          onChange(text === "" && !required ? undefined : text)
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
      placeholder={summary ?? undefined}
      min={node.schema.minimum ?? node.schema.exclusiveMinimum}
      max={node.schema.maximum ?? node.schema.exclusiveMaximum}
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
  const placeholder = defaultSummary(node) ?? undefined;

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

function Suggestions({ id, options }: { id: string; options: string[] }) {
  return (
    <datalist id={id}>
      {options.map((option) => (
        <option key={option} value={option} />
      ))}
    </datalist>
  );
}

function ValueEditor({
  name,
  label,
  node,
  root,
  value,
  path,
  onChange,
}: {
  name: string;
  label: string;
  node: SchemaNode;
  root: JsonSchema;
  value: unknown;
  path: SchemaPath;
  onChange: (next: unknown) => void;
}) {
  const references = useReferenceOptions();
  switch (node.kind) {
    case "object":
      return (
        <SchemaFields
          schema={node.schema}
          root={root}
          value={value}
          path={path}
          onFieldChange={(key, next) => {
            const updated = setObjectKey(value, key, next);
            // An emptied optional block falls back to its defaults.
            onChange(
              Object.keys(updated).length === 0 && !node.nullable
                ? undefined
                : updated,
            );
          }}
        />
      );
    // Absent collections show their default; emptying one is only the same
    // as removing the key when the default is empty too.
    case "list":
      return (
        <ListEditor
          label={label}
          node={node}
          root={root}
          value={
            Array.isArray(value)
              ? value
              : value === undefined && Array.isArray(node.defaultValue)
                ? node.defaultValue
                : []
          }
          path={path}
          onChange={(items) =>
            onChange(
              items.length === 0 && !node.nullable && hasEmptyDefault(node)
                ? undefined
                : items,
            )
          }
        />
      );
    case "map":
      return (
        <MapEditor
          label={label}
          node={node}
          root={root}
          value={
            isPlainObject(value)
              ? value
              : value === undefined && isPlainObject(node.defaultValue)
                ? node.defaultValue
                : {}
          }
          path={path}
          onChange={(entries) =>
            onChange(
              Object.keys(entries).length === 0 &&
                !node.nullable &&
                hasEmptyDefault(node)
                ? undefined
                : entries,
            )
          }
        />
      );
    case "union":
      return (
        <UnionEditor
          name={name}
          label={label}
          node={node}
          root={root}
          value={value}
          path={path}
          onChange={onChange}
          references={references}
        />
      );
    case "freeform":
      return <YamlEditor label={label} value={value} onChange={onChange} />;
    default:
      return (
        <SchemaField
          name={name}
          label={label}
          schema={node.schema}
          root={root}
          value={value}
          path={path}
          required
          onChange={onChange}
        />
      );
  }
}

function isStringItems(node: SchemaNode, root: JsonSchema): boolean {
  if (node.hint.reference != null) {
    return true;
  }
  const items = classifySchemaNode(node.items ?? {}, root);
  return items.kind === "string" || items.kind === "enum";
}

function ListEditor({
  label,
  node,
  root,
  value,
  path,
  onChange,
}: {
  label: string;
  node: SchemaNode;
  root: JsonSchema;
  value: unknown[];
  path: SchemaPath;
  onChange: (items: unknown[]) => void;
}) {
  const references = useReferenceOptions();
  if (isStringItems(node, root)) {
    return (
      <StringListEditor
        label={label}
        values={value.map(String)}
        suggestions={
          node.hint.reference ? references[node.hint.reference] : undefined
        }
        onChange={onChange}
      />
    );
  }
  const itemNode = classifySchemaNode(node.items ?? {}, root);
  const move = (index: number, offset: number) => {
    const next = [...value];
    const [item] = next.splice(index, 1);
    next.splice(index + offset, 0, item);
    onChange(next);
  };
  return (
    <div className="space-y-3">
      {value.map((item, index) => (
        <div
          key={index}
          className="space-y-3 rounded-lg border border-border/60 p-3"
        >
          <div className="flex items-center justify-between gap-2">
            <Badge variant="outline">
              {label} {index + 1}
            </Badge>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Move ${label} ${index + 1} up`}
                disabled={index === 0}
                onClick={() => move(index, -1)}
              >
                <ArrowUp className="h-4 w-4" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Move ${label} ${index + 1} down`}
                disabled={index === value.length - 1}
                onClick={() => move(index, 1)}
              >
                <ArrowDown className="h-4 w-4" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove ${label} ${index + 1}`}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <NestedValue
            name={`${label} ${index + 1}`}
            node={itemNode}
            schema={node.items ?? {}}
            root={root}
            value={item}
            path={[...path, index]}
            onChange={(next) =>
              onChange(
                value.map((current, i) => (i === index ? next : current)),
              )
            }
          />
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() =>
          onChange([...value, initialValue(itemNode, root, references)])
        }
      >
        <Plus className="mr-2 h-4 w-4" />
        Add {label.toLowerCase()}
      </Button>
    </div>
  );
}

/** Render an item or map value: objects inline, anything else as a labeled field. */
function NestedValue({
  name,
  node,
  schema,
  root,
  value,
  path,
  onChange,
}: {
  name: string;
  node: SchemaNode;
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  path: SchemaPath;
  onChange: (next: unknown) => void;
}) {
  if (node.kind === "object") {
    return (
      <SchemaFields
        schema={node.schema}
        root={root}
        value={value}
        path={path}
        onFieldChange={(key, next) => onChange(setObjectKey(value, key, next))}
      />
    );
  }
  return (
    <SchemaField
      name={name}
      label={name}
      schema={schema}
      root={root}
      value={value}
      path={path}
      required
      onChange={onChange}
    />
  );
}

function StringListEditor({
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
  const add = () => {
    const trimmed = draft.trim();
    if (trimmed === "" || values.includes(trimmed)) {
      return;
    }
    onChange([...values, trimmed]);
    setDraft("");
  };
  const available = suggestions?.filter((option) => !values.includes(option));

  return (
    <div className="space-y-2">
      {values.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {values.map((item) => (
            <Badge key={item} variant="secondary" className="gap-1 pr-1">
              <span className="font-mono text-xs">{item}</span>
              <button
                type="button"
                className="rounded-sm p-0.5 hover:bg-muted"
                aria-label={`Remove ${item}`}
                onClick={() =>
                  onChange(values.filter((value) => value !== item))
                }
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Input
          aria-label={`New ${label.toLowerCase()} entry`}
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
          aria-label={`Add ${label.toLowerCase()} entry`}
          disabled={draft.trim() === ""}
          onClick={add}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function MapEditor({
  label,
  node,
  root,
  value,
  path,
  onChange,
}: {
  label: string;
  node: SchemaNode;
  root: JsonSchema;
  value: Record<string, unknown>;
  path: SchemaPath;
  onChange: (entries: Record<string, unknown>) => void;
}) {
  const references = useReferenceOptions();
  const [draftKey, setDraftKey] = useState("");
  const keyListId = useId();
  const valueSchema = node.values ?? {};
  const valueNode = classifySchemaNode(valueSchema, root);
  const scalarValues =
    valueNode.kind === "string" ||
    valueNode.kind === "number" ||
    valueNode.kind === "enum" ||
    valueNode.kind === "boolean";
  // Hints on a map describe its values; the value schema itself carries none.
  const valueFieldSchema: JsonSchema = {
    ...valueSchema,
    "x-mindroom": {
      ...(node.hint.reference ? { reference: node.hint.reference } : {}),
      ...(node.hint.secret ? { secret: true } : {}),
      ...(node.hint.multiline ? { multiline: true } : {}),
    },
  };
  const keySuggestions = node.hint.key_reference
    ? references[node.hint.key_reference].filter((key) => !(key in value))
    : undefined;
  const add = () => {
    const key = draftKey.trim();
    if (key === "" || key in value) {
      return;
    }
    onChange({
      ...value,
      [key]: initialValue(
        classifySchemaNode(valueFieldSchema, root),
        root,
        references,
      ),
    });
    setDraftKey("");
  };

  return (
    <div className="space-y-3">
      {Object.entries(value).map(([key, entry]) => (
        <div
          key={key}
          className={cn(
            "space-y-2",
            !scalarValues && "rounded-lg border border-border/60 p-3",
          )}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-sm">{key}</span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Remove ${key}`}
              onClick={() => onChange(setObjectKey(value, key, undefined))}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
          <NestedValue
            name={key}
            node={valueNode}
            schema={valueFieldSchema}
            root={root}
            value={entry}
            path={[...path, key]}
            onChange={(next) =>
              onChange(
                setObjectKey(value, key, next === undefined ? entry : next),
              )
            }
          />
        </div>
      ))}
      <div className="flex items-center gap-2">
        <Input
          aria-label={`New ${label.toLowerCase()} key`}
          placeholder="Key"
          value={draftKey}
          list={keySuggestions ? keyListId : undefined}
          onChange={(event) => setDraftKey(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        {keySuggestions && (
          <Suggestions id={keyListId} options={keySuggestions} />
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`Add ${label.toLowerCase()} key`}
          disabled={draftKey.trim() === "" || draftKey.trim() in value}
          onClick={add}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function UnionEditor({
  name,
  label,
  node,
  root,
  value,
  path,
  onChange,
  references,
}: {
  name: string;
  label: string;
  node: SchemaNode;
  root: JsonSchema;
  value: unknown;
  path: SchemaPath;
  onChange: (next: unknown) => void;
  references: Record<ReferenceKind, string[]>;
}) {
  // Show the schema default until the user picks a variant.
  const effective =
    value === undefined && node.hasDefault ? node.defaultValue : value;
  const index = matchUnionVariant(node, effective, root);
  const variantNodes = useMemo(
    () =>
      node.variants.map((variant) => classifySchemaNode(variant.schema, root)),
    [node.variants, root],
  );

  if (node.discriminator != null) {
    const discriminator = node.discriminator;
    const selected = index >= 0 ? node.variants[index] : null;
    return (
      <div className="space-y-4">
        <NativeSelect
          label={`${label} type`}
          value={index >= 0 ? String(index) : ""}
          options={[
            ...(index >= 0 ? [] : [{ value: "", label: "Choose…" }]),
            ...node.variants.map((variant, i) => ({
              value: String(i),
              label: variant.label,
            })),
          ]}
          onChange={(next) =>
            onChange(initialValue(variantNodes[Number(next)], root, references))
          }
        />
        {selected?.schema.description && (
          <p className="text-xs text-muted-foreground">
            {selected.schema.description}
          </p>
        )}
        {selected && (
          <SchemaFields
            schema={selected.schema}
            root={root}
            value={effective}
            path={path}
            exclude={[discriminator]}
            onFieldChange={(key, next) =>
              onChange(setObjectKey(effective, key, next))
            }
          />
        )}
      </div>
    );
  }

  // Plain unions, such as true/false or a list of patterns.
  const options = node.variants.flatMap((variant, i) =>
    variantNodes[i].kind === "boolean"
      ? [
          { value: "true", label: "Yes" },
          { value: "false", label: "No" },
        ]
      : [{ value: `variant-${i}`, label: variant.label }],
  );
  const current =
    typeof effective === "boolean"
      ? String(effective)
      : index >= 0
        ? `variant-${index}`
        : "";
  const selectedNode =
    index >= 0 && variantNodes[index].kind !== "boolean"
      ? variantNodes[index]
      : null;
  return (
    <div className="space-y-3">
      <NativeSelect
        label={`${label} mode`}
        value={current}
        options={[
          ...(current === "" ? [{ value: "", label: "Choose…" }] : []),
          ...options,
        ]}
        onChange={(next) => {
          if (next === "true" || next === "false") {
            onChange(next === "true");
            return;
          }
          const variantNode =
            variantNodes[Number(next.replace("variant-", ""))];
          onChange(initialValue(variantNode, root, references));
        }}
      />
      {selectedNode && (
        <ValueEditor
          name={name}
          label={label}
          // Nullable keeps an emptied variant value instead of reverting to the
          // union default, so the chosen variant stays selected.
          node={{
            ...selectedNode,
            nullable: true,
            hint: { ...selectedNode.hint, ...node.hint },
          }}
          root={root}
          value={effective}
          path={path}
          onChange={onChange}
        />
      )}
    </div>
  );
}

function dumpYaml(value: unknown): string {
  return value === undefined ? "" : yaml.dump(value);
}

function YamlEditor({
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
