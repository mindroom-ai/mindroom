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
});
