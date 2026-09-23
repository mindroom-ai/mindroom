// Interprets the configuration JSON schema served by /api/config/schema.
// The backend emits Pydantic's model_json_schema(); this module normalizes its
// nullable, $ref, and union shapes into one node kind per form widget.

export type ReferenceKind = "model" | "agent" | "room" | "tool";

export type ReferenceOptions = Record<ReferenceKind, string[]>;

export interface SchemaHint {
  reference?: ReferenceKind;
  key_reference?: ReferenceKind;
  secret?: boolean;
  multiline?: boolean;
}

export interface JsonSchema {
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  discriminator?: { propertyName: string };
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  "x-mindroom"?: SchemaHint;
}

export type SchemaNodeKind =
  | "boolean"
  | "number"
  | "string"
  | "enum"
  | "const"
  | "list"
  | "map"
  | "object"
  | "union"
  | "freeform";

export interface UnionVariant {
  label: string;
  schema: JsonSchema;
  discriminatorValue?: unknown;
}

export interface SchemaNode {
  kind: SchemaNodeKind;
  /** The resolved, non-null schema the widget renders. */
  schema: JsonSchema;
  /** Whether an explicit null is a valid value. */
  nullable: boolean;
  description?: string;
  /** Whether the schema declares a default; absent when the default depends on other fields. */
  hasDefault: boolean;
  defaultValue?: unknown;
  hint: SchemaHint;
  integer: boolean;
  options: unknown[];
  items?: JsonSchema;
  values?: JsonSchema;
  variants: UnionVariant[];
  discriminator?: string;
}

const ACRONYMS: Record<string, string> = {
  api: "API",
  id: "ID",
  livekit: "LiveKit",
  llm: "LLM",
  mcp: "MCP",
  oauth: "OAuth",
  pkce: "PKCE",
  stt: "STT",
  tts: "TTS",
  url: "URL",
};

export function isPlainObject(
  value: unknown,
): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function fieldLabel(key: string): string {
  const [first, ...rest] = key
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => ACRONYMS[word.toLowerCase()] ?? word.toLowerCase());
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

/** Follow $ref chains, letting the referring schema's metadata win. */
export function resolveSchema(
  schema: JsonSchema,
  root: JsonSchema,
): JsonSchema {
  let resolved = schema;
  while (resolved.$ref != null) {
    // Pydantic only emits local "#/$defs/<Name>" references.
    const { $ref, ...outer } = resolved;
    resolved = { ...root.$defs![$ref.slice("#/$defs/".length)], ...outer };
  }
  return resolved;
}

export function objectProperties(
  schema: JsonSchema,
  root: JsonSchema,
): Array<[string, JsonSchema]> {
  return Object.entries(resolveSchema(schema, root).properties!);
}

function plainVariantLabel(variant: JsonSchema, root: JsonSchema): string {
  const node = classifySchemaNode(variant, root);
  switch (node.kind) {
    case "boolean":
      return "On or off";
    case "list":
      return "List";
    case "string":
    case "enum":
      return "Text";
    case "number":
      return "Number";
    default:
      return variant.title ?? "Object";
  }
}

export function classifySchemaNode(
  schema: JsonSchema,
  root: JsonSchema,
): SchemaNode {
  const outer = resolveSchema(schema, root);
  let resolved = outer;
  let nullable = false;
  let variants: JsonSchema[] | null = null;

  if (outer.anyOf != null) {
    const branches = outer.anyOf.filter((branch) => branch.type !== "null");
    nullable = branches.length < outer.anyOf.length;
    if (branches.length === 1) {
      resolved = resolveSchema(branches[0], root);
    } else {
      variants = branches.map((branch) => resolveSchema(branch, root));
    }
  }

  const node: SchemaNode = {
    kind: "freeform",
    schema: resolved,
    nullable,
    description: outer.description ?? resolved.description,
    hasDefault: Object.prototype.hasOwnProperty.call(outer, "default"),
    defaultValue: outer.default,
    hint: { ...(resolved["x-mindroom"] ?? {}), ...(outer["x-mindroom"] ?? {}) },
    integer: false,
    options: [],
    variants: [],
  };

  if (variants != null) {
    node.kind = "union";
    node.variants = variants.map((variant) => ({
      label: plainVariantLabel(variant, root),
      schema: variant,
    }));
    return node;
  }
  // Pydantic emits oneOf only for discriminated unions, tagged by a const.
  if (resolved.oneOf != null) {
    const discriminator = resolved.discriminator!.propertyName;
    node.kind = "union";
    node.discriminator = discriminator;
    node.variants = resolved.oneOf.map((branch) => {
      const variant = resolveSchema(branch, root);
      const discriminatorValue = variant.properties![discriminator].const;
      return {
        label: fieldLabel(String(discriminatorValue)),
        schema: variant,
        discriminatorValue,
      };
    });
    return node;
  }
  if (Object.prototype.hasOwnProperty.call(resolved, "const")) {
    node.kind = "const";
    return node;
  }
  if (resolved.enum != null) {
    node.kind = "enum";
    node.options = resolved.enum;
    return node;
  }
  switch (resolved.type) {
    case "boolean":
      node.kind = "boolean";
      return node;
    case "integer":
    case "number":
      node.kind = "number";
      node.integer = resolved.type === "integer";
      return node;
    case "string":
      node.kind = "string";
      return node;
    case "array":
      node.kind = "list";
      node.items = resolved.items ?? {};
      return node;
  }
  if (resolved.properties != null) {
    node.kind = "object";
    return node;
  }
  if (isPlainObject(resolved.additionalProperties)) {
    node.kind = "map";
    node.values = resolved.additionalProperties;
    return node;
  }
  return node;
}

