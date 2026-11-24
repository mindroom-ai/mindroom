import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, onTestFinished } from "vitest";
import { AgentEditor } from "./AgentEditor";
import { useConfigStore } from "@/store/configStore";
import {
  Agent,
  AgentPoliciesByAgent,
  normalizeAgentUpdates,
  SHARED_CONTEXT_FILE_PLACEHOLDER,
} from "@/types/config";
import { useTools } from "@/hooks/useTools";
import { useConfigSchema } from "@/hooks/useConfigSchema";

// Mock the store
vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));

vi.mock("@/components/ui/toaster", () => ({
  toast: vi.fn(),
}));

vi.mock("@/hooks/useConfigSchema", () => ({
  useConfigSchema: vi.fn(() => ({ schema: null, error: null, retry: vi.fn() })),
}));

// Mock useTools hook
vi.mock("@/hooks/useTools", () => ({
  useTools: vi.fn(),
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: vi.fn(() => ({
    skills: [
      {
        name: "debugging",
        description: "Debug issues quickly",
        origin: "bundled",
        can_edit: false,
      },
      {
        name: "code-review",
        description: "Perform code reviews",
        origin: "user",
        can_edit: true,
      },
    ],
    loading: false,
  })),
}));

describe("AgentEditor", () => {
  const makeAgentPolicies = (
    overrides: Partial<AgentPoliciesByAgent[string]> = {},
  ): AgentPoliciesByAgent => ({
    test_agent: {
      agent_name: "test_agent",
      is_private: false,
      effective_execution_scope: null,
      scope_label: "unscoped",
      scope_source: "unscoped",
      dashboard_credentials_supported: true,
      team_eligibility_reason: null,
      private_knowledge_base_id: null,
      private_workspace_enabled: false,
      private_agent_knowledge_enabled: false,
      ...overrides,
    },
  });

  const mockAgent: Agent = {
    id: "test_agent",
    display_name: "Test Agent",
    role: "Test role",
    tools: ["calculator"],
    skills: ["debugging"],
    instructions: ["Test instruction"],
    rooms: ["test_room"],
    knowledge_bases: ["research"],
  };

  const mockConfig = {
    models: {
      default: { provider: "test", id: "test-model" },
      custom: { provider: "custom", id: "custom-model" },
    },
    memory: {
      backend: "mem0",
      embedder: {
        provider: "openai",
        config: { model: "text-embedding-3-small" },
      },
    },
    knowledge_bases: {
      legal: { path: "./legal", watch: true },
      research: { path: "./research", watch: true },
    },
    defaults: {},
  };

  const mockStore = {
    agents: [mockAgent],
    rooms: [
      {
        id: "test_room",
        display_name: "Test Room",
        description: "Test room",
        agents: ["test_agent"],
      },
      {
        id: "other_room",
        display_name: "Other Room",
        description: "Another room",
        agents: [],
      },
    ],
    selectedAgentId: "test_agent",
    updateAgent: vi.fn(),
    setAgentPrivateEnabled: vi.fn(),
    deleteAgent: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue({ status: "saved" }),
    config: mockConfig,
    agentPoliciesByAgent: makeAgentPolicies(),
    isDirty: false,
    diagnostics: [],
    selectAgent: vi.fn(),
    getAgentToolOverrides: vi.fn(() => null),
  };

  const mockStoreWith = (
    agent: Agent,
    overrides: Record<string, unknown> = {},
  ) => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [agent],
      ...overrides,
    });
  };

  const shellTool = {
    name: "shell",
    display_name: "Shell Commands",
    setup_type: "none",
    status: "available",
    agent_override_fields: [
      {
        name: "extra_env_passthrough",
        label: "Env Passthrough",
        type: "string[]",
      },
    ],
  };

  const disabledCompactionDefaults = {
    compaction: {
      enabled: false,
      reserve_tokens: 16384,
      threshold_percent: 0.8,
    },
  };

  const { id: _mockAgentId, ...mockAgentConfig } = mockAgent;

  // Render against the real store, loaded from a stubbed backend, so edits
  // re-render the editor with the store's next state.
  const loadRealStore = async (
    agentConfig: Record<string, unknown> = mockAgentConfig,
    configOverrides: Record<string, unknown> = {},
  ) => {
    const actual = await vi.importActual<typeof import("@/store/configStore")>(
      "@/store/configStore",
    );
    const store = actual.useConfigStore;
    store.setState(store.getInitialState(), true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        Response.json(
          url === "/api/config/load"
            ? {
                ...mockConfig,
                ...configOverrides,
                agents: { test_agent: agentConfig },
              }
            : { agent_policies: makeAgentPolicies() },
        ),
      ),
    );
    onTestFinished(() => {
      vi.unstubAllGlobals();
    });
    await store.getState().loadConfig();
    store.getState().selectAgent("test_agent");
    const updateAgent = vi.fn(store.getState().updateAgent);
    store.setState({ updateAgent });
    (useConfigStore as any).mockImplementation(() => store());
    return { store, updateAgent };
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue(mockStore);
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: null,
      error: null,
      retry: vi.fn(),
    });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "calculator",
          display_name: "Calculator",
          setup_type: "none",
          status: "available",
        },
        {
          name: "delegate",
          display_name: "Subagents",
          setup_type: "none",
          status: "available",
        },
        {
          name: "file",
          display_name: "File",
          setup_type: "none",
          status: "available",
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });
  });

  it("requests agent-scoped tools for the selected agent", () => {
    render(<AgentEditor />);

    expect(useTools).toHaveBeenCalledWith("test_agent", null);
  });

  it("requests tools using inherited defaults.worker_scope", () => {
    mockStoreWith(mockAgent, {
      agentPoliciesByAgent: makeAgentPolicies({
        effective_execution_scope: "user",
        scope_label: "worker_scope=user",
        scope_source: "defaults.worker_scope",
        dashboard_credentials_supported: false,
      }),
    });

    render(<AgentEditor />);

    expect(useTools).toHaveBeenCalledWith("test_agent", "user");
  });

  it("shows inherited default tools even when defaults.tools is omitted from authored config", () => {
    mockStoreWith(
      { ...mockAgent, include_default_tools: undefined },
      { config: { ...mockConfig, defaults: {} } },
    );

    render(<AgentEditor />);

    expect(screen.getByLabelText("worker scheduler")).toBeInTheDocument();
  });

  it("fails closed when agent policy preview is unavailable", () => {
    mockStoreWith(mockAgent, { agentPoliciesByAgent: {} });

    render(<AgentEditor />);

    expect(useTools).toHaveBeenCalledWith(null, null);
    expect(
      screen.getByText(
        "Agent policy preview is unavailable. Save or refresh to re-validate tool scope support for this draft.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Tool availability preview is unavailable while agent policy preview is unavailable. Save or refresh to validate tool assignments.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Selected But Unavailable"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "This tool is no longer available in the current registry. Uncheck it to remove it.",
      ),
    ).not.toBeInTheDocument();
  });

  it("prefers the unavailable preview message over generic tool loading", () => {
    mockStoreWith(mockAgent, { agentPoliciesByAgent: {} });
    (useTools as any).mockReturnValue({
      tools: [],
      loading: true,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    expect(
      screen.getByText(
        "Tool availability preview is unavailable while agent policy preview is unavailable. Save or refresh to validate tool assignments.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Loading available tools..."),
    ).not.toBeInTheDocument();
  });

  it("shows selectable setup-required tools instead of hiding them", () => {
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "calculator",
          display_name: "Calculator",
          setup_type: "none",
          status: "available",
        },
        {
          name: "weather",
          display_name: "Weather",
          setup_type: "api_key",
          status: "requires_config",
          dashboard_configuration_supported: true,
        },
      ],
      loading: false,
    });

    render(<AgentEditor />);

    expect(screen.getByText("Setup Required")).toBeInTheDocument();
    expect(screen.getByLabelText("Weather")).toBeInTheDocument();
  });

  it("shows backend tool descriptions inline and rich metadata in a details popover", () => {
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "shell",
          display_name: "Shell Commands",
          description:
            "Run shell commands in the agent workspace with timeout handling.",
          helper_text: "Use this for tests, scripts, and workspace automation.",
          setup_type: "none",
          status: "available",
          default_execution_target: "worker",
          docs_url: "https://docs.example.com/shell",
          dependencies: ["bash"],
          function_names: [
            "run_command",
            "read_process",
            "stop_process",
            "list_processes",
            "send_stdin",
            "write_file",
            "extra_function",
          ],
        },
        {
          name: "file",
          display_name: "File",
          description: "Read and write workspace files.",
          setup_type: "none",
          status: "available",
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    const inlineSummary = screen.getByText(
      "Run shell commands in the agent workspace with timeout handling.",
    );
    expect(inlineSummary).toBeInTheDocument();
    expect(inlineSummary).toHaveClass("line-clamp-2");
    expect(
      screen.getByRole("checkbox", { name: "Shell Commands" }),
    ).not.toHaveAttribute("aria-label");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Show Shell Commands tool details",
      }),
    );

    const details = screen.getByRole("dialog", {
      name: "Shell Commands tool details",
    });
    expect(
      within(details).getByText(
        "Use this for tests, scripts, and workspace automation.",
      ),
    ).toBeInTheDocument();
    expect(
      within(details).getByRole("link", { name: "Open documentation" }),
    ).toHaveAttribute("href", "https://docs.example.com/shell");
    expect(within(details).getByText("Setup")).toBeInTheDocument();
    expect(within(details).getByText("None")).toBeInTheDocument();
    expect(within(details).getByText("Default Runtime")).toBeInTheDocument();
    expect(within(details).getByText("Worker")).toBeInTheDocument();
    expect(within(details).getByText("run_command")).toBeInTheDocument();
    expect(within(details).getByText("+1 more")).toBeInTheDocument();
  });

  it("omits placeholder noise for tools without optional metadata", () => {
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "file",
          display_name: "File",
          description: "Read and write workspace files.",
          setup_type: "none",
          status: "available",
          docs_url: "javascript:alert(1)",
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    expect(
      screen.getByText("Read and write workspace files."),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Show File tool details" }),
    );

    const details = screen.getByRole("dialog", {
      name: "File tool details",
    });
    expect(
      within(details).queryByText("No helper text"),
    ).not.toBeInTheDocument();
    expect(
      within(details).queryByText("No documentation"),
    ).not.toBeInTheDocument();
    expect(within(details).queryByText("No functions")).not.toBeInTheDocument();
    expect(
      within(details).queryByRole("link", { name: "Open documentation" }),
    ).not.toBeInTheDocument();
  });

  it("offers lazy loading only for tools whose entries may set it", () => {
    mockStoreWith({ ...mockAgent, tools: ["calculator", "dynamic_tools"] });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "calculator",
          display_name: "Calculator",
          setup_type: "none",
          status: "available",
          lazy_loading_supported: true,
        },
        {
          name: "dynamic_tools",
          display_name: "Dynamic Tools",
          setup_type: "none",
          status: "available",
          lazy_loading_supported: false,
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    // A control-plane tool with no fields has no settings to open.
    expect(
      screen.queryByRole("button", { name: "Dynamic Tools" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Calculator" }));
    expect(screen.getByLabelText("Defer until searched")).toBeInTheDocument();
  });

  it("shows customized indicators and opens the inline tool settings panel for checked tools", () => {
    const getAgentToolOverrides = vi.fn((agentId: string, toolName: string) =>
      agentId === "test_agent" && toolName === "shell"
        ? { extra_env_passthrough: ["GITEA_TOKEN"] }
        : null,
    );

    mockStoreWith(
      { ...mockAgent, tools: ["shell"] },
      { getAgentToolOverrides },
    );
    (useTools as any).mockReturnValue({
      tools: [shellTool],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    expect(screen.getByText("Customized")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Shell Commands" }),
    ).toHaveAttribute("aria-label", "Shell Commands");
    fireEvent.click(screen.getByRole("button", { name: "Shell Commands" }));

    expect(
      screen.getByText("Shell Commands — Per-Agent Settings"),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("GITEA_TOKEN")).toBeInTheDocument();
  });

  it("clears a tool's customized badge when the tool is unchecked", async () => {
    await loadRealStore({
      display_name: "Test Agent",
      tools: [{ shell: { extra_env_passthrough: ["TOKEN"] } }],
    });
    (useTools as any).mockReturnValue({
      tools: [shellTool],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);
    expect(screen.getByText("Customized")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Shell Commands" }));

    expect(screen.queryByText("Customized")).not.toBeInTheDocument();
  });

  it("keeps the other tools' entries when a tool is checked", async () => {
    const { store } = await loadRealStore({
      display_name: "Test Agent",
      tools: [{ shell: { extra_env_passthrough: ["TOKEN"] } }],
    });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "shell",
          display_name: "Shell Commands",
          setup_type: "none",
          status: "available",
        },
        {
          name: "calculator",
          display_name: "Calculator",
          setup_type: "none",
          status: "available",
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Calculator" }));

    expect(store.getState().agents[0].tools).toEqual([
      { shell: { extra_env_passthrough: ["TOKEN"] } },
      "calculator",
    ]);
  });

  it("opens a tool's settings when the tool is checked", async () => {
    await loadRealStore({ display_name: "Test Agent", tools: [] });
    (useTools as any).mockReturnValue({
      tools: [shellTool],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Shell Commands" }));

    expect(
      screen.getByText("Shell Commands — Per-Agent Settings"),
    ).toBeInTheDocument();
  });

  it("keeps selected unsupported tools visible so they can be removed", () => {
    mockStoreWith({ ...mockAgent, tools: ["gmail"] });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "gmail",
          display_name: "Gmail",
          setup_type: "oauth",
          status: "requires_config",
          execution_scope_supported: false,
        },
      ],
      loading: false,
    });

    render(<AgentEditor />);

    expect(screen.getByText("Selected But Unavailable")).toBeInTheDocument();
    expect(screen.getByLabelText("Gmail")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Not supported for this execution scope. Uncheck it to remove it.",
      ),
    ).toBeInTheDocument();
  });

  it("lists tools by section, sorted by name, with section-specific rows", () => {
    mockStoreWith({ ...mockAgent, tools: ["retired_tool", "zeta"] });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: "zeta",
          display_name: "Zeta",
          setup_type: "none",
          status: "available",
        },
        {
          name: "alpha",
          display_name: "Alpha",
          setup_type: "none",
          status: "available",
        },
        {
          name: "github",
          display_name: "GitHub",
          setup_type: "api_key",
          status: "available",
        },
        {
          name: "weather",
          display_name: "Weather",
          setup_type: "api_key",
          status: "requires_config",
          dashboard_configuration_supported: false,
        },
      ],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);

    expect(document.getElementById("configured-github")).toBeInTheDocument();
    expect(document.getElementById("setup-weather")).toBeInTheDocument();
    expect(
      [...document.querySelectorAll('[id^="default-"]')].map((el) => el.id),
    ).toEqual(["default-alpha", "default-zeta"]);
    expect(screen.getAllByText("Setup required")).toHaveLength(1);
    expect(
      screen.getAllByText(/dashboard\s+credential setup is only supported/),
    ).toHaveLength(1);
    expect(
      screen.getByText(
        "This tool is no longer available in the current registry. Uncheck it to remove it.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "retired_tool" }),
    ).toBeChecked();
  });

  it("closes a tool's settings when the tool leaves the agent", async () => {
    const { store } = await loadRealStore({
      display_name: "Test Agent",
      tools: ["shell"],
    });
    (useTools as any).mockReturnValue({
      tools: [shellTool],
      loading: false,
      statusAuthoritative: true,
    });

    render(<AgentEditor />);
    fireEvent.click(screen.getByRole("button", { name: "Shell Commands" }));
    expect(
      screen.getByText("Shell Commands — Per-Agent Settings"),
    ).toBeInTheDocument();

    act(() => store.getState().updateAgent("test_agent", { tools: [] }));
    act(() => store.getState().updateAgent("test_agent", { tools: ["shell"] }));

    expect(
      screen.queryByText("Shell Commands — Per-Agent Settings"),
    ).not.toBeInTheDocument();
  });

  it("displays selected agent details", () => {
    render(<AgentEditor />);

    expect(screen.getByDisplayValue("Test Agent")).toBeTruthy();
    expect(screen.getByDisplayValue("Test role")).toBeTruthy();
    expect(screen.getByDisplayValue("Test instruction")).toBeTruthy();
    const testRoomCheckbox = screen.getByRole("checkbox", {
      name: /Test Room/i,
    });
    expect(testRoomCheckbox).toBeChecked();
  });

  it("clears zero history runs instead of writing them through", () => {
    render(<AgentEditor />);

    const historyRunsInput = screen.getByLabelText("History Runs");
    fireEvent.change(historyRunsInput, { target: { value: "0" } });

    expect(mockStore.updateAgent).toHaveBeenCalledWith("test_agent", {
      num_history_runs: null,
    });
  });

  it("shows empty state when no agent is selected", () => {
    mockStoreWith(mockAgent, { selectedAgentId: null });

    render(<AgentEditor />);
    expect(screen.getByText("Select an agent to edit")).toBeTruthy();
  });

  it("handles save button click", async () => {
    mockStoreWith(mockAgent, { isDirty: true });

    render(<AgentEditor />);

    const saveButton = screen.getByRole("button", { name: /save/i });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockStore.saveConfig).toHaveBeenCalled();
    });
  });

  it("shows a toast when a save is superseded by newer draft edits", async () => {
    mockStoreWith(mockAgent, {
      isDirty: true,
      saveConfig: vi.fn().mockResolvedValue({ status: "stale" }),
    });

    render(<AgentEditor />);

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    const { toast } = await import("@/components/ui/toaster");
    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith({
        title: "Save Failed",
        description: "Save was superseded by newer draft edits.",
        variant: "destructive",
      });
    });
  });

  it("renders backend validation errors for private fields", () => {
    const privateAgent: Agent = {
      ...mockAgent,
      private: {
        per: "user",
        root: "../outside",
        template_dir: "   ",
        context_files: ["../SOUL.md"],
        knowledge: {
          enabled: true,
          path: "../memory",
          watch: true,
        },
      },
    };

    mockStoreWith(privateAgent, {
      diagnostics: [
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "private", "root"],
            msg: "private.root must stay within the private instance root",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "private", "template_dir"],
            msg: "template_dir must not be blank",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "private", "context_files"],
            msg: "private.context_files must stay under the private root",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "private", "knowledge", "path"],
            msg: "private.knowledge.path must stay under the private root",
            type: "value_error",
          },
        },
      ],
    });

    render(<AgentEditor />);

    expect(
      screen.getByText(
        "private.root must stay within the private instance root",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("template_dir must not be blank"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "private.context_files must stay under the private root",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "private.knowledge.path must stay under the private root",
      ),
    ).toBeInTheDocument();
  });

  it("disables save button when not dirty", () => {
    render(<AgentEditor />);

    const saveButton = screen.getByRole("button", { name: /save/i });
    expect(saveButton).toBeDisabled();
  });

  it("handles delete button click with confirmation", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<AgentEditor />);

    const deleteButton = screen.getByRole("button", { name: /delete/i });
    fireEvent.click(deleteButton);

    expect(confirmSpy).toHaveBeenCalledWith(
      "Are you sure you want to delete this agent?",
    );
    expect(mockStore.deleteAgent).toHaveBeenCalledWith("test_agent");

    confirmSpy.mockRestore();
  });

  it("does not delete when user cancels confirmation", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<AgentEditor />);

    const deleteButton = screen.getByRole("button", { name: /delete/i });
    fireEvent.click(deleteButton);

    expect(mockStore.deleteAgent).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it("adds, edits, and removes instructions", async () => {
    const { store } = await loadRealStore();

    render(<AgentEditor />);
    fireEvent.click(screen.getByTestId("add-instruction-button"));
    expect(store.getState().agents[0].instructions).toEqual([
      "Test instruction",
      "",
    ]);

    fireEvent.change(screen.getAllByPlaceholderText("Instruction...")[1], {
      target: { value: "Second" },
    });
    expect(store.getState().agents[0].instructions).toEqual([
      "Test instruction",
      "Second",
    ]);

    fireEvent.click(
      screen
        .getByDisplayValue("Test instruction")
        .parentElement!.querySelector("button")!,
    );
    expect(store.getState().agents[0].instructions).toEqual(["Second"]);
  });

  it("adds an instruction to an agent loaded without instructions", async () => {
    const { store } = await loadRealStore({ display_name: "Bare" });

    render(<AgentEditor />);
    fireEvent.click(screen.getByTestId("add-instruction-button"));

    expect(store.getState().agents[0].instructions).toEqual([""]);
  });

  it("adds and removes rooms", async () => {
    const { updateAgent } = await loadRealStore(mockAgentConfig, {
      rooms: {
        test_room: { display_name: "Test Room", description: "Test room" },
        other_room: { display_name: "Other Room", description: "Another room" },
      },
    });

    render(<AgentEditor />);

    // Test Room checkbox should be checked initially
    const testRoomCheckbox = screen.getByRole("checkbox", {
      name: /Test Room/i,
    });
    expect(testRoomCheckbox).toBeChecked();

    // Uncheck Test Room
    fireEvent.click(testRoomCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        rooms: [],
      }),
    );

    // Check Other Room
    const otherRoomCheckbox = screen.getByRole("checkbox", {
      name: /Other Room/i,
    });
    fireEvent.click(otherRoomCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        rooms: ["other_room"],
      }),
    );
  });

  it("clears explicit worker_scope when enabling private state and restores it on disable", async () => {
    const { store } = await loadRealStore({
      ...mockAgentConfig,
      worker_scope: "user_agent",
    });

    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText("Enable requester-private state"));
    expect(store.getState().agents[0].private).toEqual({ per: "user" });
    expect(store.getState().agents[0].worker_scope).toBeUndefined();

    fireEvent.click(screen.getByLabelText("Enable requester-private state"));
    expect(store.getState().agents[0].private).toBeUndefined();
    expect(store.getState().agents[0].worker_scope).toBe("user_agent");
  });

  it("keeps the spaces typed into the private knowledge path", async () => {
    const { store } = await loadRealStore({
      ...mockAgentConfig,
      private: {
        per: "user",
        knowledge: { enabled: true, path: "memory", watch: true },
      },
    });
    render(<AgentEditor />);

    const path = screen.getByLabelText("Private Knowledge Path");
    for (const value of ["", "my", "my ", "my notes"]) {
      fireEvent.change(path, { target: { value } });
      expect(path).toHaveDisplayValue(value);
    }
    expect(store.getState().agents[0].private?.knowledge?.path).toBe(
      "my notes",
    );
  });

  it("writes every private field edit to the store", async () => {
    const { store } = await loadRealStore({
      ...mockAgentConfig,
      private: {
        per: "user",
        root: "mind_data",
        template_dir: "./mind_template",
        context_files: ["SOUL.md", "USER.md"],
        knowledge: { enabled: true, path: "memory", watch: true },
      },
    });

    render(<AgentEditor />);

    expect(screen.getByDisplayValue("mind_data")).toBeInTheDocument();
    expect(screen.getByDisplayValue("./mind_template")).toBeInTheDocument();
    expect(screen.getByDisplayValue("SOUL.md")).toBeInTheDocument();
    expect(screen.getByDisplayValue("memory")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Private Scope"));
    fireEvent.click(screen.getByRole("option", { name: "user_agent" }));
    fireEvent.change(screen.getByLabelText("Private Root"), {
      target: { value: "updated_data" },
    });
    expect(store.getState().agents[0].private).toMatchObject({
      per: "user_agent",
      root: "updated_data",
    });
    fireEvent.change(screen.getByLabelText("Private Root"), {
      target: { value: "  " },
    });
    fireEvent.change(screen.getByLabelText("Template Directory"), {
      target: { value: " " },
    });
    fireEvent.change(screen.getByDisplayValue("SOUL.md"), {
      target: { value: "MIND.md" },
    });
    fireEvent.click(
      screen
        .getByDisplayValue("USER.md")
        .parentElement!.querySelector("button")!,
    );
    const privateContextFiles = screen
      .getByText("Private Context Files")
      .closest(".space-y-2") as HTMLElement;
    fireEvent.click(
      within(privateContextFiles).getByRole("button", { name: "Add" }),
    );
    fireEvent.change(screen.getByLabelText("Private Knowledge Path"), {
      target: { value: "  " },
    });
    fireEvent.change(screen.getByLabelText("Private Knowledge Description"), {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByLabelText("Refresh on access"));
    fireEvent.click(screen.getByLabelText("Enable private knowledge"));

    expect(store.getState().agents[0].private).toEqual({
      per: "user_agent",
      context_files: ["MIND.md", ""],
      knowledge: {
        enabled: false,
        description: "notes",
        path: "memory",
        watch: false,
      },
    });
  });

  it("enables private knowledge with a default path", async () => {
    const { store } = await loadRealStore();

    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText("Enable requester-private state"));
    fireEvent.click(screen.getByLabelText("Enable private knowledge"));

    expect(store.getState().agents[0].private).toEqual({
      per: "user",
      knowledge: {
        enabled: true,
        path: "memory",
        watch: true,
      },
    });
  });

  it("drops empty compaction overrides during normalization", () => {
    expect(
      normalizeAgentUpdates(mockAgent, { compaction: {} }).compaction,
    ).toBeUndefined();
    expect(
      normalizeAgentUpdates(
        { ...mockAgent, compaction: { enabled: true, threshold_tokens: 2000 } },
        { compaction: { threshold_tokens: undefined, model: "   " } },
      ).compaction,
    ).toBeUndefined();
  });

  it.each([
    {
      name: "preserves explicit disabled compaction overrides during normalization",
      compaction: { enabled: false },
      expected: { enabled: false },
    },
    {
      name: "preserves explicit compaction model clears during normalization",
      compaction: { model: null },
      expected: { model: null },
    },
    {
      name: "preserves explicit compaction fallback model clears during normalization",
      compaction: { fallback_model: null },
      expected: { fallback_model: null },
    },
    {
      name: "enables authored compaction overrides and clears the inherited sibling threshold",
      compaction: { threshold_percent: 0.6, threshold_tokens: null },
      expected: {
        enabled: true,
        threshold_percent: 0.6,
        threshold_tokens: null,
      },
    },
    {
      name: "preserves replay-window-only compaction overrides as enabled",
      compaction: { replay_window_tokens: 200_000 },
      expected: { enabled: true, replay_window_tokens: 200_000 },
    },
    {
      name: "preserves timeout-only compaction overrides as enabled",
      compaction: { timeout_seconds: 300 },
      expected: { enabled: true, timeout_seconds: 300 },
    },
  ])("$name", ({ compaction, expected }) => {
    expect(normalizeAgentUpdates(mockAgent, { compaction }).compaction).toEqual(
      expected,
    );
  });

  it("authors a replay window from the editor", () => {
    render(<AgentEditor />);

    fireEvent.change(screen.getByLabelText("Replay Window Tokens"), {
      target: { value: "200000" },
    });

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        compaction: { replay_window_tokens: 200_000 },
      }),
    );
  });

  it("clears invalid compaction integer input instead of writing NaN", () => {
    mockStoreWith({
      ...mockAgent,
      compaction: { enabled: true, threshold_tokens: 2000 },
    });

    render(<AgentEditor />);

    fireEvent.change(screen.getByLabelText("Threshold Tokens"), {
      target: { value: "abc" },
    });

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        compaction: { enabled: true },
      }),
    );
  });

  it("clears the compaction model as an explicit null override", () => {
    mockStoreWith({
      ...mockAgent,
      compaction: { enabled: true, model: "summary-model" },
    });

    render(<AgentEditor />);

    fireEvent.change(screen.getByLabelText("Compaction Model"), {
      target: { value: "" },
    });

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        compaction: { enabled: true, model: null },
      }),
    );
  });

  it.each<{
    name: string;
    compaction?: Agent["compaction"];
    defaults: Record<string, unknown>;
    checked: boolean;
  }>([
    {
      name: "treats authored compaction overrides as enabled in the editor",
      compaction: { threshold_tokens: 2000 },
      defaults: {},
      checked: true,
    },
    {
      name: "treats a timeout-only override as enabled even when defaults disable compaction",
      compaction: { timeout_seconds: 300 },
      defaults: { compaction: { enabled: false } },
      checked: true,
    },
    {
      name: "shows automatic required compaction as disabled for a pure model clear when defaults are disabled",
      compaction: { model: null },
      defaults: disabledCompactionDefaults,
      checked: false,
    },
    {
      name: "shows automatic required compaction as disabled for a pure fallback model clear when defaults are disabled",
      compaction: { fallback_model: null },
      defaults: disabledCompactionDefaults,
      checked: false,
    },
    {
      name: "shows automatic required compaction as disabled for an empty authored override when defaults are disabled",
      compaction: {},
      defaults: disabledCompactionDefaults,
      checked: false,
    },
    {
      name: "shows replay-window-only compaction as enabled when defaults are disabled",
      compaction: { replay_window_tokens: 200_000 },
      defaults: disabledCompactionDefaults,
      checked: true,
    },
    {
      name: "shows automatic required compaction as enabled when defaults.compaction is omitted",
      defaults: {},
      checked: true,
    },
    {
      name: "shows automatic required compaction as disabled when defaults.compaction is null",
      defaults: { compaction: null },
      checked: false,
    },
    {
      name: "shows automatic required compaction as enabled when defaults.compaction is an authored empty object",
      defaults: { compaction: {} },
      checked: true,
    },
  ])("$name", ({ compaction, defaults, checked }) => {
    mockStoreWith(
      compaction === undefined ? mockAgent : { ...mockAgent, compaction },
      { config: { ...mockConfig, defaults } },
    );

    render(<AgentEditor />);

    const checkbox = screen.getByLabelText(
      "Enable automatic required compaction",
    );
    if (checked) {
      expect(checkbox).toBeChecked();
    } else {
      expect(checkbox).not.toBeChecked();
    }
  });

  it("shows field-level history and compaction validation errors", () => {
    mockStoreWith(mockAgent, {
      diagnostics: [
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "num_history_runs"],
            msg: "History runs must be at least 1.",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "num_history_messages"],
            msg: "History messages must be at least 1.",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "max_tool_calls_from_history"],
            msg: "Max tool calls must be at least 0.",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "test_agent", "compaction"],
            msg: "Compaction config is invalid.",
            type: "value_error",
          },
        },
      ],
    });

    render(<AgentEditor />);

    expect(
      screen.getByText("History runs must be at least 1."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("History messages must be at least 1."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Max tool calls must be at least 0."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Compaction config is invalid."),
    ).toBeInTheDocument();
  });

  it("uses the canonical shared context placeholder", async () => {
    await loadRealStore({ ...mockAgentConfig, context_files: [] });

    render(<AgentEditor />);

    fireEvent.click(screen.getByTestId("add-context-file-button"));

    expect(
      screen.getByPlaceholderText(SHARED_CONTEXT_FILE_PLACEHOLDER),
    ).toBeInTheDocument();
  });

  it("lists knowledge bases sorted by name", () => {
    mockStoreWith(mockAgent, {
      config: {
        ...mockConfig,
        knowledge_bases: {
          research: { path: "./research", watch: true },
          legal: { path: "./legal", watch: true },
        },
      },
    });

    render(<AgentEditor />);

    expect(
      [...document.querySelectorAll('[id^="knowledge-base-"]')].map(
        (el) => el.id,
      ),
    ).toEqual(["knowledge-base-legal", "knowledge-base-research"]);
  });

  it("updates knowledge bases when checkboxes are toggled", async () => {
    const { updateAgent } = await loadRealStore();

    render(<AgentEditor />);

    const researchCheckbox = screen.getByRole("checkbox", {
      name: /research/i,
    });
    expect(researchCheckbox).toBeChecked();

    const legalCheckbox = screen.getByRole("checkbox", { name: /legal/i });
    fireEvent.click(legalCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        knowledge_bases: ["research", "legal"],
      }),
    );

    fireEvent.click(researchCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        knowledge_bases: ["legal"],
      }),
    );
  });

  it("hides delegate from the tools picker", () => {
    render(<AgentEditor />);

    // calculator and file should appear as checkboxes
    expect(screen.getByRole("checkbox", { name: "Calculator" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "File" })).toBeTruthy();

    // delegate should NOT appear even though useTools returns it
    expect(screen.queryByRole("checkbox", { name: "Subagents" })).toBeNull();
  });

  it("allows a single agent to enable and disable fresh self subagents", async () => {
    const { updateAgent } = await loadRealStore();

    render(<AgentEditor />);
    const self = screen.getByRole("checkbox", {
      name: /this agent/i,
    });
    expect(self).not.toBeChecked();
    fireEvent.click(self);
    expect(updateAgent).toHaveBeenLastCalledWith(
      "test_agent",
      expect.objectContaining({ delegate_to: ["test_agent"] }),
    );
    fireEvent.click(self);
    expect(updateAgent).toHaveBeenLastCalledWith(
      "test_agent",
      expect.objectContaining({ delegate_to: [] }),
    );
  });

  it("updates tools when checkboxes are toggled", async () => {
    const { updateAgent } = await loadRealStore();

    render(<AgentEditor />);

    // Find the calculator checkbox (should be checked) — use exact name to
    // distinguish from the worker-tools checkboxes which have "worker ..." labels
    const calculatorCheckbox = screen.getByRole("checkbox", {
      name: "Calculator",
    });
    expect(calculatorCheckbox).toBeChecked();

    // Uncheck it
    fireEvent.click(calculatorCheckbox);

    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        tools: [],
      }),
    );

    // Check another tool
    const fileCheckbox = screen.getByRole("checkbox", { name: "File" });
    fireEvent.click(fileCheckbox);

    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        tools: ["file"],
      }),
    );
  });

  it("updates skills when checkboxes are toggled", async () => {
    const { updateAgent } = await loadRealStore();

    render(<AgentEditor />);

    const debuggingCheckbox = await screen.findByRole("checkbox", {
      name: /debugging/i,
    });
    expect(debuggingCheckbox).toBeChecked();

    fireEvent.click(debuggingCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        skills: [],
      }),
    );

    const codeReviewCheckbox = screen.getByRole("checkbox", {
      name: /code-review/i,
    });
    fireEvent.click(codeReviewCheckbox);
    expect(updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        skills: ["code-review"],
      }),
    );
  });

  it("renders missing assigned skills so they can be removed", () => {
    mockStoreWith({ ...mockAgent, skills: ["ghost-skill"] });

    render(<AgentEditor />);

    const ghostSkillCheckbox = screen.getByRole("checkbox", {
      name: /ghost-skill/i,
    });
    expect(ghostSkillCheckbox).toBeChecked();

    fireEvent.click(ghostSkillCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        skills: [],
      }),
    );
  });

  it("writes unchecked boolean toggles as false", async () => {
    const { store } = await loadRealStore({
      ...mockAgentConfig,
      allow_self_config: true,
    });

    render(<AgentEditor />);
    expect(screen.getByLabelText("worker scheduler")).toBeInTheDocument();
    for (const label of [
      "Include default tools",
      "Enable markdown",
      "Show tool calls inline",
      "Enable self-configuration",
    ]) {
      fireEvent.click(screen.getByLabelText(label));
    }

    expect(store.getState().agents[0]).toMatchObject({
      include_default_tools: false,
      markdown: false,
      show_tool_calls: false,
      allow_self_config: false,
    });
    expect(screen.queryByLabelText("worker scheduler")).not.toBeInTheDocument();
  });

  it("handles model selection", () => {
    render(<AgentEditor />);

    // Open the select dropdown
    const modelSelect = screen.getByLabelText("Model");
    fireEvent.click(modelSelect);

    // Select a different model
    const customOption = screen.getByRole("option", { name: "custom" });
    fireEvent.click(customOption);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        model: "custom",
      }),
    );
  });

  it.each([
    {
      name: "updates memory backend when selected",
      agent: mockAgent,
      option: "File (markdown memory)",
      expected: "file",
    },
    {
      name: "updates memory backend to disabled when none is selected",
      agent: mockAgent,
      option: "Disabled (stateless)",
      expected: "none",
    },
    {
      name: "clears memory backend override when inherit is selected",
      agent: { ...mockAgent, memory_backend: "file" as const },
      option: /Inherit global/i,
      expected: undefined,
    },
  ])("$name", ({ agent, option, expected }) => {
    mockStoreWith(agent);

    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText("Memory Backend"));
    fireEvent.click(screen.getByRole("option", { name: option }));

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      "test_agent",
      expect.objectContaining({
        memory_backend: expected,
      }),
    );
  });

  describe("worker_tools inheritance", () => {
    const twoToolAgent = { ...mockAgent, tools: ["calculator", "file"] };

    it("shows inherited defaults as checked with (default) label", () => {
      mockStoreWith(twoToolAgent, {
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, worker_tools: ["calculator"] },
        },
      });

      render(<AgentEditor />);

      const workerCalc = screen.getByRole("checkbox", {
        name: "worker calculator",
      });
      expect(workerCalc).toBeChecked();
      expect(screen.getByText("calculator (default)")).toBeTruthy();

      const workerFile = screen.getByRole("checkbox", { name: "worker file" });
      expect(workerFile).not.toBeChecked();
    });

    it("seeds from defaults on first toggle so other defaults are preserved", () => {
      mockStoreWith(twoToolAgent, {
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, worker_tools: ["calculator"] },
        },
      });

      render(<AgentEditor />);

      // Toggle file ON — should seed from defaults first, so calculator stays
      const workerFile = screen.getByRole("checkbox", { name: "worker file" });
      fireEvent.click(workerFile);

      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        "test_agent",
        expect.objectContaining({
          worker_tools: ["calculator", "file"],
        }),
      );
    });

    it("renders empty list as explicit disable (all unchecked, no default labels)", () => {
      mockStoreWith(
        { ...twoToolAgent, worker_tools: [] },
        {
          config: {
            ...mockConfig,
            defaults: { ...mockConfig.defaults, worker_tools: ["calculator"] },
          },
        },
      );

      render(<AgentEditor />);

      const workerCalc = screen.getByRole("checkbox", {
        name: "worker calculator",
      });
      expect(workerCalc).not.toBeChecked();

      const workerFile = screen.getByRole("checkbox", { name: "worker file" });
      expect(workerFile).not.toBeChecked();

      // No worker tool label should show "(default)"
      expect(screen.queryByText("calculator (default)")).toBeNull();
      expect(screen.queryByText("file (default)")).toBeNull();
    });
  });

  it("writes one store update per keystroke and shows the store value", async () => {
    const { store, updateAgent } = await loadRealStore();

    render(<AgentEditor />);
    const displayNameInput = screen.getByLabelText("Display Name");
    for (let i = 0; i < 10; i++) {
      fireEvent.change(displayNameInput, { target: { value: `Updated ${i}` } });
    }

    expect(updateAgent).toHaveBeenCalledTimes(10);
    expect(updateAgent).toHaveBeenLastCalledWith("test_agent", {
      display_name: "Updated 9",
    });
    expect(store.getState().agents[0].display_name).toBe("Updated 9");
    expect(displayNameInput).toHaveValue("Updated 9");
  });

  it("edits fields the editor does not render through More settings", () => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          AgentConfig: {
            type: "object",
            properties: {
              display_name: { type: "string" },
              worker_scope: {
                anyOf: [
                  { enum: ["shared", "user", "user_agent"], type: "string" },
                  { type: "null" },
                ],
                default: null,
              },
              participation: {
                anyOf: [
                  { $ref: "#/$defs/ParticipationConfig" },
                  { type: "null" },
                ],
                default: null,
                description: "Opt-in adaptive participation",
              },
            },
          },
          ParticipationConfig: {
            type: "object",
            properties: {
              debounce_seconds: { type: "number", default: 3 },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<AgentEditor />);
    const section = screen.getByRole("button", { name: /More settings/ });
    expect(section).toHaveTextContent("Worker scope, Participation");
    fireEvent.click(section);
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Configure participation" }),
    );

    expect(mockStore.updateAgent).toHaveBeenCalledWith("test_agent", {
      participation: {},
    });
  });

  it("edits compaction fields the editor does not render through More compaction settings", () => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          AgentConfig: { type: "object", properties: {} },
          CompactionOverrideConfig: {
            type: "object",
            properties: {
              model: { anyOf: [{ type: "string" }, { type: "null" }] },
              fallback_model: {
                anyOf: [{ type: "string" }, { type: "null" }],
                default: null,
                "x-mindroom": { reference: "model" },
              },
              timeout_seconds: {
                anyOf: [
                  { exclusiveMinimum: 0, type: "number" },
                  { type: "null" },
                ],
                default: null,
              },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<AgentEditor />);
    const section = screen.getByRole("button", {
      name: /More compaction settings/,
    });
    expect(section).toHaveTextContent("Fallback model, Timeout seconds");
    fireEvent.click(section);
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Timeout seconds" }),
      { target: { value: "30" } },
    );
    expect(mockStore.updateAgent).toHaveBeenLastCalledWith("test_agent", {
      compaction: { timeout_seconds: 30 },
    });
  });

  it("edits private knowledge chunking through More private knowledge settings", () => {
    const privateAgent: Agent = {
      ...mockAgent,
      private: {
        per: "user",
        knowledge: { enabled: true, path: "memory", watch: true },
      },
    };
    mockStoreWith(privateAgent, {
      agentPoliciesByAgent: makeAgentPolicies({
        is_private: true,
        effective_execution_scope: "user",
        scope_label: "private.per=user",
        scope_source: "private.per",
        private_workspace_enabled: true,
        private_agent_knowledge_enabled: true,
      }),
    });
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          AgentConfig: { type: "object", properties: {} },
          AgentPrivateKnowledgeConfig: {
            type: "object",
            properties: {
              path: { type: "string" },
              chunk_size: { type: "integer", default: 5000, minimum: 128 },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<AgentEditor />);
    fireEvent.click(
      screen.getByRole("button", { name: /More private knowledge settings/ }),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Chunk size" }), {
      target: { value: "2000" },
    });

    expect(mockStore.updateAgent).toHaveBeenLastCalledWith("test_agent", {
      private: {
        per: "user",
        knowledge: {
          enabled: true,
          path: "memory",
          watch: true,
          chunk_size: 2000,
        },
      },
    });
  });
});
