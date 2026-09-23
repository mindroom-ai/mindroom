import { describe, expect, it } from "vitest";

import {
  classifySchemaNode,
  fieldLabel,
  initialValue,
  matchUnionVariant,
  objectProperties,
  setObjectKey,
  type JsonSchema,
  type ReferenceOptions,
} from "./configSchema";

// Shapes below mirror what Pydantic's model_json_schema() emits.
const ROOT: JsonSchema = {
  type: "object",
  properties: {},
  $defs: {
    LLMJudgmentConfig: {
      type: "object",
      additionalProperties: false,
      properties: {
        provider: { const: "llm", type: "string", description: "LLM" },
        model: {
          type: "string",
          minLength: 1,
          description: "Model alias",
          "x-mindroom": { reference: "model" },
        },
        timeout_seconds: { type: "number", default: 5, maximum: 30 },
      },
      required: ["provider", "model"],
    },
    TypeSafeJudgmentConfig: {
      type: "object",
      properties: {
        provider: { const: "typesafe", type: "string" },
        threshold: { type: "number", default: 0.8, minimum: 0, maximum: 1 },
      },
      required: ["provider"],
    },
    JudgmentConfig: {
      oneOf: [
        { $ref: "#/$defs/LLMJudgmentConfig" },
        { $ref: "#/$defs/TypeSafeJudgmentConfig" },
      ],
      discriminator: {
        propertyName: "provider",
        mapping: {
          llm: "#/$defs/LLMJudgmentConfig",
          typesafe: "#/$defs/TypeSafeJudgmentConfig",
        },
      },
    },
    ParticipationConfig: {
      type: "object",
      description: "Participation settings",
      properties: {
        debounce_seconds: {
          type: "number",
          default: 3,
          minimum: 0,
          maximum: 30,
          description: "Quiet window",
        },
        judgment: {
          anyOf: [{ $ref: "#/$defs/JudgmentConfig" }, { type: "null" }],
          default: null,
          description: "Judgment backend",
        },
      },
    },
  },
};

const REFERENCES: ReferenceOptions = {
  model: ["sonnet", "haiku"],
  agent: ["helper"],
  room: ["lobby"],
  tool: ["shell"],
};

describe("classifySchemaNode", () => {
  it("unwraps nullable $ref and keeps outer metadata", () => {
    const node = classifySchemaNode(
      {
        anyOf: [{ $ref: "#/$defs/ParticipationConfig" }, { type: "null" }],
        default: null,
        description: "Opt-in participation",
      },
      ROOT,
    );
    expect(node.kind).toBe("object");
    expect(node.nullable).toBe(true);
    expect(node.hasDefault).toBe(true);
    expect(node.defaultValue).toBeNull();
    expect(node.description).toBe("Opt-in participation");
    expect(objectProperties(node.schema, ROOT).map(([key]) => key)).toEqual([
      "debounce_seconds",
      "judgment",
    ]);
  });

  it("classifies a discriminated union behind a nullable alias", () => {
    const node = classifySchemaNode(
      ROOT.$defs!.ParticipationConfig.properties!.judgment,
      ROOT,
    );
    expect(node.kind).toBe("union");
    expect(node.nullable).toBe(true);
    expect(node.discriminator).toBe("provider");
    expect(node.variants.map((variant) => variant.label)).toEqual([
      "LLM",
      "Typesafe",
    ]);
    expect(node.variants.map((variant) => variant.discriminatorValue)).toEqual([
      "llm",
      "typesafe",
    ]);
  });

  it("classifies a plain boolean-or-list union", () => {
    const node = classifySchemaNode(
      {
        anyOf: [
          { type: "boolean" },
          { type: "array", items: { type: "string" } },
        ],
        default: true,
      },
      ROOT,
    );
    expect(node.kind).toBe("union");
    expect(node.nullable).toBe(false);
    expect(node.discriminator).toBeUndefined();
    expect(node.variants).toHaveLength(2);
  });

  it("classifies scalar kinds", () => {
    expect(classifySchemaNode({ type: "boolean" }, ROOT).kind).toBe("boolean");
    const integer = classifySchemaNode({ type: "integer", minimum: 1 }, ROOT);
    expect(integer.kind).toBe("number");
    expect(integer.integer).toBe(true);
    expect(classifySchemaNode({ type: "string" }, ROOT).kind).toBe("string");
    const literal = classifySchemaNode(
      { enum: ["sidecar", "split"], type: "string" },
      ROOT,
    );
    expect(literal.kind).toBe("enum");
    expect(literal.options).toEqual(["sidecar", "split"]);
    expect(
      classifySchemaNode({ const: "llm", type: "string" }, ROOT).kind,
    ).toBe("const");
  });

  it("classifies nullable enums and lists", () => {
    const nullableEnum = classifySchemaNode(
      {
        anyOf: [{ enum: ["a", "b"], type: "string" }, { type: "null" }],
        default: null,
      },
      ROOT,
    );
    expect(nullableEnum.kind).toBe("enum");
    expect(nullableEnum.nullable).toBe(true);

    const nullableList = classifySchemaNode(
      {
        anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }],
        default: null,
        "x-mindroom": { reference: "tool" },
      },
      ROOT,
    );
    expect(nullableList.kind).toBe("list");
    expect(nullableList.items).toEqual({ type: "string" });
    expect(nullableList.hint).toEqual({ reference: "tool" });
  });

  it("classifies maps and freeform objects", () => {
    const stringMap = classifySchemaNode(
      { type: "object", additionalProperties: { type: "string" } },
      ROOT,
    );
    expect(stringMap.kind).toBe("map");
    expect(stringMap.values).toEqual({ type: "string" });

    const objectMap = classifySchemaNode(
      {
        type: "object",
        additionalProperties: { $ref: "#/$defs/ParticipationConfig" },
      },
      ROOT,
    );
    expect(objectMap.kind).toBe("map");
    expect(classifySchemaNode(objectMap.values!, ROOT).kind).toBe("object");

    expect(
      classifySchemaNode({ type: "object", additionalProperties: true }, ROOT)
        .kind,
    ).toBe("freeform");
    expect(classifySchemaNode({}, ROOT).kind).toBe("freeform");
  });
});

