// Schema-driven config form: SchemaFields renders an object's properties and
// SchemaField dispatches one property to a widget, recursing into nested
// objects, collections, and unions.
import { useId, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  type SchemaNode,
} from "@/lib/configSchema";
import { cn } from "@/lib/utils";

import {
  DEFAULT_OPTION,
  FieldChrome,
  MapKeyInput,
  NativeSelect,
  NestedBlock,
  NONE_OPTION,
  ObjectDisclosure,
  ResetButton,
  ScalarInput,
  StringListEditor,
  Suggestions,
  YamlEditor,
  helperText,
  inlineLabel,
  moveItem,
  noneLabel,
  presenceMode,
  unsetOptions,
  type SchemaPath,
} from "./inputs";
import { useReferenceOptions } from "./references";

interface SchemaFieldsProps {
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
  /**
   * Show errors reported for the object itself, such as model validators.
   * Leave off when a surrounding field already shows them.
   */
  showOwnError?: boolean;
}

interface SchemaFieldProps {
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
  showOwnError = false,
}: SchemaFieldsProps) {
  const errorForPath = useScopedConfigValidation(path);
  const ownError = showOwnError ? errorForPath([], true) : undefined;
  const resolved = resolveSchema(schema, root);
  const required = new Set(resolved.required ?? []);
  const record = isPlainObject(value) ? value : {};
  const keys = schemaFieldKeys(resolved, root, { exclude, include });

  return (
    <div className="space-y-5">
      {ownError && <p className="text-xs text-destructive">{ownError}</p>}
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
  // Plain-union errors carry a branch name in their location, so match any
  // error under the field; discriminated unions render tagged paths below.
  const error = errorForPath(
    [],
    !(node.kind === "union" && node.discriminator == null),
  );
  const hasNestedError = errorForPath([], false) !== undefined;
  const id = useId();
  const label = labelOverride ?? fieldLabel(name);
  const presence = presenceMode(node, required, value);
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
      ...unsetOptions(node, required, value, "Default"),
      { value: "true", label: "On" },
      { value: "false", label: "Off" },
      ...(presence === "tri"
        ? [{ value: NONE_OPTION, label: noneLabel(node) }]
        : []),
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
          hasNestedError={hasNestedError}
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
          Configure {inlineLabel(label)}
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
          ...unsetOptions(node, required, value, "Default"),
          { value: NONE_OPTION, label: noneLabel(node) },
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
        />
      );
    case "freeform":
      return (
        <YamlEditor
          label={label}
          value={value}
          secret={node.hint.secret === true}
          onChange={onChange}
        />
      );
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
        showOwnError
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

// Reference-hinted lists hold names; the store normalizes structured tool
// entries in defaults.tools to names before they reach the form.
function isStringItems(
  node: SchemaNode,
  root: JsonSchema,
  value: unknown[],
): boolean {
  if (!value.every((item) => typeof item === "string")) {
    return false;
  }
  if (node.hint.reference != null) {
    return true;
  }
  const items = classifySchemaNode(node.items!, root);
  return items.kind === "string" || items.kind === "enum";
}

/**
 * React keys that follow each item through moves and removals, so per-item
 * state such as a revealed secret or an open block stays with its item.
 */
function useItemKeys(length: number) {
  const [stored, setStored] = useState<number[]>([]);
  // Items added here or outside this editor (Reset, reload) get fresh keys.
  const keys = stored.slice(0, length);
  for (let next = Math.max(-1, ...stored) + 1; keys.length < length; next++) {
    keys.push(next);
  }
  return [keys, setStored] as const;
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
  const [itemKeys, setItemKeys] = useItemKeys(value.length);
  if (isStringItems(node, root, value)) {
    return (
      <StringListEditor
        label={label}
        values={value as string[]}
        suggestions={
          node.hint.reference ? references[node.hint.reference] : undefined
        }
        onChange={onChange}
      />
    );
  }
  const itemNode = classifySchemaNode(node.items!, root);
  const move = (index: number, offset: number) => {
    setItemKeys(moveItem(itemKeys, index, offset));
    onChange(moveItem(value, index, offset));
  };
  const remove = (index: number) => {
    setItemKeys(itemKeys.filter((_, i) => i !== index));
    onChange(value.filter((_, i) => i !== index));
  };
  return (
    <div className="space-y-3">
      {value.map((item, index) => (
        <div
          key={itemKeys[index]}
          className="space-y-3 rounded-lg border border-border/60 p-3"
        >
          <div className="flex items-center justify-between gap-2">
            <Badge variant="outline">#{index + 1}</Badge>
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
                onClick={() => remove(index)}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <NestedValue
            name={`${label} ${index + 1}`}
            node={itemNode}
            schema={node.items!}
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
        Add {inlineLabel(label)}
      </Button>
    </div>
  );
}

function renameKey(
  entries: Record<string, unknown>,
  from: string,
  to: string,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(entries).map(([key, entry]) => [
      key === from ? to : key,
      entry,
    ]),
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
  // Hints on a map describe its values, except the one for its keys.
  const { key_reference: keyReference, ...valueHint } = node.hint;
  const valueSchema: JsonSchema = { ...node.values!, "x-mindroom": valueHint };
  const valueNode = classifySchemaNode(valueSchema, root);
  const scalarValues =
    valueNode.kind === "string" ||
    valueNode.kind === "number" ||
    valueNode.kind === "enum" ||
    valueNode.kind === "boolean";
  const keys = Object.keys(value);
  const keySuggestions = keyReference
    ? references[keyReference].filter((key) => !keys.includes(key))
    : undefined;
  const add = () => {
    const key = draftKey.trim();
    if (key === "" || keys.includes(key)) {
      return;
    }
    onChange({
      ...value,
      [key]: initialValue(valueNode, root, references),
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
            <MapKeyInput
              entryKey={key}
              taken={keys}
              onRename={(next) => onChange(renameKey(value, key, next))}
            />
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
            schema={valueSchema}
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
          aria-label={`New ${inlineLabel(label)} key`}
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
          aria-label={`Add ${inlineLabel(label)} key`}
          disabled={draftKey.trim() === "" || keys.includes(draftKey.trim())}
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
  // Show the schema default until the user picks a variant.
  const effective =
    value === undefined && node.hasDefault ? node.defaultValue : value;
  const index = matchUnionVariant(node, effective, root);
  const variantNodes = node.variants.map((variant) =>
    classifySchemaNode(variant.schema, root),
  );

  if (node.discriminator != null) {
    const discriminator = node.discriminator;
    const selected = index >= 0 ? node.variants[index] : null;
    // Discriminated variants are objects; keep fields both variants define
    // the same way, such as timeout_seconds, but not a free-form model ID that
    // would become a configured model name.
    const switchVariant = (next: number) => {
      const source = index >= 0 ? variantNodes[index] : null;
      const target = variantNodes[next];
      const sameField = (key: string) => {
        const from = source?.schema.properties![key];
        const to = target.schema.properties![key];
        if (from == null || to == null) {
          return false;
        }
        const fromNode = classifySchemaNode(from, root);
        const toNode = classifySchemaNode(to, root);
        return (
          fromNode.kind === toNode.kind &&
          fromNode.hint.reference === toNode.hint.reference
        );
      };
      const shared = Object.entries(
        isPlainObject(effective) ? effective : {},
      ).filter(([key]) => key !== discriminator && sameField(key));
      onChange({
        ...(initialValue(target, root, references) as Record<string, unknown>),
        ...Object.fromEntries(shared),
      });
    };
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
          onChange={(next) => switchVariant(Number(next))}
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
            // Pydantic reports discriminated-union errors under the tag value.
            path={[...path, String(selected.discriminatorValue)]}
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
