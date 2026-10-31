import { IntegrationProvider, IntegrationScope } from "../types";
import { HomeAssistantIntegration } from "@/components/HomeAssistantIntegration/HomeAssistantIntegration";
import { API_BASE_URL, withAgentExecutionScope } from "@/lib/api";

const homeAssistantUrl = (
  action: "disconnect" | "status",
  scope?: IntegrationScope,
) =>
  withAgentExecutionScope(
    `${API_BASE_URL}/api/homeassistant/${action}`,
    scope?.agentName,
    scope?.executionScope,
  );

export const homeAssistantIntegration: IntegrationProvider = {
  SetupComponent: HomeAssistantIntegration,

  async disconnect(scope) {
    const response = await fetch(homeAssistantUrl("disconnect", scope), {
      method: "POST",
    });
    if (!response.ok) {
      throw new Error("Failed to disconnect Home Assistant");
    }
  },

  async loadStatus(scope) {
    try {
      const response = await fetch(homeAssistantUrl("status", scope));
      if (response.ok && (await response.json()).connected) {
        return { status: "connected" };
      }
    } catch (error) {
      console.error("Failed to load Home Assistant status:", error);
    }
    return { status: "available" };
  },
};