describe("initialValue", () => {
  it("prefers schema defaults", () => {
    const node = classifySchemaNode({ type: "number", default: 3 }, ROOT);
    expect(initialValue(node, ROOT, REFERENCES)).toBe(3);
  });

  it("fills required fields, discriminators, and model references", () => {
    const union = classifySchemaNode({ $ref: "#/$defs/JudgmentConfig" }, ROOT);
    expect(initialValue(union, ROOT, REFERENCES)).toEqual({
      provider: "llm",
      model: "sonnet",
    });
  });

  it("starts objects with only their required fields", () => {
    const node = classifySchemaNode(
      { $ref: "#/$defs/ParticipationConfig" },
      ROOT,
    );
    expect(initialValue(node, ROOT, REFERENCES)).toEqual({});
  });

  it("starts numbers inside every declared bound", () => {
    const start = (schema: JsonSchema) =>
      initialValue(classifySchemaNode(schema, ROOT), ROOT, REFERENCES);
    expect(start({ type: "number" })).toBe(0);
    expect(start({ type: "integer", minimum: 1 })).toBe(1);
    expect(start({ type: "integer", exclusiveMinimum: 0 })).toBe(1);
    expect(
      start({ type: "number", exclusiveMinimum: 0, exclusiveMaximum: 1 }),
    ).toBe(0.5);
    expect(start({ type: "number", exclusiveMinimum: 0, maximum: 1 })).toBe(1);
    expect(start({ type: "number", maximum: -5 })).toBe(-5);
  });

  it("starts collections empty", () => {
    expect(
      initialValue(
        classifySchemaNode({ type: "array", items: { type: "string" } }, ROOT),
        ROOT,
        REFERENCES,
      ),
    ).toEqual([]);
    expect(
      initialValue(
        classifySchemaNode(
          { type: "object", additionalProperties: { type: "string" } },
          ROOT,
        ),
        ROOT,
        REFERENCES,
      ),
    ).toEqual({});
  });
});

describe("matchUnionVariant", () => {
  it("matches discriminated variants by tag", () => {
    const node = classifySchemaNode({ $ref: "#/$defs/JudgmentConfig" }, ROOT);
    expect(matchUnionVariant(node, { provider: "typesafe" }, ROOT)).toBe(1);
    expect(matchUnionVariant(node, { provider: "unknown" }, ROOT)).toBe(-1);
  });

  it("matches plain variants by value shape", () => {
    const node = classifySchemaNode(
      {
        anyOf: [
          { type: "boolean" },
          { type: "array", items: { type: "string" } },
        ],
      },
      ROOT,
    );
    expect(matchUnionVariant(node, false, ROOT)).toBe(0);
    expect(matchUnionVariant(node, ["@a:example.org"], ROOT)).toBe(1);
    expect(matchUnionVariant(node, "text", ROOT)).toBe(-1);
  });
});

describe("fieldLabel", () => {
  it("sentence-cases keys and keeps acronyms", () => {
    expect(fieldLabel("debounce_seconds")).toBe("Debounce seconds");
    expect(fieldLabel("livekit_service_url")).toBe("LiveKit service URL");
    expect(fieldLabel("log_llm_requests")).toBe("Log LLM requests");
    expect(fieldLabel("database_url_env")).toBe("Database URL env");
    expect(fieldLabel("mcp_servers")).toBe("MCP servers");
  });
});

describe("setObjectKey", () => {
  it("sets and deletes keys without mutating the input", () => {
    const original = { a: 1, b: 2 };
    expect(setObjectKey(original, "c", 3)).toEqual({ a: 1, b: 2, c: 3 });
    expect(setObjectKey(original, "a", undefined)).toEqual({ b: 2 });
    expect(original).toEqual({ a: 1, b: 2 });
    expect(setObjectKey(undefined, "a", null)).toEqual({ a: null });
  });
});
