import {
  IntegrationProvider,
  IntegrationConfig,
  IntegrationScope,
  IntegrationStatus,
} from "../types";
import { HomeAssistantIntegration as HomeAssistantIntegrationComponent } from "@/components/HomeAssistantIntegration/HomeAssistantIntegration";
import { API_BASE_URL, withAgentExecutionScope } from "@/lib/api";

class HomeAssistantIntegrationProvider implements IntegrationProvider {
  getConfig(scope?: IntegrationScope): IntegrationConfig {
    const agentName = scope?.agentName ?? null;
    const executionScope = scope?.executionScope;
    return {
      onDisconnect: async () => {
        const response = await fetch(
          withAgentExecutionScope(
            `${API_BASE_URL}/api/homeassistant/disconnect`,
            agentName,
            executionScope,
          ),
          {
            method: "POST",
          },
        );
        if (!response.ok) {
          throw new Error("Failed to disconnect Home Assistant");
        }
      },
      ConfigComponent: (props) => (
        <HomeAssistantIntegrationComponent
          onSuccess={props.onSuccess}
          agentName={agentName}
          executionScope={executionScope}
        />
      ),
    };
  }

  async loadStatus(scope?: IntegrationScope): Promise<IntegrationStatus> {
    const agentName = scope?.agentName ?? null;
    const executionScope = scope?.executionScope;
    try {
      const response = await fetch(
        withAgentExecutionScope(
          `${API_BASE_URL}/api/homeassistant/status`,
          agentName,
          executionScope,
        ),
      );
      if (response.ok) {
        const data = await response.json();
        if (data.connected) {
          return { status: "connected" };
        }
      }
    } catch (error) {
      console.error("Failed to load Home Assistant status:", error);
    }
    return { status: "available" };
  }
}

export const homeAssistantIntegration = new HomeAssistantIntegrationProvider();
