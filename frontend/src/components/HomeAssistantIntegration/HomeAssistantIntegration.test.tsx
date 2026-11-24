import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { HomeAssistantIntegration } from "./HomeAssistantIntegration";

const { mockToast } = vi.hoisted(() => ({ mockToast: vi.fn() }));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
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

  it("connects with OAuth from the instance URL alone, sending the private URL opt-in", async () => {
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
            allow_private_url: true,
          }),
        }),
      );
    });
  });

  it.each([
    { method: "OAuth", message: "Failed to start OAuth flow" },
    { method: "Token", message: "Failed to connect" },
  ])(
    "reports a $method connect failure without a JSON body by its fallback message",
    async ({ method, message }) => {
      global.fetch = vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          json: async () => ({ connected: false }),
        })
        .mockResolvedValueOnce({
          ok: false,
          json: async () => {
            throw new SyntaxError("Unexpected token '<'");
          },
        }) as any;

      render(<HomeAssistantIntegration />);

      if (method === "Token") {
        fireEvent.mouseDown(
          await screen.findByRole("tab", { name: "Access Token" }),
          { button: 0 },
        );
        fireEvent.change(
          screen.getByLabelText("Access Token", { selector: "input" }),
          { target: { value: "token-value" } },
        );
      }
      fireEvent.change(await screen.findByLabelText("Home Assistant URL"), {
        target: { value: "http://homeassistant.example" },
      });
      fireEvent.click(
        screen.getByRole("button", { name: `Connect with ${method}` }),
      );

      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            description: message,
            variant: "destructive",
          }),
        );
      });
    },
  );

  it("connects with a token and notifies the parent", async () => {
    const onSuccess = vi.fn();
    render(
      <HomeAssistantIntegration
        onSuccess={onSuccess}
        agentName="helper"
        executionScope="user"
      />,
    );

    const tokenTab = await screen.findByRole("tab", { name: "Access Token" });
    fireEvent.mouseDown(tokenTab, { button: 0 });
    fireEvent.change(await screen.findByLabelText("Home Assistant URL"), {
      target: { value: "http://homeassistant.local:8123" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Allow private or local URL" }),
    );
    fireEvent.change(
      screen.getByLabelText("Access Token", { selector: "input" }),
      {
        target: { value: "token-value" },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Connect with Token/i }),
    );

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/homeassistant/connect/token?agent_name=helper&execution_scope=user",
      expect.objectContaining({
        body: JSON.stringify({
          instance_url: "http://homeassistant.local:8123",
          long_lived_token: "token-value",
          allow_private_url: true,
        }),
      }),
    );
  });
});
