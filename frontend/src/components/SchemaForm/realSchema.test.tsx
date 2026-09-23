import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Settings } from "@/components/Settings/Settings";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { useTools } from "@/hooks/useTools";
import { setObjectKey, type JsonSchema } from "@/lib/configSchema";
import { useConfigStore } from "@/store/configStore";
// Regenerated from the backend models by .github/scripts/generate_config_schema.py.
import configSchema from "@/test/fixtures/config-schema.json";

import { SchemaSection } from "./SchemaSection";

vi.mock("@/hooks/useConfigSchema", () => ({ useConfigSchema: vi.fn() }));
vi.mock("@/hooks/useTools", () => ({ useTools: vi.fn() }));

const ROOT = configSchema as JsonSchema;

// Definitions the hand-built editors render in More settings sections.
const EDITOR_DEFINITIONS = [
  "AgentConfig",
  "AgentPrivateKnowledgeConfig",
  "TeamConfig",
  "CompactionOverrideConfig",
  "RoomConfig",
  "MemoryConfig",
  "EmbedderConfig",
  "KnowledgeBaseConfig",
  "VoiceSTTConfig",
  "CallsConfig",
  "ModelConfig",
];

/** Open every collapsed block and enable every optional one, a few levels deep. */
function expandEverything() {
  for (let depth = 0; depth < 4; depth += 1) {
    screen
      .queryAllByRole("button", { expanded: false })
      .forEach((button) => fireEvent.click(button));
    screen
      .queryAllByRole("checkbox", { name: /^Configure / })
      .filter((checkbox) => checkbox.getAttribute("aria-checked") !== "true")
      .forEach((checkbox) => fireEvent.click(checkbox));
    screen
      .queryAllByRole("combobox")
      .filter(
        (select): select is HTMLSelectElement =>
          select instanceof HTMLSelectElement &&
          [...select.options].some((option) => option.value === "custom") &&
          select.value !== "custom",
      )
      .forEach((select) =>
        fireEvent.change(select, { target: { value: "custom" } }),
      );
  }
}

function StatefulSection({ definition }: { definition: string }) {
  const [value, setValue] = useState<Record<string, unknown>>({});
  return (
    <SchemaSection
      title="More settings"
      definition={definition}
      value={value}
      path={["probe"]}
      onFieldChange={(key, next) =>
        setValue((current) => setObjectKey(current, key, next))
      }
    />
  );
}

describe("forms rendered from the real configuration schema", () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: ROOT,
      error: null,
      retry: vi.fn(),
    });
    vi.mocked(useTools).mockReturnValue({
      tools: [],
      loading: false,
      error: null,
    } as never);
    useConfigStore.setState({
      config: {
        models: { default: { provider: "openai", id: "gpt-6-astra" } },
        memory: {
          embedder: {
            provider: "openai",
            config: { model: "text-embedding-3-small" },
          },
        },
        agents: {},
        defaults: { markdown: true, tools: ["scheduler"] },
        router: { model: "default" },
      },
      agents: [],
      rooms: [],
      diagnostics: [],
    });
    consoleError = vi.spyOn(console, "error");
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it("renders and expands every Settings root", () => {
    render(<Settings />);
    expandEverything();
    // Reached through the router's optional judgment block.
    expect(
      screen.getByRole("spinbutton", { name: "Threshold" }),
    ).toBeInTheDocument();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it.each(EDITOR_DEFINITIONS)("renders and expands %s", (definition) => {
    render(<StatefulSection definition={definition} />);
    expandEverything();
    if (definition === "AgentConfig") {
      // Reached through participation and its optional judgment union.
      expect(
        screen.getByRole("spinbutton", { name: "Debounce seconds" }),
      ).toBeInTheDocument();
      expect(
        screen.getAllByRole("combobox", { name: "Judgment type" }).length,
      ).toBeGreaterThan(0);
    }
    expect(consoleError).not.toHaveBeenCalled();
  });
});
