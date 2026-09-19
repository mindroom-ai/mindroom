import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { Dashboard } from "./Dashboard";
import { useConfigStore } from "@/store/configStore";
import type { Agent, Config, Room, Team } from "@/types/config";

global.fetch = vi.fn();

const agents: Agent[] = [
  {
    id: "code",
    display_name: "Code Agent",
    role: "Writes and reviews code",
    model: "default",
    tools: ["browser", "shell"],
    skills: ["review"],
    instructions: [],
    rooms: ["lobby"],
  },
  {
    id: "analyst",
    display_name: "Research Analyst",
    role: "Finds evidence",
    tools: ["web_search"],
    skills: [],
    instructions: [],
    rooms: [],
  },
];

const rooms: Room[] = [
  {
    id: "lobby",
    display_name: "Lobby",
    description: "General requests",
    agents: ["code"],
    model: "fast",
  },
];

const teams: Team[] = [
  {
    id: "builders",
    display_name: "Builder Team",
    role: "Ships product changes",
    agents: ["code", "analyst"],
    rooms: ["lobby"],
    mode: "coordinate",
    model: "default",
  },
];

const config: Config = {
  memory: {
    embedder: { provider: "openai", config: { model: "embedding" } },
  },
  models: {
    default: { provider: "ollama", id: "qwen" },
    fast: { provider: "ollama", id: "qwen-small" },
  },
  agents: Object.fromEntries(agents.map(({ id, ...agent }) => [id, agent])),
  teams: Object.fromEntries(
    teams.map(({ id, rooms: _, ...team }) => [id, team]),
  ),
  rooms: {
    lobby: { display_name: "Lobby", description: "General requests" },
  },
  room_models: { lobby: "fast" },
  defaults: { markdown: true },
  router: { model: "default" },
};

function Pathname() {
  return (
    <output aria-label="Current pathname">{useLocation().pathname}</output>
  );
}

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Dashboard />
      <Pathname />
    </MemoryRouter>,
  );
}

function seedDashboard(
  overrides: Partial<ReturnType<typeof useConfigStore.getState>> = {},
) {
  useConfigStore.setState({
    config,
    agents,
    teams,
    rooms,
    ...overrides,
  });
}

