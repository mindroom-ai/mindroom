import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { PersonalAgents, type PersonalAgent } from "./PersonalAgents";
import { useConfigStore } from "@/store/configStore";
import type { Config } from "@/types/config";

vi.mock("@/store/configStore");

const agents: PersonalAgent[] = [
  {
    template: "mind",
    user_id: "@alice:example.org",
    agent_name: "mind_alice",
    matrix_user_id: "@mindroom_mind_alice:example.org",
    display_name: "Alice's Mind",
    pending: false,
    template_exists: true,
  },
  {
    template: "mind",
    user_id: "@bob:example.org",
    agent_name: "mind_bob",
    matrix_user_id: "@mindroom_mind_bob:example.org",
    display_name: null,
    pending: true,
    template_exists: true,
  },
  {
    template: "old",
    user_id: "@carol:example.org",
    agent_name: "old_carol",
    matrix_user_id: "@mindroom_old_carol:example.org",
    display_name: null,
    pending: false,
    template_exists: false,
  },
];

function renderSection(
  config: Partial<Config>,
  payload: PersonalAgent[] | { detail: string },
  status = 200,
) {
  vi.mocked(useConfigStore).mockReturnValue({
    config,
    committedGeneration: 1,
  } as unknown as ReturnType<typeof useConfigStore>);
  vi.mocked(fetch).mockResolvedValue(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <PersonalAgents />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, client };
}

beforeEach(() => vi.mocked(fetch).mockReset());
afterEach(() => cleanup());

describe("PersonalAgents", () => {
  it("lists each generated agent with its owner, account, template, and status", async () => {
    renderSection({ personal_agents: { mind: {} } }, agents);

    expect(await screen.findByText("3 agents")).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/api/config/personal-agents",
    );
    const alice = screen.getByText("Alice's Mind").closest("li")!;
    expect(alice).toHaveTextContent("@mindroom_mind_alice:example.org");
    expect(alice).toHaveTextContent("Owner @alice:example.org, template mind");
    expect(alice).toHaveTextContent("Active");
    expect(screen.getByText("mind_bob").closest("li")).toHaveTextContent(
      "Pending",
    );
    expect(screen.getByText("old_carol").closest("li")).toHaveTextContent(
      "Template removed",
    );
    expect(
      screen.getByRole("link", { name: "Settings → Personal rooms" }),
    ).toHaveAttribute("href", "/settings");
  });

  it("is hidden without templates or generated agents", async () => {
    const { container, client } = renderSection({}, []);

    await waitFor(() =>
      expect(client.getQueryState(["personal-agents", 1])?.status).toBe(
        "success",
      ),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("stays visible with its error when the request fails", async () => {
    const { client } = renderSection({}, { detail: "Roster unavailable" }, 500);

    await waitFor(() =>
      expect(client.getQueryState(["personal-agents", 1])?.status).toBe(
        "error",
      ),
    );
    expect(screen.getByText("Personal agents")).toBeInTheDocument();
  });
});
