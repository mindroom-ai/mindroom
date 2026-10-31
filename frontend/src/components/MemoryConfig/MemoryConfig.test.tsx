import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { MemoryConfig } from "./MemoryConfig";
import { useConfigStore } from "@/store/configStore";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { Config } from "@/types/config";

// Mock the store
vi.mock("@/store/configStore");
vi.mock("@/hooks/useConfigSchema", () => ({
  useConfigSchema: vi.fn(() => ({ schema: null, error: null, retry: vi.fn() })),
}));

describe("MemoryConfig", () => {
  const mockConfig: Partial<Config> = {
    memory: {
      embedder: {
        provider: "openai",
        config: {
          model: "text-embedding-3-small",
        },
      },
    },
  };

  const mockUpdateMemoryConfig = vi.fn();
  const mockUpdateConfigValue = vi.fn();
  const mockSaveConfig = vi.fn();

  const mockStore = (
    config: Partial<Config>,
    extra: Record<string, unknown> = {},
  ) => {
    (useConfigStore as any).mockReturnValue({
      config,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
      ...extra,
    });
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockStore(mockConfig);
  });

  it("renders memory configuration", () => {
    render(<MemoryConfig />);

    expect(screen.getByText("Memory Configuration")).toBeInTheDocument();
    expect(
      screen.getByText(/Configure the embedder for agent memory/),
    ).toBeInTheDocument();
  });

  it("displays current configuration", () => {
    render(<MemoryConfig />);

    expect(screen.getByText("Current Configuration")).toBeInTheDocument();
    expect(
      screen.getByText("openai", { selector: ".font-mono" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("text-embedding-3-small", { selector: ".font-mono" }),
    ).toBeInTheDocument();
  });

  it("shows correct provider in select", () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById("provider");
    expect(providerSelect).toBeInTheDocument();
    expect(providerSelect).toHaveTextContent("OpenAI");
  });

  it("shows model as free-text input", () => {
    render(<MemoryConfig />);

    const modelInput = document.getElementById("model") as HTMLInputElement;
    expect(modelInput).toBeInTheDocument();
    expect(modelInput).toHaveValue("text-embedding-3-small");
    expect(modelInput).toHaveAttribute(
      "placeholder",
      "e.g. text-embedding-3-small",
    );
    expect(modelInput.tagName).toBe("INPUT");
  });

  it("suggests no model for an embedder provider it has no default for", () => {
    mockStore({
      memory: {
        embedder: { provider: "custom_embedder", config: { model: "" } },
      },
    });
    render(<MemoryConfig />);

    expect(document.getElementById("model")).toHaveAttribute(
      "placeholder",
      "Model name",
    );
  });

  it("shows host input for openai provider", () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById("host") as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
  });

  it("binds the OpenAI embedder to a named credential service", async () => {
    render(<MemoryConfig />);

    const credentialsServiceInput = document.getElementById(
      "credentials-service",
    ) as HTMLInputElement;
    fireEvent.change(credentialsServiceInput, {
      target: { value: "embedding-production" },
    });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            config: expect.objectContaining({
              credentials_service: "embedding-production",
            }),
          }),
        }),
      );
    });
  });

  it("removes a cleared credential service from saved config", async () => {
    const configWithCredentialService: Partial<Config> = {
      memory: {
        embedder: {
          provider: "openai",
          config: {
            model: "text-embedding-3-small",
            credentials_service: "embedding-production",
          },
        },
      },
    };
    mockStore(configWithCredentialService);
    render(<MemoryConfig />);

    const credentialsServiceInput = document.getElementById(
      "credentials-service",
    ) as HTMLInputElement;
    fireEvent.change(credentialsServiceInput, { target: { value: "" } });

    await waitFor(() => {
      const calls = mockUpdateMemoryConfig.mock.calls;
      const nextConfig = calls[calls.length - 1]?.[0];
      expect(nextConfig.embedder.config).not.toHaveProperty(
        "credentials_service",
      );
    });
  });

  it("updates backend to disabled memory when none is selected", async () => {
    render(<MemoryConfig />);

    const backendSelect = document.getElementById("memory-backend");
    expect(backendSelect).toBeInTheDocument();
    fireEvent.click(backendSelect!);

    const disabledOption = await screen.findByText("Disabled (stateless)");
    fireEvent.click(disabledOption);

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          backend: "none",
        }),
      );
    });
  });

  it("shows host input for ollama provider", () => {
    const ollamaConfig: Partial<Config> = {
      memory: {
        embedder: {
          provider: "ollama",
          config: {
            model: "nomic-embed-text",
            host: "http://localhost:11434",
          },
        },
      },
    };

    mockStore(ollamaConfig);

    render(<MemoryConfig />);

    const hostInput = document.getElementById("host") as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue("http://localhost:11434");
    expect(
      screen.getByText("Local embeddings using Ollama"),
    ).toBeInTheDocument();
  });

  it("leaves an unset Ollama host empty, so a save keeps OLLAMA_HOST in charge", async () => {
    mockStore({
      memory: {
        embedder: {
          provider: "ollama",
          config: { model: "nomic-embed-text" },
        },
      },
    });

    render(<MemoryConfig />);

    const hostInput = document.getElementById("host") as HTMLInputElement;
    expect(hostInput).toHaveValue("");
    expect(hostInput).toHaveAttribute("placeholder", "http://localhost:11434");
    expect(screen.getByText("Ollama Host URL")).not.toHaveTextContent("*");
    expect(screen.getByText(/Empty uses OLLAMA_HOST/)).toBeInTheDocument();

    fireEvent.change(document.getElementById("model") as HTMLInputElement, {
      target: { value: "mxbai-embed-large" },
    });

    await waitFor(() => {
      const calls = mockUpdateMemoryConfig.mock.calls;
      expect(calls[calls.length - 1]?.[0]?.embedder).toEqual({
        provider: "ollama",
        config: { model: "mxbai-embed-large", host: "" },
      });
    });
  });

  it("hides host input for sentence-transformers provider", () => {
    const localConfig: Partial<Config> = {
      memory: {
        embedder: {
          provider: "sentence_transformers",
          config: {
            model: "sentence-transformers/all-MiniLM-L6-v2",
          },
        },
      },
    };

    mockStore(localConfig);

    render(<MemoryConfig />);

    expect(document.getElementById("host")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Fully local embeddings using the sentence-transformers Python runtime",
      ),
    ).toBeInTheDocument();
  });

  it("changes provider and resets model to default", async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById("provider");
    expect(providerSelect).toBeInTheDocument();
    fireEvent.click(providerSelect!);

    const ollamaOption = await screen.findByText("Ollama");
    fireEvent.click(ollamaOption);

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: "ollama",
            config: expect.objectContaining({
              model: "nomic-embed-text",
              host: "",
            }),
          }),
        }),
      );
    });
  });

  it("changes provider to sentence-transformers and clears host", async () => {
    const configWithHost: Partial<Config> = {
      memory: {
        embedder: {
          provider: "openai",
          config: {
            model: "text-embedding-3-small",
            host: "http://localhost:9292/v1",
            dimensions: 1536,
          },
        },
      },
    };

    mockStore(configWithHost);

    render(<MemoryConfig />);

    const providerSelect = document.getElementById("provider");
    expect(providerSelect).toBeInTheDocument();
    fireEvent.click(providerSelect!);

    const sentenceTransformersOption = await screen.findByText(
      "Sentence Transformers",
    );
    fireEvent.click(sentenceTransformersOption);

    await waitFor(() => {
      const calls = mockUpdateMemoryConfig.mock.calls;
      const nextConfig = calls[calls.length - 1]?.[0];
      expect(nextConfig?.embedder).toEqual({
        provider: "sentence_transformers",
        config: {
          model: "sentence-transformers/all-MiniLM-L6-v2",
          host: "",
        },
      });
    });
  });

  it("offers credential binding without assuming OPENAI_API_KEY", () => {
    render(<MemoryConfig />);

    expect(document.getElementById("credentials-service")).toBeInTheDocument();
    expect(screen.queryByText(/OPENAI_API_KEY/)).not.toBeInTheDocument();
  });

  it("keeps credential binding available for a custom base URL", () => {
    const configWithHost: Partial<Config> = {
      memory: {
        embedder: {
          provider: "openai",
          config: {
            model: "text-embedding-3-small",
            host: "http://localhost:9292/v1",
          },
        },
      },
    };

    mockStore(configWithHost);

    render(<MemoryConfig />);

    expect(document.getElementById("credentials-service")).toBeInTheDocument();
  });

  it("allows typing custom model name", async () => {
    render(<MemoryConfig />);

    const modelInput = document.getElementById("model") as HTMLInputElement;
    fireEvent.change(modelInput, {
      target: { value: "my-custom-embedding-model" },
    });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: "openai",
            config: expect.objectContaining({
              model: "my-custom-embedding-model",
              host: "",
            }),
          }),
        }),
      );
    });
  });

  it("updates host when input changes", async () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById("host") as HTMLInputElement;
    fireEvent.change(hostInput, {
      target: { value: "http://localhost:9292/v1" },
    });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: "openai",
            config: expect.objectContaining({
              model: "text-embedding-3-small",
              host: "http://localhost:9292/v1",
            }),
          }),
        }),
      );
    });
  });

  it("loads and updates the entrypoint token cap without touching other file settings", async () => {
    mockStore({
      memory: {
        backend: "file",
        embedder: mockConfig.memory!.embedder,
        file: {
          max_entrypoint_lines: 120,
          max_entrypoint_tokens: 30000,
        },
      },
    });
    render(<MemoryConfig />);

    const tokenInput = document.getElementById(
      "entrypoint-max-tokens",
    ) as HTMLInputElement;
    expect(tokenInput.value).toBe("30000");
    fireEvent.change(tokenInput, { target: { value: "20000" } });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          file: {
            max_entrypoint_lines: 120,
            max_entrypoint_tokens: 20000,
          },
        }),
      );
    });
  });

  it("falls back to the default when a numeric field is cleared", () => {
    mockStore({
      memory: {
        backend: "file",
        embedder: mockConfig.memory!.embedder,
        file: { max_entrypoint_lines: 120 },
      },
    });
    render(<MemoryConfig />);

    fireEvent.change(document.getElementById("entrypoint-max-lines")!, {
      target: { value: "" },
    });

    expect(mockUpdateMemoryConfig).toHaveBeenLastCalledWith(
      expect.objectContaining({
        file: { max_entrypoint_lines: 200, max_entrypoint_tokens: 50000 },
      }),
    );
  });

  it("enables auto flush and entrypoint search from their selects", async () => {
    mockStore({
      memory: { backend: "file", embedder: mockConfig.memory!.embedder },
    });
    render(<MemoryConfig />);

    fireEvent.click(document.getElementById("auto-flush-enabled")!);
    fireEvent.click(await screen.findByRole("option", { name: "Enabled" }));
    expect(mockUpdateMemoryConfig).toHaveBeenLastCalledWith(
      expect.objectContaining({
        auto_flush: expect.objectContaining({ enabled: true }),
      }),
    );

    fireEvent.click(document.getElementById("memory-search-entrypoint")!);
    fireEvent.click(await screen.findByRole("option", { name: "Enabled" }));
    expect(mockUpdateMemoryConfig).toHaveBeenLastCalledWith(
      expect.objectContaining({
        search: expect.objectContaining({ include_entrypoint: true }),
      }),
    );
  });

  it("updates search include patterns without touching other search settings", async () => {
    mockStore({
      memory: {
        backend: "file",
        embedder: mockConfig.memory!.embedder,
        search: {
          mode: "semantic",
          include: ["memory/**/*.md"],
          include_entrypoint: true,
        },
      },
    });
    render(<MemoryConfig />);

    fireEvent.change(document.getElementById("memory-search-include")!, {
      target: { value: "notes/*.md, memory/*.md" },
    });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          search: {
            mode: "semantic",
            include: ["notes/*.md", "memory/*.md"],
            include_entrypoint: true,
          },
        }),
      );
    });
  });

  it("keeps a trailing comma typed into the include patterns", async () => {
    const actual = await vi.importActual<typeof import("@/store/configStore")>(
      "@/store/configStore",
    );
    actual.useConfigStore.setState({
      config: {
        ...mockConfig,
        memory: { ...mockConfig.memory!, backend: "file" },
      } as Config,
    });
    (useConfigStore as any).mockImplementation(() => actual.useConfigStore());
    render(<MemoryConfig />);

    const input = document.getElementById(
      "memory-search-include",
    ) as HTMLInputElement;
    for (const value of ["a", "a,", "a, ", "a, b"]) {
      fireEvent.change(input, { target: { value } });
      expect(input).toHaveValue(value);
    }
    expect(
      actual.useConfigStore.getState().config?.memory.search?.include,
    ).toEqual(["a", "b"]);

    act(() => {
      actual.useConfigStore.getState().updateMemoryConfig({
        ...actual.useConfigStore.getState().config!.memory,
        search: { include: ["z/*.md"] },
      });
    });
    expect(input).toHaveValue("z/*.md");
  });

  it("calls saveConfig when save button is clicked", async () => {
    // Re-mock with isDirty: true so the button is enabled
    mockStore(mockConfig, { isDirty: true });

    render(<MemoryConfig />);

    const saveButton = screen.getByRole("button", { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });

  it("disables save button when not dirty", () => {
    render(<MemoryConfig />);

    const saveButton = screen.getByRole("button", { name: /Save/i });
    expect(saveButton).toBeDisabled();
  });

  it("shows auto-flush settings without the retired memory settings", () => {
    mockStore({ memory: { ...mockConfig.memory!, backend: "file" } });

    render(<MemoryConfig />);

    expect(screen.getByLabelText("Flush Interval (seconds)")).toHaveValue(300);
    expect(screen.getByLabelText("Stale TTL (seconds)")).toHaveValue(86400);
    expect(
      screen.queryByLabelText("Max Dirty Age (seconds)"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Cross-Session Reprioritize"),
    ).not.toBeInTheDocument();
    expect(
      document.getElementById("team-reads-member-memory"),
    ).not.toBeInTheDocument();
    expect(document.getElementById("file-memory-path")).not.toBeInTheDocument();
    expect(screen.queryByText("Team Reads Members:")).not.toBeInTheDocument();
  });

  it("shows provider description for openai", () => {
    render(<MemoryConfig />);

    expect(
      screen.getByText(
        "OpenAI or any OpenAI-compatible API (set Base URL below)",
      ),
    ).toBeInTheDocument();
  });

  it("shows base URL in config display when set", () => {
    const configWithHost: Partial<Config> = {
      memory: {
        embedder: {
          provider: "openai",
          config: {
            model: "text-embedding-3-small",
            host: "http://localhost:9292/v1",
          },
        },
      },
    };

    mockStore(configWithHost);

    render(<MemoryConfig />);

    expect(screen.getByText("Base URL:")).toBeInTheDocument();
    expect(
      screen.getByText("http://localhost:9292/v1", { selector: ".font-mono" }),
    ).toBeInTheDocument();
  });

  it("handles missing memory config gracefully", () => {
    mockStore({});

    render(<MemoryConfig />);

    // Should show default values
    const providerSelect = document.getElementById("provider");
    expect(providerSelect).toHaveTextContent("OpenAI");
    const modelInput = document.getElementById("model") as HTMLInputElement;
    expect(modelInput).toHaveValue("text-embedding-3-small");
  });

  it("edits memory settings the page does not render through More settings", () => {
    mockStore(mockConfig, {
      agents: [],
      rooms: [],
      diagnostics: [],
      updateConfigValue: mockUpdateConfigValue,
    });
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          MemoryConfig: {
            type: "object",
            properties: {
              backend: { type: "string" },
              llm: {
                anyOf: [{ $ref: "#/$defs/_MemoryLLMConfig" }, { type: "null" }],
                default: null,
                description: "LLM used by Mem0 for memory extraction",
              },
            },
          },
          _MemoryLLMConfig: {
            type: "object",
            properties: {
              provider: { type: "string", default: "ollama" },
            },
          },
          EmbedderConfig: {
            type: "object",
            properties: { model: { type: "string" } },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<MemoryConfig />);
    fireEvent.click(screen.getByRole("button", { name: /^More settings/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Configure LLM" }));

    // Only the edited key is written; untouched memory defaults stay unset.
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["memory", "llm"],
      {},
    );
    expect(mockUpdateMemoryConfig).not.toHaveBeenCalled();
  });

  it("edits embedder options the page does not render through More embedder settings", () => {
    mockStore(mockConfig, {
      agents: [],
      rooms: [],
      diagnostics: [],
      updateConfigValue: mockUpdateConfigValue,
    });
    vi.mocked(useConfigSchema).mockReturnValue({
      schema: {
        type: "object",
        properties: {},
        $defs: {
          MemoryConfig: { type: "object", properties: {} },
          EmbedderConfig: {
            type: "object",
            properties: {
              model: { type: "string" },
              dimensions: {
                anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }],
                default: null,
              },
            },
          },
        },
      },
      error: null,
      retry: vi.fn(),
    });

    render(<MemoryConfig />);
    fireEvent.click(
      screen.getByRole("button", { name: /More embedder settings/ }),
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Dimensions" }), {
      target: { value: "768" },
    });

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["memory", "embedder", "config", "dimensions"],
      768,
    );
  });
});