describe("Dashboard", () => {
  beforeEach(() => {
    useConfigStore.setState({
      config: null,
      agents: [],
      teams: [],
      rooms: [],
      agentPoliciesByAgent: {},
      agentPoliciesStale: false,
      agentPoliciesRequestId: 0,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedRoomId: null,
      isDirty: false,
      isLoading: false,
      diagnostics: [],
      syncStatus: "disconnected",
      privateWorkerScopeBackups: {},
    });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (HTMLElement.prototype as { scrollIntoView?: unknown })
      .scrollIntoView;
  });

  it("searches normalized tools with trimmed case-insensitive input", async () => {
    const rawConfig = {
      ...config,
      agents: {
        code: {
          ...config.agents.code,
          tools: ["browser", { shell: { sandbox: "tight" } }],
        },
      },
      teams: {},
    };

    (global.fetch as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ ok: true, json: async () => rawConfig })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ agent_policies: {} }),
      });

    await useConfigStore.getState().loadConfig();
    renderDashboard();

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: " SHELL " },
    });

    expect(
      screen.getByRole("button", { name: /Code Agent/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("shows and searches the inherited scheduler tool", () => {
    seedDashboard({
      agents: [{ ...agents[0], tools: ["shell"] }],
      rooms: [],
      teams: [],
    });
    renderDashboard();

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "scheduler" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));

    expect(
      within(screen.getByRole("complementary")).getByText("shell, scheduler"),
    ).toBeVisible();
  });

  it("does not inherit default tools when the agent disables inheritance", () => {
    seedDashboard({
      agents: [
        { ...agents[0], tools: ["shell"], include_default_tools: false },
      ],
      rooms: [],
      teams: [],
    });
    renderDashboard();

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "scheduler" },
    });
    expect(
      screen.queryByRole("button", { name: /Code Agent/ }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));
    expect(
      within(screen.getByRole("complementary")).getByText("shell"),
    ).toBeVisible();
  });

  it("uses normalized configured defaults and deduplicates authored tools", async () => {
    const rawConfig = {
      ...config,
      agents: {
        code: {
          ...config.agents.code,
          tools: ["shell"],
        },
      },
      defaults: {
        ...config.defaults,
        tools: [{ shell: { sandbox: "tight" } }, "browser"],
      },
      teams: {},
    };
    (global.fetch as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ ok: true, json: async () => rawConfig })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ agent_policies: {} }),
      });

    await useConfigStore.getState().loadConfig();
    renderDashboard();
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "browser" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));

    expect(
      within(screen.getByRole("complementary")).getByText("shell, browser"),
    ).toBeVisible();
  });

  it("does not fabricate default model metadata for a room without an override", () => {
    seedDashboard({
      config: {
        ...config,
        models: {
          default: {
            provider: "ollama",
            id: "qwen-reasoner",
            display_name: "Local Reasoner",
          },
          fast: {
            provider: "openai",
            id: "gpt-fast",
            display_name: "Fast model",
          },
        },
      },
      agents: [
        { ...agents[0], model: "fast" },
        { ...agents[1], model: undefined },
      ],
      rooms: [{ ...rooms[0], agents: ["code"], model: undefined }],
      teams: [{ ...teams[0], model: undefined }],
    });
    renderDashboard();

    for (const term of [
      "default",
      "ollama",
      "qwen-reasoner",
      "LOCAL REASONER",
    ]) {
      fireEvent.change(screen.getByRole("searchbox"), {
        target: { value: term },
      });
      expect(
        screen.getByRole("button", { name: /Research Analyst/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /Builder Team/ }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /Lobby/ }),
      ).not.toBeInTheDocument();
    }

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    const roomRow = screen.getByRole("button", { name: /Lobby/ });
    expect(within(roomRow).getByText(/no room override/i)).toBeVisible();
    fireEvent.click(roomRow);
    expect(
      within(screen.getByRole("complementary")).getByText(
        "Uses agent/team models",
      ),
    ).toBeVisible();
  });

  it("searches explicit room override alias, provider, ID, and display name", () => {
    seedDashboard({
      config: {
        ...config,
        models: {
          ...config.models,
          fast: {
            provider: "openai",
            id: "gpt-fast",
            display_name: "Fast model",
          },
        },
      },
      rooms: [{ ...rooms[0], model: "fast" }],
    });
    renderDashboard();

    for (const term of ["fast", "openai", "gpt-fast", "FAST MODEL"]) {
      fireEvent.change(screen.getByRole("searchbox"), {
        target: { value: term },
      });
      expect(screen.getByRole("button", { name: /Lobby/ })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: /Lobby/ }));
    expect(
      within(screen.getByRole("complementary")).getByText(
        "fast · openai/gpt-fast",
      ),
    ).toBeVisible();
  });

  it("filters the directory by type and includes teams", () => {
    seedDashboard();
    renderDashboard();

    fireEvent.change(screen.getByRole("combobox", { name: /type/i }), {
      target: { value: "teams" },
    });

    expect(
      screen.getByRole("button", { name: /Builder Team/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Code Agent/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Lobby/ }),
    ).not.toBeInTheDocument();
  });

  it("opens the selected agent in its editor", () => {
    seedDashboard();
    renderDashboard();

    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));
    fireEvent.click(screen.getByRole("button", { name: /Open agent editor/i }));

    expect(useConfigStore.getState().selectedAgentId).toBe("code");
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      "/agents",
    );
  });

  it("keeps same-id selections scoped to the entity type", () => {
    seedDashboard({
      teams: [{ ...teams[0], id: "code", display_name: "Code Team" }],
    });
    renderDashboard();

    fireEvent.click(screen.getByRole("button", { name: /Code Team/ }));

    expect(screen.getByRole("heading", { name: "Code Team" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: /Open team editor/i }),
    ).toBeInTheDocument();
  });

  it("reveals and focuses selected details on small screens", () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    seedDashboard();
    renderDashboard();

    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));

    const details = screen.getByRole("complementary");
    expect(details).toHaveFocus();
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "auto",
      block: "start",
    });
  });

  it("offers a clear reset when no directory entries match", () => {
    seedDashboard();
    renderDashboard();

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "no such workspace item" },
    });

    expect(screen.getByText(/No workspace items match/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Reset filters/i }));
    expect(
      screen.getByRole("button", { name: /Code Agent/ }),
    ).toBeInTheDocument();
  });

  it("does not present fabricated runtime status", () => {
    seedDashboard();
    renderDashboard();

    expect(screen.queryByText(/Last updated/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Online|Busy|Idle/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId("network-graph")).not.toBeInTheDocument();
  });

  it("clears details when the selected item is removed", () => {
    seedDashboard();
    renderDashboard();

    fireEvent.click(screen.getByRole("button", { name: /Code Agent/ }));
    expect(screen.getByRole("heading", { name: "Code Agent" })).toBeVisible();

    act(() => {
      useConfigStore.setState({
        agents: agents.filter(({ id }) => id !== "code"),
      });
    });

    expect(
      screen.queryByRole("button", { name: /Open agent editor/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Code Agent" }),
    ).not.toBeInTheDocument();
  });
});
