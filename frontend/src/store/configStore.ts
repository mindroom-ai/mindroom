import { create } from 'zustand';
import {
  Config,
  Agent,
  AgentPoliciesByAgent,
  Team,
  Room,
  ModelConfig,
  KnowledgeBaseConfig,
  Culture,
  getDefaultPrivateConfig,
  normalizeAgentUpdates,
  normalizeTeamUpdates,
  VoiceConfig,
} from '@/types/config';
import * as configService from '@/services/configService';
import type { ConfigDiagnostic } from '@/lib/configValidation';
import {
  cloneToolEntries,
  getToolOverrides as getToolOverridesFromEntries,
  normalizeToolEntries,
  rebuildToolEntries,
  setToolOverridesInEntries,
  type ToolEntry,
  type ToolOverrides,
} from '@/lib/toolEntry';

const AGENT_POLICIES_ERROR_MESSAGE = 'Failed to derive agent policies';
const CONFIG_VALIDATION_FAILED_MESSAGE = 'Configuration validation failed';

export type SaveConfigResult =
  | { status: 'saved' }
  | { status: 'stale' }
  | { status: 'error'; message: string; diagnostics: ConfigDiagnostic[] };

function retainedDraftDiagnostics(diagnostics: ConfigDiagnostic[]): ConfigDiagnostic[] {
  return diagnostics.filter(
    diagnostic =>
      diagnostic.kind === 'validation' ||
      (diagnostic.kind === 'global' && diagnostic.message === CONFIG_VALIDATION_FAILED_MESSAGE)
  );
}

function nextDraftVersion(draftVersion: number): number {
  return draftVersion + 1;
}

function syncStatusForLoadedConfig(loadedConfig: Config | null): ConfigState['syncStatus'] {
  return loadedConfig == null ? 'disconnected' : 'synced';
}

function markDraftDirty<T extends object>(
  state: Pick<ConfigState, 'draftVersion' | 'diagnostics'>,
  changes: T
): T & Pick<ConfigState, 'isDirty' | 'diagnostics' | 'draftVersion'> {
  return {
    ...changes,
    isDirty: true,
    diagnostics: retainedDraftDiagnostics(state.diagnostics),
    draftVersion: nextDraftVersion(state.draftVersion),
  };
}

function unassignAgentsFromOtherCultures(
  cultures: Culture[],
  targetCultureId: string,
  targetCultureAgents: string[]
): Culture[] {
  const assignedAgents = new Set(targetCultureAgents);
  return cultures.map(culture => {
    if (culture.id === targetCultureId) {
      return culture;
    }
    return {
      ...culture,
      agents: culture.agents.filter(agentId => !assignedAgents.has(agentId)),
    };
  });
}

function removeMissingTeamMembers(teams: Team[], agents: Agent[]): Team[] {
  const knownAgents = new Set(agents.map(agent => agent.id));
  return teams.map(team => ({
    ...team,
    agents: team.agents.filter(agentId => knownAgents.has(agentId)),
  }));
}

function normalizeAgentDelegates(delegateTo: string[] | undefined): string {
  return [...new Set(delegateTo ?? [])].sort().join('\0');
}

function normalizeAgentPolicyKey(
  agent: Pick<Agent, 'private' | 'delegate_to' | 'worker_scope'>
): string {
  return [
    agent.worker_scope ?? '',
    agent.private != null ? 'private' : '',
    agent.private?.per ?? '',
    agent.private?.knowledge?.enabled === false ? 'disabled' : 'enabled',
    agent.private?.knowledge?.path ?? '',
    normalizeAgentDelegates(agent.delegate_to),
  ].join('\0');
}

function agentPolicyChanged(
  currentAgent: Pick<Agent, 'private' | 'delegate_to' | 'worker_scope'>,
  nextAgent: Pick<Agent, 'private' | 'delegate_to' | 'worker_scope'>
): boolean {
  return normalizeAgentPolicyKey(currentAgent) !== normalizeAgentPolicyKey(nextAgent);
}

function agentPoliciesDiagnostic(blocking: boolean): ConfigDiagnostic {
  return {
    kind: 'global',
    message: AGENT_POLICIES_ERROR_MESSAGE,
    blocking,
  };
}

type MemoryEmbedderUpdate = {
  provider: string;
  model: string;
  host?: string;
};

function isMemoryEmbedderUpdate(update: object): update is MemoryEmbedderUpdate {
  return 'provider' in update && 'model' in update;
}

const rawToolEntriesByConfig = new WeakMap<Config, Map<string, ToolEntry[]>>();
const rawDefaultToolEntriesByConfig = new WeakMap<Config, ToolEntry[] | undefined>();

function cloneRawToolEntriesByAgent(
  rawEntriesByAgent: Map<string, ToolEntry[]>
): Map<string, ToolEntry[]> {
  return new Map(
    Array.from(rawEntriesByAgent.entries(), ([agentId, rawEntries]) => [
      agentId,
      cloneToolEntries(rawEntries),
    ])
  );
}

