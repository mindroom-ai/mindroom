import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelConfig } from "./ModelConfig";
import { useConfigStore } from "@/store/configStore";

vi.mock("@/store/configStore", () => ({
  useConfigStore: vi.fn(),
}));
vi.mock("@/hooks/useConfigSchema", () => ({
  useConfigSchema: vi.fn(() => ({ schema: null, error: null })),
}));

vi.mock("@/components/ui/toaster", () => ({
  toast: vi.fn(),
}));

describe("ModelConfig metadata", () => {
  const mockStore = {
    config: {
      models: {
        stable_key: {
          provider: "openai" as const,
          id: "test-model",
          api: "responses" as const,
          context_window: 32000,
          display_name: "Quick helper",
          icon: "icons/helper.png",
          extra_kwargs: {
            base_url: "http://localhost:9292/v1",
            temperature: 0.2,
          },
        },
      },
      agents: {},
      defaults: { markdown: true },
      router: { model: "stable_key" },
    },
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue({ status: "saved" }),
    isLoading: false,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    const mockedUseConfigStore = useConfigStore as unknown as {
      mockReturnValue: (value: unknown) => void;
    };
    mockedUseConfigStore.mockReturnValue(mockStore);

    Object.defineProperty(global, "fetch", {
      value: vi.fn(async () => ({
        ok: true,
        json: async () => ({ has_key: false }),
      })),
      writable: true,
      configurable: true,
    });
  });

  it("shows a display name while retaining the stable model key", async () => {
    render(<ModelConfig />);

    await waitFor(() => {
      const row = screen.getByText("Quick helper").closest("tr");
      if (!row) throw new Error("row not found");

      expect(within(row).getByText("stable_key")).toBeTruthy();
      expect(within(row).getByText("Quick helper")).toBeTruthy();
    });
  });

  it("edits metadata without changing provider parameters or the stable key", async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText("Quick helper"));
    const row = screen.getByDisplayValue("stable_key").closest("tr");
    if (!row) throw new Error("row not found");

    fireEvent.change(within(row).getByLabelText("Display name"), {
      target: { value: "  Fast helper  " },
    });
    fireEvent.change(within(row).getByLabelText("Icon"), {
      target: { value: "  mxc://server/fast-helper  " },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith("stable_key", {
        provider: "openai",
        id: "test-model",
        api: "responses",
        context_window: 32000,
        display_name: "Fast helper",
        icon: "mxc://server/fast-helper",
        extra_kwargs: {
          base_url: "http://localhost:9292/v1",
          temperature: 0.2,
        },
      });
      expect(mockStore.deleteModel).not.toHaveBeenCalled();
    });
  });

  it("clears authored metadata without disturbing other model fields", async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText("Quick helper"));
    const row = screen.getByDisplayValue("stable_key").closest("tr");
    if (!row) throw new Error("row not found");

    fireEvent.change(within(row).getByLabelText("Display name"), {
      target: { value: "   " },
    });
    fireEvent.change(within(row).getByLabelText("Icon"), {
      target: { value: "" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith("stable_key", {
        provider: "openai",
        id: "test-model",
        api: "responses",
        context_window: 32000,
        extra_kwargs: {
          base_url: "http://localhost:9292/v1",
          temperature: 0.2,
        },
      });
    });
  });

  it("creates a model with normalized optional metadata", async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole("button", { name: /add model/i }));
    fireEvent.change(screen.getByPlaceholderText("model name"), {
      target: { value: "new_key" },
    });
    fireEvent.change(screen.getByPlaceholderText("provider model id"), {
      target: { value: "new-model" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "  New helper  " },
    });
    fireEvent.change(screen.getByLabelText("Icon"), {
      target: { value: "  icons/new-helper.png  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith("new_key", {
        provider: "openrouter",
        id: "new-model",
        display_name: "New helper",
        icon: "icons/new-helper.png",
      });
    });
  });
});
