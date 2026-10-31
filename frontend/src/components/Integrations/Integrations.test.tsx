import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
  within,
} from "@testing-library/react";
import { Integrations } from "./Integrations";
import { useConfigStore } from "@/store/configStore";
import type { AgentPoliciesByAgent } from "@/types/config";

// Mock hooks
const mockTools = [
  {
    name: "weather",
    display_name: "Weather",
    description: "Get weather information",
    icon: "🌤️",
    icon_color: null,
    category: "information",
    status: "available",
    setup_type: "api_key",
    config_fields: [
      {
        name: "WEATHER_API_KEY",
        label: "API Key",
        type: "password",
        required: true,
        placeholder: "Enter your weather API key",
        description: "Your weather service API key",
      },
    ],
    helper_text: null,
    docs_url: null,
    dependencies: null,
  },
];
const scopedMockTools = [
  {
    name: "private_mail",
    display_name: "Private Mail",
    description: "Private scoped mail integration",
    icon: "📫",
    icon_color: null,
    category: "communication",
    status: "available",
    setup_type: "api_key",
    config_fields: [
      {
        name: "PRIVATE_MAIL_API_KEY",
        label: "API Key",
        type: "password",
        required: true,
        placeholder: "Enter your private mail API key",
        description: "Scoped API key",
      },
    ],
    helper_text: null,
    docs_url: null,
    dependencies: null,
  },
];
let mockStatusAuthoritative = true;
const {
  mockUseTools,
  mockOAuthLoadStatus,
  mockOAuthConnect,
  mockOAuthDisconnect,
  mockOAuthProviderCreated,
  mockHomeAssistantLoadStatus,
  mockHomeAssistantDisconnect,
  mockHomeAssistantSetup,
  mockEnhancedConfigDialogProps,
} = vi.hoisted(() => ({
  mockUseTools: vi.fn(),
  mockOAuthLoadStatus: vi.fn(),
  mockOAuthConnect: vi.fn(),
  mockOAuthDisconnect: vi.fn(),
  mockOAuthProviderCreated: vi.fn(),
  mockHomeAssistantLoadStatus: vi.fn(),
  mockHomeAssistantDisconnect: vi.fn(),
  mockHomeAssistantSetup: vi.fn(),
  mockEnhancedConfigDialogProps: vi.fn(),
}));

const sharedScope = { agentName: null, executionScope: null };
const privateScope = { agentName: "mind", executionScope: "user" };

function oauthStatus(providerId: string, status: Record<string, unknown>) {
  mockOAuthLoadStatus.mockImplementation(async (id: string) =>
    id === providerId ? status : { status: "available" },
  );
}

function googleCloudTool(name: string, displayName: string, field: string) {
  return {
    name,
    display_name: displayName,
    description: `${displayName} on the shared Google Cloud connection`,
    icon: "SiGooglecloud",
    icon_color: "text-blue-600",
    category: "development",
    status: "available",
    setup_type: "oauth",
    auth_provider: "google_cloud",
    config_fields: [
      { name: field, label: field, type: "text", required: true },
    ],
    helper_text: null,
    docs_url: null,
    dependencies: null,
  };
}

function createDeferred() {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((promiseResolve) => {
    resolve = () => promiseResolve();
  });
  return { promise, resolve };
}

function makeAgentPolicy(
  agentName: string,
  overrides: Partial<AgentPoliciesByAgent[string]> = {},
): AgentPoliciesByAgent[string] {
  return {
    agent_name: agentName,
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
  };
}

vi.mock("@/hooks/useTools", () => ({
  useTools: mockUseTools,
  mapToolToIntegration: (tool: any) => ({
    id: tool.name,
    name: tool.display_name,
    description: tool.description,
    category: tool.category,
    status: tool.status,
    setup_type: tool.setup_type,
    config_fields: tool.config_fields,
    helper_text: tool.helper_text,
    docs_url: tool.docs_url,
    oauth_fallback_fields: tool.oauth_fallback_fields,
    manual_auth_configured: tool.manual_auth_configured,
    environment_auth_configured: tool.environment_auth_configured,
  }),
}));

// Mock toast
const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock icon mapping
vi.mock("./iconMapping", () => ({
  getIconForTool: (icon: string | null, _iconColor?: string | null) => (
    <span>{icon}</span>
  ),
}));

// Mock API base URL
vi.mock("@/lib/api", () => ({
  API_BASE_URL: "http://localhost:8080",
  withAgentExecutionScope: (url: string) => url,
}));

// Mock EnhancedConfigDialog
vi.mock("./EnhancedConfigDialog", () => ({
  EnhancedConfigDialog: ({
    configFields,
    onSuccess,
    service,
    docsUrl,
    helperText,
  }: any) => {
    mockEnhancedConfigDialogProps({ configFields, service });
    // Auto-call success when dialog opens
    setTimeout(() => onSuccess?.(), 0);
    return (
      <>
        <div>Enhanced Config Dialog</div>
        <div>Service: {service}</div>
        <div>Fields: {JSON.stringify(configFields)}</div>
        <div>Docs: {docsUrl}</div>
        <div>Helper: {helperText}</div>
      </>
    );
  },
}));

// Every OAuth card goes through the generic provider; Home Assistant has its own.
vi.mock("./integrations/index", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./integrations/index")>()),
  GenericOAuthIntegrationProvider: class {
    constructor(
      private readonly providerId: string,
      displayName: string,
    ) {
      mockOAuthProviderCreated(providerId, displayName);
    }

    connect(scope?: unknown) {
      return mockOAuthConnect(this.providerId, scope);
    }

    disconnect(scope?: unknown) {
      return mockOAuthDisconnect(this.providerId, scope);
    }

    loadStatus(scope?: unknown) {
      return mockOAuthLoadStatus(this.providerId, scope);
    }
  },
}));
vi.mock("./integrations/homeassistant", () => ({
  homeAssistantIntegration: {
    SetupComponent: (props: { onSuccess: () => void }) => {
      mockHomeAssistantSetup(props);
      return (
        <button onClick={props.onSuccess}>Finish Home Assistant setup</button>
      );
    },
    disconnect: mockHomeAssistantDisconnect,
    loadStatus: mockHomeAssistantLoadStatus,
  },
}));

