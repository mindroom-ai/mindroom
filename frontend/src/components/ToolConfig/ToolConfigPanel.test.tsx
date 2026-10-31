import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ToolConfigPanel } from "./ToolConfigPanel";
import type { ToolFieldSchema } from "@/hooks/useTools";
import type { ToolEntry, ToolOverrides } from "@/lib/toolEntry";
import { useConfigStore } from "@/store/configStore";
import type { Agent, Config } from "@/types/config";

const shellFields: ToolFieldSchema[] = [
  {
    name: "extra_env_passthrough",
    label: "Env Passthrough",
    type: "string[]",
    description: "Extra env vars exposed to shell execution.",
  },
  {
    name: "shell_path_prepend",
    label: "PATH Prepend",
    type: "string[]",
    description: "Path entries prepended to PATH.",
  },
  { name: "timeout", label: "Timeout", type: "number", default: 120 },
  {
    name: "enable_run_shell_command",
    label: "Run Shell Command",
    type: "boolean",
  },
];

/** Back the panel with the real store, holding one agent and defaults.tools. */
function seedStore({
  agentTools = ["shell"],
  defaultTools = [],
}: { agentTools?: ToolEntry[]; defaultTools?: ToolEntry[] } = {}) {
  const config = {
    agents: { openclaw: { display_name: "OpenClaw", tools: agentTools } },
    defaults: { tools: defaultTools },
  } as unknown as Config;
  useConfigStore.setState({
    config,
    loadedConfig: config,
    agents: [
      {
        id: "openclaw",
        display_name: "OpenClaw",
        tools: agentTools,
        rooms: [],
      } as unknown as Agent,
    ],
  });
}

function agentOverrides(toolName = "shell"): ToolOverrides | null {
  return useConfigStore.getState().getAgentToolOverrides("openclaw", toolName);
}

function renderShellPanel() {
  render(
    <ToolConfigPanel
      target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
      toolName="shell"
      toolDisplayName="Shell Commands"
      overrideFields={shellFields}
    />,
  );
}

function envInputs(): HTMLInputElement[] {
  return screen.queryAllByPlaceholderText("Env Passthrough");
}