function rememberRawToolEntries(
  config: Config,
  rawEntriesByAgent: Map<string, ToolEntry[]>,
  rawDefaultToolEntries: ToolEntry[] | undefined
): void {
  rawToolEntriesByConfig.set(config, cloneRawToolEntriesByAgent(rawEntriesByAgent));
  rawDefaultToolEntriesByConfig.set(
    config,
    rawDefaultToolEntries === undefined ? undefined : cloneToolEntries(rawDefaultToolEntries)
  );
}

function preserveRawToolEntries(previousConfig: Config | null, nextConfig: Config): void {
  if (previousConfig == null) {
    return;
  }
  rememberRawToolEntries(
    nextConfig,
    rawToolEntriesByConfig.get(previousConfig) ?? new Map<string, ToolEntry[]>(),
    rawDefaultToolEntriesByConfig.get(previousConfig)
  );
}

function getRememberedRawToolEntries(config: Config | null, agentId: string): ToolEntry[] {
  if (config == null) {
    return [];
  }
  return cloneToolEntries(rawToolEntriesByConfig.get(config)?.get(agentId));
}

function getRememberedRawDefaultToolEntries(config: Config | null): ToolEntry[] | undefined {
  if (config == null) {
    return undefined;
  }
  const rawDefaultToolEntries = rawDefaultToolEntriesByConfig.get(config);
  return rawDefaultToolEntries === undefined ? undefined : cloneToolEntries(rawDefaultToolEntries);
}

function setRememberedRawToolEntries(
  config: Config,
  agentId: string,
  rawEntries: ToolEntry[]
): void {
  const rememberedEntries = rawToolEntriesByConfig.get(config);
  const nextEntriesByAgent =
    rememberedEntries == null
      ? new Map<string, ToolEntry[]>()
      : cloneRawToolEntriesByAgent(rememberedEntries);
  nextEntriesByAgent.set(agentId, cloneToolEntries(rawEntries));
  rawToolEntriesByConfig.set(config, nextEntriesByAgent);
}

function normalizeConfigToolEntries(rawConfig: configService.RawConfig): {
  normalizedConfig: Config;
  rawEntriesByAgent: Map<string, ToolEntry[]>;
  rawDefaultToolEntries: ToolEntry[] | undefined;
} {
  const { agents: _rawAgents, defaults: rawDefaults, ...restConfig } = rawConfig;
  const rawEntriesByAgent = new Map<string, ToolEntry[]>();
  const rawDefaultToolEntries =
    rawDefaults?.tools === undefined ? undefined : cloneToolEntries(rawDefaults.tools);
  const normalizedAgents = Object.fromEntries(
    Object.entries(rawConfig.agents).map(([agentId, agentConfig]) => {
      const rawEntries = cloneToolEntries(agentConfig.tools);
      rawEntriesByAgent.set(agentId, rawEntries);
      return [
        agentId,
        {
          ...agentConfig,
          tools: normalizeToolEntries(rawEntries),
        },
      ];
    })
  );
  const normalizedDefaults = rawDefaults
    ? {
        ...rawDefaults,
        tools:
          rawDefaults.tools === undefined ? undefined : normalizeToolEntries(rawDefaultToolEntries),
      }
    : undefined;

  return {
    normalizedConfig: {
      ...restConfig,
      agents: normalizedAgents,
      ...(normalizedDefaults ? { defaults: normalizedDefaults } : {}),
    } as Config,
    rawEntriesByAgent,
    rawDefaultToolEntries,
  };
}

interface ConfigState {
  // State
  loadedConfig: Config | null;
  config: Config | null;
  draftVersion: number;
  agents: Agent[];
  teams: Team[];
  cultures: Culture[];
  rooms: Room[];
  agentPoliciesByAgent: AgentPoliciesByAgent;
  agentPoliciesStale: boolean;
  agentPoliciesRequestId: number;
  loadConfigRequestId: number;
  saveConfigRequestId: number;
  selectedAgentId: string | null;
  selectedTeamId: string | null;
  selectedCultureId: string | null;
  selectedRoomId: string | null;
  isDirty: boolean;
  isLoading: boolean;
  diagnostics: ConfigDiagnostic[];
  syncStatus: 'synced' | 'syncing' | 'error' | 'disconnected';
  // UI-only backup so a draft private toggle can restore the prior explicit worker_scope
  // until the draft is either saved successfully or toggled back off.
  privateWorkerScopeBackups: Record<string, Agent['worker_scope'] | null>;

