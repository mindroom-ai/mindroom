import { readFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import {
  act,
  cleanup,
  fireEvent,
  render,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentList } from "@/components/AgentList/AgentList";
import { AgentEditor } from "@/components/AgentEditor/AgentEditor";
import { ModelConfig } from "@/components/ModelConfig/ModelConfig";
import { Navigation } from "@/components/Navigation/Navigation";
import { useConfigStore } from "@/store/configStore";

// A navigation contract probe, not a browser screenshot test: real route links,
// real editors, agent cards, and store run in jsdom. The route shell models App's
// conditional workspaces; browser pixels and filesystem writes are intercepted.
function Workspaces() {
  const { pathname } = useLocation();
  return (
    <div id="root">
      <Navigation mode="desktop" />
      <output id="current-path">{pathname}</output>
      {pathname === "/dashboard" && (
        <section aria-label="Dashboard workspace">
          <h2>Home</h2>
        </section>
      )}
      {pathname === "/agents" && (
        <section aria-label="Agents workspace">
          <AgentList />
          <AgentEditor />
        </section>
      )}
      {pathname === "/models" && (
        <section aria-label="Models workspace">
          <ModelConfig />
        </section>
      )}
    </div>
  );
}

interface Capture {
  name: string;
  route: string | null;
  selectedAgent: string | null;
}

function screenshotProbe() {
  const captures: Capture[] = [];
  let closed = false;
  function element(selector: string) {
    const match = document.querySelector(selector);
    if (!match) throw new Error(`Required selector missing: ${selector}`);
    return match;
  }
  function handle(match: Element) {
    return {
      click: async () => {
        fireEvent.click(match);
      },
      element: match,
    };
  }
  const page = {
    setViewport: async () => {},
    goto: async () => {
      render(
        <MemoryRouter initialEntries={["/dashboard"]}>
          <Workspaces />
        </MemoryRouter>,
      );
    },
    waitForSelector: async (selector: string) =>
      waitFor(() => handle(element(selector))),
    click: async (selector: string) => {
      fireEvent.click(element(selector));
    },
    $$: async (selector: string) =>
      Array.from(document.querySelectorAll(selector), handle),
    evaluate: async (
      callback: (match: Element) => unknown,
      target: ReturnType<typeof handle>,
    ) => callback(target.element),
    screenshot: async ({ path: filename }: { path: string }) => {
      captures.push({
        name: path.basename(filename).replace(/-\d{4}-.*\.png$/, ".png"),
        route: document.querySelector("#current-path")?.textContent ?? null,
        selectedAgent: useConfigStore.getState().selectedAgentId,
      });
    },
  };
  const scriptModule = {
    exports: {} as { takeScreenshot: () => Promise<unknown> },
  };
  runInNewContext(
    readFileSync(path.resolve("scripts/screenshot.cjs"), "utf8"),
    {
      require: (name: string) => {
        if (name === "puppeteer")
          return {
            launch: async () => ({
              newPage: async () => page,
              close: async () => {
                closed = true;
              },
            }),
          };
        if (name === "path") return path;
        if (name === "fs") return { existsSync: () => true };
        throw new Error(`Unexpected dependency: ${name}`);
      },
      module: scriptModule,
      __dirname: path.resolve("scripts"),
      process: { env: {} },
      console: { log: () => {}, error: () => {} },
      setTimeout: (callback: () => void) => callback(),
    },
  );
  return {
    run: scriptModule.exports.takeScreenshot,
    captures,
    isClosed: () => closed,
  };
}

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const url = String(input);
    const data = url.includes("/api/tools")
      ? { tools: [] }
      : url.includes("/api/skills")
        ? []
        : { has_key: false };
    return new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  });
  useConfigStore.setState(useConfigStore.getInitialState(), true);
  useConfigStore.setState({
    config: {
      memory: { backend: "mem0" },
      models: { default: { provider: "openai", id: "gpt-6-astra" } },
      agents: {},
      defaults: { markdown: true },
      router: { model: "default" },
    },
    agentPoliciesByAgent: {
      helper: {
        agent_name: "helper",
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
    agents: [
      {
        id: "helper",
        display_name: "Helper",
        role: "Help",
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      },
    ],
    selectedAgentId: null,
  });
});
afterEach(cleanup);

