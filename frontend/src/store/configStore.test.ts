import { describe, it, expect, beforeEach, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { readConfigRoot, useConfigStore } from "./configStore";
import type { ConfigPath } from "@/lib/configSchema";
import type { Agent, AgentPoliciesByAgent, Team, Config } from "@/types/config";

// Mock fetch globally
global.fetch = vi.fn();

function makeAgentPolicy(
  agentName: string,
  overrides: Partial<AgentPoliciesByAgent[string]> = {},
): AgentPoliciesByAgent[string] {
  return {
    agent_name: agentName,
    is_private: false,
    effective_execution_scope: null,
    scope_label: "unscoped",
    scope_source: "unscoped",
    dashboard_credentials_supported: true,
    team_eligibility_reason: null,
    private_knowledge_base_id: null,
    private_workspace_enabled: false,
    private_agent_knowledge_enabled: false,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

// The load response for a config whose first plugin entry fails validation.
function pluginValidationFailure() {
  return Response.json(
    {
      detail: [
        {
          loc: ["plugins", 0],
          msg: "Plugin tools_module must be a string",
          type: "value_error",
        },
      ],
    },
    { status: 422 },
  );
}

// Saves the draft and returns the payload of that save.
async function savedPayload() {
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ success: true }));
  await useConfigStore.getState().saveConfig();
  const saveCalls = vi
    .mocked(fetch)
    .mock.calls.filter(([url]) => url === "/api/config/save");
  const [, init] = saveCalls[saveCalls.length - 1];
  return JSON.parse(String(init?.body));
}

// Loads `config` together with the agent policies derived for it.
async function loadFixture(
  config: object,
  agentPolicies: AgentPoliciesByAgent = {},
) {
  vi.mocked(fetch).mockResolvedValueOnce(Response.json(config));
  vi.mocked(fetch).mockResolvedValueOnce(
    Response.json({ agent_policies: agentPolicies }),
  );
  await useConfigStore.getState().loadConfig();
}