  // Actions
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<SaveConfigResult>;
  refreshAgentPolicies: (agents: Agent[]) => Promise<void>;
  selectAgent: (agentId: string | null) => void;
  updateAgent: (agentId: string, updates: Partial<Agent>) => void;
  setAgentPrivateEnabled: (agentId: string, enabled: boolean) => void;
  createAgent: (agent: Omit<Agent, 'id'>) => void;
  deleteAgent: (agentId: string) => void;
  selectTeam: (teamId: string | null) => void;
  updateTeam: (teamId: string, updates: Partial<Team>) => void;
  createTeam: (team: Omit<Team, 'id'>) => void;
  deleteTeam: (teamId: string) => void;
  selectCulture: (cultureId: string | null) => void;
  updateCulture: (cultureId: string, updates: Partial<Culture>) => void;
  createCulture: (culture: Omit<Culture, 'id'>) => void;
  deleteCulture: (cultureId: string) => void;
  selectRoom: (roomId: string | null) => void;
  updateRoom: (roomId: string, updates: Partial<Room>) => void;
  createRoom: (room: Omit<Room, 'id'>) => void;
  deleteRoom: (roomId: string) => void;
  addAgentToRoom: (roomId: string, agentId: string) => void;
  removeAgentFromRoom: (roomId: string, agentId: string) => void;
  updateRoomModels: (roomModels: Record<string, string>) => void;
  updateMemoryConfig: (memoryConfig: MemoryEmbedderUpdate | Config['memory']) => void;
  updateKnowledgeBase: (baseName: string, baseConfig: KnowledgeBaseConfig) => void;
  deleteKnowledgeBase: (baseName: string) => void;
  updateModel: (modelId: string, updates: Partial<ModelConfig>) => void;
  deleteModel: (modelId: string) => void;
  updateToolConfig: (toolId: string, config: unknown) => void;
  updateVoiceConfig: (voiceConfig: VoiceConfig) => void;
  getAgentToolOverrides: (agentId: string, toolName: string) => ToolOverrides | null;
  updateAgentToolOverrides: (
    agentId: string,
    toolName: string,
    overrides: ToolOverrides | null
  ) => void;
  markDirty: () => void;
}

function clearedLoadedConfigState(
  diagnostics: ConfigDiagnostic[],
  agentPoliciesRequestId: number,
  draftVersion: number
): Pick<
  ConfigState,
  | 'loadedConfig'
  | 'config'
  | 'draftVersion'
  | 'agents'
  | 'teams'
  | 'cultures'
  | 'rooms'
  | 'agentPoliciesByAgent'
  | 'agentPoliciesStale'
  | 'agentPoliciesRequestId'
  | 'selectedAgentId'
  | 'selectedTeamId'
  | 'selectedCultureId'
  | 'selectedRoomId'
  | 'isDirty'
  | 'isLoading'
  | 'diagnostics'
  | 'syncStatus'
  | 'privateWorkerScopeBackups'
