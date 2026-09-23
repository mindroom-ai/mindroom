import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToolConfigPanel } from "./ToolConfigPanel";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));

import { useConfigStore } from "@/store/configStore";

describe("ToolConfigPanel", () => {
  const mockStore = {
    getAgentToolOverrides: vi.fn(),
    updateAgentToolOverrides: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockStore.getAgentToolOverrides.mockReturnValue({
      extra_env_passthrough: ["GITEA_TOKEN"],
    });
    vi.mocked(useConfigStore).mockReturnValue(mockStore as never);
  });

  it("renders an empty state when no tool is selected", () => {
    render(<ToolConfigPanel agentId="openclaw" toolName={null} />);

    expect(
      screen.getByText("Select a checked tool to edit per-agent settings."),
    ).toBeInTheDocument();
  });

  it('renders "no settings available" when tool has neither override fields nor config fields', () => {
    mockStore.getAgentToolOverrides.mockReturnValue(null);
    render(
      <ToolConfigPanel
        agentId="openclaw"
        toolName="browser"
        toolDisplayName="Browser"
        overrideFields={null}
        configFields={null}
      />,
    );

    expect(
      screen.getByText("No per-agent settings available for this tool."),
    ).toBeInTheDocument();
  });

  it("renders override fields with toggle controls for string[] fields", () => {
    render(
      <ToolConfigPanel
        agentId="openclaw"
        toolName="shell"
        toolDisplayName="Shell Commands"
        overrideFields={[
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
        ]}
      />,
    );

    expect(
      screen.getByText("Shell Commands — Per-Agent Settings"),
    ).toBeInTheDocument();
    // extra_env_passthrough has an override value, so its toggle should be checked
    expect(screen.getByDisplayValue("GITEA_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("Customized")).toBeInTheDocument();
  });

  it("commits override updates when toggling and editing string[] fields", () => {
    render(
      <ToolConfigPanel
        agentId="openclaw"
        toolName="shell"
        toolDisplayName="Shell Commands"
        overrideFields={[
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
        ]}
      />,
    );

    // Enable the PATH Prepend override toggle
    const pathToggle = screen.getByRole("checkbox", {
      name: "Override PATH Prepend",
    });
    fireEvent.click(pathToggle);

    // Now add a value
    fireEvent.click(screen.getAllByText("Add value")[1]);
    fireEvent.change(screen.getByPlaceholderText("PATH Prepend"), {
      target: { value: "/run/wrappers/bin" },
    });

    expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
      "openclaw",
      "shell",
      {
        extra_env_passthrough: ["GITEA_TOKEN"],
        shell_path_prepend: ["/run/wrappers/bin"],
      },
    );

    // Remove the env passthrough value
    fireEvent.click(screen.getByLabelText("Remove Env Passthrough value 1"));

    expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
      "openclaw",
      "shell",
      {
        extra_env_passthrough: null,
        shell_path_prepend: ["/run/wrappers/bin"],
      },
    );
  });

  it("disables override toggle to clear an override", () => {
    mockStore.getAgentToolOverrides.mockReturnValue({
      bot_token: "agent-specific-token",
    });

    render(
      <ToolConfigPanel
        agentId="openclaw"
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

    // The toggle should be checked since there's an override
    const toggle = screen.getByRole("checkbox", { name: "Override Bot Token" });
    expect(toggle).toBeChecked();

    // Uncheck it to revert to the tool default
    fireEvent.click(toggle);

    expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
      "openclaw",
      "discord",
      null,
    );
  });

  it("falls back to configFields when no overrideFields are provided", () => {
    mockStore.getAgentToolOverrides.mockReturnValue(null);

    render(
      <ToolConfigPanel
        agentId="openclaw"
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

    // Should show the field with its tool default (password masked)
    expect(screen.getByText("Default: ••••••••")).toBeInTheDocument();
    // Toggle should be unchecked
    const toggle = screen.getByRole("checkbox", { name: "Override Bot Token" });
    expect(toggle).not.toBeChecked();
  });

  it("prefers overrideFields over configFields when both are present", () => {
    mockStore.getAgentToolOverrides.mockReturnValue(null);
    render(
      <ToolConfigPanel
        agentId="openclaw"
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

    // Should show override field, not config field
    expect(screen.getByText("Env Passthrough")).toBeInTheDocument();
    expect(screen.queryByText("Timeout")).not.toBeInTheDocument();
  });

  describe("lazy loading", () => {
    const shellFields = [
      {
        name: "extra_env_passthrough",
        label: "Extra Env Passthrough",
        type: "string[]" as const,
      },
    ];

    it("stores defer beside existing overrides", () => {
      render(
        <ToolConfigPanel
          agentId="openclaw"
          toolName="shell"
          overrideFields={shellFields}
        />,
      );
      fireEvent.click(screen.getByRole("checkbox", { name: "Load lazily" }));

      expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
        "openclaw",
        "shell",
        { defer: true, extra_env_passthrough: ["GITEA_TOKEN"] },
      );
    });

    it("offers lazy loading for tools without override fields", () => {
      mockStore.getAgentToolOverrides.mockReturnValue(null);
      render(<ToolConfigPanel agentId="openclaw" toolName="browser" />);

      expect(
        screen.getByRole("checkbox", { name: "Load at session start" }),
      ).toBeDisabled();
      fireEvent.click(screen.getByRole("checkbox", { name: "Load lazily" }));
      expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
        "openclaw",
        "browser",
        { defer: true },
      );
    });

    it("keeps lazy flags when an override field is cleared", () => {
      mockStore.getAgentToolOverrides.mockReturnValue({
        defer: true,
        initial: true,
        extra_env_passthrough: ["GITEA_TOKEN"],
      });
      render(
        <ToolConfigPanel
          agentId="openclaw"
          toolName="shell"
          overrideFields={shellFields}
        />,
      );
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: "Override Extra Env Passthrough",
        }),
      );

      expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
        "openclaw",
        "shell",
        { defer: true, initial: true },
      );
    });

    it("clears initial loading together with defer", () => {
      mockStore.getAgentToolOverrides.mockReturnValue({
        defer: true,
        initial: true,
      });
      render(<ToolConfigPanel agentId="openclaw" toolName="browser" />);
      fireEvent.click(screen.getByRole("checkbox", { name: "Load lazily" }));

      expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith(
        "openclaw",
        "browser",
        null,
      );
    });
  });
});
