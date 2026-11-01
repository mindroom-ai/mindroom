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

/** Back the panel with the real store, holding agents with the same tools and defaults.tools. */
function seedStore({
  agentIds = ["openclaw"],
  agentTools = ["shell"],
  defaultTools = [],
}: {
  agentIds?: string[];
  agentTools?: ToolEntry[];
  defaultTools?: ToolEntry[];
} = {}) {
  const config = {
    agents: Object.fromEntries(
      agentIds.map((id) => [id, { display_name: id, tools: agentTools }]),
    ),
    defaults: { tools: defaultTools },
  } as unknown as Config;
  useConfigStore.setState({
    config,
    loadedConfig: config,
    agents: Object.entries(config.agents).map(
      ([id, agent]) => ({ id, rooms: [], ...agent }) as unknown as Agent,
    ),
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

/** The fields the panel offers, by their Override boxes, in order. */
function overrideLabels(): (string | null)[] {
  return screen
    .getAllByRole("checkbox", { name: /^Override / })
    .map((box) => box.getAttribute("aria-label"));
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

    // Removing the last value stores the empty list the field shows.
    fireEvent.click(screen.getByLabelText("Remove Env Passthrough value 1"));
    expect(agentOverrides()).toEqual({
      extra_env_passthrough: [],
      shell_path_prepend: ["/run/wrappers/bin"],
    });
    const envToggle = screen.getByRole("checkbox", {
      name: "Override Env Passthrough",
    });
    expect(envToggle).toBeChecked();
    expect(screen.getByText("No values.")).toBeInTheDocument();

    fireEvent.click(envToggle);
    expect(envToggle).not.toBeChecked();

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
    expect(agentOverrides()).toEqual({ extra_env_passthrough: [] });
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

  it("stores the state a ticked boolean without a default shows", () => {
    seedStore();
    renderShellPanel();

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Override Run Shell Command" }),
    );
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(agentOverrides()).toEqual({ enable_run_shell_command: false });
  });

  it.each([
    [
      "an agent",
      { kind: "agent", agentId: "openclaw", lazyLoading: true } as const,
    ],
    ["the defaults", { kind: "defaults" } as const],
  ])(
    "keeps a stored empty list for %s while a value is added and left blank",
    (_owner, target) => {
      const tools = [{ docker: { include_tools: [] } }];
      seedStore({ agentTools: tools, defaultTools: tools });
      const stored = () =>
        target.kind === "agent"
          ? agentOverrides("docker")
          : useConfigStore.getState().getDefaultToolOverrides("docker");
      render(
        <ToolConfigPanel
          target={target}
          toolName="docker"
          configFields={[
            {
              name: "include_tools",
              label: "Exposed Docker Commands",
              type: "string[]",
            },
          ]}
        />,
      );

      expect(
        screen.getByRole("checkbox", {
          name: "Override Exposed Docker Commands",
        }),
      ).toBeChecked();
      expect(screen.getByText("No values.")).toBeInTheDocument();

      fireEvent.click(screen.getByText("Add value"));
      expect(
        screen.getAllByPlaceholderText("Exposed Docker Commands"),
      ).toHaveLength(1);
      expect(stored()).toEqual({ include_tools: [] });
    },
  );

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
      agentTools: [{ discord: { channel: "agent-specific-channel" } }],
    });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="discord"
        toolDisplayName="Discord"
        configFields={[{ name: "channel", label: "Channel", type: "text" }]}
      />,
    );

    const toggle = screen.getByRole("checkbox", { name: "Override Channel" });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);

    expect(agentOverrides("discord")).toBeNull();
    expect(toggle).not.toBeChecked();
  });

  it("shows an unticked config field with its default", () => {
    seedStore({ agentTools: ["discord"] });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="discord"
        toolDisplayName="Discord"
        configFields={[
          {
            name: "channel",
            label: "Channel",
            type: "text",
            description: "Discord channel",
            default: "general",
          },
        ]}
      />,
    );

    expect(screen.getByText("Default: general")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Override Channel" }),
    ).not.toBeChecked();
  });

  it("offers only fields a tool entry may set and points secrets to the Tools page", () => {
    seedStore({ agentTools: ["reddit"] });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="reddit"
        toolDisplayName="Reddit"
        configFields={[
          { name: "client_id", label: "Client ID", type: "text" },
          {
            name: "client_secret",
            label: "Client Secret",
            type: "password",
            authored_override: false,
          },
          {
            name: "password",
            label: "Password",
            type: "password",
            authored_override: false,
          },
          {
            name: "base_dir",
            label: "Base Dir",
            type: "text",
            authored_override: false,
          },
        ]}
      />,
    );

    expect(
      screen.getByRole("checkbox", { name: "Override Client ID" }),
    ).toBeInTheDocument();
    for (const label of ["Client Secret", "Password", "Base Dir"]) {
      expect(
        screen.queryByRole("checkbox", { name: `Override ${label}` }),
      ).not.toBeInTheDocument();
    }
    expect(
      screen.getByText(
        "Secrets are set on the Tools page: Client Secret, Password.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a list stored on a text field as the comma-separated text the backend reads", () => {
    seedStore({
      defaultTools: [{ shell: { extra_env_passthrough: ["GITEA_*", "HOME"] } }],
    });
    render(
      <ToolConfigPanel
        target={{ kind: "defaults" }}
        toolName="shell"
        toolDisplayName="Shell"
        configFields={[
          {
            name: "extra_env_passthrough",
            label: "Extra Env Passthrough",
            type: "text",
          },
        ]}
      />,
    );

    expect(screen.getByPlaceholderText("Extra Env Passthrough")).toHaveValue(
      "GITEA_*, HOME",
    );
  });

  it("offers an agent entry its config fields and its agent-only fields", () => {
    seedStore({ agentTools: ["slack"] });
    render(
      <ToolConfigPanel
        target={{ kind: "agent", agentId: "openclaw", lazyLoading: true }}
        toolName="slack"
        toolDisplayName="Slack"
        overrideFields={[
          { name: "channels", label: "Project Channels", type: "string[]" },
          { name: "room", label: "Room", type: "text" },
        ]}
        configFields={[
          { name: "write", label: "Allow Write", type: "boolean" },
        ]}
      />,
    );

    expect(overrideLabels()).toEqual([
      "Override Allow Write",
      "Override Project Channels",
      "Override Room",
    ]);
  });

  it("shows a config field once, in the list form its agent-only twin takes", () => {
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
        configFields={[
          {
            name: "enable_run_shell_command",
            label: "Run Shell Command",
            type: "boolean",
          },
          {
            name: "extra_env_passthrough",
            label: "Extra Env Passthrough",
            type: "text",
          },
        ]}
      />,
    );

    expect(overrideLabels()).toEqual([
      "Override Run Shell Command",
      "Override Env Passthrough",
    ]);
  });

  it("keeps a draft to the agent it was typed for", () => {
    seedStore({
      agentIds: ["a", "b"],
      agentTools: [{ shell: { extra_env_passthrough: ["HOME"] } }],
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
      agentTools: [{ discord: { channel: "agent" } }],
      defaultTools: [{ discord: { channel: "abc" } }],
    });
    render(
      <ToolConfigPanel
        target={{ kind: "defaults" }}
        toolName="discord"
        toolDisplayName="Discord"
        configFields={[{ name: "channel", label: "Channel", type: "text" }]}
      />,
    );

    expect(screen.getByText("Discord — Default Settings")).toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "Defer until searched" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Override Channel" }));
    expect(
      useConfigStore.getState().getDefaultToolOverrides("discord"),
    ).toBeNull();
    expect(agentOverrides("discord")).toEqual({ channel: "agent" });
  });
});