/** A starting number inside every bound the schema declares. */
function initialNumber(node: SchemaNode): number {
  const { minimum, maximum, exclusiveMinimum, exclusiveMaximum } = node.schema;
  const fits = (value: number) =>
    (minimum == null || value >= minimum) &&
    (maximum == null || value <= maximum) &&
    (exclusiveMinimum == null || value > exclusiveMinimum) &&
    (exclusiveMaximum == null || value < exclusiveMaximum);
  const lower = minimum ?? exclusiveMinimum;
  const upper = maximum ?? exclusiveMaximum;
  const candidates = [
    0,
    minimum,
    exclusiveMinimum != null ? exclusiveMinimum + 1 : undefined,
    maximum,
    exclusiveMaximum != null ? exclusiveMaximum - 1 : undefined,
    lower != null && upper != null && !node.integer
      ? (lower + upper) / 2
      : undefined,
  ];
  return (
    candidates.find((value): value is number => value != null && fits(value)) ??
    lower ??
    0
  );
}

export function initialValue(
  node: SchemaNode,
  root: JsonSchema,
  references: ReferenceOptions,
): unknown {
  // Object defaults list every field; starting empty lets fields show their own defaults.
  if (node.hasDefault && node.defaultValue != null && node.kind !== "object") {
    return structuredClone(node.defaultValue);
  }
  switch (node.kind) {
    case "const":
      return node.schema.const;
    case "boolean":
      return false;
    case "number":
      return initialNumber(node);
    case "string":
      return node.hint.reference != null
        ? (references[node.hint.reference][0] ?? "")
        : "";
    case "enum":
      return node.options[0];
    case "list":
      return [];
    case "map":
    case "freeform":
      return {};
    case "object":
      return Object.fromEntries(
        (node.schema.required ?? []).map((key) => [
          key,
          initialValue(
            classifySchemaNode(node.schema.properties![key], root),
            root,
            references,
          ),
        ]),
      );
    case "union":
      return initialValue(
        classifySchemaNode(node.variants[0].schema, root),
        root,
        references,
      );
  }
}

function valueMatchesKind(kind: SchemaNodeKind, value: unknown): boolean {
  switch (kind) {
    case "boolean":
      return typeof value === "boolean";
    case "number":
      return typeof value === "number";
    case "string":
    case "enum":
    case "const":
      return typeof value === "string";
    case "list":
      return Array.isArray(value);
    case "map":
    case "object":
    case "freeform":
      return isPlainObject(value);
    case "union":
      return false;
  }
}

/** Index of the union variant the current value belongs to, or -1. */
export function matchUnionVariant(
  node: SchemaNode,
  value: unknown,
  root: JsonSchema,
): number {
  if (node.discriminator != null) {
    const tag = isPlainObject(value) ? value[node.discriminator] : undefined;
    return node.variants.findIndex(
      (variant) => variant.discriminatorValue === tag,
    );
  }
  return node.variants.findIndex((variant) =>
    valueMatchesKind(classifySchemaNode(variant.schema, root).kind, value),
  );
}

/** Copy an object with one key set, or removed when next is undefined. */
export function setObjectKey<T extends object>(
  value: T | null | undefined,
  key: string,
  next: unknown,
): T;
export function setObjectKey(
  value: unknown,
  key: string,
  next: unknown,
): Record<string, unknown>;
export function setObjectKey(
  value: unknown,
  key: string,
  next: unknown,
): Record<string, unknown> {
  const copy = isPlainObject(value) ? { ...value } : {};
  if (next === undefined) {
    delete copy[key];
  } else {
    copy[key] = next;
  }
  return copy;
}

/** Whether an absent value and an empty collection mean the same thing. */
export function hasEmptyDefault(node: SchemaNode): boolean {
  const value = node.defaultValue;
  if (!node.hasDefault || value == null) {
    return true;
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  return isPlainObject(value) && Object.keys(value).length === 0;
}

export type ConfigPath = readonly [string, ...string[]];

/** Copy an object with the value at a key path set, or removed when next is undefined. */
export function setPathValue<T extends object>(
  value: T | null | undefined,
  path: ConfigPath,
  next: unknown,
): T;
export function setPathValue(
  value: unknown,
  path: ConfigPath,
  next: unknown,
): Record<string, unknown>;
export function setPathValue(
  value: unknown,
  [key, childKey, ...rest]: ConfigPath,
  next: unknown,
): Record<string, unknown> {
  if (childKey === undefined) {
    return setObjectKey(value, key, next);
  }
  const child = isPlainObject(value) ? value[key] : undefined;
  return setObjectKey(
    value,
    key,
    setPathValue(child, [childKey, ...rest], next),
  );
}
