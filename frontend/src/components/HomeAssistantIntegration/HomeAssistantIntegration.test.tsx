import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { HomeAssistantIntegration } from "./HomeAssistantIntegration";

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

describe("HomeAssistantIntegration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        connected: false,
        has_credentials: false,
        entities_count: 0,
      }),
    }) as any;
  });

  it("shows the live API callback URL in OAuth setup instructions", async () => {
    render(<HomeAssistantIntegration />);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/homeassistant/status");
    });

    expect(
      await screen.findByText(
        `${window.location.origin}/api/homeassistant/callback`,
      ),
    ).toBeInTheDocument();
  });

  it("sends the private URL opt-in when connecting with OAuth", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          connected: false,
          has_credentials: false,
          entities_count: 0,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ auth_url: "https://homeassistant.example/auth" }),
      }) as any;
    vi.spyOn(window, "open").mockReturnValue({ closed: false } as Window);

    render(<HomeAssistantIntegration />);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/homeassistant/status");
    });

    fireEvent.change(screen.getByLabelText("Home Assistant URL"), {
      target: { value: "http://homeassistant.local:8123" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Allow private or local URL" }),
    );
    fireEvent.change(screen.getByLabelText("OAuth Client ID"), {
      target: { value: "client-id" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Connect with OAuth/i }),
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        "/api/homeassistant/connect/oauth",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            instance_url: "http://homeassistant.local:8123",
            client_id: "client-id",
            allow_private_url: true,
          }),
        }),
      );
    });
  });
});
