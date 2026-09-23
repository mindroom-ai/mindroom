import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { JsonSchema } from "@/lib/configSchema";
import type { ConfigDiagnostic } from "@/lib/configValidation";
import { useConfigStore } from "@/store/configStore";

import { useConfigSchema } from "@/hooks/useConfigSchema";

import { SchemaFields } from "./SchemaField";
import { SchemaSection } from "./SchemaSection";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));
vi.mock("@/hooks/useConfigSchema", () => ({ useConfigSchema: vi.fn() }));

// Shapes mirror Pydantic's model_json_schema() output for the real config models.
const ROOT: JsonSchema = {
  type: "object",
  properties: {},
  $defs: {
    LLMJudgmentConfig: {
      type: "object",
      properties: {
        provider: { const: "llm", type: "string" },
        model: {
          type: "string",
          description: "Configured model alias",
          "x-mindroom": { reference: "model" },
        },
        timeout_seconds: { type: "number", default: 5 },
      },
      required: ["provider", "model"],
    },
    TypeSafeJudgmentConfig: {
      type: "object",
      properties: {
        provider: { const: "typesafe", type: "string" },
        threshold: { type: "number", default: 0.8 },
        timeout_seconds: { type: "number", default: 1.5 },
      },
      required: ["provider"],
    },
    StreamingConfig: {
      type: "object",
      properties: {
        update_interval: { type: "number", default: 5 },
      },
    },
    JudgmentConfig: {
      oneOf: [
        { $ref: "#/$defs/LLMJudgmentConfig" },
        { $ref: "#/$defs/TypeSafeJudgmentConfig" },
      ],
      discriminator: { propertyName: "provider" },
    },
    ParticipationConfig: {
      type: "object",
      properties: {
        debounce_seconds: {
          type: "number",
          default: 3,
          minimum: 0,
          maximum: 30,
          description: "Quiet window",
        },
        instructions: {
          type: "string",
          default: "",
          "x-mindroom": { multiline: true },
        },
        judgment: {
          anyOf: [{ $ref: "#/$defs/JudgmentConfig" }, { type: "null" }],
          default: null,
          description: "Judgment backend",
        },
      },
    },
    RuleConfig: {
      type: "object",
      properties: { match: { type: "string", description: "Glob" } },
      required: ["match"],
    },
    Fixture: {
      type: "object",
      properties: {
        enable_streaming: {
          type: "boolean",
          default: true,
          description: "Stream replies",
        },
        markdown: {
          anyOf: [{ type: "boolean" }, { type: "null" }],
          default: null,
        },
        large_message_strategy: {
          enum: ["sidecar", "split"],
          type: "string",
          default: "sidecar",
        },
        model: {
          type: "string",
          default: "default",
          "x-mindroom": { reference: "model" },
        },
        api_key: {
          anyOf: [{ type: "string" }, { type: "null" }],
          default: null,
          "x-mindroom": { secret: true },
        },
        temperature: {
          anyOf: [{ type: "number" }, { type: "null" }],
          default: 0.2,
        },
        onboarding_rooms: {
          type: "array",
          items: { type: "string" },
          "x-mindroom": { reference: "room" },
        },
        tools: {
          type: "array",
          items: { type: "string" },
          default: ["scheduler"],
          "x-mindroom": { reference: "tool" },
        },
        room_models: {
          type: "object",
          additionalProperties: { type: "string" },
          "x-mindroom": { key_reference: "room", reference: "model" },
        },
        participation: {
          anyOf: [{ $ref: "#/$defs/ParticipationConfig" }, { type: "null" }],
          default: null,
          description: "Opt-in participation",
        },
        accept_invites: {
          anyOf: [
            { type: "boolean" },
            { type: "array", items: { type: "string" } },
          ],
          default: true,
        },
        rules: {
          type: "array",
          items: { $ref: "#/$defs/RuleConfig" },
        },
        settings: { type: "object", additionalProperties: true },
        extra_kwargs: {
          type: "object",
          additionalProperties: true,
          "x-mindroom": { secret: true },
        },
        welcome: {
          type: "string",
          default: "Welcome!",
          "x-mindroom": { multiline: true },
        },
        streaming: {
          $ref: "#/$defs/StreamingConfig",
          description: "Streaming timing",
        },
      },
    },
  },
};