describe("ToolConfigPanel", () => {
  beforeEach(() => {
    useConfigStore.setState(useConfigStore.getInitialState(), true);
  });

  it("hides lazy loading for tools whose entries may not set it", () => {
    seedStore();
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: false }}
        toolName="shell"
        toolDisplayName="Shell"
        overrideFields={[{ name: "timeout", label: "Timeout", type: "number" }]}
      />,
    );

    expect(
      screen.queryByLabelText("Defer until searched"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/Toggle fields/)).toBeInTheDocument();
  });

  it("offers only lazy loading when a tool has neither override fields nor config fields", () => {
    seedStore({ agentTools: ["browser"] });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="browser"
        toolDisplayName="Browser"
        overrideFields={null}
        configFields={null}
      />,
    );

    expect(screen.getByLabelText("Defer until searched")).toBeInTheDocument();
    expect(
      screen.getByText("This tool has no other settings."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Toggle fields/)).not.toBeInTheDocument();
  });

  it("shows stored string[] overrides as ticked fields", () => {
    seedStore({
      agentTools: [{ shell: { extra_env_passthrough: ["GITEA_TOKEN"] } }],
    });
    renderShellPanel();

    expect(
      screen.getByText("Shell Commands — Per-Agent Settings"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
    ).toBeChecked();
    expect(screen.getByDisplayValue("GITEA_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("Customized")).toBeInTheDocument();
  });

  it("writes only the edited field when toggling and editing string[] fields", () => {
    seedStore({
      agentTools: [{ shell: { extra_env_passthrough: ["GITEA_TOKEN"] } }],
    });
    renderShellPanel();

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override PATH Prepend" }),
    );
    fireEvent.click(screen.getAllByText("Add value")[1]);
    fireEvent.change(screen.getByPlaceholderText("PATH Prepend"), {
      target: { value: "/run/wrappers/bin" },
    });
    expect(agentOverrides()).toEqual({
      extra_env_passthrough: ["GITEA_TOKEN"],
      shell_path_prepend: ["/run/wrappers/bin"],
    });

    fireEvent.click(screen.getByLabelText("Remove Env Passthrough value 1"));
    expect(agentOverrides()).toEqual({
      shell_path_prepend: ["/run/wrappers/bin"],
    });
    // Removing the last value keeps the override open for a new one.
    expect(
      screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
    ).toBeChecked();
    expect(
      screen.getByText("No values. Add one to create an override."),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
    );
    expect(
      screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
    ).not.toBeChecked();

    // Ticking a field starts from the tool default.
    fireEvent.click(screen.getByRole("checkbox", { name: "Override Timeout" }));
    expect(agentOverrides()).toEqual({
      shell_path_prepend: ["/run/wrappers/bin"],
      timeout: 120,
    });
  });

  it.each([
    ["the documented string form", "DAWARICH_*", ["DAWARICH_*"]],
    ["a comma-separated string", "DAWARICH_*, HOME", ["DAWARICH_*", "HOME"]],
    ["a newline-separated string", "DAWARICH_*\nHOME", ["DAWARICH_*", "HOME"]],
    ["a list", ["DAWARICH_*"], ["DAWARICH_*"]],
  ])(
    "keeps a value added to %s while other fields change",
    (_form, stored, storedValues) => {
      seedStore({ agentTools: [{ shell: { extra_env_passthrough: stored } }] });
      renderShellPanel();

      expect(envInputs().map((input) => input.value)).toEqual(storedValues);
      fireEvent.click(screen.getAllByText("Add value")[0]);
      expect(envInputs().map((input) => input.value)).toEqual([
        ...storedValues,
        "",
      ]);

      fireEvent.click(
        screen.getByRole("checkbox", { name: "Override Timeout" }),
      );
      fireEvent.change(screen.getByPlaceholderText("Timeout"), {
        target: { value: "30" },
      });
      expect(envInputs().map((input) => input.value)).toEqual([
        ...storedValues,
        "",
      ]);

      fireEvent.change(envInputs()[storedValues.length], {
        target: { value: "GITEA_*" },
      });
      expect(agentOverrides()).toEqual({
        extra_env_passthrough: [...storedValues, "GITEA_*"],
        timeout: 30,
      });
    },
  );

  it("keeps an entry and its override while its text is cleared", () => {
    seedStore({
      agentTools: [{ shell: { extra_env_passthrough: ["GITEA_TOKEN"] } }],
    });
    renderShellPanel();

    fireEvent.change(screen.getByDisplayValue("GITEA_TOKEN"), {
      target: { value: "" },
    });
    expect(agentOverrides()).toBeNull();
    expect(envInputs().map((input) => input.value)).toEqual([""]);
    expect(
      screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
    ).toBeChecked();

    fireEvent.change(envInputs()[0], { target: { value: "HOME" } });
    expect(agentOverrides()).toEqual({ extra_env_passthrough: ["HOME"] });
  });

  it("keeps a text override ticked while its text is cleared", () => {
    seedStore({ agentTools: [{ shell: { timeout: 30 } }] });
    renderShellPanel();

    fireEvent.change(screen.getByPlaceholderText("Timeout"), {
      target: { value: "" },
    });
    expect(agentOverrides()).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: "Override Timeout" }),
    ).toBeChecked();
  });

  it("edits a boolean override", () => {
    seedStore({ agentTools: [{ shell: { enable_run_shell_command: true } }] });
    renderShellPanel();

    fireEvent.click(screen.getByText("Enabled"));
    expect(agentOverrides()).toEqual({ enable_run_shell_command: false });
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });

  it("shows a value stored from elsewhere in place of the draft", () => {
    seedStore({
      agentTools: [{ shell: { extra_env_passthrough: ["GITEA_TOKEN"] } }],
    });
    renderShellPanel();
    fireEvent.click(screen.getAllByText("Add value")[0]);

    act(() =>
      useConfigStore.getState().updateAgentToolOverrides("openclaw", "shell", {
        extra_env_passthrough: ["HOME"],
      }),
    );

    expect(envInputs().map((input) => input.value)).toEqual(["HOME"]);
  });

  it("keeps inherit-cleared fields as authored and lets unticking restore inheritance", () => {
    seedStore({
      agentTools: [
        {
          shell: {
            extra_env_passthrough: "__MINDROOM_INHERIT__",
            enable_run_shell_command: "__MINDROOM_INHERIT__",
            timeout: "__MINDROOM_INHERIT__",
          },
        },
      ],
    });
    renderShellPanel();

    for (const label of ["Env Passthrough", "Run Shell Command", "Timeout"]) {
      expect(
        screen.getByRole("checkbox", { name: `Override ${label}` }),
      ).toBeChecked();
    }
    expect(screen.getAllByText(/__MINDROOM_INHERIT__/)).toHaveLength(3);
    expect(envInputs()).toHaveLength(0);
    expect(screen.queryByText("Disabled")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override PATH Prepend" }),
    );
    fireEvent.click(screen.getByText("Add value"));
    fireEvent.change(screen.getByPlaceholderText("PATH Prepend"), {
      target: { value: "/run/wrappers/bin" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Defer until searched" }),
    );
    expect(agentOverrides()).toEqual({
      defer: true,
      extra_env_passthrough: "__MINDROOM_INHERIT__",
      enable_run_shell_command: "__MINDROOM_INHERIT__",
      timeout: "__MINDROOM_INHERIT__",
      shell_path_prepend: ["/run/wrappers/bin"],
    });

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override Run Shell Command" }),
    );
    expect(agentOverrides()).not.toHaveProperty("enable_run_shell_command");
    expect(
      screen.getByRole("checkbox", { name: "Override Run Shell Command" }),
    ).not.toBeChecked();
  });

  it("unticks an override to clear it", () => {
    seedStore({
      agentTools: [{ discord: { bot_token: "agent-specific-token" } }],
    });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="discord"
        toolDisplayName="Discord"
        configFields={[
          {
            name: "bot_token",
            label: "Bot Token",
            type: "password",
            required: true,
            description: "Discord bot token",
          },
        ]}
      />,
    );

    const toggle = screen.getByRole("checkbox", { name: "Override Bot Token" });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);

    expect(agentOverrides("discord")).toBeNull();
    expect(toggle).not.toBeChecked();
  });

  it("falls back to configFields when no overrideFields are provided", () => {
    seedStore({ agentTools: ["discord"] });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="discord"
        toolDisplayName="Discord"
        overrideFields={null}
        configFields={[
          {
            name: "bot_token",
            label: "Bot Token",
            type: "password",
            description: "Discord bot token",
            default: "default-token",
          },
        ]}
      />,
    );

    expect(screen.getByText("Default: ••••••••")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Override Bot Token" }),
    ).not.toBeChecked();
  });

  it("prefers overrideFields over configFields when both are present", () => {
    seedStore();
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="shell"
        toolDisplayName="Shell"
        overrideFields={[
          {
            name: "extra_env_passthrough",
            label: "Env Passthrough",
            type: "string[]",
          },
        ]}
        configFields={[{ name: "timeout", label: "Timeout", type: "number" }]}
      />,
    );

    expect(screen.getByText("Env Passthrough")).toBeInTheDocument();
    expect(screen.queryByText("Timeout")).not.toBeInTheDocument();
  });

  it("keeps a draft to the agent it was typed for", () => {
    const tools = [{ shell: { extra_env_passthrough: ["HOME"] } }];
    const config = {
      agents: {
        a: { display_name: "A", tools },
        b: { display_name: "B", tools },
      },
    } as unknown as Config;
    useConfigStore.setState({
      config,
      loadedConfig: config,
      agents: Object.entries(config.agents).map(
        ([id, agent]) => ({ id, rooms: [], ...agent }) as unknown as Agent,
      ),
    });
    const panel = (agentId: string) => (
      <ToolConfigPanel
        target={{ kind: "agent", agentId, lazyLoading: false }}
        toolName="shell"
        overrideFields={shellFields}
      />
    );
    const { rerender } = render(panel("a"));
    fireEvent.click(screen.getAllByText("Add value")[0]);
    expect(envInputs().map((input) => input.value)).toEqual(["HOME", ""]);

    rerender(panel("b"));
    expect(envInputs().map((input) => input.value)).toEqual(["HOME"]);
  });

  describe("lazy loading", () => {
    it("stores defer beside existing overrides", () => {
      seedStore({
        agentTools: [{ shell: { extra_env_passthrough: ["GITEA_TOKEN"] } }],
      });
      renderShellPanel();
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Defer until searched" }),
      );

      expect(agentOverrides()).toEqual({
        defer: true,
        extra_env_passthrough: ["GITEA_TOKEN"],
      });
    });

    it("offers lazy loading for tools without override fields", () => {
      seedStore({ agentTools: ["browser"] });
      render(
        <ToolConfigPanel
          target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
          toolName="browser"
        />,
      );

      expect(
        screen.getByRole("checkbox", { name: "Send on every request" }),
      ).toBeDisabled();
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Defer until searched" }),
      );
      expect(agentOverrides("browser")).toEqual({ defer: true });
    });

    it("keeps lazy flags when an override field is cleared", () => {
      seedStore({
        agentTools: [
          {
            shell: {
              defer: true,
              initial: true,
              extra_env_passthrough: ["GITEA_TOKEN"],
            },
          },
        ],
      });
      renderShellPanel();
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Override Env Passthrough" }),
      );

      expect(agentOverrides()).toEqual({ defer: true, initial: true });
    });

    it("clears initial loading together with defer", () => {
      seedStore({ agentTools: [{ browser: { defer: true, initial: true } }] });
      render(
        <ToolConfigPanel
          target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
          toolName="browser"
        />,
      );
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Defer until searched" }),
      );

      expect(agentOverrides("browser")).toBeNull();
    });
  });

  it("edits shared default tool overrides without lazy-loading controls", () => {
    seedStore({
      agentTools: [{ discord: { bot_token: "agent" } }],
      defaultTools: [{ discord: { bot_token: "abc" } }],
    });
    render(
      <ToolConfigPanel
        target={{ kind: "defaults" }}
        toolName="discord"
        toolDisplayName="Discord"
        configFields={[
          { name: "bot_token", label: "Bot Token", type: "password" },
        ]}
      />,
    );

    expect(screen.getByText("Discord — Default Settings")).toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "Defer until searched" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override Bot Token" }),
    );
    expect(
      useConfigStore.getState().getDefaultToolOverrides("discord"),
    ).toBeNull();
    expect(agentOverrides("discord")).toEqual({ bot_token: "agent" });
  });
});
