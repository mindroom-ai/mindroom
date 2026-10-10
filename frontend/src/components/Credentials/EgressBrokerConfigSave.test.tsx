import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readConfigRoot, useConfigStore } from "@/store/configStore";
import { EgressBroker } from "./EgressBroker";

// These tests use the real config store, to show that the dashboard editor
// reads the config as authored and saves it the way Settings does.

const authoredConfig = {
  agents: {
    coder: {
      display_name: "Coder",
      role: "Writes code",
      tools: ["shell"],
      skills: [],
      instructions: [],
      rooms: [],
    },
  },
  defaults: { markdown: true },
  models: { default: { provider: "ollama", id: "test-model" } },
  router: { model: "default" },
  egress_broker: {
    services: {
      github: { preset: "github" },
      openai: {
        rules: [{ host: "api.openai.com", auth: { type: "bearer" } }],
      },
    },
  },
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

const row = (name: string, display_name: string) => ({
  name,
  display_name,
  description: "",
  source: "config",
  configured: false,
  updated_at: null,
  active_source: null,
  key_configured: false,
  key_updated_at: null,
  oauth: null,
});

let saveResponse: () => Response;

// The Config type does not model egress_broker, so read it like Settings does.
function storedBroker() {
  const config = useConfigStore.getState().config;
  return config
    ? (readConfigRoot(config, "egress_broker") as
        { services: Record<string, unknown> } | undefined)
    : undefined;
}

function saveBodies() {
  return vi
    .mocked(fetch)
    .mock.calls.filter(
      ([input, init]) =>
        String(input) === "/api/config/save" && init?.method === "PUT",
    )
    .map(([, init]) => JSON.parse(String(init?.body)));
}

beforeEach(async () => {
  saveResponse = () => json({ success: true });
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path === "/api/config/load") return json(authoredConfig);
    if (path === "/api/config/agent-policies")
      return json({ agent_policies: {} });
    if (path === "/api/config/save") return saveResponse();
    if (path === "/api/egress-broker/services")
      return json({
        services: [row("github", "GitHub"), row("openai", "OpenAI")],
      });
    if (path === "/api/egress-broker/logs") return json({ records: [] });
    throw new Error(`Unexpected request: ${String(input)}`);
  });
  useConfigStore.setState({ ...useConfigStore.getInitialState() });
  await useConfigStore.getState().loadConfig();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress broker panel with the real config store", () => {
  it("loads preset services as authored, not expanded", () => {
    expect(storedBroker()).toEqual(authoredConfig.egress_broker);
  });

  it("saves an edited preset service as the preset plus what changed", async () => {
    render(<EgressBroker />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Edit GitHub service" }),
    );
    fireEvent.click(screen.getByLabelText("Refuse other paths on these hosts"));
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

    await waitFor(() => expect(saveBodies()).toHaveLength(1));
    // Other services and the rest of the config go back untouched.
    expect(saveBodies()[0].egress_broker).toEqual({
      services: {
        github: { preset: "github", restrict_to_rules: true },
        openai: authoredConfig.egress_broker.services.openai,
      },
    });
    expect(saveBodies()[0].models).toEqual(authoredConfig.models);
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(useConfigStore.getState().isDirty).toBe(false);
    expect(storedBroker()?.services.github).toEqual({
      preset: "github",
      restrict_to_rules: true,
    });
  });

  it("saves a new service next to the others", async () => {
    render(<EgressBroker />);
    await screen.findByLabelText("GitHub");
    fireEvent.click(screen.getByRole("button", { name: "Add service" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "claude" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "anthropic" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

    await waitFor(() => expect(saveBodies()).toHaveLength(1));
    expect(saveBodies()[0].egress_broker.services).toEqual({
      ...authoredConfig.egress_broker.services,
      claude: { preset: "anthropic" },
    });
  });

  it("removes a deleted service from the saved config", async () => {
    render(<EgressBroker />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete OpenAI service" }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete",
      }),
    );

    await waitFor(() => expect(saveBodies()).toHaveLength(1));
    expect(saveBodies()[0].egress_broker.services).toEqual({
      github: { preset: "github" },
    });
  });

  it("shows the server's validation error and leaves no invalid draft behind", async () => {
    saveResponse = () =>
      json(
        {
          detail: [
            {
              loc: ["egress_broker", "services", "bad", "rules", 0, "host"],
              msg: "Value error, host must not contain scheme",
              type: "value_error",
            },
          ],
        },
        422,
      );
    render(<EgressBroker />);
    await screen.findByLabelText("GitHub");
    fireEvent.click(screen.getByRole("button", { name: "Add service" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "bad" },
    });
    fireEvent.change(screen.getByLabelText("Rule 1 host"), {
      target: { value: "api.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "bad.rules.0.host: host must not contain scheme",
    );
    expect(storedBroker()?.services).toEqual(
      authoredConfig.egress_broker.services,
    );
    expect(screen.getByLabelText("Name")).toHaveValue("bad");
  });
});
