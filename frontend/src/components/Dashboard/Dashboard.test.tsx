import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { Dashboard } from "./Dashboard";
import { useConfigStore } from "@/store/configStore";

vi.mock("./NetworkGraph", () => ({
  NetworkGraph: () => <div data-testid="network-graph" />,
}));

global.fetch = vi.fn();

describe("Dashboard", () => {
  beforeEach(() => {
    useConfigStore.setState({
      config: null,
      agents: [],
      teams: [],
      cultures: [],
      rooms: [],
      agentPoliciesByAgent: {},
      agentPoliciesStale: false,
      agentPoliciesRequestId: 0,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedCultureId: null,
      selectedRoomId: null,
      isDirty: false,
      isLoading: false,
      diagnostics: [],
      syncStatus: "disconnected",
      privateWorkerScopeBackups: {},
    });
    vi.clearAllMocks();
  });

  it("renders normalized tool names from mixed backend entries", async () => {
    const mockConfig = {
      agents: {
        code: {
          display_name: "Code Agent",
          role: "Writes code",
          tools: ["browser", { shell: { sandbox: "tight" } }],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
        },
      },
      defaults: {
        markdown: true,
      },
      models: {
        default: {
          provider: "ollama",
          id: "test-model",
        },
      },
    };

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockConfig,
    });
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        agent_policies: {
          code: {
            agent_name: "code",
            is_private: false,
            effective_execution_scope: null,
            scope_label: "unscoped",
            scope_source: "unscoped",
            dashboard_credentials_supported: true,
            team_eligibility_reason: null,
            private_knowledge_base_id: null,
            private_workspace_enabled: false,
            private_agent_knowledge_enabled: false,
          },
        },
      }),
    });

    await useConfigStore.getState().loadConfig();

    render(<Dashboard />);

    fireEvent.change(screen.getByPlaceholderText("Search..."), {
      target: { value: "shell" },
    });

    expect(screen.getByText("Code Agent")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Code Agent"));

    expect(screen.getByText("shell")).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });
});
