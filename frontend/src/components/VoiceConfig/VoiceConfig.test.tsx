import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceConfig } from "./VoiceConfig";
import { useConfigStore } from "@/store/configStore";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import type { ConfigDiagnostic } from "@/lib/configValidation";
import { Agent, Config, Room } from "@/types/config";
import type { SaveConfigResult } from "@/store/configStore";

vi.mock("@/store/configStore");
vi.mock("@/hooks/useConfigSchema", () => ({
  useConfigSchema: vi.fn(() => ({ schema: null, error: null, retry: vi.fn() })),
}));

const { mockToast, mockToaster } = vi.hoisted(() => ({
  mockToast: vi.fn(),
  mockToaster: vi.fn(),
}));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));
vi.mock("@/components/ui/toaster", () => ({
  toast: mockToaster,
}));

describe("VoiceConfig", () => {
  const mockSaveConfig = vi.fn();
  const mockUpdateConfigValue = vi.fn();
  type MockStoreState = {
    config: Config;
    agents: Agent[];
    rooms: Room[];
    diagnostics: ConfigDiagnostic[];
    syncStatus: "synced" | "syncing" | "error" | "disconnected";
    isDirty: boolean;
    isLoading: boolean;
    saveConfig: () => Promise<SaveConfigResult>;
    updateConfigValue: typeof mockUpdateConfigValue;
  };
  type MockedStoreHook = {
    (): MockStoreState;
    getState: () => MockStoreState;
    mockReturnValue: (value: MockStoreState) => void;
  };
  const mockedUseConfigStore = useConfigStore as unknown as MockedStoreHook;
  let mockStoreState: MockStoreState;

  const createConfig = (): Partial<Config> => ({
    models: {
      default: { provider: "openai", id: "gpt-5.6-luna" },
      fast: { provider: "openai", id: "gpt-5.6-terra" },
    },
    voice: {
      enabled: true,
      visible_router_echo: true,
      stt: {
        provider: "openai_compatible",
        model: "whisper-1",
        host: "http://localhost:8080",
        api_key: "",
      },
      intelligence: {
        model: "default",
      },
    },
  });

  const setMockStore = (config: Partial<Config>) => {
    mockStoreState = {
      config: config as Config,
      agents: [],
      rooms: [],
      diagnostics: [],
      syncStatus: "synced",
      isDirty: false,
      isLoading: false,
      saveConfig: mockSaveConfig,
      updateConfigValue: mockUpdateConfigValue,
    };
    mockedUseConfigStore.mockReturnValue(mockStoreState);
    mockedUseConfigStore.getState = vi.fn(() => mockStoreState);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = "synced";
      mockStoreState.isDirty = false;
      return { status: "saved" };
    });
    setMockStore(createConfig());
  });

  it("shows current effective settings summary", () => {
    render(<VoiceConfig />);

    expect(screen.getByText("Current Effective Settings")).toBeInTheDocument();
    expect(screen.getByText("OpenAI-compatible API")).toBeInTheDocument();
    expect(screen.getAllByText("OpenAI-compatible")).not.toHaveLength(0);
    expect(
      screen.getByText("http://localhost:8080/v1/audio/transcriptions"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Non-secret compatibility placeholder"),
    ).toBeInTheDocument();
  });

  it("always shows optional base url input", () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.stt.provider = "openai";

    setMockStore(config);

    render(<VoiceConfig />);

    const hostInput = document.getElementById(
      "stt-base-url",
    ) as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue("http://localhost:8080");
  });

  it("uses default openai endpoint when base url is cleared", async () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.stt.provider = "openai";
    setMockStore(config);

    render(<VoiceConfig />);

    const hostInput = document.getElementById(
      "stt-base-url",
    ) as HTMLInputElement;
    fireEvent.change(hostInput, { target: { value: "" } });

    await waitFor(() => {
      expect(mockUpdateConfigValue).toHaveBeenCalledWith(["voice"], {
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: "openai",
          model: "whisper-1",
          host: "",
          api_key: "",
        },
        intelligence: {
          model: "default",
        },
      });
      expect(
        screen.getByText("https://api.openai.com/v1/audio/transcriptions"),
      ).toBeInTheDocument();
    });
  });

  it("normalizes and saves voice settings without rewriting the provider", async () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.stt.host = "http://localhost:8080/";
    setMockStore(config);

    render(<VoiceConfig />);

    const saveButton = screen.getByRole("button", {
      name: "Save Voice Configuration",
    });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
      expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(["voice"], {
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: "openai_compatible",
          model: "whisper-1",
          host: "http://localhost:8080",
        },
        intelligence: {
          model: "default",
        },
      });
      expect(mockToast).toHaveBeenCalledWith({
        title: "Voice Configuration Saved",
        description: "Your voice settings have been updated successfully.",
      });
    });
  });

  it("does not append a duplicate v1 path", () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.stt.host = "http://localhost:8080/v1/";
    setMockStore(config);

    render(<VoiceConfig />);

    expect(
      screen.getByText("http://localhost:8080/v1/audio/transcriptions"),
    ).toBeInTheDocument();
  });

  it("uses the current transcription model for new voice config", () => {
    const config = createConfig();
    delete config.voice;
    setMockStore(config);

    render(<VoiceConfig />);

    expect(document.getElementById("stt-model")).toHaveValue("gpt-transcribe");
  });

  it("shows an error toast when saving fails", async () => {
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = "error";
      mockStoreState.isDirty = true;
      return {
        status: "error",
        message: "Configuration validation failed",
        diagnostics: mockStoreState.diagnostics,
      };
    });
    mockStoreState.diagnostics = [
      {
        kind: "global",
        message: "Configuration validation failed",
        blocking: false,
      },
    ];

    render(<VoiceConfig />);

    fireEvent.click(
      screen.getByRole("button", { name: "Save Voice Configuration" }),
    );

    await waitFor(() => {
      expect(mockToaster).toHaveBeenCalledWith({
        title: "Save Failed",
        description: "Configuration validation failed",
        variant: "destructive",
      });
    });
  });

  it("shows a stale-save toast when a newer voice draft supersedes the request", async () => {
    mockSaveConfig.mockResolvedValueOnce({ status: "stale" });

    render(<VoiceConfig />);

    fireEvent.click(
      screen.getByRole("button", { name: "Save Voice Configuration" }),
    );

    await waitFor(() => {
      expect(mockToaster).toHaveBeenCalledWith({
        title: "Save Failed",
        description: "Save was superseded by newer voice configuration edits.",
        variant: "destructive",
      });
    });
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Voice Configuration Saved" }),
    );
  });

  it("shows save button even when voice is disabled", async () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.enabled = false;

    setMockStore(config);

    render(<VoiceConfig />);

    const saveButton = screen.getByRole("button", {
      name: "Save Voice Configuration",
    });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });

  it("updates visible router echo from the voice tab", async () => {
    const config = createConfig();
    if (!config.voice) throw new Error("Expected voice config");
    config.voice.visible_router_echo = false;
    setMockStore(config);

    render(<VoiceConfig />);

    const visibleRouterEchoToggle = document.getElementById(
      "visible-router-echo",
    ) as HTMLInputElement;
    fireEvent.click(visibleRouterEchoToggle);

    await waitFor(() => {
      expect(mockUpdateConfigValue).toHaveBeenCalledWith(["voice"], {
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: "openai_compatible",
          model: "whisper-1",
          host: "http://localhost:8080",
          api_key: "",
        },
        intelligence: {
          model: "default",
        },
      });
      expect(screen.getByText("Visible Router Echo:")).toBeInTheDocument();
      expect(visibleRouterEchoToggle).toBeChecked();
    });
  });

  it("disables save while a save is already in progress", () => {
    const config = createConfig();
    setMockStore(config);
    mockStoreState.isLoading = true;

    render(<VoiceConfig />);

    expect(
      screen.getByRole("button", { name: "Save Voice Configuration" }),
    ).toBeDisabled();
  });

  it("enables voice calls from the Voice Calls card", async () => {
    mockStoreState.diagnostics = [];
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: { calls: { $ref: "#/$defs/CallsConfig" } },
        $defs: {
          CallsConfig: {
            type: "object",
            properties: {
              enabled: {
                type: "boolean",
                default: false,
                description: "Enable agents joining Element Call voice calls",
              },
            },
          },
          VoiceSTTConfig: { type: "object", properties: {} },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<VoiceConfig />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Enabled" }));

    expect(mockUpdateConfigValue).toHaveBeenCalledWith(
      ["calls", "enabled"],
      true,
    );

    mockUpdateConfigValue.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Save Call Settings" }));
    await waitFor(() => expect(mockSaveConfig).toHaveBeenCalled());
    // Saving calls must not write the voice form's defaults.
    expect(mockUpdateConfigValue).not.toHaveBeenCalled();
  });

  it("edits speech-to-text options through More speech-to-text settings", () => {
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          VoiceSTTConfig: {
            type: "object",
            properties: {
              model: { type: "string" },
              credentials_service: {
                anyOf: [{ type: "string" }, { type: "null" }],
                description: "Named speech credential service",
              },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<VoiceConfig />);
    fireEvent.click(
      screen.getByRole("button", { name: /More speech-to-text settings/ }),
    );
    fireEvent.change(screen.getByLabelText("Credentials service"), {
      target: { value: "speech" },
    });

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["voice", "stt", "credentials_service"],
      "speech",
    );
  });
});