describe("screenshot navigation", () => {
  it.each([
    {
      hasKey: false,
      toolOrder: ["tools", "skills"],
      keyOrder: ["provider", "model"],
    },
    {
      hasKey: true,
      toolOrder: ["skills", "tools"],
      keyOrder: ["model", "provider"],
    },
  ])(
    "waits for editor requests before capture, credentials configured=$hasKey",
    async ({ hasKey, toolOrder, keyOrder }) => {
      const pending = new Map<string, () => void>();
      vi.mocked(fetch).mockImplementation(async (input) => {
        const url = new URL(String(input), "http://localhost");
        let data: unknown;
        let gate: string | null = null;
        if (url.pathname === "/api/tools") {
          data = { tools: [] };
          if (url.searchParams.get("agent_name") === "helper") gate = "tools";
        } else if (url.pathname === "/api/skills") {
          data = [];
          gate = "skills";
        } else if (url.pathname === "/api/credentials/openai/api-key") {
          data = {
            has_key: hasKey,
            source: "environment",
            masked_key: "test...key",
          };
          gate = "provider";
        } else if (url.pathname === "/api/credentials/model:default/api-key") {
          data = { has_key: false };
          gate = "model";
        } else {
          throw new Error(`Unexpected request: ${url.pathname}`);
        }
        if (gate)
          await new Promise<void>((resolve) => pending.set(gate, resolve));
        return new Response(JSON.stringify(data), {
          headers: { "Content-Type": "application/json" },
        });
      });
      const probe = screenshotProbe();
      const running = probe.run();
      await waitFor(() =>
        expect(pending.has("tools") && pending.has("skills")).toBe(true),
      );
      expect(probe.captures).toHaveLength(1);
      await act(async () => pending.get(toolOrder[0])!());
      expect(probe.captures).toHaveLength(1);
      await act(async () => pending.get(toolOrder[1])!());
      await waitFor(() =>
        expect(pending.has("provider") && pending.has("model")).toBe(true),
      );
      expect(probe.captures).toHaveLength(2);
      await act(async () => pending.get(keyOrder[0])!());
      expect(probe.captures).toHaveLength(2);
      await act(async () => pending.get(keyOrder[1])!());
      await running;
      expect(probe.captures).toHaveLength(3);
      expect(probe.isClosed()).toBe(true);
    },
  );

  it("closes the browser without capturing an editor whose requests never settle", async () => {
    const respond = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input).includes("/api/credentials/")
        ? new Promise<Response>(() => {})
        : respond(input, init),
    );
    const probe = screenshotProbe();
    await expect(probe.run()).rejects.toThrow(/Required selector missing/);
    expect(probe.captures.map(({ name }) => name)).toEqual([
      "mindroom-dashboard-fullpage.png",
      "mindroom-dashboard-agents.png",
    ]);
    expect(probe.isClosed()).toBe(true);
  });

  it("captures all promised views after following actual route links and selecting an agent", async () => {
    const probe = screenshotProbe();
    await probe.run();
    expect(probe.captures).toEqual([
      {
        name: "mindroom-dashboard-fullpage.png",
        route: "/dashboard",
        selectedAgent: null,
      },
      {
        name: "mindroom-dashboard-agents.png",
        route: "/agents",
        selectedAgent: "helper",
      },
      {
        name: "mindroom-dashboard-models.png",
        route: "/models",
        selectedAgent: "helper",
      },
    ]);
    expect(probe.isClosed()).toBe(true);
  });

  it("fails clearly and closes the browser when no agent can be captured", async () => {
    useConfigStore.setState({ agents: [] });
    const probe = screenshotProbe();
    await expect(probe.run()).rejects.toThrow(/agent/i);
    expect(probe.captures.map(({ name }) => name)).toEqual([
      "mindroom-dashboard-fullpage.png",
    ]);
    expect(probe.isClosed()).toBe(true);
  });
});
