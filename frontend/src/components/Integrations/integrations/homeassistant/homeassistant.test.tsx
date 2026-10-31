import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  API_BASE_URL: "https://backend.example.test",
}));

import { homeAssistantIntegration } from "./index";

global.fetch = vi.fn();

describe("HomeAssistantIntegrationProvider", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("loadStatus", () => {
    it("should return connected status when API returns connected", async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: true }),
      });

      const status = await homeAssistantIntegration.loadStatus();

      expect(status).toEqual({ status: "connected" });
      expect(global.fetch).toHaveBeenCalledWith(
        "https://backend.example.test/api/homeassistant/status",
      );
    });

    it("should return available status when not connected", async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      const status = await homeAssistantIntegration.loadStatus();

      expect(status).toEqual({ status: "available" });
    });

    it("should handle API errors gracefully", async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error("Network error"));

      const status = await homeAssistantIntegration.loadStatus();

      expect(status).toEqual({ status: "available" });
    });

    it("appends agent_name and execution_scope when checking scoped status", async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ connected: false }),
      });

      await homeAssistantIntegration.loadStatus({
        agentName: "code",
        executionScope: "shared",
      });

      expect(global.fetch).toHaveBeenCalledWith(
        "https://backend.example.test/api/homeassistant/status?agent_name=code&execution_scope=shared",
      );
    });
  });

  describe("disconnect", () => {
    it("fails when the backend rejects the disconnect", async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: false });

      await expect(homeAssistantIntegration.disconnect()).rejects.toThrow(
        "Failed to disconnect Home Assistant",
      );
    });

    it("appends agent_name and execution_scope to the disconnect request", async () => {
      (global.fetch as any).mockResolvedValueOnce({ ok: true });

      await homeAssistantIntegration.disconnect({
        agentName: "code",
        executionScope: "shared",
      });

      expect(global.fetch).toHaveBeenCalledWith(
        "https://backend.example.test/api/homeassistant/disconnect?agent_name=code&execution_scope=shared",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