> {
  return {
    loadedConfig: null,
    config: null,
    draftVersion,
    agents: [],
    teams: [],
    cultures: [],
    rooms: [],
    agentPoliciesByAgent: {},
    agentPoliciesStale: false,
    agentPoliciesRequestId,
    selectedAgentId: null,
    selectedTeamId: null,
    selectedCultureId: null,
    selectedRoomId: null,
    isDirty: false,
    isLoading: false,
    diagnostics,
    syncStatus: 'error',
    privateWorkerScopeBackups: {},
  };
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  // Initial state
  loadedConfig: null,
  config: null,
  draftVersion: 0,
  agents: [],
  teams: [],
  cultures: [],
  rooms: [],
  agentPoliciesByAgent: {},
  agentPoliciesStale: false,
  agentPoliciesRequestId: 0,
  loadConfigRequestId: 0,
  saveConfigRequestId: 0,
  selectedAgentId: null,
  selectedTeamId: null,
  selectedCultureId: null,
  selectedRoomId: null,
  isDirty: false,
  isLoading: false,
  diagnostics: [],
  syncStatus: 'disconnected',
  privateWorkerScopeBackups: {},

  // Load configuration from backend
  loadConfig: async () => {
    const loadConfigRequestId = get().loadConfigRequestId + 1;
    set({ isLoading: true, diagnostics: [], loadConfigRequestId });
    try {
      const rawConfig = await configService.loadConfig();
      const {
        normalizedConfig: loadedConfig,
        rawEntriesByAgent,
        rawDefaultToolEntries,
      } = normalizeConfigToolEntries(rawConfig);
      const normalizedConfig: Config = {
        ...loadedConfig,
        knowledge_bases: loadedConfig.knowledge_bases || {},
        cultures: loadedConfig.cultures || {},
      };
      rememberRawToolEntries(normalizedConfig, rawEntriesByAgent, rawDefaultToolEntries);
      const defaultLearning = normalizedConfig.defaults?.learning ?? true;
      const defaultLearningMode = normalizedConfig.defaults?.learning_mode ?? 'always';
      const agents = Object.entries(normalizedConfig.agents).map(([id, agent]) => ({
        id,
        ...agent,
        skills: agent.skills ?? [],
        knowledge_bases: agent.knowledge_bases || [],
        delegate_to: agent.delegate_to || [],
        context_files: agent.context_files ?? [],
        learning: agent.learning ?? defaultLearning,
        learning_mode: agent.learning_mode ?? defaultLearningMode,
      }));
      const teams = normalizedConfig.teams
        ? Object.entries(normalizedConfig.teams).map(([id, team]) => ({
            id,
            ...team,
          }))
        : [];
      const cultures = normalizedConfig.cultures
        ? Object.entries(normalizedConfig.cultures).map(([id, culture]) => ({
            id,
            ...culture,
            agents: culture.agents ?? [],
            mode: culture.mode ?? 'automatic',
            description: culture.description ?? '',
          }))
        : [];

      // Extract unique rooms from agents and create Room objects
      const roomIds = new Set<string>();
      agents.forEach(agent => {
        agent.rooms.forEach(room => roomIds.add(room));
      });

      const rooms: Room[] = Array.from(roomIds).map(roomId => {
        const agentsInRoom = agents.filter(agent => agent.rooms.includes(roomId)).map(a => a.id);
        const roomModel = normalizedConfig.room_models?.[roomId];
        return {
          id: roomId,
          display_name: roomId.charAt(0).toUpperCase() + roomId.slice(1),
          description: '',
          agents: agentsInRoom,
          model: roomModel,
        };
      });
      let agentPoliciesByAgent: AgentPoliciesByAgent = {};
      let diagnostics: ConfigDiagnostic[] = [];
      let agentPoliciesStale = false;
      try {
        agentPoliciesByAgent = await configService.getAgentPolicies(normalizedConfig, agents);
      } catch {
        diagnostics = [agentPoliciesDiagnostic(false)];
        agentPoliciesStale = true;
      }
      if (get().loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      const nextAgentPoliciesRequestId = get().agentPoliciesRequestId + 1;
      const nextDraft = nextDraftVersion(get().draftVersion);

      set({
        loadedConfig: normalizedConfig,
        config: normalizedConfig,
        draftVersion: nextDraft,
        agents,
        teams,
        cultures,
        rooms,
        agentPoliciesByAgent,
        agentPoliciesStale,
        agentPoliciesRequestId: nextAgentPoliciesRequestId,
        isLoading: false,
        syncStatus: 'synced',
        isDirty: false,
        diagnostics,
        privateWorkerScopeBackups: {},
      });
    } catch (error) {
      if (get().loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      const nextAgentPoliciesRequestId = get().agentPoliciesRequestId + 1;
      if (error instanceof configService.ConfigValidationError) {
        const nextDraft = nextDraftVersion(get().draftVersion);
        set(
          clearedLoadedConfigState(
            [
              {
                kind: 'global',
                message: CONFIG_VALIDATION_FAILED_MESSAGE,
                blocking: true,
              },
              ...error.issues.map(issue => ({
                kind: 'validation' as const,
                issue,
              })),
            ],
            nextAgentPoliciesRequestId,
            nextDraft
          )
        );
        return;
      }
      set({
        diagnostics: [
          {
            kind: 'global',
            message: error instanceof Error ? error.message : 'Failed to load config',
            blocking: true,
          },
        ],
        isLoading: false,
        syncStatus: 'error',
      });
    }
  },

  refreshAgentPolicies: async agents => {
    const { config } = get();
    if (config == null) {
      return;
    }
    const agentPoliciesRequestId = get().agentPoliciesRequestId + 1;
    set({
      agentPoliciesRequestId,
      agentPoliciesByAgent: {},
      agentPoliciesStale: true,
      diagnostics: retainedDraftDiagnostics(get().diagnostics),
    });
    try {
      const agentPoliciesByAgent = await configService.getAgentPolicies(config, agents);
      if (get().agentPoliciesRequestId != agentPoliciesRequestId) {
        return;
      }
      set({
        agentPoliciesByAgent,
        agentPoliciesStale: false,
        diagnostics: retainedDraftDiagnostics(get().diagnostics),
      });
    } catch {
      if (get().agentPoliciesRequestId != agentPoliciesRequestId) {
        return;
      }
      set({
        agentPoliciesByAgent: {},
        agentPoliciesStale: true,
        diagnostics: [
          ...retainedDraftDiagnostics(get().diagnostics),
          agentPoliciesDiagnostic(false),
        ],
      });
    }
  },

  // Save configuration to backend
  saveConfig: async () => {
    const {
      config,
      agents,
      teams,
      cultures,
      rooms,
      agentPoliciesStale,
      draftVersion,
      loadedConfig,
      diagnostics,
    } = get();
    if (!config) {
      return {
        status: 'error',
        message: 'No configuration draft is available to save.',
        diagnostics,
      };
    }
    const saveConfigRequestId = get().saveConfigRequestId + 1;
    const savedDraftVersion = draftVersion;

    set({
      isLoading: true,
      diagnostics: [],
      syncStatus: 'syncing',
      saveConfigRequestId,
    });
    try {
      const rawEntriesByAgent = new Map(
        agents.map(agent => [
          agent.id,
          rebuildToolEntries(agent.tools, getRememberedRawToolEntries(config, agent.id)),
        ])
      );
      const rawDefaultToolEntries = getRememberedRawDefaultToolEntries(config);

      // Convert agents array back to object format
      const normalizedAgentsObject = agents.reduce(
        (acc, agent) => {
          const { id, ...rest } = agent;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Agent, 'id'>>
      );
      const payloadAgentsObject = agents.reduce(
        (acc, agent) => {
          const { id, ...rest } = agent;
          const rawToolEntries = rawEntriesByAgent.get(id);
          acc[id] = {
            ...rest,
            tools: rawToolEntries ?? rest.tools,
          };
          return acc;
        },
        {} as configService.ConfigSavePayload['agents']
      );

      const culturesObject = cultures.reduce(
        (acc, culture) => {
          const { id, ...rest } = culture;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Culture, 'id'>>
      );

      // Extract room_models from rooms that have a model set
      const roomModels: Record<string, string> = {};
      rooms.forEach(room => {
        if (room.model) {
          roomModels[room.id] = room.model;
        }
      });

      const updatedConfig: Config = {
        ...config,
        agents: normalizedAgentsObject,
        teams: teams.reduce(
          (acc, team) => {
            const { id, ...rest } = team;
            acc[id] = rest;
            return acc;
          },
          {} as Record<string, Omit<Team, 'id'>>
        ),
        cultures: culturesObject,
        room_models: Object.keys(roomModels).length > 0 ? roomModels : undefined,
      };
      const payload: configService.ConfigSavePayload = {
        ...updatedConfig,
        agents: payloadAgentsObject,
        defaults: {
          ...updatedConfig.defaults,
          tools: rawDefaultToolEntries ?? updatedConfig.defaults.tools,
        },
      };

      await configService.saveConfig(payload);
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      rememberRawToolEntries(updatedConfig, rawEntriesByAgent, rawDefaultToolEntries);
      if (draftChangedSinceSaveStarted) {
        const baselineConfig =
          currentState.loadedConfig ?? currentState.config ?? loadedConfig ?? config;
        set({
          loadedConfig: updatedConfig,
          isLoading: false,
          syncStatus: syncStatusForLoadedConfig(baselineConfig),
        });
        if (currentState.agentPoliciesStale) {
          void get().refreshAgentPolicies(currentState.agents);
        }
        return { status: 'stale' };
      }
      set({
        loadedConfig: updatedConfig,
        config: updatedConfig,
        isLoading: false,
        syncStatus: 'synced',
        isDirty: false,
        diagnostics: [],
        privateWorkerScopeBackups: {},
      });
      if (agentPoliciesStale) {
        void get().refreshAgentPolicies(agents);
      }
      return { status: 'saved' };
    } catch (error) {
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      if (draftChangedSinceSaveStarted) {
        const baselineConfig =
          currentState.loadedConfig ?? currentState.config ?? loadedConfig ?? config;
        set({
          isLoading: false,
          syncStatus: syncStatusForLoadedConfig(baselineConfig),
        });
        return { status: 'stale' };
      }
      if (error instanceof configService.ConfigValidationError) {
        const errorDiagnostics = [
          {
            kind: 'global' as const,
            message: 'Configuration validation failed',
            blocking: false,
          },
          ...error.issues.map(issue => ({
            kind: 'validation' as const,
            issue,
          })),
        ];
        set({
          diagnostics: errorDiagnostics,
          isLoading: false,
          syncStatus: 'error',
        });
        return {
          status: 'error',
          message: 'Configuration validation failed',
          diagnostics: errorDiagnostics,
        };
      }
      const errorMessage = error instanceof Error ? error.message : 'Failed to save config';
      const errorDiagnostics = [
        {
          kind: 'global' as const,
          message: errorMessage,
          blocking: false,
        },
      ];
      set({
        diagnostics: errorDiagnostics,
        isLoading: false,
        syncStatus: 'error',
      });
      return {
        status: 'error',
        message: errorMessage,
        diagnostics: errorDiagnostics,
      };
    }
  },

  // Select an agent for editing
  selectAgent: agentId => {
    set({ selectedAgentId: agentId });
  },

  // Update an existing agent
  updateAgent: (agentId, updates) => {
    let nextAgents: Agent[] = [];
    let shouldRefreshAgentPolicies = false;
    set(state => {
      const currentAgent = state.agents.find(agent => agent.id === agentId);
      if (!currentAgent) {
        return state;
      }

      const normalizedUpdates = normalizeAgentUpdates(currentAgent, updates);
      nextAgents = state.agents.map(agent =>
        agent.id === agentId ? { ...agent, ...normalizedUpdates } : agent
      );
      const nextAgent = nextAgents.find(agent => agent.id === agentId) ?? currentAgent;
      shouldRefreshAgentPolicies = agentPolicyChanged(currentAgent, nextAgent);

      return {
        agents: nextAgents,
        ...markDraftDirty(state, {}),
      };
    });
    if (shouldRefreshAgentPolicies && get().config != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  setAgentPrivateEnabled: (agentId, enabled) => {
    let nextAgents: Agent[] = [];
    let shouldRefreshAgentPolicies = false;
    set(state => {
      const currentAgent = state.agents.find(agent => agent.id === agentId);
      if (!currentAgent) {
        return state;
      }

      const nextBackups = { ...state.privateWorkerScopeBackups };
      const privateUpdates = enabled
        ? (() => {
            if (!(agentId in nextBackups)) {
              nextBackups[agentId] = currentAgent.worker_scope ?? null;
            }
            return { private: getDefaultPrivateConfig(currentAgent) };
          })()
        : (() => {
            const restoredWorkerScope = nextBackups[agentId];
            delete nextBackups[agentId];
            return restoredWorkerScope != null
              ? { private: undefined, worker_scope: restoredWorkerScope }
              : { private: undefined };
          })();

      const normalizedUpdates = normalizeAgentUpdates(currentAgent, privateUpdates);
      nextAgents = state.agents.map(agent =>
        agent.id === agentId ? { ...agent, ...normalizedUpdates } : agent
      );
      const nextAgent = nextAgents.find(agent => agent.id === agentId) ?? currentAgent;
      shouldRefreshAgentPolicies = agentPolicyChanged(currentAgent, nextAgent);

      return {
        agents: nextAgents,
        ...markDraftDirty(state, {}),
        privateWorkerScopeBackups: nextBackups,
      };
    });
    if (shouldRefreshAgentPolicies && get().config != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  // Create a new agent
  createAgent: agentData => {
    const id = agentData.display_name.toLowerCase().replace(/\s+/g, '_');
    const defaultLearning = get().config?.defaults.learning ?? true;
    const defaultLearningMode = get().config?.defaults.learning_mode ?? 'always';
    const newAgent: Agent = {
      id,
      ...agentData,
      knowledge_bases: agentData.knowledge_bases ?? [],
      delegate_to: agentData.delegate_to ?? [],
      learning: agentData.learning ?? defaultLearning,
      learning_mode: agentData.learning_mode ?? defaultLearningMode,
    };
    set(state => ({
      agents: [...state.agents, newAgent],
      selectedAgentId: id,
      ...markDraftDirty(state, {}),
    }));
    if (get().config != null) {
      void get().refreshAgentPolicies([...get().agents]);
    }
  },

  // Delete an agent
  deleteAgent: agentId => {
    const state = get();
    const deletedAgent = state.agents.find(agent => agent.id === agentId);
    const nextAgents = state.agents
      .filter(agent => agent.id !== agentId)
      .map(agent => {
        if (!agent.delegate_to?.includes(agentId)) {
          return agent;
        }
        return {
          ...agent,
          delegate_to: agent.delegate_to.filter(id => id !== agentId),
        };
      });
    const nextAgentPoliciesByAgent = Object.fromEntries(
      Object.entries(state.agentPoliciesByAgent).filter(([id]) => id !== agentId)
    );
    const { [agentId]: _removedBackup, ...remainingBackups } = state.privateWorkerScopeBackups;
    set({
      agents: nextAgents,
      teams: removeMissingTeamMembers(state.teams, nextAgents),
      cultures: state.cultures.map(culture => ({
        ...culture,
        agents: culture.agents.filter(id => id !== agentId),
      })),
      agentPoliciesByAgent: nextAgentPoliciesByAgent,
      privateWorkerScopeBackups: remainingBackups,
      selectedAgentId: state.selectedAgentId === agentId ? null : state.selectedAgentId,
      ...markDraftDirty(state, {}),
    });
    if (get().config != null && deletedAgent != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  // Select a team for editing
  selectTeam: teamId => {
    set({ selectedTeamId: teamId });
  },

  // Update an existing team
  updateTeam: (teamId, updates) => {
    set(state => {
      const currentTeam = state.teams.find(team => team.id === teamId);
      if (!currentTeam) {
        return state;
      }

      const normalizedUpdates = normalizeTeamUpdates(currentTeam, updates);
      return {
        teams: state.teams.map(team =>
          team.id === teamId ? { ...team, ...normalizedUpdates } : team
        ),
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Create a new team
  createTeam: teamData => {
    const id = teamData.display_name.toLowerCase().replace(/\s+/g, '_');
    const newTeam: Team = {
      id,
      ...teamData,
    };
    set(state => ({
      teams: [...state.teams, newTeam],
      selectedTeamId: id,
      ...markDraftDirty(state, {}),
    }));
  },

  // Delete a team
  deleteTeam: teamId => {
    set(state => ({
      teams: state.teams.filter(team => team.id !== teamId),
      selectedTeamId: state.selectedTeamId === teamId ? null : state.selectedTeamId,
      ...markDraftDirty(state, {}),
    }));
  },

  // Select a culture for editing
  selectCulture: cultureId => {
    set({ selectedCultureId: cultureId });
  },

  // Update an existing culture
  updateCulture: (cultureId, updates) => {
    set(state => {
      const updatedCultures = state.cultures.map(culture =>
        culture.id === cultureId ? { ...culture, ...updates } : culture
      );

      if (updates.agents) {
        const targetCulture = updatedCultures.find(culture => culture.id === cultureId);
        if (!targetCulture) {
          return {
            cultures: updatedCultures,
            ...markDraftDirty(state, {}),
          };
        }
        return {
          cultures: unassignAgentsFromOtherCultures(
            updatedCultures,
            cultureId,
            targetCulture.agents
          ),
          ...markDraftDirty(state, {}),
        };
      }

      return {
        cultures: updatedCultures,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Create a new culture
  createCulture: cultureData => {
    set(state => {
      const baseId = (cultureData.description || 'new_culture').toLowerCase().replace(/\s+/g, '_');
      let id = baseId;
      let counter = 1;
      while (state.cultures.some(culture => culture.id === id)) {
        id = `${baseId}_${counter}`;
        counter += 1;
      }

      const newCulture: Culture = {
        id,
        ...cultureData,
        description: cultureData.description || '',
        mode: cultureData.mode || 'automatic',
        agents: cultureData.agents || [],
      };
      const nextCultures = unassignAgentsFromOtherCultures(
        [...state.cultures, newCulture],
        id,
        newCulture.agents
      );
      return {
        cultures: nextCultures,
        selectedCultureId: id,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Delete a culture
  deleteCulture: cultureId => {
    set(state => ({
      cultures: state.cultures.filter(culture => culture.id !== cultureId),
      selectedCultureId: state.selectedCultureId === cultureId ? null : state.selectedCultureId,
      ...markDraftDirty(state, {}),
    }));
  },

  // Select a room for editing
  selectRoom: roomId => {
    set({ selectedRoomId: roomId });
  },

  // Update an existing room
  updateRoom: (roomId, updates) => {
    set(state => {
      const updatedRooms = state.rooms.map(room =>
        room.id === roomId ? { ...room, ...updates } : room
      );

      let updatedConfig = state.config;

      // If model changed, update room_models in config
      if (updates.model !== undefined && state.config) {
        const currentRoomModels = state.config.room_models || {};
        const newRoomModels = { ...currentRoomModels };

        if (updates.model) {
          // Set the room model
          newRoomModels[roomId] = updates.model;
        } else {
          // Remove the room model if it's being unset
          delete newRoomModels[roomId];
        }

        updatedConfig = {
          ...state.config,
          room_models: Object.keys(newRoomModels).length > 0 ? newRoomModels : undefined,
        };
      }

      const previousConfig = state.config;
      if (previousConfig && updatedConfig && updatedConfig !== previousConfig) {
        preserveRawToolEntries(previousConfig, updatedConfig);
      }

      // If agents changed, update the agents' rooms arrays
      if (updates.agents) {
        const oldRoom = state.rooms.find(r => r.id === roomId);
        const oldAgents = oldRoom?.agents || [];
        const newAgents = updates.agents;

        // Remove room from agents no longer in the room
        const removedAgents = oldAgents.filter(id => !newAgents.includes(id));
        // Add room to new agents
        const addedAgents = newAgents.filter(id => !oldAgents.includes(id));

        const updatedAgents = state.agents.map(agent => {
          if (removedAgents.includes(agent.id)) {
            return { ...agent, rooms: agent.rooms.filter(r => r !== roomId) };
          }
          if (addedAgents.includes(agent.id) && !agent.rooms.includes(roomId)) {
            return { ...agent, rooms: [...agent.rooms, roomId] };
          }
          return agent;
        });

        return {
          config: updatedConfig,
          rooms: updatedRooms,
          agents: updatedAgents,
          ...markDraftDirty(state, {}),
        };
      }

      return {
        config: updatedConfig,
        rooms: updatedRooms,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Create a new room
  createRoom: roomData => {
    const id = roomData.display_name.toLowerCase().replace(/\s+/g, '_');
    const newRoom: Room = {
      id,
      ...roomData,
    };

    set(state => {
      // Add room to selected agents
      const updatedAgents = state.agents.map(agent => {
        if (roomData.agents.includes(agent.id) && !agent.rooms.includes(id)) {
          return { ...agent, rooms: [...agent.rooms, id] };
        }
        return agent;
      });

      return {
        rooms: [...state.rooms, newRoom],
        agents: updatedAgents,
        selectedRoomId: id,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Delete a room
  deleteRoom: roomId => {
    set(state => {
      // Remove room from all agents
      const updatedAgents = state.agents.map(agent => ({
        ...agent,
        rooms: agent.rooms.filter(r => r !== roomId),
      }));

      // Remove room from teams
      const updatedTeams = state.teams.map(team => ({
        ...team,
        rooms: team.rooms.filter(r => r !== roomId),
      }));

      // Remove from room_models if it exists
      let updatedConfig = state.config;
      if (state.config?.room_models?.[roomId]) {
        const { [roomId]: _, ...remainingModels } = state.config.room_models;
        updatedConfig = {
          ...state.config,
          room_models: remainingModels,
        };
        preserveRawToolEntries(state.config, updatedConfig);
      }

      return {
        rooms: state.rooms.filter(room => room.id !== roomId),
        agents: updatedAgents,
        teams: updatedTeams,
        config: updatedConfig,
        selectedRoomId: state.selectedRoomId === roomId ? null : state.selectedRoomId,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Add agent to room
  addAgentToRoom: (roomId, agentId) => {
    set(state => {
      const updatedRooms = state.rooms.map(room => {
        if (room.id === roomId && !room.agents.includes(agentId)) {
          return { ...room, agents: [...room.agents, agentId] };
        }
        return room;
      });

      const updatedAgents = state.agents.map(agent => {
        if (agent.id === agentId && !agent.rooms.includes(roomId)) {
          return { ...agent, rooms: [...agent.rooms, roomId] };
        }
        return agent;
      });

      return {
        rooms: updatedRooms,
        agents: updatedAgents,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Remove agent from room
  removeAgentFromRoom: (roomId, agentId) => {
    set(state => {
      const updatedRooms = state.rooms.map(room => {
        if (room.id === roomId) {
          return { ...room, agents: room.agents.filter(id => id !== agentId) };
        }
        return room;
      });

      const updatedAgents = state.agents.map(agent => {
        if (agent.id === agentId) {
          return { ...agent, rooms: agent.rooms.filter(r => r !== roomId) };
        }
        return agent;
      });

      return {
        rooms: updatedRooms,
        agents: updatedAgents,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Update room models
  updateRoomModels: roomModels => {
    set(state => {
      if (!state.config) return state;
      const nextConfig = {
        ...state.config,
        room_models: roomModels,
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Update memory configuration
  updateMemoryConfig: memoryConfig => {
    set(state => {
      if (!state.config) return state;
      if (isMemoryEmbedderUpdate(memoryConfig)) {
        const nextConfig = {
          ...state.config,
          memory: {
            ...state.config.memory,
            embedder: {
              provider: memoryConfig.provider,
              config: {
                model: memoryConfig.model,
                ...(memoryConfig.host ? { host: memoryConfig.host } : {}),
              },
            },
          },
        };
        preserveRawToolEntries(state.config, nextConfig);
        return {
          config: nextConfig,
          ...markDraftDirty(state, {}),
        };
      }

      const nextConfig = {
        ...state.config,
        memory: memoryConfig,
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Update one knowledge base configuration
  updateKnowledgeBase: (baseName, baseConfig) => {
    set(state => {
      if (!state.config) return state;
      const existingBaseConfig = state.config.knowledge_bases?.[baseName] || {};
      const nextConfig = {
        ...state.config,
        knowledge_bases: {
          ...(state.config.knowledge_bases || {}),
          [baseName]: {
            ...existingBaseConfig,
            ...baseConfig,
          },
        },
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Delete a knowledge base and unassign it from agents
  deleteKnowledgeBase: baseName => {
    set(state => {
      if (!state.config) return state;

      const knowledgeBases = { ...(state.config.knowledge_bases || {}) };
      delete knowledgeBases[baseName];

      const agents = state.agents.map(agent => ({
        ...agent,
        knowledge_bases: (agent.knowledge_bases || []).filter(base => base !== baseName),
      }));
      const configAgents = Object.fromEntries(
        Object.entries(state.config.agents).map(([agentId, agentConfig]) => [
          agentId,
          {
            ...agentConfig,
            knowledge_bases: (agentConfig.knowledge_bases || []).filter(base => base !== baseName),
          },
        ])
      );
      const nextConfig = {
        ...state.config,
        knowledge_bases: knowledgeBases,
        agents: configAgents,
      };
      preserveRawToolEntries(state.config, nextConfig);

      return {
        config: nextConfig,
        agents,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Update a model configuration
  updateModel: (modelId, updates) => {
    set(state => {
      if (!state.config) return state;
      const nextConfig = {
        ...state.config,
        models: {
          ...state.config.models,
          [modelId]: {
            ...state.config.models[modelId],
            ...updates,
          },
        },
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Delete a model configuration
  deleteModel: modelId => {
    set(state => {
      if (!state.config) return state;
      const { [modelId]: _, ...remainingModels } = state.config.models;
      const nextConfig = {
        ...state.config,
        models: remainingModels,
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  // Update tool configuration
  updateToolConfig: (toolId, config) => {
    set(state => {
      if (!state.config) return state;
      const nextConfig = {
        ...state.config,
        tools: {
          ...state.config.tools,
          [toolId]: config,
        },
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  updateVoiceConfig: voiceConfig => {
    set(state => {
      if (!state.config) return state;
      const nextConfig = {
        ...state.config,
        voice: voiceConfig,
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}),
      };
    });
  },

  getAgentToolOverrides: (agentId, toolName) => {
    const config = get().config;
    return getToolOverridesFromEntries(toolName, getRememberedRawToolEntries(config, agentId));
  },

  updateAgentToolOverrides: (agentId, toolName, overrides) => {
    const config = get().config;
    if (!config) {
      return;
    }
    const nextRawEntries = setToolOverridesInEntries(
      toolName,
      overrides,
      getRememberedRawToolEntries(config, agentId)
    );
    setRememberedRawToolEntries(config, agentId, nextRawEntries);
    set(state => ({
      ...markDraftDirty(state, {}),
    }));
  },

  // Mark configuration as dirty
  markDirty: () => {
    set(state => ({
      ...markDraftDirty(state, {}),
    }));
  },
}));
