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
import type { ConfigDiagnostic, ConfigValidationIssue } from '@/lib/configValidation';
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

type ConfigDiagnosticPath = Array<string | number>;

function validationDiagnostics(
  issues: ConfigValidationIssue[],
  options: { blocking: boolean }
): ConfigDiagnostic[] {
  return [
    {
      kind: 'global',
      message: CONFIG_VALIDATION_FAILED_MESSAGE,
      blocking: options.blocking,
    },
    ...issues.map(issue => ({
      kind: 'validation' as const,
      issue,
    })),
  ];
}

function draftSyncStatus(state: {
  loadedConfig: Config | null;
  isDirty: boolean;
}): ConfigState['syncStatus'] {
  if (state.isDirty) {
    return 'error';
  }
  return state.loadedConfig == null ? 'disconnected' : 'synced';
}

function diagnosticsContainValidationErrors(diagnostics: ConfigDiagnostic[]): boolean {
  return diagnostics.some(diagnostic => diagnostic.kind === 'validation');
}

function pathStartsWith(path: ConfigDiagnosticPath, prefix: ConfigDiagnosticPath): boolean {
  return prefix.every((segment, index) => path[index] === segment);
}

function diagnosticsOverlapTouchedPath(
  issuePath: ConfigDiagnosticPath,
  touchedPath: ConfigDiagnosticPath
): boolean {
  return pathStartsWith(issuePath, touchedPath) || pathStartsWith(touchedPath, issuePath);
}

function retainedDraftDiagnostics(
  diagnostics: ConfigDiagnostic[],
  touchedPaths: ConfigDiagnosticPath[] = []
): ConfigDiagnostic[] {
  const filteredDiagnostics = diagnostics.filter(diagnostic => {
    if (diagnostic.kind !== 'validation') {
      return true;
    }
    return !touchedPaths.some(path => diagnosticsOverlapTouchedPath(diagnostic.issue.loc, path));
  });

  if (diagnosticsContainValidationErrors(filteredDiagnostics)) {
    return filteredDiagnostics.filter(
      diagnostic =>
        diagnostic.kind === 'validation' ||
        (diagnostic.kind === 'global' && diagnostic.message === CONFIG_VALIDATION_FAILED_MESSAGE)
    );
  }

  return filteredDiagnostics.filter(diagnostic => diagnostic.kind === 'validation');
}

function mergeDirtyRoots(
  existingDirtyRoots: string[],
  touchedPaths: ConfigDiagnosticPath[]
): string[] {
  if (touchedPaths.length === 0) {
    return existingDirtyRoots;
  }
  const dirtyRoots = new Set(existingDirtyRoots);
  for (const [root] of touchedPaths) {
    if (typeof root === 'string') {
      dirtyRoots.add(root);
    }
  }
  return Array.from(dirtyRoots);
}

function globalDiagnostics(message: string, blocking: boolean): ConfigDiagnostic[] {
  return [
    {
      kind: 'global',
      message,
      blocking,
    },
  ];
}

function firstGlobalDiagnosticMessage(
  diagnostics: ConfigDiagnostic[],
  fallbackMessage: string
): string {
  return diagnostics.find(diagnostic => diagnostic.kind === 'global')?.message ?? fallbackMessage;
}

function nextDraftVersion(draftVersion: number): number {
  return draftVersion + 1;
}

function markDraftDirty<T extends object>(
  state: Pick<ConfigState, 'draftVersion' | 'diagnostics' | 'dirtyRoots'>,
  changes: T,
  touchedPaths: ConfigDiagnosticPath[] = []
): T & Pick<ConfigState, 'isDirty' | 'diagnostics' | 'draftVersion' | 'dirtyRoots'> {
  return {
    ...changes,
    isDirty: true,
    diagnostics: retainedDraftDiagnostics(state.diagnostics, touchedPaths),
    draftVersion: nextDraftVersion(state.draftVersion),
    dirtyRoots: mergeDirtyRoots(state.dirtyRoots, touchedPaths),
  };
}