const FIXTURE: JsonSchema = { $ref: "#/$defs/Fixture" };

function Harness({
  initial,
  onValue,
}: {
  initial: Record<string, unknown>;
  onValue: (value: Record<string, unknown>) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SchemaFields
      schema={FIXTURE}
      root={ROOT}
      value={value}
      path={["fixture"]}
      onFieldChange={(key, next) => {
        const updated = { ...value };
        if (next === undefined) {
          delete updated[key];
        } else {
          updated[key] = next;
        }
        setValue(updated);
        onValue(updated);
      }}
    />
  );
}

function renderFixture(
  initial: Record<string, unknown> = {},
  diagnostics: ConfigDiagnostic[] = [],
) {
  vi.mocked(useConfigStore).mockReturnValue({
    config: { models: { default: {}, sonnet: {} } },
    agents: [{ id: "helper" }],
    rooms: [{ id: "dev" }, { id: "lobby" }],
    diagnostics,
  } as never);
  const onValue = vi.fn();
  render(<Harness initial={initial} onValue={onValue} />);
  const lastValue = () => onValue.mock.lastCall?.[0];
  return { onValue, lastValue };
}

describe("SchemaFields", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows boolean defaults and writes explicit values", () => {
    const { lastValue } = renderFixture();
    const checkbox = screen.getByRole("checkbox", { name: "Enable streaming" });
    expect(checkbox).toBeChecked();

    fireEvent.click(checkbox);
    expect(lastValue()).toEqual({ enable_streaming: false });

    fireEvent.click(
      screen.getByRole("button", { name: "Reset Enable streaming" }),
    );
    expect(lastValue()).toEqual({});
  });

  it("uses a tri-state select for nullable booleans", () => {
    const { lastValue } = renderFixture();
    const select = screen.getByRole("combobox", { name: "Markdown" });

    fireEvent.change(select, { target: { value: "false" } });
    expect(lastValue()).toEqual({ markdown: false });

    fireEvent.change(select, { target: { value: "__default__" } });
    expect(lastValue()).toEqual({});
  });

  it("selects enum values and model references", () => {
    const { lastValue } = renderFixture();
    fireEvent.change(
      screen.getByRole("combobox", { name: "Large message strategy" }),
      { target: { value: "split" } },
    );
    expect(lastValue()).toEqual({ large_message_strategy: "split" });

    const model = screen.getByRole("combobox", { name: "Model" });
    expect(
      within(model)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Default (default)", "default", "sonnet"]);
    fireEvent.change(model, { target: { value: "sonnet" } });
    expect(lastValue()).toMatchObject({ model: "sonnet" });
  });

  it("masks secrets and deletes cleared optional strings", () => {
    const { lastValue } = renderFixture({ api_key: "sk-test" });
    const input = screen.getByLabelText("API key");
    expect(input).toHaveAttribute("type", "password");

    fireEvent.change(input, { target: { value: "" } });
    expect(lastValue()).toEqual({});
  });

  it("distinguishes an explicit null from the default", () => {
    const { lastValue } = renderFixture();
    fireEvent.click(screen.getByRole("checkbox", { name: "Set to none" }));
    expect(lastValue()).toEqual({ temperature: null });
    expect(screen.getByLabelText("Temperature")).toBeDisabled();
  });

  it("adds and removes list entries", () => {
    const { lastValue } = renderFixture({ onboarding_rooms: ["lobby"] });
    // Inputs with suggestions are comboboxes.
    const input = screen.getByRole("combobox", {
      name: "New onboarding rooms entry",
    });
    fireEvent.change(input, { target: { value: "dev" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Add onboarding rooms entry" }),
    );
    expect(lastValue()).toEqual({ onboarding_rooms: ["lobby", "dev"] });

    fireEvent.click(screen.getByRole("button", { name: "Remove lobby" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove dev" }));
    expect(lastValue()).toEqual({});
  });

  it("keeps ordered lists with repeated values", () => {
    const { lastValue } = renderFixture({
      onboarding_rooms: ["--header", "a", "--header", "b"],
    });
    const input = screen.getByRole("combobox", {
      name: "New onboarding rooms entry",
    });
    fireEvent.change(input, { target: { value: "--header" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Add onboarding rooms entry" }),
    );
    expect(lastValue()).toEqual({
      onboarding_rooms: ["--header", "a", "--header", "b", "--header"],
    });

    fireEvent.click(
      screen.getAllByRole("button", { name: "Remove --header" })[1],
    );
    expect(lastValue()).toEqual({
      onboarding_rooms: ["--header", "a", "b", "--header"],
    });

    fireEvent.click(screen.getByRole("button", { name: "Move b up" }));
    expect(lastValue()).toEqual({
      onboarding_rooms: ["--header", "b", "a", "--header"],
    });
  });

  it("shows errors reported for a list item itself", () => {
    renderFixture({ rules: [{ match: "send_*" }] }, [
      {
        kind: "validation",
        issue: {
          loc: ["fixture", "rules", 0],
          msg: "must set exactly one of action or script",
          type: "value_error",
        },
      },
    ]);
    expect(
      screen.getByText("must set exactly one of action or script"),
    ).toBeInTheDocument();
  });

  it("clears a YAML parse error when the value is reset", () => {
    renderFixture({ settings: { retries: 3 } });
    const editor = screen.getByRole("textbox", { name: "Settings YAML" });
    fireEvent.change(editor, { target: { value: "retries: [" } });
    fireEvent.blur(editor);
    expect(
      screen.getByText(/unexpected end of the stream/i),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reset Settings" }));
    expect(
      screen.queryByText(/unexpected end of the stream/i),
    ).not.toBeInTheDocument();
  });

  it("shows list defaults and keeps an explicitly emptied list", () => {
    const { lastValue } = renderFixture();
    fireEvent.click(screen.getByRole("button", { name: "Remove scheduler" }));
    expect(lastValue()).toEqual({ tools: [] });
  });

  it("edits string maps with referenced values", () => {
    const { lastValue } = renderFixture();
    fireEvent.change(
      screen.getByRole("combobox", { name: "New room models key" }),
      {
        target: { value: "lobby" },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add room models key" }),
    );
    expect(lastValue()).toEqual({ room_models: { lobby: "default" } });

    fireEvent.change(screen.getByRole("combobox", { name: "lobby" }), {
      target: { value: "sonnet" },
    });
    expect(lastValue()).toEqual({ room_models: { lobby: "sonnet" } });
  });

  it("enables optional objects with their defaults and disables them by removing the key", () => {
    const { lastValue } = renderFixture();
    const toggle = screen.getByRole("checkbox", {
      name: "Configure participation",
    });
    fireEvent.click(toggle);
    expect(lastValue()).toEqual({ participation: {} });

    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Debounce seconds" }),
      {
        target: { value: "5" },
      },
    );
    expect(lastValue()).toEqual({ participation: { debounce_seconds: 5 } });

    fireEvent.click(toggle);
    expect(lastValue()).toEqual({});
  });

  it("switches discriminated union variants", () => {
    const { lastValue } = renderFixture({ participation: {} });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Configure judgment" }),
    );
    expect(lastValue()).toEqual({
      participation: { judgment: { provider: "llm", model: "default" } },
    });

    fireEvent.change(screen.getByRole("combobox", { name: "Judgment type" }), {
      target: { value: "1" },
    });
    expect(lastValue()).toEqual({
      participation: { judgment: { provider: "typesafe" } },
    });
    expect(
      screen.getByRole("spinbutton", { name: "Threshold" }),
    ).toBeInTheDocument();
  });

  it("switches plain unions between a boolean and a list", () => {
    const { lastValue } = renderFixture();
    const mode = screen.getByRole("combobox", { name: "Accept invites mode" });
    expect(mode).toHaveValue("true");

    fireEvent.change(mode, { target: { value: "variant-1" } });
    expect(lastValue()).toEqual({ accept_invites: [] });
    fireEvent.change(
      screen.getByRole("textbox", { name: "New accept invites entry" }),
      { target: { value: "@alice:example.org" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add accept invites entry" }),
    );
    expect(lastValue()).toEqual({ accept_invites: ["@alice:example.org"] });
  });

  it("adds, reorders, and removes object list items", () => {
    const { lastValue } = renderFixture({
      rules: [{ match: "send_*" }, { match: "upload_*" }],
    });
    fireEvent.click(screen.getByRole("button", { name: "Move Rules 2 up" }));
    expect(lastValue()).toEqual({
      rules: [{ match: "upload_*" }, { match: "send_*" }],
    });

    fireEvent.click(screen.getByRole("button", { name: "Add rules" }));
    expect(lastValue()).toEqual({
      rules: [{ match: "upload_*" }, { match: "send_*" }, { match: "" }],
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove Rules 1" }));
    expect(lastValue()).toEqual({
      rules: [{ match: "send_*" }, { match: "" }],
    });
  });

  it("parses freeform YAML on blur and reports invalid YAML", () => {
    const { lastValue } = renderFixture();
    const editor = screen.getByRole("textbox", { name: "Settings YAML" });

    fireEvent.change(editor, { target: { value: "retries: 3\n" } });
    fireEvent.blur(editor);
    expect(lastValue()).toEqual({ settings: { retries: 3 } });

    fireEvent.change(editor, { target: { value: "retries: [" } });
    fireEvent.blur(editor);
    expect(
      screen.getByText(/unexpected end of the stream/i),
    ).toBeInTheDocument();
  });

  it("stores an explicit empty string when the default is not empty", () => {
    const { lastValue } = renderFixture({ welcome: "Hi" });
    fireEvent.change(screen.getByLabelText("Welcome"), {
      target: { value: "" },
    });
    expect(lastValue()).toEqual({ welcome: "" });

    fireEvent.click(screen.getByRole("button", { name: "Reset Welcome" }));
    expect(lastValue()).toEqual({});
  });

  it("renames map keys in place", () => {
    const { lastValue } = renderFixture({
      room_models: { lobby: "default", dev: "sonnet" },
    });
    const rename = screen.getByRole("textbox", { name: "Rename lobby" });
    fireEvent.change(rename, { target: { value: "dev" } });
    fireEvent.blur(rename);
    // Taken keys are rejected.
    expect(lastValue()).toBeUndefined();

    fireEvent.change(rename, { target: { value: "hall" } });
    fireEvent.blur(rename);
    expect(lastValue()).toEqual({
      room_models: { hall: "default", dev: "sonnet" },
    });
  });

  it("keeps fields both variants define when switching union variants", () => {
    const { lastValue } = renderFixture({
      participation: {
        judgment: { provider: "llm", model: "sonnet", timeout_seconds: 9 },
      },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Judgment type" }), {
      target: { value: "1" },
    });
    expect(lastValue()).toEqual({
      participation: { judgment: { provider: "typesafe", timeout_seconds: 9 } },
    });
  });

  it("hides secret freeform YAML until revealed", () => {
    renderFixture({ extra_kwargs: { api_key: "sk-secret" } });
    expect(screen.queryByText(/sk-secret/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show extra kwargs" }));
    expect(
      screen.getByRole("textbox", { name: "Extra kwargs YAML" }),
    ).toHaveValue("api_key: sk-secret\n");
  });

  it("shows an empty secret freeform mapping without hiding it", () => {
    renderFixture({ extra_kwargs: {} });
    expect(
      screen.getByRole("textbox", { name: "Extra kwargs YAML" }),
    ).toHaveValue("{}\n");
  });

  it("clears a YAML parse error when the draft is reverted", () => {
    renderFixture({ settings: { retries: 3 } });
    const editor = screen.getByRole("textbox", { name: "Settings YAML" });
    fireEvent.change(editor, { target: { value: "retries: [" } });
    fireEvent.blur(editor);
    expect(
      screen.getByText(/unexpected end of the stream/i),
    ).toBeInTheDocument();

    fireEvent.change(editor, { target: { value: "retries: 3\n" } });
    fireEvent.blur(editor);
    expect(
      screen.queryByText(/unexpected end of the stream/i),
    ).not.toBeInTheDocument();
  });

  it("does not mark freeform YAML dirty when it only loses focus", () => {
    const { onValue } = renderFixture({ settings: { retries: 3 } });
    const editor = screen.getByRole("textbox", { name: "Settings YAML" });
    fireEvent.focus(editor);
    fireEvent.blur(editor);
    expect(onValue).not.toHaveBeenCalled();
  });

  it("shows discriminated-union errors reported under the tag value", () => {
    renderFixture(
      { participation: { judgment: { provider: "llm", model: "gone" } } },
      [
        {
          kind: "validation",
          issue: {
            loc: ["fixture", "participation", "judgment", "llm", "model"],
            msg: "Unknown model alias",
            type: "value_error",
          },
        },
      ],
    );
    expect(screen.getByText("Unknown model alias")).toBeInTheDocument();
  });

  it("opens collapsed blocks that contain a validation error", () => {
    renderFixture({}, [
      {
        kind: "validation",
        issue: {
          loc: ["fixture", "streaming", "update_interval"],
          msg: "Input should be greater than 0",
          type: "greater_than",
        },
      },
    ]);
    expect(
      screen.getByRole("button", { name: /Streaming/, expanded: true }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Input should be greater than 0"),
    ).toBeInTheDocument();
  });

  it("shows validation errors at the matching path", () => {
    renderFixture({}, [
      {
        kind: "validation",
        issue: {
          loc: ["fixture", "large_message_strategy"],
          msg: "Input should be 'sidecar' or 'split'",
          type: "literal_error",
        },
      },
    ]);
    expect(
      screen.getByText("Input should be 'sidecar' or 'split'"),
    ).toBeInTheDocument();
  });
});

describe("SchemaSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useConfigStore).mockReturnValue({
      config: { models: {} },
      agents: [],
      rooms: [],
      diagnostics: [],
    } as never);
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: ROOT,
      error: null,
      retry: vi.fn(),
    });
  });

  const renderSection = (exclude: readonly string[]) =>
    render(
      <SchemaSection
        title="More settings"
        definition="ParticipationConfig"
        value={{}}
        path={["participation"]}
        exclude={exclude}
        onFieldChange={vi.fn()}
      />,
    );

  it("summarizes the remaining fields while collapsed", () => {
    renderSection(["judgment"]);
    const toggle = screen.getByRole("button", { name: /More settings/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("Debounce seconds, Instructions");

    fireEvent.click(toggle);
    expect(
      screen.getByRole("spinbutton", { name: "Debounce seconds" }),
    ).toBeInTheDocument();
  });

  it("opens and flags the section when a rendered field has an error", () => {
    vi.mocked(useConfigStore).mockReturnValue({
      config: { models: {} },
      agents: [],
      rooms: [],
      diagnostics: [
        {
          kind: "validation",
          issue: {
            loc: ["participation", "debounce_seconds"],
            msg: "Input should be less than or equal to 30",
            type: "less_than_equal",
          },
        },
      ],
    } as never);
    renderSection(["judgment"]);

    expect(
      screen.getByRole("button", { name: /More settings/ }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(
      screen.getByText("Input should be less than or equal to 30"),
    ).toBeInTheDocument();
  });

  it("renders nothing when the editor owns every field", () => {
    const { container } = renderSection([
      "debounce_seconds",
      "instructions",
      "judgment",
    ]);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a definition the backend does not define", () => {
    const { container } = render(
      <SchemaSection
        title="More settings"
        definition="FutureConfig"
        value={{}}
        path={[]}
        onFieldChange={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("explains when the schema cannot be loaded", () => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: null,
      error: "API call failed: 500",
      retry: vi.fn(),
    });
    renderSection([]);
    expect(
      screen.getByText("More settings are unavailable: API call failed: 500"),
    ).toBeInTheDocument();
  });
});