describe("configStore", () => {
  beforeEach(() => {
    // Reset store state
    useConfigStore.setState(
      { ...useConfigStore.getInitialState(), committedGeneration: 0 },
      true,
    );

    // Clear all mocks
    vi.clearAllMocks();
  });

  describe.each(["structured", "raw"] as const)(
    "%s configuration conflicts",
    (mode) => {
      const conflictMessage =
        "Configuration changed elsewhere. Your draft has not been saved. Copy any changes you want to keep, then refresh this page and reapply them.";
      const save = () =>
        mode === "structured"
          ? useConfigStore.getState().saveConfig()
          : useConfigStore.getState().saveRecoveryConfigSource();
      const edit = () =>
        mode === "structured"
          ? useConfigStore.getState().updateConfigValue(["models", "default"], {
              provider: "test",
              id: "newer-edit",
            })
          : useConfigStore
              .getState()
              .updateRecoveryConfigSource("agents: {}\n# newer edit\n");

      beforeEach(() => {
        const config: Config = {
          agents: {},
          models: { default: { provider: "test", id: "local-edit" } },
          memory: {
            embedder: { provider: "test", config: { model: "test-embedder" } },
          },
          defaults: { markdown: true },
          router: { model: "default" },
        };
        useConfigStore.setState({
          committedGeneration: 7,
          config: mode === "structured" ? config : null,
          loadedConfig: mode === "structured" ? config : null,
          recoveryConfigSource:
            mode === "raw" ? "agents: {}\n# local edit\n" : null,
          recoveryConfigSourceOriginal: mode === "raw" ? "agents: {}\n" : null,
          isDirty: true,
          syncStatus: "error",
        });
      });

      it.each([false, true])(
        "reports a server conflict and preserves the draft (newer edits: %s)",
        async (newerEdits) => {
          const response = deferred<Response>();
          vi.mocked(fetch).mockReturnValueOnce(response.promise);
          const savePromise = save();
          if (newerEdits) edit();
          const draft = useConfigStore.getState();
          response.resolve(
            new Response(
              JSON.stringify({
                detail:
                  "Configuration changed while request was in progress. Retry the operation.",
              }),
              { status: 409 },
            ),
          );

          const result = await savePromise;
          expect(result).toEqual({
            status: "error",
            message: conflictMessage,
            diagnostics: [
              {
                kind: "global",
                code: "config_conflict",
                message: conflictMessage,
                blocking: mode === "raw",
              },
            ],
          });
          expect(useConfigStore.getState()).toMatchObject({
            config: draft.config,
            recoveryConfigSource: draft.recoveryConfigSource,
            recoveryConfigSourceOriginal: draft.recoveryConfigSourceOriginal,
            committedGeneration: 7,
            draftVersion: draft.draftVersion,
            isDirty: true,
            isLoading: false,
            syncStatus: "error",
            diagnostics: result.status === "error" ? result.diagnostics : [],
          });
          // A rejected full replacement must not refresh the generation and retry implicitly.
          expect(fetch).toHaveBeenCalledTimes(1);
          expect(fetch).toHaveBeenCalledWith(
            mode === "structured" ? "/api/config/save" : "/api/config/raw",
            expect.objectContaining({
              method: "PUT",
              headers: expect.objectContaining({
                "x-mindroom-config-generation": "7",
              }),
            }),
          );
        },
      );

      it("rejects retries locally while retaining conflict and validation diagnostics", async () => {
        const validationDiagnostic = {
          kind: "validation" as const,
          issue: {
            loc: ["agents", "helper", "role"],
            msg: "role is required",
            type: "value_error",
          },
        };
        useConfigStore.setState({ diagnostics: [validationDiagnostic] });
        vi.mocked(fetch).mockImplementation(
          async () =>
            new Response(JSON.stringify({ detail: "Configuration changed" }), {
              status: 409,
            }),
        );

        await save();
        edit();
        expect((await save()).status).toBe("error");

        expect(useConfigStore.getState().diagnostics).toEqual([
          {
            kind: "global",
            code: "config_conflict",
            message: conflictMessage,
            blocking: mode === "raw",
          },
          validationDiagnostic,
        ]);
        expect(useConfigStore.getState().committedGeneration).toBe(7);
        expect(fetch).toHaveBeenCalledTimes(1);
      });

      it("keeps conflict guidance after editing without validation errors", async () => {
        vi.mocked(fetch).mockResolvedValueOnce(
          new Response("{}", { status: 409 }),
        );
        await save();
        edit();

        expect(useConfigStore.getState().diagnostics).toEqual([
          {
            kind: "global",
            code: "config_conflict",
            message: conflictMessage,
            blocking: mode === "raw",
          },
        ]);
        expect(useConfigStore.getState().committedGeneration).toBe(7);
      });

      it("keeps conflict guidance when a reload fails", async () => {
        vi.mocked(fetch).mockResolvedValueOnce(
          new Response("{}", { status: 409 }),
        );
        await save();
        vi.mocked(fetch).mockRejectedValueOnce(
          new Error("Network unavailable"),
        );
        await useConfigStore.getState().loadConfig();

        expect(useConfigStore.getState().diagnostics).toEqual(
          expect.arrayContaining([
            expect.objectContaining({
              code: "config_conflict",
              message: conflictMessage,
            }),
          ]),
        );
        expect(useConfigStore.getState().committedGeneration).toBe(7);
      });

      it("ignores a conflict from a save superseded by a newer request", async () => {
        const response = deferred<Response>();
        vi.mocked(fetch).mockReturnValueOnce(response.promise);
        const savePromise = save();
        useConfigStore.setState({
          saveConfigRequestId: 2,
          syncStatus: "syncing",
        });
        response.resolve(
          new Response(JSON.stringify({ detail: "Configuration changed" }), {
            status: 409,
          }),
        );

        expect(await savePromise).toEqual({ status: "stale" });
        expect(useConfigStore.getState()).toMatchObject({
          diagnostics: [],
          syncStatus: "syncing",
        });
      });
    },
  );

  it("keeps a raw conflict after undoing edits and failing to reload", async () => {
    useConfigStore.setState({
      recoveryConfigSource: "agents: {}\n# draft\n",
      recoveryConfigSourceOriginal: "agents: {}\n",
      committedGeneration: 7,
      isDirty: true,
    });
    vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 409 }));
    await useConfigStore.getState().saveRecoveryConfigSource();
    useConfigStore.getState().updateRecoveryConfigSource("agents: {}\n");
    expect(useConfigStore.getState().isDirty).toBe(false);
    vi.mocked(fetch).mockRejectedValueOnce(new Error("Network unavailable"));
    await useConfigStore.getState().loadConfig();
    expect(useConfigStore.getState().diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "config_conflict" }),
      ]),
    );
    useConfigStore
      .getState()
      .updateRecoveryConfigSource("agents: {}\n# retry\n");
    expect(
      (await useConfigStore.getState().saveRecoveryConfigSource()).status,
    ).toBe("error");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("retains conflict guidance when an outstanding policy refresh finishes", async () => {
    const config: Config = {
      agents: {},
      models: { default: { provider: "test", id: "draft" } },
      memory: {
        embedder: { provider: "test", config: { model: "test-embedder" } },
      },
      defaults: { markdown: true },
      router: { model: "default" },
    };
    useConfigStore.setState({
      config,
      loadedConfig: config,
      committedGeneration: 7,
      isDirty: true,
    });
    const policies = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(policies.promise);
    const refresh = useConfigStore.getState().refreshAgentPolicies([]);
    vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 409 }));
    await useConfigStore.getState().saveConfig();
    policies.resolve(new Response(JSON.stringify({ agent_policies: {} })));
    await refresh;

    expect(useConfigStore.getState().diagnostics).toEqual([
      expect.objectContaining({ code: "config_conflict" }),
    ]);
    expect(useConfigStore.getState().committedGeneration).toBe(7);
  });

  describe("loadConfig", () => {
    it("keeps draft edits made during a reload even when they leave the draft clean", async () => {
      const config = {
        agents: {},
        models: { default: { provider: "ollama", id: "test-model" } },
        router: { model: "default" },
      };
      await loadFixture(config);
      const reload = deferred<Response>();
      vi.mocked(fetch).mockReturnValueOnce(reload.promise);
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ agent_policies: {} }),
      );
      const loadPromise = useConfigStore.getState().loadConfig();

      const { isDirty, dirtyRoots, draftVersion } = useConfigStore.getState();
      useConfigStore.getState().updateConfigValue(["router"], { model: "x" });
      useConfigStore.getState().restoreConfigValue(["router"], config.router, {
        isDirty,
        dirtyRoots,
        draftVersion,
      });
      expect(useConfigStore.getState().isDirty).toBe(false);

      const draft = useConfigStore.getState().config;
      reload.resolve(Response.json({ ...config, router: { model: "server" } }));
      await loadPromise;

      const state = useConfigStore.getState();
      expect(state.loadedConfig?.router).toEqual({ model: "server" });
      expect(state.config).toBe(draft);
    });

    it("should load configuration successfully", async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: "Test Agent",
            role: "Test role",
            tools: ["calculator"],
            skills: [],
            instructions: ["Test instruction"],
            rooms: ["lobby"],
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
      };

      await loadFixture(mockConfig, { test: makeAgentPolicy("test") });

      const state = useConfigStore.getState();
      expect(state.config).toEqual({
        ...mockConfig,
        knowledge_bases: {},
      });
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe("test");
      expect(state.agents[0].display_name).toBe("Test Agent");
      expect(state.agentPoliciesByAgent).toEqual({
        test: makeAgentPolicy("test"),
      });
      expect(state.syncStatus).toBe("synced");
    });

    it("records when the loaded config is composed from include files", async () => {
      const mockConfig = {
        agents: {},
        models: { default: { provider: "ollama", id: "test-model" } },
      };

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json(mockConfig, {
          headers: { "x-mindroom-config-uses-includes": "true" },
        }),
      );
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ agent_policies: {} }),
      );

      await useConfigStore.getState().loadConfig();

      expect(useConfigStore.getState().configUsesIncludes).toBe(true);
    });

    it("records when the saved event journal only takes effect after a restart", async () => {
      // The backend accepts the edit and keeps using the store it opened at
      // startup, so the editor showing the saved value without saying so would
      // be reporting a database nothing is writing to.
      const mockConfig = {
        agents: {},
        models: { default: { provider: "ollama", id: "test-model" } },
      };

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json(mockConfig, {
          headers: { "x-mindroom-config-pending-restart": "true" },
        }),
      );
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ agent_policies: {} }),
      );

      await useConfigStore.getState().loadConfig();

      expect(useConfigStore.getState().configJournalPendingRestart).toBe(true);
    });

    it("uses the room id fallback for blank authored room display names", async () => {
      const mockConfig = {
        agents: {},
        rooms: {
          project_room: {
            display_name: "   ",
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
      };

      await loadFixture(mockConfig);

      const state = useConfigStore.getState();
      expect(state.rooms).toEqual([
        {
          id: "project_room",
          display_name: "Project Room",
          description: "",
          agents: [],
          model: undefined,
        },
      ]);
      expect(state.config?.rooms?.project_room).not.toHaveProperty(
        "display_name",
      );
    });

    it("shows room-specific model overrides even without room metadata or memberships", async () => {
      const mockConfig = {
        agents: {},
        room_models: {
          project_room: "claude",
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
          claude: {
            provider: "anthropic",
            id: "claude-sonnet-5",
          },
        },
      };

      await loadFixture(mockConfig);

      expect(useConfigStore.getState().rooms).toEqual([
        {
          id: "project_room",
          display_name: "Project Room",
          description: "",
          agents: [],
          model: "claude",
        },
      ]);
    });

    describe("agents and teams that omit defaulted fields", () => {
      const authoredAgent = { display_name: "Test Agent" };
      const authoredTeam = {
        display_name: "Ops Team",
        role: "Coordinates operations",
        agents: ["test"],
      };

      async function loadAuthored({
        agent = authoredAgent as object,
        team = authoredTeam as object,
        defaults = { markdown: true } as object,
      } = {}) {
        await loadFixture(
          {
            agents: { test: agent },
            teams: { ops: team },
            models: { default: { provider: "ollama", id: "test-model" } },
            defaults,
            router: { model: "default" },
            memory: {
              embedder: {
                provider: "openai",
                config: { model: "text-embedding-3-small" },
              },
            },
          },
          { test: makeAgentPolicy("test") },
        );
      }

      it("fills them in on load", async () => {
        await loadAuthored();

        const state = useConfigStore.getState();
        expect(state.syncStatus).toBe("synced");
        expect(state.agents).toEqual([
          {
            id: "test",
            display_name: "Test Agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            knowledge_bases: [],
            delegate_to: [],
            context_files: [],
          },
        ]);
        expect(state.teams).toEqual([
          { id: "ops", ...authoredTeam, rooms: [], mode: "coordinate" },
        ]);
        expect(state.rooms).toEqual([]);
      });

      it("saves only the edited fields of agents and teams", async () => {
        await loadAuthored();

        useConfigStore.getState().updateAgent("test", {
          role: "Edited role",
          instructions: ["Be brief"],
        });
        useConfigStore.getState().updateTeam("ops", { mode: "collaborate" });

        const payload = await savedPayload();
        expect(payload.agents.test).toEqual({
          ...authoredAgent,
          role: "Edited role",
          instructions: ["Be brief"],
        });
        expect(payload.teams.ops).toEqual({
          ...authoredTeam,
          mode: "collaborate",
        });
      });

      it("saves untouched agents as authored", async () => {
        await loadAuthored();

        useConfigStore
          .getState()
          .updateConfigValue(["router"], { model: "default" });

        expect((await savedPayload()).agents.test).toEqual(authoredAgent);
      });

      it("keeps authored values, including empty ones", async () => {
        const agent = {
          ...authoredAgent,
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
          knowledge_bases: [],
          delegate_to: [],
          context_files: [],
        };
        const team = { ...authoredTeam, rooms: [], mode: "coordinate" };
        await loadAuthored({ agent, team });

        useConfigStore.getState().updateAgent("test", { role: "Edited role" });
        useConfigStore.getState().updateTeam("ops", { role: "Edited role" });

        const payload = await savedPayload();
        expect(payload.agents.test).toEqual({ ...agent, role: "Edited role" });
        expect(payload.teams.ops).toEqual({ ...team, role: "Edited role" });
      });

      it.each([
        {
          edits: "an instruction added and removed again",
          edit: () => {
            useConfigStore
              .getState()
              .updateAgent("test", { instructions: [""] });
            useConfigStore.getState().updateAgent("test", { instructions: [] });
          },
          agent: { instructions: [] },
        },
        {
          edits: "the team mode switched back to coordinate",
          edit: () => {
            useConfigStore
              .getState()
              .updateTeam("ops", { mode: "collaborate" });
            useConfigStore.getState().updateTeam("ops", { mode: "coordinate" });
          },
          team: { mode: "coordinate" },
        },
      ])("keeps a field after $edits", async ({ edit, agent, team }) => {
        await loadAuthored();

        edit();

        const payload = await savedPayload();
        expect(payload.agents.test).toEqual({ ...authoredAgent, ...agent });
        expect(payload.teams.ops).toEqual({ ...authoredTeam, ...team });
      });

      it("keeps untouched fields omitted after a reload during edits", async () => {
        await loadAuthored();
        useConfigStore.getState().updateAgent("test", { role: "Edited role" });

        await loadAuthored({ defaults: { markdown: false } });

        expect((await savedPayload()).agents.test).toEqual({
          ...authoredAgent,
          role: "Edited role",
        });
      });

      it("keeps untouched fields omitted after an edit during a save", async () => {
        await loadAuthored();
        useConfigStore.getState().updateAgent("test", { role: "First edit" });
        const response = deferred<Response>();
        vi.mocked(fetch).mockReturnValueOnce(response.promise);
        const firstSave = useConfigStore.getState().saveConfig();
        useConfigStore.getState().updateAgent("test", { role: "Second edit" });
        response.resolve(
          new Response(JSON.stringify({ success: true }), { status: 200 }),
        );
        expect(await firstSave).toEqual({ status: "stale" });

        expect((await savedPayload()).agents.test).toEqual({
          ...authoredAgent,
          role: "Second edit",
        });
      });

      it("treats an authored null like an omitted field", async () => {
        await loadAuthored({
          agent: { ...authoredAgent, instructions: null, rooms: null },
        });
        expect(useConfigStore.getState().agents[0]).toMatchObject({
          instructions: [],
          rooms: [],
        });

        useConfigStore.getState().updateAgent("test", { role: "Edited role" });

        expect((await savedPayload()).agents.test).toEqual({
          ...authoredAgent,
          role: "Edited role",
        });
      });
    });

    it("should preserve private agent configuration from the backend", async () => {
      const mockConfig = {
        agents: {
          mind: {
            display_name: "Mind",
            role: "Private assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: {
              per: "user",
              root: "mind_data",
              context_files: ["SOUL.md"],
              knowledge: {
                enabled: true,
                path: "memory",
                watch: true,
              },
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
      };

      await loadFixture(mockConfig, {
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          team_eligibility_reason:
            "Private agents cannot be configured as team members.",
          private_workspace_enabled: true,
        }),
      });

      const state = useConfigStore.getState();
      expect(state.agents[0].private).toEqual({
        per: "user",
        root: "mind_data",
        context_files: ["SOUL.md"],
        knowledge: {
          enabled: true,
          path: "memory",
          watch: true,
        },
      });
    });

    it("should handle load errors", async () => {
      const existingConfig: Config = {
        memory: {
          backend: "mem0",
          embedder: {
            provider: "openai",
            config: { model: "text-embedding-3-small" },
          },
        },
        models: {
          default: { provider: "ollama", id: "test-model" },
        },
        agents: {
          assistant: {
            display_name: "Assistant",
            role: "Helpful",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        defaults: { markdown: true },
        router: { model: "default" },
      };
      const existingAgent: Agent = {
        id: "assistant",
        display_name: "Assistant",
        role: "Helpful",
        tools: [],
        skills: [],
        instructions: [],
        rooms: ["lobby"],
      };
      useConfigStore.setState({
        config: existingConfig,
        agents: [existingAgent],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["assistant"],
          },
        ],
        selectedAgentId: "assistant",
        isDirty: true,
        privateWorkerScopeBackups: {
          assistant: "shared",
        },
        agentPoliciesByAgent: {
          assistant: makeAgentPolicy("assistant"),
        },
      });
      vi.mocked(fetch).mockRejectedValueOnce(new Error("Network error"));

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("error");
      expect(state.config).toEqual(existingConfig);
      expect(state.agents).toEqual([existingAgent]);
      expect(state.rooms).toEqual([
        {
          id: "lobby",
          display_name: "Lobby",
          description: "",
          agents: ["assistant"],
        },
      ]);
      expect(state.selectedAgentId).toBe("assistant");
      expect(state.isDirty).toBe(true);
      expect(state.privateWorkerScopeBackups).toEqual({
        assistant: "shared",
      });
      expect(state.agentPoliciesByAgent).toEqual({
        assistant: makeAgentPolicy("assistant"),
      });
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Network error",
          blocking: true,
        },
      ]);
    });

    it("stores backend validation issues and loads raw recovery source when load returns 422", async () => {
      await loadFixture(
        {
          agents: {
            assistant: {
              display_name: "Assistant",
              role: "Helpful",
              tools: [],
              skills: [],
              instructions: [],
              rooms: ["lobby"],
            },
          },
          models: {
            default: {
              provider: "ollama",
              id: "test-model",
            },
          },
        },
        { assistant: makeAgentPolicy("assistant") },
      );

      vi.mocked(fetch).mockResolvedValueOnce(pluginValidationFailure());
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          source: "agents:\n  assistant:\n    role: broken\n",
        }),
      );

      await useConfigStore.getState().loadConfig();

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("error");
      expect(state.config).toBeNull();
      expect(state.recoveryConfigSource).toBe(
        "agents:\n  assistant:\n    role: broken\n",
      );
      expect(state.recoveryConfigSourceOriginal).toBe(
        "agents:\n  assistant:\n    role: broken\n",
      );
      expect(state.agents).toEqual([]);
      expect(state.rooms).toEqual([]);
      expect(state.agentPoliciesByAgent).toEqual({});
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        {
          kind: "validation",
          issue: {
            loc: ["plugins", 0],
            msg: "Plugin tools_module must be a string",
            type: "value_error",
          },
        },
      ]);
    });

    it("surfaces raw recovery fetch failures instead of masking them as validation blockers", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(pluginValidationFailure());
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json(
          {
            detail:
              "Authentication required. Please log in to access this instance.",
          },
          { status: 401 },
        ),
      );

      await useConfigStore.getState().loadConfig();

      expect(useConfigStore.getState()).toMatchObject({
        config: null,
        recoveryConfigSource: null,
        syncStatus: "error",
        diagnostics: [
          {
            kind: "global",
            message:
              "Authentication required. Please log in to access this instance.",
            blocking: true,
          },
        ],
      });
    });

    it("ignores stale successful load results after a newer 422 failure", async () => {
      const pendingConfigResponse = deferred<Response>();

      vi.mocked(fetch)
        .mockReturnValueOnce(pendingConfigResponse.promise)
        .mockResolvedValueOnce(pluginValidationFailure())
        .mockResolvedValueOnce(
          Response.json({
            source: "plugins:\n  - bad\n",
          }),
        )
        .mockResolvedValueOnce(
          Response.json({
            agent_policies: { assistant: makeAgentPolicy("assistant") },
          }),
        );

      const firstLoadPromise = useConfigStore.getState().loadConfig();
      const secondLoadPromise = useConfigStore.getState().loadConfig();

      await secondLoadPromise;

      let state = useConfigStore.getState();
      expect(state.loadConfigRequestId).toBe(2);
      expect(state.syncStatus).toBe("error");
      expect(state.config).toBeNull();
      expect(state.recoveryConfigSource).toBe("plugins:\n  - bad\n");

      pendingConfigResponse.resolve(
        Response.json({
          agents: {
            assistant: {
              display_name: "Assistant",
              role: "Helpful",
              tools: [],
              skills: [],
              instructions: [],
              rooms: ["lobby"],
            },
          },
          models: {
            default: {
              provider: "ollama",
              id: "test-model",
            },
          },
        }),
      );

      await firstLoadPromise;

      state = useConfigStore.getState();
      expect(state.loadConfigRequestId).toBe(2);
      expect(state.syncStatus).toBe("error");
      expect(state.config).toBeNull();
      expect(state.agents).toEqual([]);
      expect(state.rooms).toEqual([]);
      expect(state.agentPoliciesByAgent).toEqual({});
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        {
          kind: "validation",
          issue: {
            loc: ["plugins", 0],
            msg: "Plugin tools_module must be a string",
            type: "value_error",
          },
        },
      ]);
    });

    it("invalidates in-flight policy refreshes when load returns 422", async () => {
      const pendingPolicyResponse = deferred<Response>();

      useConfigStore.setState({
        config: {
          memory: {
            backend: "mem0",
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {
            default: { provider: "ollama", id: "test-model" },
          },
          agents: {},
          defaults: { markdown: true },
          router: { model: "default" },
        },
        agentPoliciesByAgent: {
          assistant: makeAgentPolicy("assistant"),
        },
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingPolicyResponse.promise);
      const refreshPromise = useConfigStore.getState().refreshAgentPolicies([
        {
          id: "assistant",
          display_name: "Assistant",
          role: "Helpful",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ]);

      expect(useConfigStore.getState().agentPoliciesRequestId).toBe(1);

      vi.mocked(fetch).mockResolvedValueOnce(pluginValidationFailure());
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          source: "plugins:\n  - bad\n",
        }),
      );

      await useConfigStore.getState().loadConfig();

      let state = useConfigStore.getState();
      expect(state.agentPoliciesRequestId).toBe(2);
      expect(state.config).toBeNull();
      expect(state.recoveryConfigSource).toBe("plugins:\n  - bad\n");
      expect(state.agentPoliciesByAgent).toEqual({});

      pendingPolicyResponse.resolve(
        Response.json({
          agent_policies: {
            assistant: makeAgentPolicy("assistant"),
          },
        }),
      );

      await refreshPromise;

      state = useConfigStore.getState();
      expect(state.agentPoliciesRequestId).toBe(2);
      expect(state.config).toBeNull();
      expect(state.agentPoliciesByAgent).toEqual({});
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        {
          kind: "validation",
          issue: {
            loc: ["plugins", 0],
            msg: "Plugin tools_module must be a string",
            type: "value_error",
          },
        },
      ]);
    });

    it("preserves a newer dirty structured draft when a late load returns 422", async () => {
      const existingConfig = {
        memory: {
          backend: "mem0",
          embedder: {
            provider: "openai",
            config: { model: "text-embedding-3-small" },
          },
        },
        knowledge_bases: {},
        agents: {
          assistant: {
            display_name: "Assistant",
            role: "Original role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        defaults: {
          markdown: true,
        },
        models: {
          default: {
            provider: "ollama",
            id: "existing-model",
          },
        },
        router: {
          model: "default",
        },
      } satisfies Config;
      const pendingRawSourceResponse = deferred<Response>();

      useConfigStore.setState({
        loadedConfig: existingConfig,
        config: existingConfig,
        agents: [
          {
            id: "assistant",
            display_name: "Assistant",
            role: "Original role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
      });

      vi.mocked(fetch)
        .mockResolvedValueOnce(pluginValidationFailure())
        .mockReturnValueOnce(pendingRawSourceResponse.promise);

      const loadPromise = useConfigStore.getState().loadConfig();
      await waitFor(() => expect(vi.mocked(fetch).mock.calls).toHaveLength(2));

      useConfigStore
        .getState()
        .updateAgent("assistant", { role: "Edited role" });

      pendingRawSourceResponse.resolve(
        Response.json({
          source: "plugins:\n  - bad\n",
        }),
      );

      await loadPromise;

      const state = useConfigStore.getState();
      expect(state.agents[0].role).toBe("Edited role");
      expect(state.recoveryConfigSource).toBeNull();
      expect(state.isDirty).toBe(true);
      expect(state.syncStatus).toBe("error");
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["plugins", 0],
            msg: "Plugin tools_module must be a string",
            type: "value_error",
          },
        },
      ]);
    });

    it("preserves newer recovery edits when a retry load returns 422", async () => {
      const pendingRawSourceResponse = deferred<Response>();

      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: true,
          },
        ],
        syncStatus: "error",
        recoveryConfigSource: "plugins:\n  - bad\n",
        recoveryConfigSourceOriginal: "plugins:\n  - bad\n",
      });

      vi.mocked(fetch)
        .mockResolvedValueOnce(pluginValidationFailure())
        .mockReturnValueOnce(pendingRawSourceResponse.promise);

      const loadPromise = useConfigStore.getState().loadConfig();
      await waitFor(() => expect(vi.mocked(fetch).mock.calls).toHaveLength(2));

      useConfigStore
        .getState()
        .updateRecoveryConfigSource("plugins:\n  - fixed\n");

      pendingRawSourceResponse.resolve(
        Response.json({
          source: "plugins:\n  - bad\n",
        }),
      );

      await loadPromise;

      const state = useConfigStore.getState();
      expect(state.recoveryConfigSource).toBe("plugins:\n  - fixed\n");
      expect(state.recoveryConfigSourceOriginal).toBe("plugins:\n  - bad\n");
      expect(state.isDirty).toBe(true);
      expect(state.syncStatus).toBe("error");
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: true,
        },
        {
          kind: "validation",
          issue: {
            loc: ["plugins", 0],
            msg: "Plugin tools_module must be a string",
            type: "value_error",
          },
        },
      ]);
    });

    it("exits recovery mode when a retry load succeeds after local recovery edits", async () => {
      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: true,
          },
        ],
        syncStatus: "error",
        recoveryConfigSource: "agents:\n  helper:\n    role: Fixed locally\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        isDirty: true,
        draftVersion: 1,
      });

      await loadFixture(
        {
          agents: {
            helper: {
              display_name: "Helper",
              role: "Fixed on disk",
              tools: [],
              skills: [],
              instructions: [],
              rooms: [],
            },
          },
          models: {
            default: {
              provider: "ollama",
              id: "test-model",
            },
          },
        },
        { helper: makeAgentPolicy("helper") },
      );

      expect(useConfigStore.getState()).toMatchObject({
        recoveryConfigSource: null,
        recoveryConfigSourceOriginal: null,
        isDirty: false,
        syncStatus: "synced",
        diagnostics: [],
      });
      expect(useConfigStore.getState().config?.agents.helper.role).toBe(
        "Fixed on disk",
      );
    });

    it("saves raw recovery source and reloads the structured config", async () => {
      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: true,
          },
        ],
        syncStatus: "error",
        recoveryConfigSource: "agents:\n  helper:\n    role: Fixed\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        isDirty: true,
      });

      vi.mocked(fetch)
        .mockResolvedValueOnce(Response.json({ success: true }))
        .mockResolvedValueOnce(
          Response.json({
            agents: {
              helper: {
                display_name: "Helper",
                role: "Fixed",
                tools: [],
                skills: [],
                instructions: [],
                rooms: [],
              },
            },
            models: {
              default: {
                provider: "ollama",
                id: "test-model",
              },
            },
          }),
        )
        .mockResolvedValueOnce(
          Response.json({
            agent_policies: { helper: makeAgentPolicy("helper") },
          }),
        );

      const result = await useConfigStore.getState().saveRecoveryConfigSource();

      expect(result).toEqual({ status: "saved" });
      expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/api/config/raw");
      expect(vi.mocked(fetch).mock.calls[1][0]).toBe("/api/config/load");
      expect(useConfigStore.getState()).toMatchObject({
        recoveryConfigSource: null,
        recoveryConfigSourceOriginal: null,
        syncStatus: "synced",
        isDirty: false,
        diagnostics: [],
      });
      expect(useConfigStore.getState().config?.agents.helper.role).toBe(
        "Fixed",
      );
    });

    it("returns an error when the post-save recovery reload fails", async () => {
      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: true,
          },
        ],
        syncStatus: "error",
        recoveryConfigSource: "agents:\n  helper:\n    role: Fixed\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        isDirty: true,
      });

      vi.mocked(fetch)
        .mockResolvedValueOnce(
          Response.json(
            { success: true },
            { headers: { "x-mindroom-config-generation": "7" } },
          ),
        )
        .mockResolvedValueOnce(
          Response.json(
            {
              detail:
                "Authentication required. Please log in to access this instance.",
            },
            { status: 401 },
          ),
        );

      const result = await useConfigStore.getState().saveRecoveryConfigSource();

      expect(result).toEqual({
        status: "error",
        message:
          "Authentication required. Please log in to access this instance.",
        diagnostics: [
          {
            kind: "global",
            message:
              "Authentication required. Please log in to access this instance.",
            blocking: true,
          },
        ],
      });
      expect(useConfigStore.getState()).toMatchObject({
        committedGeneration: 7,
        recoveryConfigSource: "agents:\n  helper:\n    role: Fixed\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        isDirty: true,
        syncStatus: "error",
      });
    });

    it("preserves newer recovery edits when an older recovery save finishes later", async () => {
      const pendingSaveResponse = deferred<Response>();

      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: true,
          },
        ],
        syncStatus: "error",
        recoveryConfigSource: "agents:\n  helper:\n    role: Fixed\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        draftVersion: 1,
        isDirty: true,
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingSaveResponse.promise);

      const savePromise = useConfigStore.getState().saveRecoveryConfigSource();
      useConfigStore
        .getState()
        .updateRecoveryConfigSource(
          "agents:\n  helper:\n    role: Newer fix\n",
        );

      pendingSaveResponse.resolve(Response.json({ success: true }));

      const result = await savePromise;

      expect(result).toEqual({ status: "stale" });
      expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
      expect(useConfigStore.getState()).toMatchObject({
        recoveryConfigSource: "agents:\n  helper:\n    role: Newer fix\n",
        recoveryConfigSourceOriginal: "agents:\n  helper:\n    role: Broken\n",
        syncStatus: "error",
        isDirty: true,
      });
    });

    it("loads config and records a non-blocking diagnostic when agent policy derivation fails during reload", async () => {
      const existingConfig = {
        memory: {
          backend: "mem0",
          embedder: {
            provider: "openai",
            config: { model: "text-embedding-3-small" },
          },
        },
        knowledge_bases: {},
        agents: {
          existing: {
            display_name: "Existing Agent",
            role: "Existing role",
            tools: ["calculator"],
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
            id: "existing-model",
          },
        },
        router: {
          model: "default",
        },
      } satisfies Config;

      useConfigStore.setState({
        config: existingConfig,
        agents: [
          {
            id: "existing",
            display_name: "Existing Agent",
            role: "Existing role",
            tools: ["calculator"],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        agentPoliciesByAgent: {
          existing: makeAgentPolicy("existing"),
        },
        syncStatus: "synced",
      });

      const replacementConfig = {
        agents: {
          replacement: {
            display_name: "Replacement Agent",
            role: "Replacement role",
            tools: ["weather"],
            skills: [],
            instructions: [],
            rooms: ["desk"],
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "replacement-model",
          },
        },
      };

      vi.mocked(fetch).mockResolvedValueOnce(Response.json(replacementConfig));
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ detail: "boom" }, { status: 500 }),
      );

      await useConfigStore.getState().loadConfig();

      const state = useConfigStore.getState();
      expect(state.config).toEqual({
        ...replacementConfig,
        knowledge_bases: {},
      });
      expect(state.agents).toEqual([
        {
          id: "replacement",
          display_name: "Replacement Agent",
          role: "Replacement role",
          tools: ["weather"],
          skills: [],
          instructions: [],
          rooms: ["desk"],
          knowledge_bases: [],
          delegate_to: [],
          context_files: [],
        },
      ]);
      expect(state.agentPoliciesByAgent).toEqual({});
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Failed to derive agent policies",
          blocking: false,
        },
      ]);
      expect(state.syncStatus).toBe("synced");
    });
  });

  describe("refreshAgentPolicies", () => {
    it("stores backend-derived agent policies", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            helper: makeAgentPolicy("helper"),
            mind: makeAgentPolicy("mind", {
              is_private: true,
              effective_execution_scope: "user",
              scope_label: "private.per=user",
              scope_source: "private.per",
              dashboard_credentials_supported: false,
              team_eligibility_reason:
                "Private agents cannot be configured as team members.",
              private_workspace_enabled: true,
            }),
          },
        }),
      );

      useConfigStore.setState({
        config: {
          memory: {
            backend: "mem0",
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {
            default: { provider: "ollama", id: "test-model" },
          },
          agents: {},
          defaults: { markdown: true },
          router: { model: "default" },
        },
      });

      await useConfigStore.getState().refreshAgentPolicies([
        {
          id: "helper",
          display_name: "Helper",
          role: "Helps",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: "mind",
          display_name: "Mind",
          role: "Private",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
          private: { per: "user" },
        },
      ]);

      expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
        helper: makeAgentPolicy("helper"),
        mind: makeAgentPolicy("mind", {
          is_private: true,
          effective_execution_scope: "user",
          scope_label: "private.per=user",
          scope_source: "private.per",
          dashboard_credentials_supported: false,
          team_eligibility_reason:
            "Private agents cannot be configured as team members.",
          private_workspace_enabled: true,
        }),
      });
    });

    it("invalidates the current preview while a refresh is in flight", async () => {
      const pendingResponse = deferred<Response>();

      vi.mocked(fetch).mockReturnValueOnce(pendingResponse.promise);

      useConfigStore.setState({
        config: {
          memory: {
            backend: "mem0",
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {
            default: { provider: "ollama", id: "test-model" },
          },
          agents: {},
          defaults: { markdown: true },
          router: { model: "default" },
        },
        agentPoliciesByAgent: {
          helper: makeAgentPolicy("helper"),
        },
      });

      const refreshPromise = useConfigStore.getState().refreshAgentPolicies([
        {
          id: "helper",
          display_name: "Helper",
          role: "Helps",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ]);

      expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({});
      expect(useConfigStore.getState().agentPoliciesStale).toBe(true);

      pendingResponse.resolve(
        Response.json({
          agent_policies: {
            helper: makeAgentPolicy("helper"),
          },
        }),
      );

      await refreshPromise;

      expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
        helper: makeAgentPolicy("helper"),
      });
      expect(useConfigStore.getState().agentPoliciesStale).toBe(false);
    });

    it("clears policies and records a non-blocking diagnostic when refresh fails", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ detail: "boom" }, { status: 500 }),
      );

      useConfigStore.setState({
        config: {
          memory: {
            backend: "mem0",
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {
            default: { provider: "ollama", id: "test-model" },
          },
          agents: {},
          defaults: { markdown: true },
          router: { model: "default" },
        },
        agentPoliciesByAgent: {
          helper: makeAgentPolicy("helper"),
        },
      });

      await useConfigStore.getState().refreshAgentPolicies([
        {
          id: "helper",
          display_name: "Helper",
          role: "Helps",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ]);

      expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({});
      expect(useConfigStore.getState().diagnostics).toEqual([
        {
          kind: "global",
          message: "Failed to derive agent policies",
          blocking: false,
        },
      ]);
    });
  });

  describe("saveConfig", () => {
    it.each([
      { mode: "structured", blocking: false },
      { mode: "raw", blocking: true },
    ] as const)(
      "refuses a $mode save without a committed generation",
      async ({ mode, blocking }) => {
        useConfigStore.setState({
          committedGeneration: null,
          config:
            mode === "structured"
              ? ({ agents: {}, models: {} } as unknown as Config)
              : null,
          recoveryConfigSource: mode === "raw" ? "agents: {}\n" : null,
        });

        const result =
          mode === "structured"
            ? await useConfigStore.getState().saveConfig()
            : await useConfigStore.getState().saveRecoveryConfigSource();

        const diagnostics = [
          {
            kind: "global",
            message: "Missing committed configuration generation.",
            blocking,
          },
        ];
        expect(result).toEqual({
          status: "error",
          message: "Missing committed configuration generation.",
          diagnostics,
        });
        expect(useConfigStore.getState()).toMatchObject({
          diagnostics,
          isLoading: false,
          syncStatus: "error",
        });
        expect(fetch).not.toHaveBeenCalled();
      },
    );

    it("surfaces the includes rejection message instead of a stale retry", async () => {
      const includesMessage =
        "configuration is composed from multiple files via !include; edit the source files instead";
      useConfigStore.setState({
        config: {
          agents: {},
          models: { default: { provider: "ollama", id: "test-model" } },
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          defaults: { markdown: true },
          router: { model: "default" },
        } as Config,
        isDirty: true,
        syncStatus: "synced",
      });
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json(
          {
            detail: {
              code: "config_composed_from_includes",
              message: includesMessage,
            },
          },
          { status: 409 },
        ),
      );

      const result = await useConfigStore.getState().saveConfig();

      expect(result).toEqual({
        status: "error",
        message: includesMessage,
        diagnostics: [
          { kind: "global", message: includesMessage, blocking: false },
        ],
      });
      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("error");
      expect(state.configUsesIncludes).toBe(true);
    });

    it("should save configuration successfully", async () => {
      // Set up initial state with agents array
      const mockConfig: Config = {
        agents: {
          test: {
            display_name: "Test",
            role: "Test role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        models: {},
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const mockAgents = [
        {
          id: "test",
          display_name: "Test",
          role: "Test role",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ];
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        isDirty: true,
        syncStatus: "synced",
      });
      vi.mocked(fetch).mockResolvedValueOnce(Response.json({ success: true }));

      const { saveConfig } = useConfigStore.getState();
      await saveConfig();

      // The saveConfig removes the id field when saving
      const { id: _id, ...agentWithoutId } = mockAgents[0];
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenNthCalledWith(1, "/api/config/save", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "x-mindroom-config-generation": "0",
        },
        body: JSON.stringify({
          ...mockConfig,
          agents: { test: agentWithoutId },
        }),
      });

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(false);
      expect(state.syncStatus).toBe("synced");
      expect(state.config?.agents).toEqual({ test: agentWithoutId });
    });

    it("stores backend validation issues without poisoning the global load error", async () => {
      const mockConfig: Config = {
        models: {
          default: { provider: "test", id: "test-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        agents: {},
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const mockAgents = [
        {
          id: "mind",
          display_name: "Mind",
          role: "Assistant",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
          private: {
            per: "user" as const,
            root: "../outside",
          },
        },
      ];
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        isDirty: true,
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json(
          {
            detail: [
              {
                loc: ["agents", "mind", "private", "root"],
                msg: "private.root must stay within the private instance root",
                type: "value_error",
              },
            ],
          },
          { status: 422 },
        ),
      );

      const result = await useConfigStore.getState().saveConfig();

      const state = useConfigStore.getState();
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "mind", "private", "root"],
            msg: "private.root must stay within the private instance root",
            type: "value_error",
          },
        },
      ]);
      expect(result).toEqual({
        status: "error",
        message: "Configuration validation failed",
        diagnostics: state.diagnostics,
      });
    });

    it("ignores stale successful save results after a newer validation failure", async () => {
      const pendingSaveResponse = deferred<Response>();
      const config: Config = {
        models: {
          default: { provider: "test", id: "test-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        agents: {
          helper: {
            display_name: "Helper",
            role: "Helpful",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const agents = [
        {
          id: "helper",
          display_name: "Helper",
          role: "Helpful",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ];
      useConfigStore.setState({
        config,
        agents,
        isDirty: true,
      });

      vi.mocked(fetch)
        .mockReturnValueOnce(pendingSaveResponse.promise)
        .mockResolvedValueOnce(
          Response.json(
            {
              detail: [
                {
                  loc: ["agents", "helper", "role"],
                  msg: "role is required",
                  type: "value_error",
                },
              ],
            },
            { status: 422 },
          ),
        );

      const firstSavePromise = useConfigStore.getState().saveConfig();
      const secondSavePromise = useConfigStore.getState().saveConfig();

      await secondSavePromise;

      let state = useConfigStore.getState();
      expect(state.saveConfigRequestId).toBe(2);
      expect(state.syncStatus).toBe("error");
      expect(state.isDirty).toBe(true);
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "helper", "role"],
            msg: "role is required",
            type: "value_error",
          },
        },
      ]);

      pendingSaveResponse.resolve(Response.json({ success: true }));
      await firstSavePromise;

      state = useConfigStore.getState();
      expect(state.saveConfigRequestId).toBe(2);
      expect(state.syncStatus).toBe("error");
      expect(state.isDirty).toBe(true);
      expect(state.diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "helper", "role"],
            msg: "role is required",
            type: "value_error",
          },
        },
      ]);
    });

    it("preserves newer draft edits when an older save finishes later", async () => {
      const pendingSaveResponse = deferred<Response>();
      const config: Config = {
        models: {
          default: { provider: "test", id: "test-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        agents: {
          helper: {
            display_name: "Helper",
            role: "Helpful",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const agents = [
        {
          id: "helper",
          display_name: "Helper",
          role: "Helpful",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ];
      useConfigStore.setState({
        config,
        agents,
        isDirty: true,
        privateWorkerScopeBackups: {
          helper: "shared",
        },
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingSaveResponse.promise);

      const savePromise = useConfigStore.getState().saveConfig();
      useConfigStore
        .getState()
        .updateAgent("helper", { role: "Still editing" });

      pendingSaveResponse.resolve(Response.json({ success: true }));
      await savePromise;

      const state = useConfigStore.getState();
      expect(state.agents[0].role).toBe("Still editing");
      expect(state.isDirty).toBe(true);
      expect(state.privateWorkerScopeBackups).toEqual({
        helper: "shared",
      });
      expect(state.syncStatus).toBe("error");
    });

    it("retains unrelated validation diagnostics when a stale save is superseded by newer edits", async () => {
      const pendingSaveResponse = deferred<Response>();
      const config: Config = {
        agents: {
          helper: {
            display_name: "Helper",
            role: "",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        teams: {
          ops: {
            display_name: "",
            role: "Runs ops",
            agents: ["helper"],
            rooms: [],
            mode: "coordinate",
          },
        },
        defaults: { markdown: true },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        models: {
          default: { provider: "test", id: "test-model" },
        },
        router: { model: "default" },
      };
      useConfigStore.setState({
        config,
        loadedConfig: config,
        agents: [
          {
            id: "helper",
            display_name: "Helper",
            role: "",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: "ops",
            display_name: "",
            role: "Runs ops",
            agents: ["helper"],
            rooms: [],
            mode: "coordinate",
          },
        ],
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: false,
          },
          {
            kind: "validation",
            issue: {
              loc: ["agents", "helper", "role"],
              msg: "role is required",
              type: "value_error",
            },
          },
          {
            kind: "validation",
            issue: {
              loc: ["teams", "ops", "display_name"],
              msg: "display_name is required",
              type: "value_error",
            },
          },
        ],
        isDirty: true,
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingSaveResponse.promise);

      const savePromise = useConfigStore.getState().saveConfig();
      useConfigStore.getState().updateAgent("helper", { role: "Now valid" });

      pendingSaveResponse.resolve(Response.json({ success: true }));

      expect(await savePromise).toEqual({ status: "stale" });
      expect(useConfigStore.getState().diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["teams", "ops", "display_name"],
            msg: "display_name is required",
            type: "value_error",
          },
        },
      ]);
    });

    it("preserves newer voice edits when an older save finishes later", async () => {
      const pendingSaveResponse = deferred<Response>();
      const config: Config = {
        models: {
          default: { provider: "test", id: "test-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        agents: {},
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
        voice: {
          enabled: false,
          visible_router_echo: true,
          stt: {
            provider: "openai",
            model: "whisper-1",
          },
          intelligence: {
            model: "default",
          },
        },
      };
      useConfigStore.setState({
        config,
        agents: [],
        isDirty: true,
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingSaveResponse.promise);

      const savePromise = useConfigStore.getState().saveConfig();
      useConfigStore.getState().updateConfigValue(["voice"], {
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: "openai",
          model: "whisper-1",
          host: "http://localhost:8080",
        },
        intelligence: {
          model: "default",
        },
      });

      pendingSaveResponse.resolve(Response.json({ success: true }));
      await savePromise;

      const state = useConfigStore.getState();
      expect(state.config?.voice).toEqual({
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: "openai",
          model: "whisper-1",
          host: "http://localhost:8080",
        },
        intelligence: {
          model: "default",
        },
      });
      expect(state.isDirty).toBe(true);
      expect(state.syncStatus).toBe("error");
    });

    it("ignores successful load results while a dirty draft save is still in flight", async () => {
      const pendingSaveResponse = deferred<Response>();
      const loadedConfig: Config = {
        models: {
          default: { provider: "test", id: "old-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        agents: {
          helper: {
            display_name: "Helper",
            role: "Helpful",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const draftConfig: Config = {
        ...loadedConfig,
        models: {
          default: { provider: "test", id: "new-model" },
        },
      };
      const agents = [
        {
          id: "helper",
          display_name: "Helper",
          role: "Helpful",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ];
      useConfigStore.setState({
        loadedConfig,
        config: draftConfig,
        agents,
        isDirty: true,
        dirtyRoots: ["models"],
      });

      vi.mocked(fetch)
        .mockReturnValueOnce(pendingSaveResponse.promise)
        .mockResolvedValueOnce(Response.json(loadedConfig))
        .mockResolvedValueOnce(
          Response.json({
            agent_policies: { helper: makeAgentPolicy("helper") },
          }),
        );

      const savePromise = useConfigStore.getState().saveConfig();
      const loadPromise = useConfigStore.getState().loadConfig();

      await loadPromise;

      let state = useConfigStore.getState();
      expect(state.config?.models.default.id).toBe("new-model");
      expect(state.loadedConfig?.models.default.id).toBe("old-model");
      expect(state.isDirty).toBe(true);
      expect(state.syncStatus).toBe("syncing");

      pendingSaveResponse.resolve(Response.json({ success: true }));
      await savePromise;

      state = useConfigStore.getState();
      expect(state.config?.models.default.id).toBe("new-model");
      expect(state.loadedConfig?.models.default.id).toBe("new-model");
      expect(state.isDirty).toBe(false);
      expect(state.syncStatus).toBe("synced");
    });

    it("saves against the latest loaded config for untouched sections", async () => {
      const loadedConfig: Config = {
        agents: {
          assistant: {
            display_name: "Assistant",
            role: "Original role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        models: {
          default: { provider: "test", id: "test-model" },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
        voice: {
          enabled: false,
          visible_router_echo: true,
          stt: {
            provider: "openai",
            model: "whisper-1",
          },
          intelligence: {
            model: "default",
          },
        },
      };
      const staleDraftConfig: Config = {
        ...loadedConfig,
        voice: {
          ...loadedConfig.voice!,
          visible_router_echo: false,
        },
      };
      useConfigStore.setState({
        loadedConfig,
        config: staleDraftConfig,
        agents: [
          {
            id: "assistant",
            display_name: "Assistant",
            role: "Edited role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        isDirty: true,
        dirtyRoots: ["agents"],
      });

      const saveBody = await savedPayload();
      expect(saveBody.voice.visible_router_echo).toBe(true);
      expect(saveBody.agents.assistant.role).toBe("Edited role");
      expect(useConfigStore.getState().config?.voice?.visible_router_echo).toBe(
        true,
      );
    });

    it("clears validation issues for fields edited after a failed save while keeping unrelated ones", async () => {
      const config: Config = {
        agents: {
          helper: {
            display_name: "Helper",
            role: "",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        teams: {
          ops: {
            display_name: "",
            role: "Runs ops",
            agents: ["helper"],
            rooms: [],
            mode: "coordinate",
          },
        },
        defaults: { markdown: true },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        models: {
          default: { provider: "test", id: "test-model" },
        },
        router: { model: "default" },
      };
      const diagnostics = [
        {
          kind: "global" as const,
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation" as const,
          issue: {
            loc: ["agents", "helper", "role"],
            msg: "role is required",
            type: "value_error",
          },
        },
        {
          kind: "validation" as const,
          issue: {
            loc: ["teams", "ops", "display_name"],
            msg: "display_name is required",
            type: "value_error",
          },
        },
      ];
      useConfigStore.setState({
        loadedConfig: config,
        config,
        agents: [
          {
            id: "helper",
            display_name: "Helper",
            role: "",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: "ops",
            display_name: "",
            role: "Runs ops",
            agents: ["helper"],
            rooms: [],
            mode: "coordinate",
          },
        ],
        diagnostics,
        syncStatus: "error",
      });

      useConfigStore.getState().updateAgent("helper", { role: "Now valid" });

      expect(useConfigStore.getState().diagnostics).toEqual([
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["teams", "ops", "display_name"],
            msg: "display_name is required",
            type: "value_error",
          },
        },
      ]);
    });

    it("refreshes agent policies after a successful save when preview state is stale", async () => {
      const mockConfig: Config = {
        agents: {
          helper: {
            display_name: "Helper",
            role: "Helps",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        models: {},
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };
      const mockAgents = [
        {
          id: "helper",
          display_name: "Helper",
          role: "Helps",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ];
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        agentPoliciesByAgent: {},
        agentPoliciesStale: true,
        diagnostics: [],
        isDirty: true,
      });
      vi.mocked(fetch)
        .mockResolvedValueOnce(Response.json({ success: true }))
        .mockResolvedValueOnce(
          Response.json({
            agent_policies: {
              helper: makeAgentPolicy("helper"),
            },
          }),
        );

      await useConfigStore.getState().saveConfig();

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          helper: makeAgentPolicy("helper"),
        });
      });

      expect(useConfigStore.getState().agentPoliciesStale).toBe(false);
      expect(global.fetch).toHaveBeenNthCalledWith(
        1,
        "/api/config/save",
        expect.any(Object),
      );
      expect(global.fetch).toHaveBeenNthCalledWith(
        2,
        "/api/config/agent-policies",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            defaults: mockConfig.defaults,
            agents: {
              helper: {
                display_name: "Helper",
                role: "Helps",
                tools: [],
                skills: [],
                instructions: [],
                rooms: [],
                knowledge_bases: [],
                delegate_to: [],
                context_files: [],
              },
            },
          }),
        },
      );
    });
  });

  describe("updateAgent", () => {
    it("clears legacy worker_scope when private state is enabled", () => {
      useConfigStore.setState({
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().updateAgent("mind", {
        private: { per: "user_agent" },
      });

      const state = useConfigStore.getState();
      expect(state.agents[0].worker_scope).toBeUndefined();
      expect(state.agents[0].private).toEqual({ per: "user_agent" });
    });

    it("defaults private knowledge path when enabling it from an empty state", () => {
      useConfigStore.setState({
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().updateAgent("mind", {
        private: {
          per: "user",
          knowledge: { enabled: true, watch: true },
        },
      });

      const state = useConfigStore.getState();
      expect(state.agents[0].private?.knowledge).toEqual({
        enabled: true,
        path: "memory",
        watch: true,
      });
    });

    it.each([
      { enabled: true, path: "memory" },
      { enabled: false, path: undefined },
    ])(
      "replaces a blank private knowledge path (enabled: $enabled)",
      ({ enabled, path }) => {
        useConfigStore.setState({
          agents: [
            {
              id: "mind",
              display_name: "Mind",
              role: "Assistant",
              tools: [],
              skills: [],
              instructions: [],
              rooms: [],
            },
          ],
        });

        useConfigStore.getState().updateAgent("mind", {
          private: { per: "user", knowledge: { enabled, path: "  " } },
        });

        expect(useConfigStore.getState().agents[0].private?.knowledge).toEqual({
          enabled,
          path,
        });
      },
    );

    it("restores the backed-up worker_scope when private mode is disabled", () => {
      useConfigStore.setState({
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().setAgentPrivateEnabled("mind", true);
      useConfigStore.getState().setAgentPrivateEnabled("mind", false);

      const state = useConfigStore.getState();
      expect(state.agents[0].private).toBeUndefined();
      expect(state.agents[0].worker_scope).toBe("user_agent");
      expect(state.privateWorkerScopeBackups).toEqual({});
    });

    it("preserves the private-toggle worker_scope backup across failed saves", async () => {
      const mockConfig: Config = {
        agents: {
          mind: {
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        },
        models: {},
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };

      useConfigStore.setState({
        config: mockConfig,
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().setAgentPrivateEnabled("mind", true);
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ detail: "save failed" }, { status: 500 }),
      );

      await useConfigStore.getState().saveConfig();
      useConfigStore.getState().setAgentPrivateEnabled("mind", false);

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("error");
      expect(state.agents[0].private).toBeUndefined();
      expect(state.agents[0].worker_scope).toBe("user_agent");
    });

    it("clears the private-toggle worker_scope backup after a successful save", async () => {
      const mockConfig: Config = {
        agents: {
          mind: {
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        },
        models: {},
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };

      useConfigStore.setState({
        config: mockConfig,
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Assistant",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: "user_agent",
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().setAgentPrivateEnabled("mind", true);
      vi.mocked(fetch).mockResolvedValueOnce(Response.json({ success: true }));

      await useConfigStore.getState().saveConfig();
      useConfigStore.getState().setAgentPrivateEnabled("mind", false);

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("synced");
      expect(state.agents[0].private).toBeUndefined();
      expect(state.agents[0].worker_scope).toBeUndefined();
    });

    it("keeps draft team membership when backend eligibility marks an edited agent unsupported", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: "default" },
        },
        agents: [
          {
            id: "leader",
            display_name: "Leader",
            role: "Lead",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
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
        teams: [
          {
            id: "duo",
            display_name: "Duo",
            role: "Two agents",
            agents: ["leader", "helper"],
            rooms: [],
            mode: "coordinate",
          },
        ],
        agentPoliciesByAgent: {
          leader: makeAgentPolicy("leader"),
          helper: makeAgentPolicy("helper"),
        },
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            leader: makeAgentPolicy("leader", {
              is_private: true,
              effective_execution_scope: "user",
              scope_label: "private.per=user",
              scope_source: "private.per",
              dashboard_credentials_supported: false,
              team_eligibility_reason:
                "Private agents cannot be configured as team members.",
              private_workspace_enabled: true,
            }),
            helper: makeAgentPolicy("helper"),
          },
        }),
      );

      useConfigStore.getState().updateAgent("leader", {
        private: { per: "user" },
      });

      await waitFor(() => {
        expect(useConfigStore.getState().teams[0].agents).toEqual([
          "leader",
          "helper",
        ]);
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          leader: makeAgentPolicy("leader", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
          helper: makeAgentPolicy("helper"),
        });
      });
    });

    it("does not refresh agent policies for non-policy agent edits", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: "default" },
        },
        agents: [
          {
            id: "leader",
            display_name: "Leader",
            role: "Lead",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: [],
          },
        ],
      });

      useConfigStore.getState().updateAgent("leader", {
        display_name: "Updated Leader",
      });

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
      expect(useConfigStore.getState().agents[0].display_name).toBe(
        "Updated Leader",
      );
    });

    it("does not refresh agent policies for private workspace edits that keep private mode enabled", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: "default" },
        },
        agents: [
          {
            id: "mind",
            display_name: "Mind",
            role: "Private",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: {
              per: "user",
              root: "mind_data",
            },
          },
        ],
      });

      useConfigStore.getState().updateAgent("mind", {
        private: {
          per: "user",
          root: "updated_root",
        },
      });

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
      expect(useConfigStore.getState().agents[0].private?.root).toBe(
        "updated_root",
      );
    });

    it("keeps draft team membership when delegation now reaches a private agent", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: "default" },
        },
        agents: [
          {
            id: "leader",
            display_name: "Leader",
            role: "Lead",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: [],
          },
          {
            id: "helper",
            display_name: "Helper",
            role: "Help",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: "mind",
            display_name: "Mind",
            role: "Private",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: { per: "user" },
          },
        ],
        teams: [
          {
            id: "duo",
            display_name: "Duo",
            role: "Two agents",
            agents: ["leader", "helper"],
            rooms: [],
            mode: "coordinate",
          },
        ],
        agentPoliciesByAgent: {
          leader: makeAgentPolicy("leader"),
          helper: makeAgentPolicy("helper"),
          mind: makeAgentPolicy("mind", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        },
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            leader: makeAgentPolicy("leader", {
              team_eligibility_reason:
                "Delegates to private agent 'mind', so it cannot participate in teams.",
            }),
            helper: makeAgentPolicy("helper"),
            mind: makeAgentPolicy("mind", {
              is_private: true,
              effective_execution_scope: "user",
              scope_label: "private.per=user",
              scope_source: "private.per",
              dashboard_credentials_supported: false,
              team_eligibility_reason:
                "Private agents cannot be configured as team members.",
              private_workspace_enabled: true,
            }),
          },
        }),
      );

      useConfigStore.getState().updateAgent("leader", {
        delegate_to: ["mind"],
      });

      await waitFor(() => {
        expect(useConfigStore.getState().teams[0].agents).toEqual([
          "leader",
          "helper",
        ]);
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          leader: makeAgentPolicy("leader", {
            team_eligibility_reason:
              "Delegates to private agent 'mind', so it cannot participate in teams.",
          }),
          helper: makeAgentPolicy("helper"),
          mind: makeAgentPolicy("mind", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        });
      });
    });

    it("drops stale policy immediately when a policy-affecting edit triggers refresh", async () => {
      const pendingResponse = deferred<Response>();

      vi.mocked(fetch).mockReturnValueOnce(pendingResponse.promise);

      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: "default" },
        },
        agents: [
          {
            id: "leader",
            display_name: "Leader",
            role: "Lead",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: [],
          },
          {
            id: "mind",
            display_name: "Mind",
            role: "Private",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: { per: "user" },
          },
        ],
        agentPoliciesByAgent: {
          leader: makeAgentPolicy("leader"),
          mind: makeAgentPolicy("mind", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        },
      });

      useConfigStore.getState().updateAgent("leader", {
        delegate_to: ["mind"],
      });

      expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({});
      expect(useConfigStore.getState().agentPoliciesStale).toBe(true);

      pendingResponse.resolve(
        Response.json({
          agent_policies: {
            leader: makeAgentPolicy("leader", {
              team_eligibility_reason:
                "Delegates to private agent 'mind', so it cannot participate in teams.",
            }),
            mind: makeAgentPolicy("mind", {
              is_private: true,
              effective_execution_scope: "user",
              scope_label: "private.per=user",
              scope_source: "private.per",
              dashboard_credentials_supported: false,
              team_eligibility_reason:
                "Private agents cannot be configured as team members.",
              private_workspace_enabled: true,
            }),
          },
        }),
      );

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          leader: makeAgentPolicy("leader", {
            team_eligibility_reason:
              "Delegates to private agent 'mind', so it cannot participate in teams.",
          }),
          mind: makeAgentPolicy("mind", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        });
      });
      expect(useConfigStore.getState().agentPoliciesStale).toBe(false);
    });
  });

  describe("agent operations", () => {
    beforeEach(() => {
      // Set up agents
      const agents: Agent[] = [
        {
          id: "agent1",
          display_name: "Agent 1",
          role: "Role 1",
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: "agent2",
          display_name: "Agent 2",
          role: "Role 2",
          tools: ["calculator"],
          skills: [],
          instructions: ["Test"],
          rooms: ["lobby"],
        },
      ];
      useConfigStore.setState({ agents });
    });

    it("should select agent", () => {
      const { selectAgent } = useConfigStore.getState();
      selectAgent("agent2");

      const state = useConfigStore.getState();
      expect(state.selectedAgentId).toBe("agent2");
    });

    it("should update agent", () => {
      const { updateAgent } = useConfigStore.getState();
      updateAgent("agent1", { display_name: "Updated Agent" });

      const state = useConfigStore.getState();
      const updatedAgent = state.agents.find((a) => a.id === "agent1");
      expect(updatedAgent?.display_name).toBe("Updated Agent");
      expect(state.isDirty).toBe(true);
    });

    it("should create new agent", () => {
      const newAgentData = {
        display_name: "New Agent",
        role: "New role",
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      };

      const { createAgent } = useConfigStore.getState();
      createAgent(newAgentData);

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(3);
      const newAgent = state.agents[2];
      expect(newAgent.display_name).toBe("New Agent");
      expect(state.selectedAgentId).toBe(newAgent.id);
      expect(state.isDirty).toBe(true);
    });

    it("refreshes agent policies when creating a standard shared agent", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            new_agent: makeAgentPolicy("new_agent"),
          },
        }),
      );

      useConfigStore.getState().createAgent({
        display_name: "New Agent",
        role: "New role",
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      });

      await Promise.resolve();

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          new_agent: makeAgentPolicy("new_agent"),
        });
      });
    });

    it("refreshes agent policies when creating a private agent draft", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            new_agent: makeAgentPolicy("new_agent", {
              is_private: true,
              effective_execution_scope: "user",
              scope_label: "private.per=user",
              scope_source: "private.per",
              dashboard_credentials_supported: false,
              team_eligibility_reason:
                "Private agents cannot be configured as team members.",
              private_workspace_enabled: true,
            }),
          },
        }),
      );

      useConfigStore.getState().createAgent({
        display_name: "New Agent",
        role: "New role",
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
        private: { per: "user" },
      });

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          new_agent: makeAgentPolicy("new_agent", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        });
      });
    });

    it("should delete agent", () => {
      useConfigStore.setState({
        teams: [
          {
            id: "team1",
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1", "agent2"],
            rooms: [],
            mode: "coordinate",
          },
        ],
      });
      const { deleteAgent } = useConfigStore.getState();
      deleteAgent("agent1");

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe("agent2");
      expect(state.teams[0].agents).toEqual(["agent2"]);
      expect(state.isDirty).toBe(true);
      expect(state.dirtyRoots).toEqual(
        expect.arrayContaining(["agents", "teams"]),
      );
    });

    it("serializes dependent team removals after deleteAgent", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Role 1",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          agent2: {
            display_name: "Agent 2",
            role: "Role 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        teams: {
          team1: {
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1", "agent2"],
            rooms: [],
            mode: "coordinate",
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "test",
            },
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };

      useConfigStore.setState({
        loadedConfig: mockConfig as Config,
        config: mockConfig as Config,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Role 1",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: "agent2",
            display_name: "Agent 2",
            role: "Role 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: "team1",
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1", "agent2"],
            rooms: [],
            mode: "coordinate",
          },
        ],
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: { agent2: makeAgentPolicy("agent2") },
        }),
      );

      useConfigStore.getState().deleteAgent("agent1");
      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          agent2: makeAgentPolicy("agent2"),
        });
      });

      expect(await savedPayload()).toMatchObject({
        agents: {
          agent2: mockConfig.agents.agent2,
        },
        teams: {
          team1: {
            ...mockConfig.teams.team1,
            agents: ["agent2"],
          },
        },
      });
    });

    it("drops the deleted agent's summary model and call entry so the deletion saves", async () => {
      const agent = (id: string): Agent => ({
        id,
        display_name: id,
        role: "Role",
        tools: [],
        skills: [],
        instructions: [],
        rooms: ["lobby"],
      });
      const config = {
        agents: {
          helper: { display_name: "helper", role: "Role", rooms: ["lobby"] },
          scout: { display_name: "scout", role: "Role", rooms: ["lobby"] },
        },
        models: { default: { provider: "ollama", id: "test-model" } },
        router: { model: "default" },
        room_thread_summary_models: { helper: "default", lobby: "default" },
        calls: { enabled: true, agents: { helper: "voice", scout: "voice" } },
      };
      useConfigStore.setState({
        loadedConfig: config as unknown as Config,
        config: config as unknown as Config,
        agents: [agent("helper"), agent("scout")],
      });
      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({ agent_policies: { scout: makeAgentPolicy("scout") } }),
      );

      useConfigStore.getState().deleteAgent("helper");
      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          scout: makeAgentPolicy("scout"),
        });
      });

      const payload = await savedPayload();
      expect(payload.room_thread_summary_models).toEqual({ lobby: "default" });
      expect(payload.calls).toEqual({
        enabled: true,
        agents: { scout: "voice" },
      });
    });

    it("keeps a summary model key that also names a room when deleting the agent", () => {
      const config = {
        agents: {
          dev: { display_name: "Dev", role: "Role", rooms: [] },
          coder: { display_name: "Coder", role: "Role", rooms: ["dev"] },
        },
        models: { default: { provider: "ollama", id: "test-model" } },
        router: { model: "default" },
        room_thread_summary_models: { dev: "default" },
      };
      useConfigStore.setState({
        loadedConfig: config as unknown as Config,
        config: config as unknown as Config,
        agents: [
          {
            id: "dev",
            display_name: "Dev",
            role: "Role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: "coder",
            display_name: "Coder",
            role: "Role",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["dev"],
          },
        ],
      });

      useConfigStore.getState().deleteAgent("dev");

      const state = useConfigStore.getState();
      expect(
        readConfigRoot(state.config!, "room_thread_summary_models"),
      ).toEqual({ dev: "default" });
      expect(state.dirtyRoots).not.toContain("room_thread_summary_models");
    });

    it("refreshes agent policies when deleting an unrelated shared agent", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Role 1",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: "agent2",
            display_name: "Agent 2",
            role: "Role 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            agent2: makeAgentPolicy("agent2"),
          },
        }),
      );

      useConfigStore.getState().deleteAgent("agent1");

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          agent2: makeAgentPolicy("agent2"),
        });
      });
    });

    it("refreshes agent policies when deleting an agent referenced by delegation", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: { model: "text-embedding-3-small" },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
        agents: [
          {
            id: "leader",
            display_name: "Leader",
            role: "Lead",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: ["mind"],
          },
          {
            id: "mind",
            display_name: "Mind",
            role: "Private",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: { per: "user" },
          },
        ],
        agentPoliciesByAgent: {
          leader: makeAgentPolicy("leader", {
            team_eligibility_reason:
              "Delegates to private agent 'mind', so it cannot participate in teams.",
          }),
          mind: makeAgentPolicy("mind", {
            is_private: true,
            effective_execution_scope: "user",
            scope_label: "private.per=user",
            scope_source: "private.per",
            dashboard_credentials_supported: false,
            team_eligibility_reason:
              "Private agents cannot be configured as team members.",
            private_workspace_enabled: true,
          }),
        },
      });

      vi.mocked(fetch).mockResolvedValueOnce(
        Response.json({
          agent_policies: {
            leader: makeAgentPolicy("leader"),
          },
        }),
      );

      useConfigStore.getState().deleteAgent("mind");

      await waitFor(() => {
        expect(useConfigStore.getState().agentPoliciesByAgent).toEqual({
          leader: makeAgentPolicy("leader"),
        });
      });
    });
  });

  describe("teams", () => {
    beforeEach(() => {
      const mockTeams: Team[] = [
        {
          id: "team1",
          display_name: "Team 1",
          role: "Test team 1",
          agents: ["agent1", "agent2"],
          rooms: ["room1"],
          mode: "coordinate",
        },
        {
          id: "team2",
          display_name: "Team 2",
          role: "Test team 2",
          agents: ["agent3"],
          rooms: ["room2"],
          mode: "collaborate",
          model: "gpt4",
        },
      ];

      useConfigStore.setState({
        teams: mockTeams,
        selectedTeamId: "team1",
      });
    });

    it("should select team", () => {
      const { selectTeam } = useConfigStore.getState();
      selectTeam("team2");

      const state = useConfigStore.getState();
      expect(state.selectedTeamId).toBe("team2");
    });

    it("should update team", () => {
      const { updateTeam } = useConfigStore.getState();
      updateTeam("team1", { display_name: "Updated Team" });

      const state = useConfigStore.getState();
      const updatedTeam = state.teams.find((t) => t.id === "team1");
      expect(updatedTeam?.display_name).toBe("Updated Team");
      expect(state.isDirty).toBe(true);
    });

    it("normalizes empty team compaction overrides away", () => {
      const { updateTeam } = useConfigStore.getState();
      updateTeam("team1", { compaction: {} });

      const state = useConfigStore.getState();
      const updatedTeam = state.teams.find((team) => team.id === "team1");
      expect(updatedTeam?.compaction).toBeUndefined();
    });

    it("should create new team", () => {
      const { createTeam } = useConfigStore.getState();
      const newTeamData = {
        display_name: "New Team",
        role: "New team role",
        agents: ["agent1"],
        rooms: ["lobby"],
        mode: "coordinate" as const,
      };

      createTeam(newTeamData);

      const state = useConfigStore.getState();
      expect(state.teams).toHaveLength(3);
      const newTeam = state.teams[2];
      expect(newTeam.display_name).toBe("New Team");
      expect(newTeam.id).toBe("new_team");
      expect(state.selectedTeamId).toBe("new_team");
      expect(state.isDirty).toBe(true);
    });

    it("drops the deleted team's summary model so the deletion saves", async () => {
      const config = {
        agents: {},
        teams: {
          crew: { display_name: "Crew", role: "Work", agents: [], rooms: [] },
        },
        models: { default: { provider: "ollama", id: "test-model" } },
        router: { model: "default" },
        room_thread_summary_models: { crew: "default", lobby: "default" },
      };
      useConfigStore.setState({
        loadedConfig: config as unknown as Config,
        config: config as unknown as Config,
        agents: [],
        teams: [
          {
            id: "crew",
            display_name: "Crew",
            role: "Work",
            agents: [],
            rooms: [],
            mode: "coordinate",
          },
        ],
      });

      useConfigStore.getState().deleteTeam("crew");

      const payload = await savedPayload();
      expect(payload.teams).toEqual({});
      expect(payload.room_thread_summary_models).toEqual({ lobby: "default" });
    });

    it("should delete team", () => {
      const { deleteTeam } = useConfigStore.getState();
      deleteTeam("team1");

      const state = useConfigStore.getState();
      expect(state.teams).toHaveLength(1);
      expect(state.teams[0].id).toBe("team2");
      expect(state.selectedTeamId).toBe(null);
      expect(state.isDirty).toBe(true);
    });
  });

  describe("memory config", () => {
    it("should update memory configuration", () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: "openai",
              config: {
                model: "text-embedding-3-small",
              },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
      });

      const { updateMemoryConfig } = useConfigStore.getState();
      const newMemoryConfig = {
        backend: "mem0" as const,
        embedder: {
          provider: "ollama",
          config: {
            model: "nomic-embed-text",
            host: "http://localhost:11434",
          },
        },
      };

      updateMemoryConfig(newMemoryConfig);

      const state = useConfigStore.getState();
      expect(state.config?.memory).toEqual(newMemoryConfig);
      expect(state.dirtyRoots).toEqual(["memory"]);
      expect(state.isDirty).toBe(true);
    });
  });

  describe("knowledge bases", () => {
    it("should preserve git settings when updating base path or watch", () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: { provider: "openai", config: { model: "test" } },
          },
          knowledge_bases: {
            docs: {
              path: "./docs",
              watch: true,
              chunk_size: 5000,
              chunk_overlap: 0,
              git: {
                repo_url: "https://github.com/pipefunc/pipefunc",
                branch: "main",
                include_patterns: ["docs/**"],
              },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        } as Config,
      });

      const { updateKnowledgeBase } = useConfigStore.getState();
      updateKnowledgeBase("docs", { path: "./docs-sync", watch: false });

      const state = useConfigStore.getState();
      expect(state.config?.knowledge_bases?.docs).toEqual({
        path: "./docs-sync",
        watch: false,
        chunk_size: 5000,
        chunk_overlap: 0,
        git: {
          repo_url: "https://github.com/pipefunc/pipefunc",
          branch: "main",
          include_patterns: ["docs/**"],
        },
      });
      expect(state.isDirty).toBe(true);
    });

    it("should remove deleted knowledge base from all agent assignments", async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: { provider: "openai", config: { model: "test" } },
          },
          knowledge_bases: {
            legal: { path: "./legal", watch: true },
            research: { path: "./research", watch: true },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: "default" },
        },
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            knowledge_bases: ["research", "legal"],
          },
          {
            id: "agent2",
            display_name: "Agent 2",
            role: "Test agent 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            knowledge_bases: ["research"],
          },
        ],
      });

      const { deleteKnowledgeBase } = useConfigStore.getState();
      deleteKnowledgeBase("research");

      const state = useConfigStore.getState();
      expect(state.config?.knowledge_bases).toEqual({
        legal: { path: "./legal", watch: true },
      });
      expect(
        state.agents.find((agent) => agent.id === "agent1")?.knowledge_bases,
      ).toEqual(["legal"]);
      expect(
        state.agents.find((agent) => agent.id === "agent2")?.knowledge_bases,
      ).toEqual([]);
      expect(state.isDirty).toBe(true);
      const payload = await savedPayload();
      expect(payload.agents.agent1.knowledge_bases).toEqual(["legal"]);
      expect(payload.agents.agent2.knowledge_bases).toEqual([]);
    });
  });

  describe("rooms", () => {
    beforeEach(() => {
      const mockRooms = [
        {
          id: "lobby",
          display_name: "Lobby",
          description: "Main room",
          agents: ["agent1"],
          model: "default",
        },
        {
          id: "dev",
          display_name: "Dev Room",
          description: "Development room",
          agents: ["agent2"],
        },
      ];

      const mockAgents = [
        {
          id: "agent1",
          display_name: "Agent 1",
          role: "Test agent",
          tools: [],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
        },
        {
          id: "agent2",
          display_name: "Agent 2",
          role: "Test agent 2",
          tools: [],
          skills: [],
          instructions: [],
          rooms: ["dev"],
        },
      ];

      useConfigStore.setState({
        rooms: mockRooms,
        agents: mockAgents,
        selectedRoomId: "lobby",
      });
    });

    it("should select room", () => {
      const { selectRoom } = useConfigStore.getState();
      selectRoom("dev");

      const state = useConfigStore.getState();
      expect(state.selectedRoomId).toBe("dev");
    });

    it("drops the room model block once its last override is cleared", () => {
      useConfigStore.setState({
        config: {
          agents: {},
          models: {},
          room_models: { lobby: "default" },
        } as unknown as Config,
      });

      useConfigStore.getState().updateRoom("lobby", { model: undefined });

      expect(useConfigStore.getState().config?.room_models).toBeUndefined();
    });

    it("drops the room model block when the room with the last override is deleted", () => {
      useConfigStore.setState({
        config: {
          agents: {},
          models: {},
          room_models: { lobby: "default" },
        } as unknown as Config,
        rooms: [
          { id: "lobby", display_name: "Lobby", description: "", agents: [] },
        ],
      });

      useConfigStore.getState().deleteRoom("lobby");

      expect(useConfigStore.getState().config?.room_models).toBeUndefined();
    });

    it("keeps an authored room entry when its metadata is cleared", () => {
      useConfigStore.setState({
        config: {
          agents: {},
          models: {},
          rooms: { lobby: { display_name: "Lobby", description: "Main room" } },
        } as unknown as Config,
      });

      useConfigStore
        .getState()
        .updateRoom("lobby", { display_name: "", description: "" });

      expect(useConfigStore.getState().config?.rooms).toEqual({
        lobby: { description: "" },
      });
    });

    it("should update room", () => {
      const { updateRoom } = useConfigStore.getState();
      updateRoom("lobby", { display_name: "Updated Lobby" });

      const state = useConfigStore.getState();
      const updatedRoom = state.rooms.find((r) => r.id === "lobby");
      expect(updatedRoom?.display_name).toBe("Updated Lobby");
      expect(state.isDirty).toBe(true);
    });

    it("keeps a room name as typed while editing and saves it trimmed", async () => {
      const mockConfig = {
        agents: {},
        rooms: { lobby: { display_name: "Lobby" } },
        models: { default: { provider: "ollama", id: "test-model" } },
        defaults: { markdown: true },
        router: { model: "default" },
      } as unknown as Config;
      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [],
        rooms: [
          { id: "lobby", display_name: "Lobby", description: "", agents: [] },
        ],
      });

      useConfigStore.getState().updateRoom("lobby", { display_name: "Big " });
      expect(
        useConfigStore.getState().rooms.find((room) => room.id === "lobby")
          ?.display_name,
      ).toBe("Big ");

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({ lobby: { display_name: "Big" } });
    });

    it("should ignore unchanged room updates while a save is in progress", async () => {
      const pendingSaveResponse = deferred<Response>();
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
          agent2: {
            display_name: "Agent 2",
            role: "Test agent 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        rooms: {
          lobby: {
            display_name: "Lobby",
            description: "Main room",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
          {
            id: "agent2",
            display_name: "Agent 2",
            role: "Test agent 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "Main room",
            agents: ["agent1", "agent2"],
          },
        ],
        isDirty: true,
        dirtyRoots: ["rooms"],
      });

      vi.mocked(fetch).mockReturnValueOnce(pendingSaveResponse.promise);

      const savePromise = useConfigStore.getState().saveConfig();
      useConfigStore.getState().updateRoom("lobby", {
        display_name: "Lobby",
        agents: ["agent2", "agent1"],
      });

      pendingSaveResponse.resolve(Response.json({ success: true }));

      await expect(savePromise).resolves.toEqual({ status: "saved" });
    });

    it("should update agents when room agents change", () => {
      const { updateRoom } = useConfigStore.getState();
      updateRoom("lobby", { agents: ["agent1", "agent2"] });

      const state = useConfigStore.getState();
      const agent2 = state.agents.find((a) => a.id === "agent2");
      expect(agent2?.rooms).toContain("lobby");
      expect(state.isDirty).toBe(true);
    });

    it("writes room metadata edits to draft config rooms", () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().updateRoom("lobby", {
        display_name: "Project Lobby",
      });

      const state = useConfigStore.getState();
      expect(state.config?.rooms).toEqual({
        lobby: {
          display_name: "Project Lobby",
        },
      });
      expect(
        state.rooms.find((room) => room.id === "lobby")?.display_name,
      ).toBe("Project Lobby");
      expect(state.dirtyRoots).toContain("rooms");
    });

    it("normalizes blank room display-name edits to omitted metadata", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["project_room"],
          },
        },
        rooms: {
          project_room: {
            display_name: "Project Alpha",
            description: "Planning space",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["project_room"],
          },
        ],
        rooms: [
          {
            id: "project_room",
            display_name: "Project Alpha",
            description: "Planning space",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().updateRoom("project_room", {
        display_name: "   ",
      });

      const state = useConfigStore.getState();
      expect(state.config?.rooms?.project_room).toEqual({
        description: "Planning space",
      });
      expect(
        state.rooms.find((room) => room.id === "project_room")?.display_name,
      ).toBe("Project Room");

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({
        project_room: {
          description: "Planning space",
        },
      });
    });

    it("does not promote derived rooms when clearing default display names", () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["project_room"],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["project_room"],
          },
        ],
        rooms: [
          {
            id: "project_room",
            display_name: "Project Room",
            description: "",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().updateRoom("project_room", {
        display_name: "   ",
      });

      const state = useConfigStore.getState();
      expect(state.config?.rooms).toBeUndefined();
      expect(state.rooms).toEqual([
        {
          id: "project_room",
          display_name: "Project Room",
          description: "",
          agents: ["agent1"],
          model: undefined,
        },
      ]);
      expect(state.dirtyRoots).not.toContain("rooms");
      expect(state.isDirty).toBe(false);
    });

    it("keeps membership-only updates out of managed room metadata", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
          agent2: {
            display_name: "Agent 2",
            role: "Test agent 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
          {
            id: "agent2",
            display_name: "Agent 2",
            role: "Test agent 2",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().updateRoom("lobby", {
        agents: ["agent1", "agent2"],
      });

      const state = useConfigStore.getState();
      expect(state.config?.rooms).toBeUndefined();
      expect(state.dirtyRoots).not.toContain("rooms");
      expect(state.dirtyRoots).toContain("agents");

      const payload = await savedPayload();
      expect(payload.rooms).toBeUndefined();
      expect(payload.agents.agent1.rooms).toEqual(["lobby"]);
      expect(payload.agents.agent2.rooms).toEqual(["lobby"]);
    });

    it("does not serialize derived rooms for model-only updates", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
          claude: {
            provider: "anthropic",
            id: "claude-sonnet-5",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().updateRoom("lobby", { model: "claude" });

      const payload = await savedPayload();
      expect(payload.rooms).toBeUndefined();
      expect(payload.room_models).toEqual({
        lobby: "claude",
      });
    });

    it("should create new room", () => {
      const { createRoom } = useConfigStore.getState();
      const newRoomData = {
        display_name: "New Room",
        description: "Test room",
        agents: ["agent1"],
      };

      createRoom(newRoomData);

      const state = useConfigStore.getState();
      expect(state.rooms).toHaveLength(3);
      const newRoom = state.rooms[2];
      expect(newRoom.display_name).toBe("New Room");
      expect(newRoom.id).toBe("new_room");
      expect(state.selectedRoomId).toBe("new_room");

      // Check that agent1 now has new_room in its rooms
      const agent1 = state.agents.find((a) => a.id === "agent1");
      expect(agent1?.rooms).toContain("new_room");
      expect(state.isDirty).toBe(true);
    });

    it("writes newly created managed room metadata to draft config rooms", () => {
      const mockConfig = {
        agents: {},
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [],
        rooms: [],
      });

      useConfigStore.getState().createRoom({
        display_name: "Project Room",
        description: "Planning space",
        agents: [],
      });

      const state = useConfigStore.getState();
      expect(state.config?.rooms).toEqual({
        project_room: {
          display_name: "Project Room",
          description: "Planning space",
        },
      });
      expect(state.rooms).toEqual([
        {
          id: "project_room",
          display_name: "Project Room",
          description: "Planning space",
          agents: [],
          model: undefined,
        },
      ]);
      expect(state.dirtyRoots).toContain("rooms");
    });

    it("writes a model selected during room creation to draft room models", async () => {
      const mockConfig = {
        agents: {},
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
          claude: {
            provider: "anthropic",
            id: "claude-sonnet-5",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [],
        rooms: [],
      });

      useConfigStore.getState().createRoom({
        display_name: "Project Room",
        description: "Planning space",
        agents: [],
        model: "claude",
      });

      const state = useConfigStore.getState();
      expect(state.config?.room_models).toEqual({
        project_room: "claude",
      });
      expect(state.rooms[0]).toMatchObject({
        id: "project_room",
        model: "claude",
      });
      expect(state.dirtyRoots).toEqual(
        expect.arrayContaining(["rooms", "room_models"]),
      );

      const payload = await savedPayload();
      expect(payload.room_models).toEqual({
        project_room: "claude",
      });
    });

    it("does not serialize team-derived rooms when creating a managed room", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        teams: {
          ops: {
            display_name: "Ops Team",
            role: "Coordinates operations",
            agents: ["agent1"],
            rooms: ["war_room"],
            mode: "coordinate",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: "ops",
            display_name: "Ops Team",
            role: "Coordinates operations",
            agents: ["agent1"],
            rooms: ["war_room"],
            mode: "coordinate",
          },
        ],
        rooms: [
          {
            id: "war_room",
            display_name: "War_room",
            description: "",
            agents: [],
          },
        ],
      });

      useConfigStore.getState().createRoom({
        display_name: "Project Room",
        description: "Planning space",
        agents: [],
      });

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({
        project_room: {
          display_name: "Project Room",
          description: "Planning space",
        },
      });
    });

    it("preserves omitted fields on existing managed rooms when creating another room", async () => {
      const mockConfig = {
        agents: {},
        rooms: {
          analysis_room: {},
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        rooms: [
          {
            id: "analysis_room",
            display_name: "Analysis_room",
            description: "",
            agents: [],
          },
        ],
      });

      useConfigStore.getState().createRoom({
        display_name: "Project Room",
        description: "Planning space",
        agents: [],
      });

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({
        analysis_room: {},
        project_room: {
          display_name: "Project Room",
          description: "Planning space",
        },
      });
    });

    it("should persist a newly created room even before agents are assigned", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        rooms: [],
      });

      const { createRoom } = useConfigStore.getState();
      createRoom({
        display_name: "Project Room",
        description: "Planning space",
        agents: [],
      });

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({
        project_room: {
          display_name: "Project Room",
          description: "Planning space",
        },
      });

      const state = useConfigStore.getState();
      expect(state.rooms).toEqual([
        {
          id: "project_room",
          display_name: "Project Room",
          description: "Planning space",
          agents: [],
          model: undefined,
        },
      ]);
      expect(state.config?.rooms).toEqual({
        project_room: {
          display_name: "Project Room",
          description: "Planning space",
        },
      });
    });

    it("should delete room and update agents", () => {
      const { deleteRoom } = useConfigStore.getState();
      deleteRoom("lobby");

      const state = useConfigStore.getState();
      expect(state.rooms).toHaveLength(1);
      expect(state.rooms[0].id).toBe("dev");

      // Check that agent1 no longer has lobby in its rooms
      const agent1 = state.agents.find((a) => a.id === "agent1");
      expect(agent1?.rooms).not.toContain("lobby");
      expect(state.selectedRoomId).toBe(null);
      expect(state.isDirty).toBe(true);
    });

    it("deleting a derived room does not dirty managed room metadata", () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
        ],
      });

      useConfigStore.getState().deleteRoom("lobby");

      const state = useConfigStore.getState();
      expect(state.config?.rooms).toBeUndefined();
      expect(state.dirtyRoots).not.toContain("rooms");
      expect(state.dirtyRoots).toEqual(expect.arrayContaining(["agents"]));
      expect(state.agents[0].rooms).toEqual([]);
    });

    it("deleting an unassigned managed room does not dirty responder collections", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        teams: {
          team1: {
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1"],
            rooms: ["lobby"],
            mode: "coordinate",
          },
        },
        rooms: {
          project_room: {
            display_name: "Project Room",
            description: "Planning space",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        teams: [
          {
            id: "team1",
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1"],
            rooms: ["lobby"],
            mode: "coordinate",
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
          {
            id: "project_room",
            display_name: "Project Room",
            description: "Planning space",
            agents: [],
          },
        ],
      });

      useConfigStore.getState().deleteRoom("project_room");

      const state = useConfigStore.getState();
      expect(state.dirtyRoots).toContain("rooms");
      expect(state.dirtyRoots).not.toContain("agents");
      expect(state.dirtyRoots).not.toContain("teams");

      const payload = await savedPayload();
      expect(payload.agents).toEqual(mockConfig.agents);
      expect(payload.teams).toEqual(mockConfig.teams);
      expect(payload.rooms).toEqual({});
    });

    it("does not serialize agent-derived rooms when deleting a managed room", async () => {
      const mockConfig = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        rooms: {
          project_room: {
            display_name: "Project Room",
            description: "Planning space",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
          },
          {
            id: "project_room",
            display_name: "Project Room",
            description: "Planning space",
            agents: [],
          },
        ],
      });

      useConfigStore.getState().deleteRoom("project_room");

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({});
      expect(payload.agents.agent1.rooms).toEqual(["lobby"]);
    });

    it("preserves omitted fields on existing managed rooms when deleting another room", async () => {
      const mockConfig = {
        agents: {},
        rooms: {
          analysis_room: {},
          project_room: {
            display_name: "Project Room",
            description: "Planning space",
          },
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      } as Config;

      useConfigStore.setState({
        config: mockConfig,
        loadedConfig: mockConfig,
        rooms: [
          {
            id: "analysis_room",
            display_name: "Analysis_room",
            description: "",
            agents: [],
          },
          {
            id: "project_room",
            display_name: "Project Room",
            description: "Planning space",
            agents: [],
          },
        ],
      });

      useConfigStore.getState().deleteRoom("project_room");

      const payload = await savedPayload();
      expect(payload.rooms).toEqual({
        analysis_room: {},
      });
    });
  });

  describe("saveConfig with teams", () => {
    it("should save configuration with teams and room models", async () => {
      const mockConfig: Config = {
        agents: {
          agent1: {
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        teams: {
          team1: {
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1"],
            rooms: ["lobby"],
            mode: "coordinate",
          },
        },
        room_models: {
          lobby: "default",
        },
        memory: {
          embedder: {
            provider: "ollama",
            config: {
              model: "nomic-embed-text",
              host: "http://localhost:11434",
            },
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
      };

      useConfigStore.setState({
        config: mockConfig,
        agents: [
          {
            id: "agent1",
            display_name: "Agent 1",
            role: "Test agent",
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: "team1",
            display_name: "Team 1",
            role: "Test team",
            agents: ["agent1"],
            rooms: ["lobby"],
            mode: "coordinate",
          },
        ],
        rooms: [
          {
            id: "lobby",
            display_name: "Lobby",
            description: "",
            agents: ["agent1"],
            model: "default",
          },
        ],
      });

      vi.mocked(fetch).mockResolvedValueOnce(Response.json({}));

      const { saveConfig } = useConfigStore.getState();
      await saveConfig();

      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenNthCalledWith(1, "/api/config/save", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "x-mindroom-config-generation": "0",
        },
        body: JSON.stringify(mockConfig),
      });

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe("synced");
      expect(state.isDirty).toBe(false);
    });
  });

  describe("updateConfigValue", () => {
    const baseConfig = {
      agents: {
        helper: {
          display_name: "Helper",
          role: "Helps",
          tools: [],
          skills: [],
          instructions: [],
          rooms: ["lobby"],
        },
      },
      models: { default: { provider: "ollama", id: "test-model" } },
      defaults: {
        markdown: true,
        tools: [{ gmail: { label: "support" } }, "file", "scheduler"],
      },
      router: { model: "default" },
      personal_rooms: { agent: "helper", onboarding_rooms: ["lobby"] },
      memory: {
        embedder: {
          provider: "openai",
          config: { model: "text-embedding-3-small" },
        },
      },
    };

    async function loadBaseConfig(config: object = baseConfig) {
      await loadFixture(config, { helper: makeAgentPolicy("helper") });
    }

    it("marks the root dirty and saves its new value", async () => {
      await loadBaseConfig();

      useConfigStore.getState().updateConfigValue(["router"], {
        model: "default",
        accept_invites: false,
      });

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(true);
      expect(state.dirtyRoots).toEqual(["router"]);
      expect((await savedPayload()).router).toEqual({
        model: "default",
        accept_invites: false,
      });
    });

    it("saves configs that omit the defaults root", async () => {
      const { defaults: _defaults, ...withoutDefaults } = baseConfig;
      await loadBaseConfig(withoutDefaults);
      useConfigStore
        .getState()
        .updateConfigValue(["router"], { model: "fast" });

      const payload = await savedPayload();
      expect(payload.router).toEqual({ model: "fast" });
      expect(payload).not.toHaveProperty("defaults");
    });

    it("clears only the diagnostics of the edited field", async () => {
      await loadBaseConfig({
        ...baseConfig,
        defaults: { ...baseConfig.defaults, max_preload_chars: 0 },
      });
      const siblingIssue = {
        kind: "validation" as const,
        issue: {
          loc: ["defaults", "max_preload_chars"],
          msg: "Input should be greater than or equal to 1",
          type: "greater_than_equal",
        },
      };
      useConfigStore.setState({
        diagnostics: [
          {
            kind: "validation",
            issue: {
              loc: ["defaults", "markdown"],
              msg: "Input should be a valid boolean",
              type: "bool_type",
            },
          },
          siblingIssue,
        ],
      });

      useConfigStore
        .getState()
        .updateConfigValue(["defaults", "markdown"], false);

      const state = useConfigStore.getState();
      expect(state.config?.defaults?.markdown).toBe(false);
      expect(state.dirtyRoots).toEqual(["defaults"]);
      expect(state.diagnostics).toEqual([siblingIssue]);
    });

    it("drops a root once its last key is reset", async () => {
      await loadBaseConfig();
      useConfigStore
        .getState()
        .updateConfigValue(["router", "model"], undefined);

      expect(useConfigStore.getState().config).not.toHaveProperty("router");
      expect(await savedPayload()).not.toHaveProperty("router");
    });

    it("drops defaults without tools once its last key is reset", async () => {
      await loadBaseConfig({ ...baseConfig, defaults: { markdown: false } });
      useConfigStore
        .getState()
        .updateConfigValue(["defaults", "markdown"], undefined);

      expect(useConfigStore.getState().config).not.toHaveProperty("defaults");
      expect(await savedPayload()).not.toHaveProperty("defaults");
    });

    it("drops blocks emptied by a reset unless the loaded config authors them", async () => {
      await loadBaseConfig({
        ...baseConfig,
        memory: { embedder: { provider: "openai" } },
        rooms: { dev: { encrypted: true } },
      });
      const { updateConfigValue } = useConfigStore.getState();
      updateConfigValue(["voice", "stt", "credentials_service"], "speech");
      updateConfigValue(["voice", "stt", "credentials_service"], undefined);
      updateConfigValue(["memory", "embedder", "config", "dimensions"], 256);
      updateConfigValue(
        ["memory", "embedder", "config", "dimensions"],
        undefined,
      );
      updateConfigValue(["rooms", "lobby", "encrypted"], true);
      updateConfigValue(["rooms", "lobby", "encrypted"], undefined);
      updateConfigValue(["rooms", "dev", "encrypted"], undefined);

      const { config } = useConfigStore.getState();
      expect(config).not.toHaveProperty("voice");
      expect(config?.memory).toEqual({ embedder: { provider: "openai" } });
      // The loaded config declares dev, so it stays even without settings.
      expect(config?.rooms).toEqual({ dev: {} });
    });

    it("replaces the value at its path, dropping keys it omits", async () => {
      await loadBaseConfig({
        ...baseConfig,
        models: {
          default: {
            provider: "openai",
            id: "gpt",
            display_name: "Local",
            context_window: 16384,
            extra_kwargs: { base_url: "http://localhost:9292/v1" },
          },
        },
      });

      useConfigStore.getState().updateConfigValue(["models", "default"], {
        provider: "anthropic",
        id: "claude-sonnet-5",
      });

      expect((await savedPayload()).models.default).toEqual({
        provider: "anthropic",
        id: "claude-sonnet-5",
      });
    });

    it("removes a root when given undefined", async () => {
      await loadBaseConfig();

      useConfigStore
        .getState()
        .updateConfigValue(["personal_rooms"], undefined);

      expect(await savedPayload()).not.toHaveProperty("personal_rooms");
    });
  });

  describe("restoreConfigValue", () => {
    const authored = {
      agents: {},
      models: { default: { provider: "ollama", id: "test-model" } },
      router: { model: "default" },
      egress_broker: { services: { github: { preset: "github" } } },
    };
    const path: ConfigPath = ["egress_broker", "services", "extra"];

    async function loadAuthored(config: object = authored) {
      await loadFixture(config);
    }

    function mark() {
      const { isDirty, dirtyRoots, draftVersion } = useConfigStore.getState();
      return { isDirty, dirtyRoots, draftVersion };
    }

    it("puts the value back and leaves a clean draft clean", async () => {
      await loadAuthored();
      const before = mark();
      useConfigStore.getState().updateConfigValue(path, { preset: "openai" });
      expect(useConfigStore.getState().isDirty).toBe(true);

      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(false);
      expect(state.dirtyRoots).toEqual([]);
      expect(state.syncStatus).toBe("synced");
      expect(state.config).toEqual(state.loadedConfig);
      expect(state.rooms).toEqual([]);
    });

    it("restores an earlier value of an existing entry", async () => {
      await loadAuthored();
      const githubPath: ConfigPath = ["egress_broker", "services", "github"];
      const before = mark();
      useConfigStore.getState().updateConfigValue(githubPath, {
        preset: "github",
        restrict_to_rules: true,
      });

      useConfigStore
        .getState()
        .restoreConfigValue(githubPath, { preset: "github" }, before);

      const { config, isDirty } = useConfigStore.getState();
      expect(readConfigRoot(config!, "egress_broker")).toEqual(
        authored.egress_broker,
      );
      expect(isDirty).toBe(false);
    });

    it("keeps changes made elsewhere before the write dirty", async () => {
      await loadAuthored();
      useConfigStore
        .getState()
        .updateConfigValue(["router"], { model: "fast" });
      const before = mark();
      expect(before).toMatchObject({ isDirty: true, dirtyRoots: ["router"] });
      useConfigStore.getState().updateConfigValue(path, { preset: "openai" });
      expect(useConfigStore.getState().dirtyRoots).toEqual([
        "router",
        "egress_broker",
      ]);

      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(true);
      expect(state.dirtyRoots).toEqual(["router"]);
      expect(state.config?.router).toEqual({ model: "fast" });
      expect(state.syncStatus).toBe("error");
    });

    it("leaves edits made after the write dirty and in the draft", async () => {
      await loadAuthored();
      const before = mark();
      expect(before.isDirty).toBe(false);
      useConfigStore.getState().updateConfigValue(path, { preset: "openai" });
      // The user edits something else while the write is being saved.
      useConfigStore
        .getState()
        .updateConfigValue(["router"], { model: "fast" });

      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(true);
      expect(state.dirtyRoots).toEqual(["egress_broker", "router"]);
      expect(state.config?.router).toEqual({ model: "fast" });
      expect(readConfigRoot(state.config!, "egress_broker")).toEqual(
        authored.egress_broker,
      );
      expect(state.syncStatus).toBe("error");
    });

    it("drops the validation errors that were about the restored path only", async () => {
      await loadAuthored();
      const before = mark();
      useConfigStore.getState().updateConfigValue(path, { preset: "nope" });
      useConfigStore.setState({
        diagnostics: [
          {
            kind: "global",
            message: "Configuration validation failed",
            blocking: false,
          },
          {
            kind: "validation",
            issue: {
              loc: [...path],
              msg: "unknown preset",
              type: "value_error",
            },
          },
        ],
      });

      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      expect(useConfigStore.getState().diagnostics).toEqual([]);
    });

    it("keeps other roots' validation errors and a configuration conflict", async () => {
      await loadAuthored();
      const before = mark();
      useConfigStore.getState().updateConfigValue(path, { preset: "nope" });
      const conflict = {
        kind: "global" as const,
        code: "config_conflict" as const,
        message: "Configuration changed elsewhere.",
        blocking: false,
      };
      const other = {
        kind: "validation" as const,
        issue: {
          loc: ["router", "model"],
          msg: "unknown",
          type: "value_error",
        },
      };
      useConfigStore.setState({
        diagnostics: [conflict, other],
        syncStatus: "error",
      });

      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      const state = useConfigStore.getState();
      expect(state.diagnostics).toEqual([conflict, other]);
      expect(state.syncStatus).toBe("error");
    });

    it("saves nothing different from what was loaded afterwards", async () => {
      await loadAuthored();
      const before = mark();
      useConfigStore.getState().updateConfigValue(path, { preset: "openai" });
      useConfigStore.getState().restoreConfigValue(path, undefined, before);

      expect((await savedPayload()).egress_broker).toEqual(
        authored.egress_broker,
      );
    });

    it("does nothing before a config is loaded", () => {
      useConfigStore.getState().restoreConfigValue(path, undefined, {
        isDirty: false,
        dirtyRoots: [],
        draftVersion: 0,
      });
      expect(useConfigStore.getState().config).toBeNull();
    });
  });

  describe("tool overrides", () => {
    async function loadAgentTools({
      tools = ["browser", { shell: { sandbox: "tight" } }] as unknown[],
      defaultTools = undefined as unknown[] | undefined,
    } = {}) {
      await loadFixture(
        {
          agents: {
            coder: { display_name: "Coder", role: "Codes", tools },
          },
          models: { default: { provider: "ollama", id: "test-model" } },
          defaults: {
            markdown: true,
            ...(defaultTools ? { tools: defaultTools } : {}),
          },
        },
        { coder: makeAgentPolicy("coder") },
      );
    }

    it("gives an agent re-created under a deleted agent's id no tool overrides", async () => {
      await loadAgentTools();
      useConfigStore.getState().deleteAgent("coder");
      useConfigStore.getState().createAgent({
        display_name: "Coder",
        role: "",
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      });
      useConfigStore.getState().updateAgent("coder", { tools: ["shell"] });

      expect(
        useConfigStore.getState().getAgentToolOverrides("coder", "shell"),
      ).toBeNull();
      expect((await savedPayload()).agents.coder.tools).toEqual(["shell"]);
    });

    it("drops a tool's overrides when the tool is unchecked", async () => {
      await loadAgentTools();
      useConfigStore.getState().updateAgent("coder", { tools: ["browser"] });
      expect(
        useConfigStore.getState().getAgentToolOverrides("coder", "shell"),
      ).toBeNull();

      useConfigStore
        .getState()
        .updateAgent("coder", { tools: ["browser", "shell"] });
      expect(
        useConfigStore.getState().getAgentToolOverrides("coder", "shell"),
      ).toBeNull();
      expect((await savedPayload()).agents.coder.tools).toEqual([
        "browser",
        "shell",
      ]);
    });

    it("ignores overrides for a tool the agent or the defaults do not list", async () => {
      await loadAgentTools({ defaultTools: ["file"] });
      const before = useConfigStore.getState();

      useConfigStore
        .getState()
        .updateAgentToolOverrides("coder", "gmail", { label: "support" });
      useConfigStore
        .getState()
        .updateDefaultToolOverrides("gmail", { label: "support" });

      const after = useConfigStore.getState();
      expect(after.isDirty).toBe(false);
      expect(after.draftVersion).toBe(before.draftVersion);
      expect(after.getAgentToolOverrides("coder", "gmail")).toBeNull();
      expect(after.getDefaultToolOverrides("gmail")).toBeNull();
    });

    it("leaves the loaded config unchanged when overrides are edited", async () => {
      await loadAgentTools({ defaultTools: [{ gmail: { label: "support" } }] });
      const loaded = structuredClone(useConfigStore.getState().loadedConfig);

      useConfigStore
        .getState()
        .updateAgentToolOverrides("coder", "shell", { sandbox: "loose" });
      useConfigStore
        .getState()
        .updateDefaultToolOverrides("gmail", { label: "billing" });

      expect(useConfigStore.getState().loadedConfig).toEqual(loaded);
      expect(
        useConfigStore.getState().getAgentToolOverrides("coder", "shell"),
      ).toEqual({ sandbox: "loose" });
      expect(
        useConfigStore.getState().getDefaultToolOverrides("gmail"),
      ).toEqual({ label: "billing" });
    });

    it("keeps loaded tool entries in the draft and exposes their overrides", async () => {
      const tools = [
        "browser",
        { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
      ];
      const defaultTools = [{ gmail: { label: "support" } }, "file"];
      await loadAgentTools({ tools, defaultTools });

      const state = useConfigStore.getState();
      expect(state.agents[0].tools).toEqual(tools);
      expect(state.config?.defaults?.tools).toEqual(defaultTools);
      expect(state.getAgentToolOverrides("coder", "shell")).toEqual({
        extra_env_passthrough: ["GITEA_TOKEN"],
      });
    });

    it("edits overrides of shared default tools", async () => {
      await loadAgentTools({
        defaultTools: [{ gmail: { label: "support" } }, "file", "scheduler"],
      });
      expect(
        useConfigStore.getState().getDefaultToolOverrides("gmail"),
      ).toEqual({ label: "support" });

      useConfigStore
        .getState()
        .updateDefaultToolOverrides("file", { base_dir: "/srv/files" });
      useConfigStore
        .getState()
        .updateDefaultToolOverrides("gmail", { label: null });

      expect(useConfigStore.getState().dirtyRoots).toEqual(["defaults"]);
      expect((await savedPayload()).defaults.tools).toEqual([
        "gmail",
        { file: { base_dir: "/srv/files" } },
        "scheduler",
      ]);
    });

    it("updates overrides, marks the draft dirty, and saves the edited entry", async () => {
      await loadAgentTools({
        tools: [
          "browser",
          { shell: { extra_env_passthrough: ["GITEA_TOKEN"] } },
        ],
      });
      useConfigStore.getState().updateAgentToolOverrides("coder", "shell", {
        shell_path_prepend: ["/run/wrappers/bin"],
      });

      expect(useConfigStore.getState().isDirty).toBe(true);
      expect(
        useConfigStore.getState().getAgentToolOverrides("coder", "shell"),
      ).toEqual({
        extra_env_passthrough: ["GITEA_TOKEN"],
        shell_path_prepend: ["/run/wrappers/bin"],
      });
      expect((await savedPayload()).agents.coder).toEqual({
        display_name: "Coder",
        role: "Codes",
        tools: [
          "browser",
          {
            shell: {
              extra_env_passthrough: ["GITEA_TOKEN"],
              shell_path_prepend: ["/run/wrappers/bin"],
            },
          },
        ],
      });
    });

    it("keeps tool overrides when updateRoom replaces the config object", async () => {
      const mockConfig = {
        agents: {
          openclaw: {
            display_name: "OpenClaw",
            role: "Coding agent",
            tools: [
              "browser",
              {
                shell: {
                  extra_env_passthrough: ["GITEA_TOKEN"],
                  shell_path_prepend: ["/run/wrappers/bin"],
                },
              },
            ],
            skills: [],
            instructions: [],
            rooms: ["lobby"],
          },
        },
        models: {
          default: {
            provider: "ollama",
            id: "test-model",
          },
          claude: {
            provider: "anthropic",
            id: "claude-sonnet-5",
          },
        },
        defaults: {
          markdown: true,
        },
        router: {
          model: "default",
        },
        memory: {
          embedder: {
            provider: "openai",
            config: {
              model: "text-embedding-3-small",
            },
          },
        },
      };

      await loadFixture(mockConfig, { openclaw: makeAgentPolicy("openclaw") });
      useConfigStore.getState().updateRoom("lobby", { model: "claude" });

      expect(
        useConfigStore.getState().getAgentToolOverrides("openclaw", "shell"),
      ).toEqual({
        extra_env_passthrough: ["GITEA_TOKEN"],
        shell_path_prepend: ["/run/wrappers/bin"],
      });
      expect(await savedPayload()).toMatchObject({
        ...mockConfig,
        room_models: {
          lobby: "claude",
        },
      });
    });
  });
});
