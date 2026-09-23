import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useConfigSchema } from "@/hooks/useConfigSchema";
import { useTools } from "@/hooks/useTools";
import type { JsonSchema } from "@/lib/configSchema";
import { useConfigStore } from "@/store/configStore";

import { Settings } from "./Settings";

vi.mock("@/store/configStore", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/store/configStore")>()),
  useConfigStore: vi.fn(),
}));
vi.mock("@/hooks/useConfigSchema", () => ({ useConfigSchema: vi.fn() }));
vi.mock("@/hooks/useTools", () => ({ useTools: vi.fn() }));
const { mockToast } = vi.hoisted(() => ({ mockToast: vi.fn() }));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const ROOT: JsonSchema = {
  type: "object",
  properties: {
    defaults: { $ref: "#/$defs/DefaultsConfig" },
    router: { $ref: "#/$defs/RouterConfig" },
    personal_rooms: {
      anyOf: [{ $ref: "#/$defs/PersonalRoomsConfig" }, { type: "null" }],
      default: null,
      description: "Optional native personal agent rooms",
    },
  },
  $defs: {
    DefaultsConfig: {
      type: "object",
      properties: {
        markdown: { type: "boolean", default: true },
        tools: {
          type: "array",
          items: { type: "string" },
          default: ["scheduler"],
          "x-mindroom": { reference: "tool" },
        },
      },
    },
    RouterConfig: {
      type: "object",
      properties: {
        model: {
          type: "string",
          default: "default",
          description: "Model to use for routing decisions",
          "x-mindroom": { reference: "model" },
        },
      },
    },
    PersonalRoomsConfig: {
      type: "object",
      properties: {
        agent: { type: "string", "x-mindroom": { reference: "agent" } },
      },
      required: ["agent"],
    },
  },
};

describe("Settings", () => {
  const updateConfigRoot = vi.fn();
  const saveConfig = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    saveConfig.mockResolvedValue({ status: "saved" });
    vi.mocked(useConfigStore).mockReturnValue({
      config: {
        models: { default: {}, fast: {} },
        agents: { helper: { rooms: [] } },
        defaults: { markdown: true },
        router: { model: "default" },
      },
      diagnostics: [],
      isDirty: true,
      isLoading: false,
      saveConfig,
      updateConfigRoot,
    } as never);
    vi.mocked(useConfigSchema).mockReturnValue({ schema: ROOT, error: null });
    vi.mocked(useTools).mockReturnValue({ tools: [] } as never);
  });

  it("edits a settings root through updateConfigRoot", () => {
    render(<Settings />);
    fireEvent.click(screen.getByRole("button", { name: "Router" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), {
      target: { value: "fast" },
    });
    expect(updateConfigRoot).toHaveBeenCalledWith("router", { model: "fast" });
  });

  it("enables optional roots with their required fields", () => {
    render(<Settings />);
    fireEvent.click(screen.getByRole("button", { name: "Personal rooms" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Configure personal rooms" }),
    );
    expect(updateConfigRoot).toHaveBeenCalledWith("personal_rooms", {
      agent: "helper",
    });
  });

  it("saves the draft", async () => {
    render(<Settings />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saveConfig).toHaveBeenCalled());
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Settings Saved" }),
    );
  });

  it("reports an unavailable schema", () => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: null,
      error: "API call failed: 500",
    });
    render(<Settings />);
    expect(
      screen.getByText("Settings are unavailable: API call failed: 500"),
    ).toBeInTheDocument();
  });
});