function deriveConfigCollections(
  config: Config
): Pick<ConfigState, 'agents' | 'teams' | 'cultures' | 'rooms'> {
  const defaultLearning = config.defaults?.learning ?? true;
  const defaultLearningMode = config.defaults?.learning_mode ?? 'always';
  const agents = Object.entries(config.agents).map(([id, agent]) => ({
    id,
    ...agent,
    skills: agent.skills ?? [],
    knowledge_bases: agent.knowledge_bases || [],
    delegate_to: agent.delegate_to || [],
    context_files: agent.context_files ?? [],
    learning: agent.learning ?? defaultLearning,
    learning_mode: agent.learning_mode ?? defaultLearningMode,
  }));
  const teams = config.teams
    ? Object.entries(config.teams).map(([id, team]) => ({
        id,
        ...team,
      }))
    : [];
  const cultures = config.cultures
    ? Object.entries(config.cultures).map(([id, culture]) => ({
        id,
        ...culture,
        agents: culture.agents ?? [],
        mode: culture.mode ?? 'automatic',
        description: culture.description ?? '',
      }))
    : [];

  const roomIds = new Set<string>();
  agents.forEach(agent => {
    agent.rooms.forEach(room => roomIds.add(room));
  });

  const rooms: Room[] = Array.from(roomIds).map(roomId => {
    const agentsInRoom = agents.filter(agent => agent.rooms.includes(roomId)).map(a => a.id);
    const roomModel = config.room_models?.[roomId];
    return {
      id: roomId,
      display_name: roomId.charAt(0).toUpperCase() + roomId.slice(1),
      description: '',
      agents: agentsInRoom,
      model: roomModel,
    };
  });

  return { agents, teams, cultures, rooms };
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
  recoveryConfigSource: string | null;
  recoveryConfigSourceOriginal: string | null;
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
  dirtyRoots: string[];
  isLoading: boolean;
  diagnostics: ConfigDiagnostic[];
  syncStatus: 'synced' | 'syncing' | 'error' | 'disconnected';
  // UI-only backup so a draft private toggle can restore the prior explicit worker_scope
  // until the draft is either saved successfully or toggled back off.
  privateWorkerScopeBackups: Record<string, Agent['worker_scope'] | null>;

  // Actions
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<SaveConfigResult>;
  updateRecoveryConfigSource: (source: string) => void;
  saveRecoveryConfigSource: () => Promise<SaveConfigResult>;
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
  draftVersion: number,
  recoveryConfigSource: string | null = null
): Pick<
  ConfigState,
  | 'loadedConfig'
  | 'config'
  | 'recoveryConfigSource'
  | 'recoveryConfigSourceOriginal'
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
  | 'dirtyRoots'
  | 'isLoading'
  | 'diagnostics'
  | 'syncStatus'
  | 'privateWorkerScopeBackups'