describe("Integrations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockToast.mockReset();
    mockStatusAuthoritative = true;
    oauthStatus("", {});
    mockOAuthConnect.mockResolvedValue(undefined);
    mockOAuthDisconnect.mockResolvedValue(undefined);
    mockHomeAssistantLoadStatus.mockResolvedValue({
      status: "available",
    });
    mockHomeAssistantDisconnect.mockResolvedValue(undefined);
    mockUseTools.mockImplementation(
      (agentName?: string | null, executionScope?: string | null) => ({
        tools:
          agentName === "mind" && executionScope === "user"
            ? scopedMockTools
            : mockTools,
        loading: false,
        refetch: vi.fn(),
        statusAuthoritative: mockStatusAuthoritative,
      }),
    );
    useConfigStore.setState({ agents: [], agentPoliciesByAgent: {} });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
      configurable: true,
      value: () => false,
    });
  });

  it("should render integrations list", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Tools")).toBeInTheDocument();
      expect(
        screen.getByText(
          "Connect external services to enable agent capabilities",
        ),
      ).toBeInTheDocument();
    });
  });

  it("discloses Google data handling before connection", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getAllByText(
          /By connecting, you authorize this MindRoom installation/,
        ).length,
      ).toBeGreaterThan(0);
      expect(
        screen.getAllByText(
          /Project maintainers have no automatic access merely because they maintain MindRoom; if they also operate this installation, they may have operator access/,
        ).length,
      ).toBeGreaterThan(0);
      expect(
        screen.getAllByText(
          /The installation operator and anyone with administrative or filesystem access may be able to access stored credentials and data/,
        ).length,
      ).toBeGreaterThan(0);
      expect(
        screen.getAllByText(
          /Shared or unscoped agents can use the connected account for any user authorized to invoke them/,
        ).length,
      ).toBeGreaterThan(0);
    });
    for (const link of screen.getAllByRole("link", {
      name: "Privacy policy",
    })) {
      expect(link).toHaveAttribute(
        "href",
        "https://docs.mindroom.chat/privacy/",
      );
    }
  });

  it("discloses data handling for dynamically registered Google OAuth providers", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        ...mockTools,
        {
          name: "google_photos_tool",
          display_name: "Google Photos Tool",
          description: "Manage Google Photos",
          icon: "✅",
          icon_color: null,
          category: "productivity",
          status: "available",
          setup_type: "oauth",
          config_fields: null,
          helper_text: null,
          docs_url: null,
          dependencies: null,
          auth_provider: "google_photos",
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Google Photos")).toBeInTheDocument();
      expect(
        screen.getAllByText(
          /Project maintainers have no automatic access merely because they maintain MindRoom/,
        ),
      ).toHaveLength(8);
    });
  });

  it("should display all integration cards", async () => {
    render(<Integrations />);

    await waitFor(() => {
      // Provider integrations
      expect(screen.getByText("Google Drive")).toBeInTheDocument();
      expect(
        screen.getByText(
          "Search, read, upload, and organize files in your connected Google Drive",
        ),
      ).toBeInTheDocument();
      expect(screen.getByText("Home Assistant")).toBeInTheDocument();
      expect(
        screen.getByText("Control and monitor your smart home devices"),
      ).toBeInTheDocument();

      // Backend tools
      expect(screen.getByText("Weather")).toBeInTheDocument();
      expect(screen.getByText("Get weather information")).toBeInTheDocument();
    });
  });

  it("orders built-in provider cards, then discovered providers, then tool cards", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        ...mockTools,
        {
          name: "acme_drive",
          display_name: "Acme Drive",
          description: "Search Acme Drive files",
          icon: "AcmeIcon",
          icon_color: null,
          category: "productivity",
          status: "requires_config",
          setup_type: "oauth",
          auth_provider: "acme_drive",
          config_fields: null,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const panel = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(
        within(panel)
          .getAllByRole("heading", { level: 2 })
          .map((heading) => heading.textContent),
      ).toEqual([
        "Google Calendar",
        "Google Cloud",
        "Google Docs",
        "Google Drive",
        "Gmail",
        "Google Sheets",
        "Google Tasks",
        "Home Assistant",
        "Acme Drive",
        "Weather",
      ]);
    });
    expect(mockOAuthProviderCreated).toHaveBeenCalledWith(
      "google_cloud",
      "Google Cloud",
    );
    expect(mockOAuthProviderCreated).toHaveBeenCalledWith(
      "acme_drive",
      "Acme Drive",
    );
  });

  it.each([
    ["while tools load", true],
    ["when no tools are listed", false],
  ])("shows built-in provider cards %s", async (_label, loading) => {
    mockUseTools.mockReturnValue({
      tools: [],
      loading,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    expect(await screen.findByText("Google Drive")).toBeInTheDocument();
    expect(screen.getByText("Home Assistant")).toBeInTheDocument();
  });

  it("takes a built-in card's settings from the tool named after its provider", async () => {
    oauthStatus("google_drive", {
      status: "connected",
    });
    const driveTool = (name: string, field: string) => ({
      name,
      display_name: name,
      description: `${name} on Google Drive`,
      icon: "SiGoogledrive",
      icon_color: "text-green-600",
      category: "productivity",
      status: "available",
      setup_type: "oauth",
      auth_provider: "google_drive",
      config_fields: [{ name: field, label: field, type: "text" }],
      helper_text: null,
      docs_url: null,
      dependencies: null,
    });
    mockUseTools.mockReturnValue({
      tools: [
        driveTool("drive_extra", "extra_field"),
        driveTool("google_drive", "drive_field"),
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const driveCard = (await screen.findByText("Google Drive")).closest(
      ".h-full",
    ) as HTMLElement;
    expect(screen.queryByText("drive_extra")).not.toBeInTheDocument();
    fireEvent.click(
      await within(driveCard).findByRole("button", { name: "Edit" }),
    );

    await waitFor(() => {
      expect(mockEnhancedConfigDialogProps).toHaveBeenLastCalledWith({
        service: "google_drive",
        configFields: [expect.objectContaining({ name: "drive_field" })],
      });
    });
  });

  const acmeTool = (name: string, displayName: string) => ({
    name,
    display_name: displayName,
    description: `${displayName} on the Acme connection`,
    icon: "AcmeIcon",
    icon_color: "text-cyan-600",
    category: "research",
    status: "requires_config",
    setup_type: "oauth",
    auth_provider: "acme",
    config_fields: [{ name: `${name}_field`, label: "Field", type: "text" }],
    helper_text: null,
    docs_url: null,
    dependencies: null,
  });

  it("groups a discovered provider shared by several tools into one card and keeps each tool's card", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        acmeTool("acme_files", "Acme Files"),
        acmeTool("acme_mail", "Acme Mail"),
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const panel = await screen.findByRole("tabpanel");
    await waitFor(() => {
      expect(
        within(panel)
          .getAllByRole("heading", { level: 2 })
          .map((heading) => heading.textContent)
          .slice(-3),
      ).toEqual(["Acme", "Acme Files", "Acme Mail"]);
    });
    expect(
      screen.getByText("Connect Acme OAuth for this tool."),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Requires Acme")).toHaveLength(2);
    // Without a tool standing for it, the card falls back to the default category.
    expect(
      within(screen.getByRole("tab", { name: /Research/ })).getByText("2"),
    ).toBeInTheDocument();
    expect(mockOAuthLoadStatus).toHaveBeenCalledWith("acme", sharedScope);
  });

  it("gives a discovered provider with a same-named tool one card for all its tools", async () => {
    oauthStatus("acme", {
      status: "connected",
    });
    mockUseTools.mockReturnValue({
      tools: [
        acmeTool("acme", "Acme Suite"),
        acmeTool("acme_mail", "Acme Mail"),
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const acmeCard = (await screen.findByText("Acme Suite")).closest(
      ".h-full",
    ) as HTMLElement;
    expect(screen.queryByText("Acme Mail")).not.toBeInTheDocument();
    expect(
      screen.getByText("Connect Acme Suite OAuth for Acme Suite."),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("tab", { name: /Research/ })).getByText("1"),
    ).toBeInTheDocument();
    fireEvent.click(within(acmeCard).getByRole("button", { name: "Edit" }));

    await waitFor(() => {
      expect(mockEnhancedConfigDialogProps).toHaveBeenLastCalledWith({
        service: "acme",
        configFields: [expect.objectContaining({ name: "acme_field" })],
      });
    });
  });

  it("takes a discovered card's settings from the tool named after its provider", async () => {
    oauthStatus("acme", { status: "connected" });
    mockUseTools.mockReturnValue({
      tools: [
        acmeTool("acme_mail", "Acme Mail"),
        acmeTool("acme", "Acme Suite"),
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const acmeCard = (await screen.findByText("Acme Suite")).closest(
      ".h-full",
    ) as HTMLElement;
    expect(screen.queryByText("Acme Mail")).not.toBeInTheDocument();
    fireEvent.click(within(acmeCard).getByRole("button", { name: "Edit" }));

    await waitFor(() => {
      expect(mockEnhancedConfigDialogProps).toHaveBeenLastCalledWith({
        service: "acme",
        configFields: [expect.objectContaining({ name: "acme_field" })],
      });
    });
  });

  const acmeDriveTool = {
    name: "acme_drive",
    display_name: "Acme Drive",
    description: "Search Acme Drive files",
    icon: "AcmeIcon",
    icon_color: "text-cyan-600",
    category: "productivity",
    status: "requires_config",
    setup_type: "oauth",
    auth_provider: "acme_drive",
    config_fields: null,
    helper_text: "Acme tool helper text.",
    docs_url: null,
    dependencies: null,
  };
  const providerCardSources = [
    {
      source: "built-in",
      name: "Google Drive",
      tools: [
        {
          ...acmeDriveTool,
          name: "google_drive",
          display_name: "Google Drive",
          auth_provider: "google_drive",
          helper_text: "Drive tool helper text.",
        },
      ],
      toolHelperText: "Drive tool helper text.",
      setStatus: (status: Record<string, unknown>) =>
        oauthStatus("google_drive", status),
    },
    {
      source: "discovered",
      name: "Acme Drive",
      tools: [acmeDriveTool],
      toolHelperText: "Acme tool helper text.",
      setStatus: (status: Record<string, unknown>) =>
        oauthStatus("acme_drive", status),
    },
  ];

  it.each(providerCardSources)(
    "shows a $source card's status error over its tool helper text",
    async ({ name, tools, setStatus }) => {
      setStatus({
        status: "not_connected",
        oauth_client_configured: true,
        status_error: "Requester binding failed.",
        helper_text: "Requester binding failed.",
      });
      mockUseTools.mockReturnValue({
        tools,
        loading: false,
        refetch: vi.fn(),
        statusAuthoritative: true,
      });

      render(<Integrations />);

      const card = (await screen.findByText(name)).closest(
        ".h-full",
      ) as HTMLElement;
      await waitFor(() => {
        expect(
          within(card).getByText("Status error: Requester binding failed."),
        ).toBeInTheDocument();
      });
      expect(within(card).getByText("Status error")).toBeInTheDocument();
      expect(
        within(card).getByRole("button", { name: "Retry status" }),
      ).toBeInTheDocument();
    },
  );

  it.each(providerCardSources)(
    "offers a $source card's reset with the status helper text, else its tool's",
    async ({ name, tools, toolHelperText, setStatus }) => {
      setStatus({
        status: "not_connected",
        oauth_client_configured: true,
        oauth_reset_required: true,
      });
      mockUseTools.mockReturnValue({
        tools,
        loading: false,
        refetch: vi.fn(),
        statusAuthoritative: true,
      });

      render(<Integrations />);

      const card = (await screen.findByText(name)).closest(
        ".h-full",
      ) as HTMLElement;
      expect(
        await within(card).findByRole("button", { name: "Reset connection" }),
      ).toBeInTheDocument();
      expect(within(card).getByText(toolHelperText)).toBeInTheDocument();
    },
  );

  it("merges the Home Assistant tool into the Home Assistant card", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        ...mockTools,
        {
          name: "homeassistant",
          display_name: "Home Assistant",
          description: "Control Home Assistant devices",
          icon: "Home",
          icon_color: "text-blue-500",
          category: "smart_home",
          status: "requires_config",
          setup_type: "special",
          config_fields: [
            { name: "HOMEASSISTANT_URL", label: "URL", type: "url" },
          ],
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });
    expect(screen.getAllByText("Home Assistant")).toHaveLength(1);
    expect(
      screen.getByText("Control and monitor your smart home devices"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Control Home Assistant devices"),
    ).not.toBeInTheDocument();
  });

  it("opens a provider's setup component in a dialog titled by its card", async () => {
    const refetch = vi.fn();
    mockUseTools.mockReturnValue({
      tools: mockTools,
      loading: false,
      refetch,
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const homeAssistantCard = (
      await screen.findByText("Home Assistant")
    ).closest(".h-full") as HTMLElement;
    fireEvent.click(
      within(homeAssistantCard).getByRole("button", { name: "Setup" }),
    );

    const dialog = await screen.findByRole("dialog");
    const title = within(dialog).getByText("Home Assistant Setup");
    expect(title.querySelector("svg")).not.toBeNull();
    expect(
      within(dialog).getByText("Control and monitor your smart home devices"),
    ).toBeInTheDocument();

    expect(mockHomeAssistantSetup).toHaveBeenLastCalledWith(
      expect.objectContaining(sharedScope),
    );
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "Finish Home Assistant setup",
      }),
    );

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(refetch).toHaveBeenCalledOnce();
    });
  });

  it("sets up Home Assistant for an agent with a shared runtime scope", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "code",
          display_name: "Scoped Agent",
          role: "test",
          tools: ["homeassistant"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: "shared",
        },
      ],
      agentPoliciesByAgent: {
        code: makeAgentPolicy("code", {
          effective_execution_scope: "shared",
          scope_label: "worker_scope=shared",
          scope_source: "agent.worker_scope",
        }),
      },
    });

    render(<Integrations />);

    fireEvent.keyDown(screen.getByRole("combobox"), {
      key: "ArrowDown",
      code: "ArrowDown",
    });
    fireEvent.click(await screen.findByText("Scoped Agent"));
    await screen.findByText(
      "Configuring tools for Scoped Agent (worker_scope=shared).",
    );
    const homeAssistantCard = (
      await screen.findByText("Home Assistant")
    ).closest(".h-full") as HTMLElement;
    fireEvent.click(
      within(homeAssistantCard).getByRole("button", { name: "Setup" }),
    );

    await screen.findByRole("dialog");
    expect(mockHomeAssistantSetup).toHaveBeenLastCalledWith(
      expect.objectContaining({ agentName: "code", executionScope: "shared" }),
    );
  });

  it("should show correct status badges", async () => {
    mockHomeAssistantLoadStatus.mockResolvedValue({
      status: "connected",
    });

    render(<Integrations />);

    await waitFor(() => {
      // Available integrations (the Google cards and Weather)
      expect(screen.getAllByText("Available").length).toBeGreaterThanOrEqual(2);

      // Connected integration
      expect(screen.getByText("Connected")).toBeInTheDocument(); // Home Assistant
    });
  });

  it("shows OAuth status errors without client-config wording", async () => {
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: false,
      status_error: "Requester binding failed.",
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getByText("Status error: Requester binding failed."),
      ).toBeInTheDocument();
      expect(screen.getByText("Status error")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Retry status" }),
      ).toBeInTheDocument();
      expect(screen.queryByText("Needs client config")).not.toBeInTheDocument();
    });
  });

  it("offers a reset action when OAuth credentials are unreadable", async () => {
    const refetch = vi.fn();
    const disconnect = createDeferred();
    mockOAuthDisconnect.mockReturnValueOnce(disconnect.promise);
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "google_drive",
          display_name: "Google Drive",
          description: "Google Drive with a manual fallback",
          icon: "Google Drive Icon",
          icon_color: null,
          category: "productivity",
          status: "available",
          setup_type: "oauth",
          config_fields: [
            {
              name: "credentials_json",
              label: "Credentials JSON",
              type: "password",
              required: false,
            },
          ],
          oauth_fallback_fields: ["credentials_json"],
          manual_auth_configured: true,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch,
      statusAuthoritative: true,
    });
    global.fetch = vi.fn().mockResolvedValue({ ok: true });
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: true,
      oauth_client_config_service: "google_drive_oauth_client",
      oauth_reset_required: true,
      helper_text:
        "Stored OAuth credentials cannot be read. Reset the connection to reconnect.",
    });

    render(<Integrations />);

    const resetButton = await screen.findByRole("button", {
      name: "Reset connection",
    });
    expect(
      screen.queryByRole("button", { name: "Connect with Google Drive" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Stored OAuth credentials cannot be read. Reset the connection to reconnect.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Edit credentials json" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove credentials json" }),
    ).toBeInTheDocument();

    fireEvent.click(resetButton);

    await waitFor(() => {
      expect(mockOAuthDisconnect).toHaveBeenCalledExactlyOnceWith(
        "google_drive",
        sharedScope,
      );
      const pendingResetButton = screen.getByRole("button", {
        name: "Reset connection",
      });
      expect(pendingResetButton).toBeDisabled();
      expect(pendingResetButton.querySelector(".animate-spin")).not.toBeNull();
    });

    await act(async () => disconnect.resolve());

    await waitFor(() => {
      expect(refetch).toHaveBeenCalledOnce();
      expect(mockToast).toHaveBeenCalledWith({
        title: "Connection reset",
        description: "Google Drive OAuth has been reset.",
      });
    });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("offers reset recovery when no fallback credential is configured", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "google_drive",
          display_name: "Google Drive",
          description: "Google Drive with an optional manual fallback",
          icon: "Google Drive Icon",
          icon_color: null,
          category: "productivity",
          status: "available",
          setup_type: "oauth",
          config_fields: [
            {
              name: "credentials_json",
              label: "Credentials JSON",
              type: "password",
              required: false,
            },
          ],
          oauth_fallback_fields: ["credentials_json"],
          manual_auth_configured: false,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: true,
      oauth_reset_required: true,
      helper_text:
        "Stored OAuth credentials cannot be read. Reset the connection to reconnect.",
    });

    render(<Integrations />);

    expect(
      await screen.findByRole("button", { name: "Reset connection" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Use credentials json" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Stored OAuth credentials cannot be read. Reset the connection to reconnect.",
      ),
    ).toBeInTheDocument();
  });

  it("opens OAuth client config dialog when client config is missing", async () => {
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: false,
      oauth_client_config_service: "google_drive_oauth_client",
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Configure client" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Configure client" }));

    await waitFor(() => {
      expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
      expect(
        screen.getByText("Service: google_drive_oauth_client"),
      ).toBeInTheDocument();
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /"required":true/,
      );
    });
  });

  it("opens OAuth client config dialog for existing client config", async () => {
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: true,
      oauth_custom_client_configured: true,
      oauth_client_config_service: "google_drive_oauth_client",
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Edit client" }),
      ).toBeInTheDocument();
      expect(
        screen.getAllByRole("button", { name: "Connect" }).length,
      ).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit client" }));

    await waitFor(() => {
      expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
      expect(
        screen.getByText("Service: google_drive_oauth_client"),
      ).toBeInTheDocument();
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /"required":false/,
      );
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /"requiredWhenFieldChanges":"client_id"/,
      );
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /Required when changing Client ID/,
      );
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /The saved secret is kept for edits that do not change the Client ID/,
      );
    });
  });

  it("omits redirect URI from shared OAuth client config dialog", async () => {
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: true,
      oauth_custom_client_configured: true,
      oauth_client_config_service: "google_oauth_client",
      oauth_client_redirect_uri_supported: false,
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Edit client" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit client" }));

    await waitFor(() => {
      expect(mockEnhancedConfigDialogProps).toHaveBeenCalledWith(
        expect.objectContaining({
          service: "google_oauth_client",
          configFields: [
            expect.objectContaining({ name: "client_id" }),
            expect.objectContaining({ name: "client_secret" }),
          ],
        }),
      );
      const dialogCalls = mockEnhancedConfigDialogProps.mock.calls;
      expect(dialogCalls[dialogCalls.length - 1]?.[0].configFields).not.toEqual(
        expect.arrayContaining([
          expect.objectContaining({ name: "redirect_uri" }),
        ]),
      );
    });
  });

  it("offers a custom client without blocking provisioned OAuth", async () => {
    oauthStatus("google_drive", {
      status: "available",
      oauth_client_configured: true,
      oauth_custom_client_configured: false,
      oauth_client_config_service: "google_drive_oauth_client",
      oauth_client_redirect_uri_supported: true,
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: "Connect" }).length,
      ).toBeGreaterThan(0);
      expect(
        screen.getByRole("button", { name: "Use custom client" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Configure client" }),
      ).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Use custom client" }));

    await waitFor(() => {
      expect(screen.getByText(/"name":"client_secret"/)).toHaveTextContent(
        /"required":true/,
      );
      expect(mockEnhancedConfigDialogProps).toHaveBeenCalledWith(
        expect.objectContaining({
          service: "google_drive_oauth_client",
          configFields: expect.arrayContaining([
            expect.objectContaining({ name: "redirect_uri" }),
          ]),
        }),
      );
    });
  });

  it("uses the API origin for OAuth client redirect URI placeholders", async () => {
    oauthStatus("google_drive", {
      status: "not_connected",
      oauth_client_configured: false,
      oauth_client_config_service: "google_drive_oauth_client",
      oauth_client_redirect_uri_supported: true,
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Configure client" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Configure client" }));

    await waitFor(() => {
      const dialogCalls = mockEnhancedConfigDialogProps.mock.calls;
      const configFields =
        dialogCalls[dialogCalls.length - 1]?.[0].configFields;

      expect(configFields).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            name: "redirect_uri",
            placeholder:
              "http://localhost:8080/api/oauth/google_drive/callback",
          }),
        ]),
      );
    });
  });

  it("should filter integrations by search term", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Google Drive")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search tools...");
    fireEvent.change(searchInput, { target: { value: "gmail" } });

    await waitFor(() => {
      expect(screen.getByText("Gmail")).toBeInTheDocument();
      expect(screen.queryByText("Google Drive")).not.toBeInTheDocument();
      expect(screen.queryByText("Home Assistant")).not.toBeInTheDocument();
    });
  });

  it("should filter by availability", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });

    // Click "Available" filter button
    const availableButton = screen.getByRole("button", { name: "Available" });
    fireEvent.click(availableButton);

    await waitFor(() => {
      expect(screen.getByText("Google Drive")).toBeInTheDocument(); // Available
    });
  });

  it("should display category tabs", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /All/ })).toBeInTheDocument();
      expect(
        screen.getByRole("tab", { name: /Email & Calendar/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("tab", { name: /Entertainment/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("tab", { name: /Information/ }),
      ).toBeInTheDocument();
    });
  });

  it("uses wrapping layout classes for narrow viewports", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Tools")).toBeInTheDocument();
    });

    const headerRow = screen.getByText("Tools").parentElement;
    expect(headerRow).toHaveClass("flex-wrap");

    const scopeSelector = screen.getByRole("combobox");
    expect(scopeSelector).toHaveClass("w-full", "sm:w-72");

    const controlsRow = scopeSelector.parentElement;
    expect(controlsRow).toHaveClass("flex-wrap", "w-full");

    const searchInput = screen.getByPlaceholderText("Search tools...");
    expect(searchInput).toHaveClass("w-full", "sm:w-64");

    const tabList = screen.getByRole("tablist");
    expect(tabList).toHaveClass("flex-wrap", "h-auto", "overflow-visible");
  });

  it("should show correct action buttons", async () => {
    mockHomeAssistantLoadStatus.mockResolvedValue({
      status: "connected",
    });

    render(<Integrations />);

    await waitFor(() => {
      // OAuth type
      const connectButtons = screen.getAllByRole("button", { name: /Connect/ });
      expect(connectButtons.length).toBeGreaterThan(0);

      // Connected integration
      const disconnectButtons = screen.getAllByRole("button", {
        name: /Disconnect/,
      });
      expect(disconnectButtons.length).toBeGreaterThan(0);
    });
  });

  it("should show config dialog for tools with config fields", async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });

    // Find the Weather card and its Configure button
    const weatherCard = screen.getByText("Weather").closest(".h-full");
    const configureButton = weatherCard?.querySelector("button:not(:disabled)");

    if (configureButton) {
      fireEvent.click(configureButton);

      await waitFor(() => {
        // Should show the Enhanced Config Dialog
        expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
      });
    }
  });

  it("reports a tool that needs configuration but lists no fields", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "empty_tool",
          display_name: "Empty Tool",
          description: "A tool without config fields",
          icon: null,
          icon_color: null,
          category: "information",
          status: "requires_config",
          setup_type: "api_key",
          config_fields: null,
          helper_text: null,
          docs_url: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const card = (await screen.findByText("Empty Tool")).closest(
      ".h-full",
    ) as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Configure" }));

    expect(mockToast).toHaveBeenCalledWith({
      title: "Configuration Error",
      description:
        "Empty Tool requires configuration but no fields are specified.",
      variant: "destructive",
    });
  });

  it("should handle disconnect action", async () => {
    mockHomeAssistantLoadStatus.mockResolvedValue({
      status: "connected",
    });

    render(<Integrations />);

    const homeAssistantCard = (
      await screen.findByText("Home Assistant")
    ).closest(".h-full") as HTMLElement;
    fireEvent.click(
      await within(homeAssistantCard).findByRole("button", {
        name: "Disconnect",
      }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Disconnected",
        description: "Home Assistant has been disconnected.",
      });
    });
    expect(mockHomeAssistantDisconnect).toHaveBeenCalledOnce();
  });

  it("lists worker-scoped and private agents in the scope selector", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "general",
          display_name: "Unscoped Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: null,
        },
        {
          id: "code",
          display_name: "Scoped Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: "shared",
        },
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user_agent",
          },
        },
      ],
      agentPoliciesByAgent: {
        general: makeAgentPolicy("general"),
        code: makeAgentPolicy("code", {
          effective_execution_scope: "shared",
          scope_label: "worker_scope=shared",
          scope_source: "agent.worker_scope",
        }),
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user_agent",
          scope_label: "private.per=user_agent",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Scoped Agent")).toBeInTheDocument();
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    expect(screen.queryByText("Unscoped Agent")).not.toBeInTheDocument();
  });

  it("treats defaults.worker_scope as inherited execution scope in the selector", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "general",
          display_name: "Inherited Scope Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: null,
        },
      ],
      agentPoliciesByAgent: {
        general: makeAgentPolicy("general", {
          effective_execution_scope: "user",
          scope_label: "worker_scope=user",
          scope_source: "defaults.worker_scope",
          dashboard_credentials_supported: false,
        }),
      },
      config: {
        memory: {
          backend: "mem0",
          embedder: {
            provider: "openai",
            config: { model: "text-embedding-3-small" },
          },
        },
        models: {
          default: { provider: "test", id: "test-model" },
        },
        agents: {
          general: {
            display_name: "Inherited Scope Agent",
            role: "test",
            tools: ["gmail"],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        defaults: {
          markdown: true,
          worker_scope: "user",
        },
        router: {
          model: "default",
        },
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Inherited Scope Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Inherited Scope Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Configuring tools for Inherited Scope Agent (worker_scope=user).",
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows requester-scoped status as preview only", async () => {
    mockStatusAuthoritative = false;
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Private Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(/Requester-scoped tool status is preview only/i),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/OAuth-backed integrations can be connected/i),
    ).toBeInTheDocument();
  });

  it("hides shared-only integrations for isolating worker scopes", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "code",
          display_name: "Scoped Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: "user",
        },
      ],
      agentPoliciesByAgent: {
        code: makeAgentPolicy("code", {
          effective_execution_scope: "user",
          scope_label: "worker_scope=user",
          scope_source: "agent.worker_scope",
          dashboard_credentials_supported: false,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });
    fireEvent.keyDown(combobox, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      expect(screen.getByText("Scoped Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Scoped Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(
          /dashboard credential setup, editing, and disconnect are only supported/i,
        ),
      ).toBeInTheDocument();
    });

    expect(
      screen.getByText(/home assistant and spotify remain shared-only/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("worker_scope=shared", { selector: "code" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Google Drive")).toBeInTheDocument();
    expect(screen.queryByText("Home Assistant")).not.toBeInTheDocument();
    expect(screen.queryByText("Weather")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /shared-only config/i }),
    ).toHaveLength(1);
    for (const button of screen.getAllByRole("button", {
      name: /shared-only config/i,
    })) {
      expect(button).toBeDisabled();
    }
  });

  it("hides the dynamic Spotify OAuth provider in isolating scopes", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        ...mockTools,
        {
          name: "spotify",
          display_name: "Spotify",
          description: "Music streaming service",
          icon: null,
          icon_color: null,
          category: "entertainment",
          status: "requires_config",
          setup_type: "oauth",
          auth_provider: "spotify",
          execution_scope_supported: false,
          config_fields: null,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      error: null,
      refetch: vi.fn(),
    });
    useConfigStore.setState({
      agents: [
        {
          id: "code",
          display_name: "Scoped Agent",
          role: "test",
          tools: ["spotify"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: "user",
        },
      ],
      agentPoliciesByAgent: {
        code: makeAgentPolicy("code", {
          effective_execution_scope: "user",
          scope_label: "worker_scope=user",
          scope_source: "agent.worker_scope",
          dashboard_credentials_supported: false,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });
    fireEvent.keyDown(combobox, { key: "Enter", code: "Enter" });
    await waitFor(() => {
      expect(screen.getByText("Scoped Agent")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Scoped Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(/home assistant and spotify remain shared-only/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/OAuth for Spotify/)).not.toBeInTheDocument();
  });

  it("hides every tool of a hidden shared-only provider in isolating scopes", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        ...mockTools,
        {
          name: "spotify_player",
          display_name: "Spotify Player",
          description: "Play music on Spotify",
          icon: null,
          icon_color: null,
          category: "entertainment",
          status: "requires_config",
          setup_type: "oauth",
          auth_provider: "spotify",
          execution_scope_supported: true,
          config_fields: null,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });
    useConfigStore.setState({
      agents: [
        {
          id: "code",
          display_name: "Scoped Agent",
          role: "test",
          tools: ["spotify_player"],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
          worker_scope: "user",
        },
      ],
      agentPoliciesByAgent: {
        code: makeAgentPolicy("code", {
          effective_execution_scope: "user",
          scope_label: "worker_scope=user",
          scope_source: "agent.worker_scope",
          dashboard_credentials_supported: false,
        }),
      },
    });

    render(<Integrations />);

    expect(await screen.findByText("Spotify Player")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("combobox"), {
      key: "ArrowDown",
      code: "ArrowDown",
    });
    fireEvent.click(await screen.findByText("Scoped Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(/home assistant and spotify remain shared-only/i),
      ).toBeInTheDocument();
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });
    expect(screen.queryByText("Spotify Player")).not.toBeInTheDocument();
  });

  it("treats private agents as isolating scopes in integrations", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Private Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Configuring tools for Private Agent (private.per=user).",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          /dashboard credential setup, editing, and disconnect are only supported/i,
        ),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("Google Drive")).toBeInTheDocument();
    expect(screen.queryByText("Home Assistant")).not.toBeInTheDocument();
    expect(screen.queryByText("Weather")).not.toBeInTheDocument();
    expect(screen.getByText("Private Mail")).toBeInTheDocument();
  });

  it("allows scoped OAuth providers to connect for private agents", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["google_drive"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Private Agent"));

    await waitFor(() => {
      expect(screen.getByText("Google Drive")).toBeInTheDocument();
    });

    const driveCard = screen.getByText("Google Drive").closest(".h-full");
    expect(driveCard).toBeInstanceOf(HTMLElement);
    fireEvent.click(
      within(driveCard as HTMLElement).getByRole("button", {
        name: /connect/i,
      }),
    );

    await waitFor(() => {
      expect(mockOAuthConnect).toHaveBeenCalledExactlyOnceWith(
        "google_drive",
        privateScope,
      );
    });
    expect(mockOAuthLoadStatus).toHaveBeenCalledWith(
      "google_drive",
      privateScope,
    );
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Shared-only dashboard configuration",
      }),
    );
  });

  it("disconnects a scoped OAuth connection for the selected agent", async () => {
    oauthStatus("google_drive", { status: "connected" });
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["google_drive"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    fireEvent.keyDown(screen.getByRole("combobox"), {
      key: "ArrowDown",
      code: "ArrowDown",
    });
    fireEvent.click(await screen.findByText("Private Agent"));
    await screen.findByText("Private Mail");

    const driveCard = screen
      .getByText("Google Drive")
      .closest(".h-full") as HTMLElement;
    fireEvent.click(
      within(driveCard).getByRole("button", { name: "Disconnect" }),
    );

    await waitFor(() => {
      expect(mockOAuthDisconnect).toHaveBeenCalledExactlyOnceWith(
        "google_drive",
        privateScope,
      );
    });
  });

  it("opens Google Drive tool configuration from the OAuth provider card", async () => {
    oauthStatus("google_drive", {
      status: "connected",
    });
    const googleDriveTools = [
      ...mockTools,
      {
        name: "google_drive",
        display_name: "Google Drive",
        description: "Search, read, upload, and organize files in Google Drive",
        icon: "SiGoogledrive",
        icon_color: "text-green-600",
        category: "productivity",
        status: "available",
        setup_type: "oauth",
        auth_provider: "google_drive",
        config_fields: [
          {
            name: "max_read_size",
            label: "Max Read Size",
            type: "number",
            required: false,
            default: 10485760,
            description: "Maximum file size to read in bytes.",
          },
        ],
        helper_text: null,
        docs_url: null,
        dependencies: null,
      },
    ];
    mockUseTools.mockImplementation(() => ({
      tools: googleDriveTools,
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    }));

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Google Drive")).toBeInTheDocument();
    });

    const driveCard = screen.getByText("Google Drive").closest(".h-full");
    expect(driveCard).toBeInstanceOf(HTMLElement);
    const editButton = await waitFor(() => {
      const button = within(driveCard as HTMLElement).getByRole("button", {
        name: /edit/i,
      });
      expect(button).not.toBeDisabled();
      return button;
    });
    fireEvent.click(editButton);

    await waitFor(() => {
      expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
    });
    expect(mockOAuthConnect).not.toHaveBeenCalled();
  });

  it("opens the only Google Cloud tool's settings from the Google Cloud card", async () => {
    oauthStatus("google_cloud", {
      status: "connected",
    });
    const tools = [
      googleCloudTool("google_bigquery", "Google BigQuery", "dataset"),
    ];
    mockUseTools.mockImplementation(() => ({
      tools,
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    }));

    render(<Integrations />);

    const cloudCard = await screen.findByText("Google Cloud");
    expect(screen.queryByText("Google BigQuery")).not.toBeInTheDocument();
    const editButton = await waitFor(() => {
      const button = within(
        cloudCard.closest(".h-full") as HTMLElement,
      ).getByRole("button", {
        name: /edit/i,
      });
      expect(button).not.toBeDisabled();
      return button;
    });
    fireEvent.click(editButton);

    await waitFor(() => {
      expect(screen.getByText("Service: google_bigquery")).toBeInTheDocument();
    });
    expect(mockOAuthConnect).not.toHaveBeenCalled();
  });

  it("keeps each tool's own settings card when several tools share Google Cloud", async () => {
    oauthStatus("google_cloud", {
      status: "connected",
    });
    const tools = [
      {
        ...googleCloudTool("google_bigquery", "Google BigQuery", "dataset"),
        status: "requires_config",
      },
      googleCloudTool("cloud_storage", "Cloud Storage", "project"),
    ];
    mockUseTools.mockImplementation(() => ({
      tools,
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    }));

    render(<Integrations />);

    const cloudCard = (await screen.findByText("Google Cloud")).closest(
      ".h-full",
    ) as HTMLElement;
    await waitFor(() => {
      expect(
        within(cloudCard).getByRole("button", { name: /disconnect/i }),
      ).not.toBeDisabled();
    });
    expect(screen.queryByText(/Requires Google Cloud/)).not.toBeInTheDocument();

    for (const [displayName, service, field] of [
      ["Google BigQuery", "google_bigquery", "dataset"],
      ["Cloud Storage", "cloud_storage", "project"],
    ]) {
      const toolCard = screen.getByText(displayName).closest(".h-full");
      fireEvent.click(
        within(toolCard as HTMLElement).getByRole("button", {
          name: /configure/i,
        }),
      );
      await waitFor(() => {
        expect(screen.getByText(`Service: ${service}`)).toBeInTheDocument();
      });
      expect(mockEnhancedConfigDialogProps).toHaveBeenLastCalledWith({
        service,
        configFields: [expect.objectContaining({ name: field })],
      });
    }
    expect(mockOAuthConnect).not.toHaveBeenCalled();
  });

  it("opens Gmail OAuth provider configuration using the Gmail tool service", async () => {
    oauthStatus("google_gmail", {
      status: "connected",
    });
    const gmailTools = [
      ...mockTools,
      {
        name: "gmail",
        display_name: "Gmail",
        description: "Read and manage Gmail emails",
        icon: "SiGmail",
        icon_color: "text-red-500",
        category: "email",
        status: "available",
        setup_type: "oauth",
        auth_provider: "google_gmail",
        config_fields: [
          {
            name: "max_results",
            label: "Max Results",
            type: "number",
            required: false,
            default: 10,
            description: "Maximum emails to return.",
          },
        ],
        helper_text: null,
        docs_url: "https://docs.example.test/gmail",
        dependencies: null,
      },
    ];
    mockUseTools.mockImplementation(() => ({
      tools: gmailTools,
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    }));

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Gmail")).toBeInTheDocument();
    });

    const gmailCard = screen.getByText("Gmail").closest(".h-full");
    expect(gmailCard).toBeInstanceOf(HTMLElement);
    const editButton = await waitFor(() => {
      const button = within(gmailCard as HTMLElement).getByRole("button", {
        name: /edit/i,
      });
      expect(button).not.toBeDisabled();
      return button;
    });
    fireEvent.click(editButton);

    await waitFor(() => {
      expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
      expect(screen.getByText("Service: gmail")).toBeInTheDocument();
      expect(
        screen.getByText("Docs: https://docs.example.test/gmail"),
      ).toBeInTheDocument();
    });
    expect(mockOAuthConnect).not.toHaveBeenCalled();
  });

  it("connects plugin OAuth providers discovered from backend tool metadata", async () => {
    const pluginTools = [
      {
        name: "acme_drive",
        display_name: "Acme Drive",
        description: "Search Acme Drive files",
        icon: "AcmeIcon",
        icon_color: "text-cyan-600",
        category: "productivity",
        status: "requires_config",
        setup_type: "oauth",
        auth_provider: "acme_drive",
        config_fields: null,
        helper_text: null,
        docs_url: null,
        dependencies: null,
      },
    ];
    mockUseTools.mockImplementation(() => ({
      tools: pluginTools,
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    }));

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Acme Drive")).toBeInTheDocument();
    });

    const acmeCard = screen.getByText("Acme Drive").closest(".h-full");
    expect(acmeCard).toBeInstanceOf(HTMLElement);
    expect(
      within(acmeCard as HTMLElement).getByText("AcmeIcon"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(acmeCard as HTMLElement).getByRole("button", {
        name: /connect/i,
      }),
    );

    await waitFor(() => {
      expect(mockOAuthConnect).toHaveBeenCalledWith("acme_drive", sharedScope);
    });
    // The card reloads its status once the connection flow finishes.
    await waitFor(() => {
      expect(
        mockOAuthLoadStatus.mock.calls.filter(([id]) => id === "acme_drive"),
      ).toHaveLength(2);
    });
    expect(
      screen.queryByText(/Connect acme_drive first/i),
    ).not.toBeInTheDocument();
  });

  it("offers OAuth and manual credentials for providers with a fallback", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "github",
          display_name: "GitHub",
          description: "Manage GitHub repositories",
          icon: "SiGithub",
          icon_color: null,
          category: "developer",
          status: "requires_config",
          setup_type: "oauth",
          auth_provider: "github",
          config_fields: [
            {
              name: "access_token",
              label: "Access Token",
              type: "password",
              required: false,
            },
            {
              name: "base_url",
              label: "Base URL",
              type: "url",
              required: false,
            },
          ],
          oauth_fallback_fields: ["access_token"],
          manual_auth_configured: false,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const githubCard = await waitFor(() => {
      const card = screen.getByText("GitHub").closest(".h-full");
      expect(card).toBeInstanceOf(HTMLElement);
      return card as HTMLElement;
    });
    expect(
      within(githubCard).getByRole("button", { name: "Connect with GitHub" }),
    ).toBeInTheDocument();

    fireEvent.click(
      within(githubCard).getByRole("button", { name: "Use access token" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Enhanced Config Dialog")).toBeInTheDocument();
      expect(screen.getByText("Service: github")).toBeInTheDocument();
    });
    expect(mockOAuthConnect).not.toHaveBeenCalled();
  });

  it("passes the OAuth status error as helper text to the manual credential dialog", async () => {
    oauthStatus("github", {
      status: "not_connected",
      status_error: "Requester binding failed.",
      helper_text: "Requester binding failed.",
    });
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "github",
          display_name: "GitHub",
          description: "Manage GitHub repositories",
          icon: "SiGithub",
          icon_color: null,
          category: "developer",
          status: "requires_config",
          setup_type: "oauth",
          auth_provider: "github",
          config_fields: [
            {
              name: "access_token",
              label: "Access Token",
              type: "password",
              required: false,
            },
          ],
          oauth_fallback_fields: ["access_token"],
          manual_auth_configured: false,
          helper_text: "Use a personal access token.",
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const githubCard = (await screen.findByText("GitHub")).closest(
      ".h-full",
    ) as HTMLElement;
    fireEvent.click(
      await within(githubCard).findByRole("button", {
        name: "Use access token",
      }),
    );

    // The mocked dialog closes itself right away, so finding the text is the assertion.
    await screen.findByText("Helper: Requester binding failed.");
  });

  it("edits and removes a configured OAuth fallback without disconnecting OAuth", async () => {
    const refetch = vi.fn();
    oauthStatus("github", {
      status: "not_connected",
      status_error: "Requester binding failed.",
      helper_text: "Requester binding failed.",
    });
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "github",
          display_name: "GitHub",
          description: "Manage GitHub repositories",
          icon: "SiGithub",
          icon_color: null,
          category: "developer",
          status: "available",
          setup_type: "oauth",
          auth_provider: "github",
          config_fields: [
            {
              name: "access_token",
              label: "Access Token",
              type: "password",
              required: false,
            },
            {
              name: "base_url",
              label: "Base URL",
              type: "url",
              required: false,
            },
          ],
          oauth_fallback_fields: ["access_token"],
          manual_auth_configured: true,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch,
      statusAuthoritative: true,
    });
    global.fetch = vi.fn().mockResolvedValue({ ok: true });

    render(<Integrations />);

    const githubCard = await waitFor(() => {
      const card = screen.getByText("GitHub").closest(".h-full");
      expect(card).toBeInstanceOf(HTMLElement);
      return card as HTMLElement;
    });
    expect(within(githubCard).getByText("Access Token")).toBeInTheDocument();
    expect(
      within(githubCard).getByRole("button", { name: "Edit access token" }),
    ).toBeInTheDocument();
    // A configured fallback connects the card over the OAuth status error.
    expect(within(githubCard).getByText("Connected")).toBeInTheDocument();
    expect(
      within(githubCard).queryByText(/Requester binding failed/),
    ).not.toBeInTheDocument();

    fireEvent.click(
      within(githubCard).getByRole("button", { name: "Remove access token" }),
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "http://localhost:8080/api/credentials/github",
        {
          method: "DELETE",
        },
      );
      expect(refetch).toHaveBeenCalledTimes(1);
    });
    expect(mockOAuthDisconnect).not.toHaveBeenCalled();
  });

  it("reports an environment OAuth fallback without offering to remove it", async () => {
    mockUseTools.mockReturnValue({
      tools: [
        {
          name: "github",
          display_name: "GitHub",
          description: "Manage GitHub repositories",
          icon: "SiGithub",
          icon_color: null,
          category: "developer",
          status: "available",
          setup_type: "oauth",
          auth_provider: "github",
          config_fields: [
            {
              name: "access_token",
              label: "Access Token",
              type: "password",
              required: false,
            },
            {
              name: "base_url",
              label: "Base URL",
              type: "url",
              required: false,
            },
          ],
          oauth_fallback_fields: ["access_token"],
          manual_auth_configured: false,
          environment_auth_configured: true,
          helper_text: null,
          docs_url: null,
          dependencies: null,
        },
      ],
      loading: false,
      refetch: vi.fn(),
      statusAuthoritative: true,
    });

    render(<Integrations />);

    const githubCard = await waitFor(() => {
      const card = screen.getByText("GitHub").closest(".h-full");
      expect(card).toBeInstanceOf(HTMLElement);
      return card as HTMLElement;
    });
    expect(
      within(githubCard).getByText("Environment Access Token"),
    ).toBeInTheDocument();
    expect(
      within(githubCard).getByRole("button", { name: "Configure" }),
    ).toBeInTheDocument();
    expect(
      within(githubCard).queryByRole("button", { name: "Connect with GitHub" }),
    ).not.toBeInTheDocument();
    expect(
      within(githubCard).queryByRole("button", { name: "Remove access token" }),
    ).not.toBeInTheDocument();
  });

  it("ignores stale shared-scope reloads after switching scope mid-action", async () => {
    const driveAction = createDeferred();
    mockOAuthConnect.mockImplementation(() => driveAction.promise);
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText("Google Drive")).toBeInTheDocument();
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });

    const driveCard = screen.getByText("Google Drive").closest(".h-full");
    expect(driveCard).toBeInstanceOf(HTMLElement);
    fireEvent.click(
      within(driveCard as HTMLElement).getByRole("button", {
        name: "Connect",
      }),
    );

    await waitFor(() => {
      expect(mockOAuthConnect).toHaveBeenCalledTimes(1);
    });

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Private Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Configuring tools for Private Agent (private.per=user).",
        ),
      ).toBeInTheDocument();
      expect(screen.getByText("Private Mail")).toBeInTheDocument();
      expect(screen.queryByText("Weather")).not.toBeInTheDocument();
    });

    await act(async () => {
      driveAction.resolve();
      await driveAction.promise;
    });

    await waitFor(() => {
      expect(screen.getByText("Private Mail")).toBeInTheDocument();
      expect(screen.queryByText("Weather")).not.toBeInTheDocument();
    });
  });

  it("clears the selected scope when policy preview disappears", async () => {
    useConfigStore.setState({
      agents: [
        {
          id: "mind",
          display_name: "Private Agent",
          role: "test",
          tools: ["gmail"],
          skills: [],
          instructions: [],
          rooms: ["personal"],
          private: {
            per: "user",
          },
        },
      ],
      agentPoliciesByAgent: {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          private_workspace_enabled: true,
        }),
      },
    });

    render(<Integrations />);

    const combobox = screen.getByRole("combobox");
    fireEvent.keyDown(combobox, { key: "ArrowDown", code: "ArrowDown" });

    await waitFor(() => {
      expect(screen.getByText("Private Agent")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Private Agent"));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Configuring tools for Private Agent (private.per=user).",
        ),
      ).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText("Private Mail")).toBeInTheDocument();
    });

    await act(async () => {
      useConfigStore.setState({
        agentPoliciesByAgent: {},
      });
    });

    expect(screen.queryByText("Private Mail")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(
        screen.queryByText(
          "Configuring tools for Private Agent (private.per=user).",
        ),
      ).not.toBeInTheDocument();
    });

    await waitFor(() => {
      expect(mockUseTools).toHaveBeenLastCalledWith(null, null);
    });
    await waitFor(() => {
      expect(screen.getByText("Weather")).toBeInTheDocument();
    });
  });
});
