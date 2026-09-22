import { readFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AgentList } from "@/components/AgentList/AgentList";
import { Navigation } from "@/components/Navigation/Navigation";
import { useConfigStore } from "@/store/configStore";

// A navigation contract probe, not a browser screenshot test: real route links,
// real agent cards, and the real store run in jsdom. The route shell models App's
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
        </section>
      )}
      {pathname === "/models" && (
        <section aria-label="Models workspace">
          <h2>Models</h2>
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
    waitForSelector: async (selector: string) => handle(element(selector)),
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
  useConfigStore.setState(useConfigStore.getInitialState(), true);
  useConfigStore.setState({
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