> {
  return {
    loadedConfig: null,
    config: null,
    recoveryConfigSource,
    recoveryConfigSourceOriginal: recoveryConfigSource,
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
    dirtyRoots: [],
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
  recoveryConfigSource: null,
  recoveryConfigSourceOriginal: null,
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
  dirtyRoots: [],
  isLoading: false,
  diagnostics: [],
  syncStatus: 'disconnected',
  privateWorkerScopeBackups: {},

  // Load configuration from backend
  loadConfig: async () => {
    const currentState = get();
    const loadConfigRequestId = currentState.loadConfigRequestId + 1;
    const draftVersionAtStart = currentState.draftVersion;
    set({
      isLoading: true,
      diagnostics: currentState.isDirty ? retainedDraftDiagnostics(currentState.diagnostics) : [],
      loadConfigRequestId,
    });
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
      const { agents, teams, cultures, rooms } = deriveConfigCollections(normalizedConfig);
      let agentPoliciesByAgent: AgentPoliciesByAgent = {};
      let diagnostics: ConfigDiagnostic[] = [];
      let agentPoliciesStale = false;
      try {
        agentPoliciesByAgent = await configService.getAgentPolicies(normalizedConfig, agents);
      } catch {
        diagnostics = [agentPoliciesDiagnostic(false)];
        agentPoliciesStale = true;
      }
      const latestState = get();
      if (latestState.loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      const shouldReplaceDirtyRecoveryDraft =
        (latestState.config == null || latestState.recoveryConfigSource != null) &&
        (latestState.isDirty || latestState.draftVersion != draftVersionAtStart);
      if (
        (latestState.isDirty || latestState.draftVersion != draftVersionAtStart) &&
        !shouldReplaceDirtyRecoveryDraft
      ) {
        set({
          loadedConfig: normalizedConfig,
          isLoading: false,
          syncStatus: latestState.syncStatus,
          diagnostics: retainedDraftDiagnostics(latestState.diagnostics),
        });
        return;
      }
      const nextAgentPoliciesRequestId = latestState.agentPoliciesRequestId + 1;
      const nextDraft = nextDraftVersion(latestState.draftVersion);

      set({
        loadedConfig: normalizedConfig,
        config: normalizedConfig,
        recoveryConfigSource: null,
        recoveryConfigSourceOriginal: null,
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
        dirtyRoots: [],
        diagnostics,
        privateWorkerScopeBackups: {},
      });
    } catch (error) {
      if (get().loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      const nextAgentPoliciesRequestId = get().agentPoliciesRequestId + 1;
      if (error instanceof configService.ConfigValidationError) {
        let recoveryConfigSource: string | null = null;
        let recoveryConfigSourceError: unknown = null;
        try {
          recoveryConfigSource = await configService.loadRawConfigSource();
        } catch (recoveryError) {
          recoveryConfigSourceError = recoveryError;
        }
        const latestState = get();
        if (latestState.loadConfigRequestId != loadConfigRequestId) {
          return;
        }
        if (recoveryConfigSourceError != null) {
          const recoveryDiagnostics = globalDiagnostics(
            recoveryConfigSourceError instanceof Error
              ? recoveryConfigSourceError.message
              : 'Failed to load raw configuration',
            true
          );
          if (latestState.isDirty || latestState.draftVersion != draftVersionAtStart) {
            set({
              isLoading: false,
              syncStatus: 'error',
              diagnostics: recoveryDiagnostics,
            });
            return;
          }
          const nextDraft = nextDraftVersion(get().draftVersion);
          set(clearedLoadedConfigState(recoveryDiagnostics, nextAgentPoliciesRequestId, nextDraft));
          return;
        }
        if (latestState.isDirty || latestState.draftVersion != draftVersionAtStart) {
          set({
            isLoading: false,
            syncStatus: 'error',
            diagnostics: validationDiagnostics(error.issues, {
              blocking: latestState.recoveryConfigSource != null && latestState.config == null,
            }),
          });
          return;
        }
        const nextDraft = nextDraftVersion(get().draftVersion);
        set(
          clearedLoadedConfigState(
            validationDiagnostics(error.issues, { blocking: true }),
            nextAgentPoliciesRequestId,
            nextDraft,
            recoveryConfigSource
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
      loadedConfig,
      draftVersion,
      diagnostics,
      dirtyRoots,
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
      diagnostics: retainedDraftDiagnostics(diagnostics),
      syncStatus: 'syncing',
      saveConfigRequestId,
    });
    try {
      const currentRawEntriesByAgent = new Map(
        agents.map(agent => [
          agent.id,
          rebuildToolEntries(agent.tools, getRememberedRawToolEntries(config, agent.id)),
        ])
      );
      const currentRawDefaultToolEntries = getRememberedRawDefaultToolEntries(config);
      const dirtyRootSet = new Set(dirtyRoots);
      const baseConfig = loadedConfig ?? config;
      const baseCollections = deriveConfigCollections(baseConfig);
      const baseRawDefaultToolEntries = getRememberedRawDefaultToolEntries(baseConfig);

      const currentAgentsObject = agents.reduce(
        (acc, agent) => {
          const { id, ...rest } = agent;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Agent, 'id'>>
      );
      const payloadAgentsObjectFromAgents = agents.reduce(
        (acc, agent) => {
          const { id, ...rest } = agent;
          const rawToolEntries = currentRawEntriesByAgent.get(id);
          acc[id] = {
            ...rest,
            tools: rawToolEntries ?? rest.tools,
          };
          return acc;
        },
        {} as configService.ConfigSavePayload['agents']
      );

      const currentTeamsObject = teams.reduce(
        (acc, team) => {
          const { id, ...rest } = team;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Team, 'id'>>
      );
      const currentCulturesObject = cultures.reduce(
        (acc, culture) => {
          const { id, ...rest } = culture;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Culture, 'id'>>
      );

      const roomModels: Record<string, string> = {};
      rooms.forEach(room => {
        if (room.model) {
          roomModels[room.id] = room.model;
        }
      });

      const updatedConfig: Config = {
        ...baseConfig,
        ...(dirtyRootSet.has('defaults') ? { defaults: config.defaults } : {}),
        ...(dirtyRootSet.has('memory') ? { memory: config.memory } : {}),
        ...(dirtyRootSet.has('knowledge_bases') ? { knowledge_bases: config.knowledge_bases } : {}),
        ...(dirtyRootSet.has('models') ? { models: config.models } : {}),
        ...(dirtyRootSet.has('tools') ? { tools: config.tools } : {}),
        ...(dirtyRootSet.has('voice') ? { voice: config.voice } : {}),
        ...(dirtyRootSet.has('agents') ? { agents: currentAgentsObject } : {}),
        ...(dirtyRootSet.has('teams') ? { teams: currentTeamsObject } : {}),
        ...(dirtyRootSet.has('cultures') ? { cultures: currentCulturesObject } : {}),
        ...(dirtyRootSet.has('room_models')
          ? { room_models: Object.keys(roomModels).length > 0 ? roomModels : undefined }
          : {}),
      };
      const payloadAgentsObject = dirtyRootSet.has('agents')
        ? payloadAgentsObjectFromAgents
        : Object.fromEntries(
            Object.entries(baseConfig.agents).map(([agentId, agentConfig]) => [
              agentId,
              {
                ...agentConfig,
                tools: getRememberedRawToolEntries(baseConfig, agentId) ?? agentConfig.tools,
              },
            ])
          );
      const payloadDefaultTools = dirtyRootSet.has('defaults')
        ? currentRawDefaultToolEntries ?? updatedConfig.defaults.tools
        : baseRawDefaultToolEntries ?? updatedConfig.defaults.tools;
      const payload: configService.ConfigSavePayload = {
        ...updatedConfig,
        agents: payloadAgentsObject,
        defaults: {
          ...updatedConfig.defaults,
          tools: payloadDefaultTools,
        },
      };

      await configService.saveConfig(payload);
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      const updatedRawEntriesByAgent = dirtyRootSet.has('agents')
        ? currentRawEntriesByAgent
        : new Map(
            Object.keys(baseConfig.agents).map(agentId => [
              agentId,
              rebuildToolEntries(
                baseCollections.agents.find(agent => agent.id === agentId)?.tools ??
                  updatedConfig.agents[agentId]?.tools ??
                  [],
                getRememberedRawToolEntries(baseConfig, agentId)
              ),
            ])
          );
      const updatedRawDefaultToolEntries = dirtyRootSet.has('defaults')
        ? currentRawDefaultToolEntries
        : baseRawDefaultToolEntries;
      rememberRawToolEntries(updatedConfig, updatedRawEntriesByAgent, updatedRawDefaultToolEntries);
      if (draftChangedSinceSaveStarted) {
        set({
          loadedConfig: updatedConfig,
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        if (currentState.agentPoliciesStale) {
          void get().refreshAgentPolicies(currentState.agents);
        }
        return { status: 'stale' };
      }
      const syncedCollections = deriveConfigCollections(updatedConfig);
      set({
        loadedConfig: updatedConfig,
        config: updatedConfig,
        agents: syncedCollections.agents,
        teams: syncedCollections.teams,
        cultures: syncedCollections.cultures,
        rooms: syncedCollections.rooms,
        isLoading: false,
        syncStatus: 'synced',
        isDirty: false,
        dirtyRoots: [],
        diagnostics: [],
        privateWorkerScopeBackups: {},
      });
      if (agentPoliciesStale) {
        void get().refreshAgentPolicies(syncedCollections.agents);
      }
      return { status: 'saved' };
    } catch (error) {
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: 'stale' };
      }
      if (error instanceof configService.ConfigValidationError) {
        const errorDiagnostics = validationDiagnostics(error.issues, { blocking: false });
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

  updateRecoveryConfigSource: source => {
    set(state => {
      if (source === state.recoveryConfigSource) {
        return state;
      }
      return {
        recoveryConfigSource: source,
        isDirty: source !== state.recoveryConfigSourceOriginal,
        diagnostics: retainedDraftDiagnostics(state.diagnostics),
        draftVersion: nextDraftVersion(state.draftVersion),
      };
    });
  },

  saveRecoveryConfigSource: async () => {
    const { recoveryConfigSource, diagnostics, draftVersion } = get();
    if (recoveryConfigSource == null) {
      return {
        status: 'error',
        message: 'No recovery configuration is available to save.',
        diagnostics,
      };
    }

    const saveConfigRequestId = get().saveConfigRequestId + 1;
    const savedDraftVersion = draftVersion;
    set({
      isLoading: true,
      diagnostics: retainedDraftDiagnostics(diagnostics),
      syncStatus: 'syncing',
      saveConfigRequestId,
    });

    try {
      await configService.saveRawConfigSource(recoveryConfigSource);
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: 'stale' };
      }

      await get().loadConfig();
      const latestState = get();
      if (latestState.config != null && latestState.recoveryConfigSource == null) {
        return { status: 'saved' };
      }
      return {
        status: 'error',
        message: firstGlobalDiagnosticMessage(
          latestState.diagnostics,
          'Saved replacement configuration, but failed to reload the structured config.'
        ),
        diagnostics: latestState.diagnostics,
      };
    } catch (error) {
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: 'stale' };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted = currentState.draftVersion !== savedDraftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: 'stale' };
      }
      if (error instanceof configService.ConfigValidationError) {
        const errorDiagnostics = validationDiagnostics(error.issues, { blocking: true });
        set({
          diagnostics: errorDiagnostics,
          isLoading: false,
          syncStatus: 'error',
        });
        return {
          status: 'error',
          message: CONFIG_VALIDATION_FAILED_MESSAGE,
          diagnostics: errorDiagnostics,
        };
      }
      const errorMessage =
        error instanceof Error ? error.message : 'Failed to save raw configuration';
      const errorDiagnostics = [
        {
          kind: 'global' as const,
          message: errorMessage,
          blocking: true,
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
      const touchedPaths = Object.keys(normalizedUpdates).map(
        key => ['agents', agentId, key] as ConfigDiagnosticPath
      );
      nextAgents = state.agents.map(agent =>
        agent.id === agentId ? { ...agent, ...normalizedUpdates } : agent
      );
      const nextAgent = nextAgents.find(agent => agent.id === agentId) ?? currentAgent;
      shouldRefreshAgentPolicies = agentPolicyChanged(currentAgent, nextAgent);

      return {
        agents: nextAgents,
        ...markDraftDirty(state, {}, touchedPaths),
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
      const touchedPaths = Object.keys(normalizedUpdates).map(
        key => ['agents', agentId, key] as ConfigDiagnosticPath
      );
      nextAgents = state.agents.map(agent =>
        agent.id === agentId ? { ...agent, ...normalizedUpdates } : agent
      );
      const nextAgent = nextAgents.find(agent => agent.id === agentId) ?? currentAgent;
      shouldRefreshAgentPolicies = agentPolicyChanged(currentAgent, nextAgent);

      return {
        agents: nextAgents,
        ...markDraftDirty(state, {}, touchedPaths),
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
      ...markDraftDirty(state, {}, [['agents']]),
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
      ...markDraftDirty(state, {}, [['agents']]),
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
      const touchedPaths = Object.keys(normalizedUpdates).map(
        key => ['teams', teamId, key] as ConfigDiagnosticPath
      );
      return {
        teams: state.teams.map(team =>
          team.id === teamId ? { ...team, ...normalizedUpdates } : team
        ),
        ...markDraftDirty(state, {}, touchedPaths),
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
      ...markDraftDirty(state, {}, [['teams']]),
    }));
  },

  // Delete a team
  deleteTeam: teamId => {
    set(state => ({
      teams: state.teams.filter(team => team.id !== teamId),
      selectedTeamId: state.selectedTeamId === teamId ? null : state.selectedTeamId,
      ...markDraftDirty(state, {}, [['teams']]),
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
            ...markDraftDirty(state, {}, [['cultures', cultureId]]),
          };
        }
        return {
          cultures: unassignAgentsFromOtherCultures(
            updatedCultures,
            cultureId,
            targetCulture.agents
          ),
          ...markDraftDirty(state, {}, [['cultures', cultureId, 'agents']]),
        };
      }

      return {
        cultures: updatedCultures,
        ...markDraftDirty(
          state,
          {},
          Object.keys(updates).map(key => ['cultures', cultureId, key] as ConfigDiagnosticPath)
        ),
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
        ...markDraftDirty(state, {}, [['cultures']]),
      };
    });
  },

  // Delete a culture
  deleteCulture: cultureId => {
    set(state => ({
      cultures: state.cultures.filter(culture => culture.id !== cultureId),
      selectedCultureId: state.selectedCultureId === cultureId ? null : state.selectedCultureId,
      ...markDraftDirty(state, {}, [['cultures']]),
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
          ...markDraftDirty(state, {}, [['rooms', roomId, 'agents'], ['agents']]),
        };
      }

      return {
        config: updatedConfig,
        rooms: updatedRooms,
        ...markDraftDirty(state, {}, [
          ...Object.keys(updates).map(key => ['rooms', roomId, key] as ConfigDiagnosticPath),
          ...(updates.model !== undefined
            ? ([['room_models', roomId]] as ConfigDiagnosticPath[])
            : []),
        ]),
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
        ...markDraftDirty(state, {}, [['rooms'], ['agents']]),
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
        ...markDraftDirty(state, {}, [['rooms'], ['agents'], ['teams'], ['room_models', roomId]]),
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
        ...markDraftDirty(state, {}, [['rooms', roomId, 'agents'], ['agents']]),
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
        ...markDraftDirty(state, {}, [['rooms', roomId, 'agents'], ['agents']]),
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
        ...markDraftDirty(state, {}, [['room_models']]),
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
          ...markDraftDirty(state, {}, [['memory']]),
        };
      }

      const nextConfig = {
        ...state.config,
        memory: memoryConfig,
      };
      preserveRawToolEntries(state.config, nextConfig);
      return {
        config: nextConfig,
        ...markDraftDirty(state, {}, [['memory']]),
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
        ...markDraftDirty(state, {}, [['knowledge_bases', baseName]]),
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
        ...markDraftDirty(state, {}, [['knowledge_bases', baseName], ['agents']]),
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
        ...markDraftDirty(state, {}, [['models', modelId]]),
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
        ...markDraftDirty(state, {}, [['models']]),
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
        ...markDraftDirty(state, {}, [['tools', toolId]]),
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
        ...markDraftDirty(state, {}, [['voice']]),
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
      ...markDraftDirty(state, {}, [['agents', agentId, 'tools']]),
    }));
  },

  // Mark configuration as dirty
  markDirty: () => {
    set(state => ({
      ...markDraftDirty(state, {}),
    }));
  },
}));
