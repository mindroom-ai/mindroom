import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EnhancedConfigDialog } from "./EnhancedConfigDialog";

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

global.fetch = vi.fn();

describe("EnhancedConfigDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it("loads and saves scoped credentials with explicit execution_scope", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          credentials: {
            api_key: "existing-key",
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "success" }),
      });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="weather"
        displayName="Weather"
        description="Weather integration"
        configFields={[
          {
            name: "api_key",
            label: "API Key",
            type: "password",
            required: true,
          },
        ]}
        onSuccess={onSuccess}
        agentName="code"
        executionScope="shared"
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/weather?agent_name=code&execution_scope=shared",
      );
    });

    fireEvent.change(document.getElementById("api_key") as HTMLInputElement, {
      target: { value: "scoped-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Configuration" }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/weather?agent_name=code&execution_scope=shared",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            credentials: {
              api_key: "scoped-key",
            },
          }),
        },
      );
      expect(onSuccess).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("rejects untouched required fields without defaults", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ credentials: {} }),
    });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="weather"
        displayName="Weather"
        description="Weather integration"
        configFields={[
          {
            name: "api_key",
            label: "API Key",
            type: "password",
            required: true,
          },
        ]}
        onSuccess={onSuccess}
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/credentials/weather");
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Configuration" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Validation Error",
          variant: "destructive",
        }),
      );
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("requires a dependent secret only when the tracked field changes", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        credentials: {
          client_id: "old-client-id",
        },
      }),
    });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="google_drive_oauth_client"
        displayName="Google Drive OAuth Client"
        description="OAuth client config"
        configFields={[
          {
            name: "client_id",
            label: "Client ID",
            type: "text",
            required: true,
          },
          {
            name: "client_secret",
            label: "Client Secret",
            type: "password",
            required: false,
            requiredWhenFieldChanges: "client_id",
          },
        ]}
        onSuccess={onSuccess}
        isEditing={true}
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/google_drive_oauth_client",
      );
    });

    fireEvent.change(document.getElementById("client_id") as HTMLInputElement, {
      target: { value: "new-client-id" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Update Configuration" }),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Validation Error",
          variant: "destructive",
        }),
      );
    });
    expect(
      screen.getByText("Client Secret is required when Client ID changes"),
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("shows backend save error detail", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ credentials: { api_key: "old-key" } }),
      })
      .mockResolvedValueOnce({
        ok: false,
        json: async () => ({
          detail: "client_secret is required when client_id changes.",
        }),
      });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="google_drive_oauth_client"
        displayName="Google Drive OAuth Client"
        description="OAuth client config"
        configFields={[
          {
            name: "api_key",
            label: "API Key",
            type: "password",
            required: true,
          },
        ]}
        onSuccess={onSuccess}
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/google_drive_oauth_client",
      );
    });

    fireEvent.change(document.getElementById("api_key") as HTMLInputElement, {
      target: { value: "new-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Configuration" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Configuration Failed",
          description: "client_secret is required when client_id changes.",
          variant: "destructive",
        }),
      );
    });
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("omits cleared optional number fields when saving", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          credentials: {
            max_read_size: 10485760,
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "success" }),
      });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="google_drive"
        displayName="Google Drive"
        description="Drive integration"
        configFields={[
          {
            name: "max_read_size",
            label: "Max Read Size",
            type: "number",
            required: false,
            default: 10485760,
          },
        ]}
        onSuccess={onSuccess}
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/google_drive",
      );
    });

    fireEvent.change(
      document.getElementById("max_read_size") as HTMLInputElement,
      {
        target: { value: "" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save Configuration" }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/google_drive",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            credentials: {},
          }),
        },
      );
      expect(onSuccess).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("rejects non-finite number values without explicit min or max validation", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ credentials: { max_read_size: "1e309" } }),
    });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="google_drive"
        displayName="Google Drive"
        description="Drive integration"
        configFields={[
          {
            name: "max_read_size",
            label: "Max Read Size",
            type: "number",
            required: false,
            default: 10485760,
          },
        ]}
        onSuccess={onSuccess}
      />,
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/credentials/google_drive",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Save Configuration" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Validation Error",
          variant: "destructive",
        }),
      );
    });
    expect(
      screen.getByText("Max Read Size must be a number"),
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("EnhancedConfigDialog string arrays", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  async function renderListConfig({
    credentials,
    defaultValue,
    missingCredentials = false,
  }: {
    credentials: Record<string, unknown>;
    defaultValue?: string[];
    missingCredentials?: boolean;
  }) {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: !missingCredentials,
        json: async () => ({ credentials }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "success" }),
      });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={vi.fn()}
        service="searxng"
        displayName="SearXNG"
        description="Search integration"
        configFields={[
          {
            name: "engines",
            label: "Engines",
            type: "string[]",
            default: defaultValue,
          },
        ]}
      />,
    );

    return screen.findByRole("button", { name: "Save Configuration" });
  }

  it.each([
    {
      name: "populated arrays with comma-containing items",
      stored: ["duckduckgo", "custom,engine"],
      expected: ["duckduckgo", "custom,engine"],
    },
    {
      name: "explicit empty arrays overriding populated defaults",
      stored: [],
      expected: [],
    },
  ])(
    "round-trips $name through the credential request",
    async ({ stored, expected }) => {
      const save = await renderListConfig({
        credentials: { engines: stored },
        defaultValue: ["wikipedia"],
      });

      fireEvent.click(save);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith("/api/credentials/searxng", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credentials: { engines: expected } }),
        });
      });
    },
  );

  it("edits, adds, and removes individual list items before saving", async () => {
    const save = await renderListConfig({
      credentials: { engines: ["duckduckgo", "wikipedia"] },
    });

    fireEvent.change(screen.getByDisplayValue("duckduckgo"), {
      target: { value: "bing" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Remove Engines value 2" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add value" }));
    fireEvent.change(screen.getByDisplayValue(""), {
      target: { value: "custom,engine" },
    });
    fireEvent.click(save);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/credentials/searxng", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          credentials: { engines: ["bing", "custom,engine"] },
        }),
      });
    });
  });

  it("saves an explicit empty array when the final item is removed", async () => {
    const save = await renderListConfig({
      credentials: { engines: ["duckduckgo"] },
      defaultValue: ["wikipedia"],
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Remove Engines value 1" }),
    );
    fireEvent.click(save);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/credentials/searxng", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credentials: { engines: [] } }),
      });
    });
  });

  it.each([
    {
      name: "populated defaults with an existing credentials document",
      credentials: {},
      missingCredentials: false,
      defaultValue: ["duckduckgo", "wikipedia"],
      expected: ["duckduckgo", "wikipedia"],
    },
    {
      name: "empty defaults with an existing credentials document",
      credentials: {},
      missingCredentials: false,
      defaultValue: [],
      expected: [],
    },
    {
      name: "populated defaults without an existing credentials document",
      credentials: {},
      missingCredentials: true,
      defaultValue: ["duckduckgo", "wikipedia"],
      expected: ["duckduckgo", "wikipedia"],
    },
    {
      name: "empty defaults without an existing credentials document",
      credentials: {},
      missingCredentials: true,
      defaultValue: [],
      expected: [],
    },
  ])(
    "preserves $name",
    async ({ credentials, missingCredentials, defaultValue, expected }) => {
      const save = await renderListConfig({
        credentials,
        missingCredentials,
        defaultValue,
      });

      fireEvent.click(save);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith("/api/credentials/searxng", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credentials: { engines: expected } }),
        });
      });
    },
  );

  it.each([
    { name: "legacy comma-separated text", stored: "duckduckgo,wikipedia" },
    { name: "a mixed-type array", stored: ["duckduckgo", 42] },
  ])(
    "requires explicit replacement of $name before saving a list",
    async ({ stored }) => {
      const save = await renderListConfig({
        credentials: { engines: stored },
      });

      fireEvent.click(save);

      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: "Validation Error",
            variant: "destructive",
          }),
        );
      });
      expect(global.fetch).toHaveBeenCalledTimes(1);

      fireEvent.click(
        screen.getByRole("button", { name: "Replace with empty list" }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Add value" }));
      fireEvent.change(screen.getByDisplayValue(""), {
        target: { value: "duckduckgo" },
      });
      fireEvent.click(save);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith("/api/credentials/searxng", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credentials: { engines: ["duckduckgo"] } }),
        });
      });
    },
  );
});
