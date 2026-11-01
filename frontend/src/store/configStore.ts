import { create } from "zustand";
import {
  Config,
  Agent,
  AgentConfig,
  AgentPoliciesByAgent,
  Team,
  TeamConfig,
  Room,
  RoomConfig,
  KnowledgeBaseConfig,
  getDefaultPrivateConfig,
  normalizeAgentUpdates,
  normalizeTeamUpdates,
} from "@/types/config";
import {
  getPathValue,
  isPlainObject,
  setPathValue,
  type ConfigPath,
} from "@/lib/configSchema";
import * as configService from "@/services/configService";
import {
  isConfigConflictDiagnostic,
  type ConfigDiagnostic,
  type ConfigValidationIssue,
} from "@/lib/configValidation";
import {
  getToolOverrides,
  setToolOverridesInEntries,
  toolEntryName,
  type ToolEntry,
  type ToolOverrides,
} from "@/lib/toolEntry";

const AGENT_POLICIES_ERROR_MESSAGE = "Failed to derive agent policies";
const CONFIG_VALIDATION_FAILED_MESSAGE = "Configuration validation failed";
const MISSING_GENERATION_MESSAGE =
  "Missing committed configuration generation.";
const CONFIG_CONFLICT_MESSAGE =
  "Configuration changed elsewhere. Your draft has not been saved. Copy any changes you want to keep, then refresh this page and reapply them.";

export type SaveConfigResult =
  | { status: "saved" }
  | { status: "stale" }
  | { status: "error"; message: string; diagnostics: ConfigDiagnostic[] };

type ConfigDiagnosticPath = Array<string | number>;

// The save payload rebuilds these roots from the draft agent and team
// collections; every other dirty root is copied from the draft config, with
// room names trimmed.
const COLLECTION_ROOTS = new Set(["agents", "teams"]);

// Schema-driven editors address roots by name, including roots the typed
// Config interface does not model.
export function readConfigRoot(config: Config, root: string): unknown {
  return (config as unknown as Record<string, unknown>)[root];
}

function validationDiagnostics(
  issues: ConfigValidationIssue[],
  options: { blocking: boolean },
): ConfigDiagnostic[] {
  return [
    ...globalDiagnostics(CONFIG_VALIDATION_FAILED_MESSAGE, options.blocking),
    ...issues.map((issue) => ({
      kind: "validation" as const,
      issue,
    })),
  ];
}

function draftSyncStatus(state: {
  loadedConfig: Config | null;
  isDirty: boolean;
}): ConfigState["syncStatus"] {
  if (state.isDirty) {
    return "error";
  }
  return state.loadedConfig == null ? "disconnected" : "synced";
}

function diagnosticsContainValidationErrors(
  diagnostics: ConfigDiagnostic[],
): boolean {
  return diagnostics.some((diagnostic) => diagnostic.kind === "validation");
}

function pathStartsWith(
  path: ConfigDiagnosticPath,
  prefix: ConfigDiagnosticPath,
): boolean {
  return prefix.every((segment, index) => path[index] === segment);
}

function diagnosticsOverlapTouchedPath(
  issuePath: ConfigDiagnosticPath,
  touchedPath: ConfigDiagnosticPath,
): boolean {
  return (
    pathStartsWith(issuePath, touchedPath) ||
    pathStartsWith(touchedPath, issuePath)
  );
}

function retainedDraftDiagnostics(
  diagnostics: ConfigDiagnostic[],
  touchedPaths: ConfigDiagnosticPath[] = [],
): ConfigDiagnostic[] {
  const filteredDiagnostics = diagnostics.filter((diagnostic) => {
    if (diagnostic.kind !== "validation") {
      return true;
    }
    return !touchedPaths.some((path) =>
      diagnosticsOverlapTouchedPath(diagnostic.issue.loc, path),
    );
  });

  const hasValidationErrors =
    diagnosticsContainValidationErrors(filteredDiagnostics);
  return filteredDiagnostics.filter(
    (diagnostic) =>
      diagnostic.kind === "validation" ||
      isConfigConflictDiagnostic(diagnostic) ||
      (hasValidationErrors &&
        diagnostic.kind === "global" &&
        diagnostic.message === CONFIG_VALIDATION_FAILED_MESSAGE),
  );
}

function mergeDirtyRoots(
  existingDirtyRoots: string[],
  touchedPaths: ConfigDiagnosticPath[],
): string[] {
  if (touchedPaths.length === 0) {
    return existingDirtyRoots;
  }
  const dirtyRoots = new Set(existingDirtyRoots);
  for (const [root] of touchedPaths) {
    if (typeof root === "string") {
      dirtyRoots.add(root);
    }
  }
  return Array.from(dirtyRoots);
}

function globalDiagnostics(
  message: string,
  blocking: boolean,
): ConfigDiagnostic[] {
  return [
    {
      kind: "global",
      message,
      blocking,
    },
  ];
}

function firstGlobalDiagnosticMessage(
  diagnostics: ConfigDiagnostic[],
  fallbackMessage: string,
): string {
  return (
    diagnostics.find((diagnostic) => diagnostic.kind === "global")?.message ??
    fallbackMessage
  );
}

function configConflictDiagnostics(
  diagnostics: ConfigDiagnostic[],
  blocking: boolean,
): ConfigDiagnostic[] {
  return [
    {
      kind: "global",
      code: "config_conflict",
      message: CONFIG_CONFLICT_MESSAGE,
      blocking,
    },
    ...retainedDraftDiagnostics(diagnostics).filter(
      (diagnostic) => !isConfigConflictDiagnostic(diagnostic),
    ),
  ];
}

// Sets one value in the draft config. Blocks emptied by removing their last key
// fall back to their defaults instead of persisting as empty mappings. Nested
// entries the loaded config authors, such as a room declared without settings,
// stay.
function configWithValue(
  state: Pick<ConfigState, "config" | "loadedConfig">,
  path: ConfigPath,
  value: unknown,
): Config {
  const [root] = path;
  let nextConfig = setPathValue(state.config, path, value);
  for (let depth = path.length - 1; value === undefined && depth > 0; depth--) {
    const block: ConfigPath = [root, ...path.slice(1, depth)];
    const emptied = getPathValue(nextConfig, block);
    if (
      !isPlainObject(emptied) ||
      Object.keys(emptied).length > 0 ||
      (depth > 1 && getPathValue(state.loadedConfig, block) !== undefined)
    ) {
      break;
    }
    nextConfig = setPathValue(nextConfig, block, undefined);
  }
  return nextConfig;
}

function nextDraftVersion(draftVersion: number): number {
  return draftVersion + 1;
}

function markDraftDirty(
  state: Pick<ConfigState, "draftVersion" | "diagnostics" | "dirtyRoots">,
  touchedPaths: ConfigDiagnosticPath[] = [],
): Pick<
  ConfigState,
  "isDirty" | "diagnostics" | "draftVersion" | "dirtyRoots"
> {
  return {
    isDirty: true,
    diagnostics: retainedDraftDiagnostics(state.diagnostics, touchedPaths),
    draftVersion: nextDraftVersion(state.draftVersion),
    dirtyRoots: mergeDirtyRoots(state.dirtyRoots, touchedPaths),
  };
}

function deriveRooms(
  config: Pick<Config, "rooms" | "room_models">,
  agents: Agent[],
  teams: Team[],
): Room[] {
  const roomIds = new Set<string>([
    ...Object.keys(config.rooms ?? {}),
    ...Object.keys(config.room_models ?? {}),
  ]);
  agents.forEach((agent) => {
    agent.rooms.forEach((room) => roomIds.add(room));
  });
  teams.forEach((team) => {
    team.rooms.forEach((room) => roomIds.add(room));
  });

  return Array.from(roomIds).map((roomId) => {
    const agentsInRoom = agents
      .filter((agent) => agent.rooms.includes(roomId))
      .map((a) => a.id);
    const roomConfig = config.rooms?.[roomId];
    const roomModel = config.room_models?.[roomId];
    // A draft name keeps the whitespace typed into it; saving trims it.
    const displayName = roomConfig?.display_name;
    return {
      id: roomId,
      display_name: displayName?.trim()
        ? displayName
        : defaultRoomDisplayName(roomId),
      description: roomConfig?.description ?? "",
      agents: agentsInRoom,
      model: roomModel,
    };
  });
}

// Authored agents and teams may omit these fields; for example, an agent
// reached only through personal rooms or invitations lists no rooms. Loading
// fills them in, and saving drops each filled-in field that no edit has
// touched, so config.yaml keeps its authored shape.
function agentFieldDefaults() {
  return {
    tools: [] as ToolEntry[],
    skills: [] as string[],
    instructions: [] as string[],
    rooms: [] as string[],
    knowledge_bases: [] as string[],
    delegate_to: [] as string[],
    context_files: [] as string[],
  };
}

function teamFieldDefaults() {
  return { rooms: [] as string[], mode: "coordinate" as Team["mode"] };
}

// For each agent or team object, the fields loading filled in that no edit
// has touched since.
const untouchedFilledFields = new WeakMap<object, ReadonlySet<string>>();

function withFieldDefaults<T extends object, D extends object>(
  entity: T,
  defaults: D,
): T & D {
  const filled = { ...entity } as Record<string, unknown>;
  const filledFields = new Set<string>();
  for (const [field, value] of Object.entries(defaults)) {
    if (filled[field] == null) {
      filled[field] = value;
      filledFields.add(field);
    }
  }
  untouchedFilledFields.set(filled, filledFields);
  return filled as T & D;
}

// An edit makes a field authored, whatever its new value.
function withEdits<T extends object>(entity: T, edits: Partial<T>): T {
  const edited = { ...entity, ...edits };
  const filledFields = untouchedFilledFields.get(entity);
  if (filledFields) {
    untouchedFilledFields.set(
      edited,
      new Set([...filledFields].filter((field) => !hasUpdateKey(edits, field))),
    );
  }
  return edited;
}

function authoredFields<C>(entity: Agent | Team): C {
  const fields = { ...entity } as Record<string, unknown>;
  delete fields.id;
  for (const field of untouchedFilledFields.get(entity) ?? []) {
    delete fields[field];
  }
  return fields as C;
}

function deriveConfigCollections(
  config: Config,
): Pick<ConfigState, "agents" | "teams" | "rooms"> {
  const agents = Object.entries(config.agents).map(([id, agent]) =>
    withFieldDefaults({ id, ...agent }, agentFieldDefaults()),
  );
  const teams = Object.entries(config.teams ?? {}).map(([id, team]) =>
    withFieldDefaults({ id, ...team }, teamFieldDefaults()),
  );
  const rooms = deriveRooms(config, agents, teams);

  return { agents, teams, rooms };
}

function removeMissingTeamMembers(teams: Team[], agents: Agent[]): Team[] {
  const knownAgents = new Set(agents.map((agent) => agent.id));
  return teams.map((team) =>
    withEdits(team, {
      agents: team.agents.filter((agentId) => knownAgents.has(agentId)),
    }),
  );
}

function defaultRoomDisplayName(roomId: string): string {
  return roomId
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function normalizedRoomDisplayName(
  displayName: string | undefined,
): string | undefined {
  const trimmed = displayName?.trim();
  return trimmed ? trimmed : undefined;
}

function normalizedRoomConfig(roomConfig: RoomConfig): RoomConfig {
  const { display_name: _displayName, ...remainingConfig } = roomConfig;
  const displayName = normalizedRoomDisplayName(roomConfig.display_name);
  return displayName
    ? { ...remainingConfig, display_name: displayName }
    : remainingConfig;
}

function normalizedRoomConfigs(rooms: Config["rooms"]): Config["rooms"] {
  if (!rooms) {
    return rooms;
  }
  return Object.fromEntries(
    Object.entries(rooms).map(([roomId, roomConfig]) => [
      roomId,
      normalizedRoomConfig(roomConfig),
    ]),
  );
}

function hasOwnRoomConfig(rooms: Config["rooms"], roomId: string): boolean {
  return hasUpdateKey(rooms ?? {}, roomId);
}

function roomConfigHasMetadata(roomConfig: RoomConfig): boolean {
  return (
    normalizedRoomDisplayName(roomConfig.display_name) !== undefined ||
    (roomConfig.description ?? "") !== ""
  );
}

function sameStringSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  const rightValues = new Set(right);
  return (
    rightValues.size === right.length &&
    left.every((value) => rightValues.has(value))
  );
}

function roomUpdateIsNoop(room: Room, updates: Partial<Room>): boolean {
  return Object.entries(updates).every(([key, value]) => {
    const current = room[key as keyof Room];
    if (Array.isArray(current) && Array.isArray(value)) {
      return sameStringSet(current, value);
    }
    return current === value;
  });
}

function hasUpdateKey(updates: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(updates, key);
}

function roomsFromDraft(
  config: Config | null,
  fallbackRooms: Room[],
  agents: Agent[],
  teams: Team[],
): Room[] {
  return config ? deriveRooms(config, agents, teams) : fallbackRooms;
}

function updateDraftRoomMetadata(
  config: Config,
  roomId: string,
  updates: Pick<Partial<Room>, "display_name" | "description">,
): Config {
  const nextRooms = { ...(config.rooms ?? {}) };
  const hadAuthoredRoom = hasOwnRoomConfig(config.rooms, roomId);
  const nextRoomConfig = { ...(nextRooms[roomId] ?? {}) };
  if (hasUpdateKey(updates, "display_name")) {
    if (normalizedRoomDisplayName(updates.display_name)) {
      nextRoomConfig.display_name = updates.display_name;
    } else {
      delete nextRoomConfig.display_name;
    }
  }
  if (hasUpdateKey(updates, "description")) {
    nextRoomConfig.description = updates.description ?? "";
  }
  if (!hadAuthoredRoom && !roomConfigHasMetadata(nextRoomConfig)) {
    return config;
  }
  nextRooms[roomId] = nextRoomConfig;
  return {
    ...config,
    rooms: nextRooms,
  };
}

function deleteDraftRoomMetadata(config: Config, roomId: string): Config {
  const { [roomId]: _removedRoom, ...nextRooms } = config.rooms ?? {};
  return {
    ...config,
    rooms: nextRooms,
  };
}

// Removes the config entries keyed by a deleted agent or team, so its deletion
// saves without a validation error naming them: its `calls.agents` entry, which
// only an agent has, and its own `room_thread_summary_models` key unless a room
// has the same name, because that key still names the room.
function configWithoutEntityKeys(
  state: Pick<ConfigState, "config" | "loadedConfig">,
  entityId: string,
  rooms: Room[],
): { config: Config | null; touchedPaths: ConfigDiagnosticPath[] } {
  const paths: ConfigPath[] = [["calls", "agents", entityId]];
  if (!rooms.some((room) => room.id === entityId)) {
    paths.push(["room_thread_summary_models", entityId]);
  }
  let config = state.config;
  const touchedPaths: ConfigDiagnosticPath[] = [];
  for (const path of paths) {
    if (!config || getPathValue(config, path) === undefined) {
      continue;
    }
    config = configWithValue(
      { config, loadedConfig: state.loadedConfig },
      path,
      undefined,
    );
    touchedPaths.push([...path]);
  }
  return { config, touchedPaths };
}

// Sets or clears one room's model override; the block goes once it is empty.
function configWithRoomModel(
  config: Config,
  roomId: string,
  model: string | undefined,
): Config {
  const roomModels = { ...(config.room_models ?? {}) };
  if (model) {
    roomModels[roomId] = model;
  } else {
    delete roomModels[roomId];
  }
  return {
    ...config,
    room_models: Object.keys(roomModels).length > 0 ? roomModels : undefined,
  };
}

function normalizeAgentDelegates(delegateTo: string[] | undefined): string {
  return [...new Set(delegateTo ?? [])].sort().join("\0");
}

function normalizeAgentPolicyKey(
  agent: Pick<Agent, "private" | "delegate_to" | "worker_scope">,
): string {
  return [
    agent.worker_scope ?? "",
    agent.private != null ? "private" : "",
    agent.private?.per ?? "",
    agent.private?.knowledge?.enabled === false ? "disabled" : "enabled",
    agent.private?.knowledge?.path ?? "",
    normalizeAgentDelegates(agent.delegate_to),
  ].join("\0");
}

function agentPolicyChanged(
  currentAgent: Pick<Agent, "private" | "delegate_to" | "worker_scope">,
  nextAgent: Pick<Agent, "private" | "delegate_to" | "worker_scope">,
): boolean {
  return (
    normalizeAgentPolicyKey(currentAgent) !== normalizeAgentPolicyKey(nextAgent)
  );
}

// Applies normalized updates to one draft agent, and reports whether they
// change what its derived policy depends on.
function editedAgentDraft(
  state: ConfigState,
  currentAgent: Agent,
  updates: Partial<Agent>,
) {
  const agentId = currentAgent.id;
  const normalizedUpdates = normalizeAgentUpdates(currentAgent, updates);
  const touchedPaths = Object.keys(normalizedUpdates).map(
    (key) => ["agents", agentId, key] as ConfigDiagnosticPath,
  );
  const agents = state.agents.map((agent) =>
    agent.id === agentId ? withEdits(agent, normalizedUpdates) : agent,
  );
  const nextAgent =
    agents.find((agent) => agent.id === agentId) ?? currentAgent;
  return {
    policyChanged: agentPolicyChanged(currentAgent, nextAgent),
    patch: {
      agents,
      rooms: roomsFromDraft(state.config, state.rooms, agents, state.teams),
      ...markDraftDirty(state, touchedPaths),
    },
  };
}

/**
 * Whether the draft had unsaved changes, and in which roots, at some moment,
 * and which draft version that was.
 */
export type ConfigDraftMark = Pick<
  ConfigState,
  "isDirty" | "dirtyRoots" | "draftVersion"
>;

interface ConfigState {
  // State
  committedGeneration: number | null;
  loadedConfig: Config | null;
  config: Config | null;
  recoveryConfigSource: string | null;
  recoveryConfigSourceOriginal: string | null;
  draftVersion: number;
  agents: Agent[];
  teams: Team[];
  rooms: Room[];
  agentPoliciesByAgent: AgentPoliciesByAgent;
  agentPoliciesStale: boolean;
  agentPoliciesRequestId: number;
  loadConfigRequestId: number;
  saveConfigRequestId: number;
  selectedAgentId: string | null;
  selectedTeamId: string | null;
  selectedRoomId: string | null;
  isDirty: boolean;
  dirtyRoots: string[];
  isLoading: boolean;
  diagnostics: ConfigDiagnostic[];
  syncStatus: "synced" | "syncing" | "error" | "disconnected";
  // Whether the committed config is composed from multiple files via !include,
  // in which case structured saves are rejected by the backend.
  configUsesIncludes: boolean;
  // Whether the saved event_journal names a database other than the one this
  // process has open. Saving it is accepted and changes nothing until a
  // restart, so the editor has to say so.
  configJournalPendingRestart: boolean;
  // UI-only backup so a draft private toggle can restore the prior explicit worker_scope
  // until the draft is either saved successfully or toggled back off.
  privateWorkerScopeBackups: Record<string, Agent["worker_scope"] | null>;

  // Actions
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<SaveConfigResult>;
  updateRecoveryConfigSource: (source: string) => void;
  saveRecoveryConfigSource: () => Promise<SaveConfigResult>;
  refreshAgentPolicies: (agents: Agent[]) => Promise<void>;
  selectAgent: (agentId: string | null) => void;
  updateAgent: (agentId: string, updates: Partial<Agent>) => void;
  setAgentPrivateEnabled: (agentId: string, enabled: boolean) => void;
  createAgent: (agent: Omit<Agent, "id">) => void;
  deleteAgent: (agentId: string) => void;
  selectTeam: (teamId: string | null) => void;
  updateTeam: (teamId: string, updates: Partial<Team>) => void;
  createTeam: (team: Omit<Team, "id">) => void;
  deleteTeam: (teamId: string) => void;
  selectRoom: (roomId: string | null) => void;
  updateRoom: (roomId: string, updates: Partial<Room>) => void;
  createRoom: (room: Omit<Room, "id">) => void;
  deleteRoom: (roomId: string) => void;
  updateMemoryConfig: (memoryConfig: Config["memory"]) => void;
  updateKnowledgeBase: (
    baseName: string,
    baseConfig: KnowledgeBaseConfig,
  ) => void;
  deleteKnowledgeBase: (baseName: string) => void;
  deleteModel: (modelId: string) => void;
  /** Set one config value by key path; undefined removes it. Not for agents or teams. */
  updateConfigValue: (path: ConfigPath, value: unknown) => void;
  /**
   * Put a value written with `updateConfigValue` back, together with the dirty
   * state read before that write, so a write that failed to save leaves the
   * draft as it was and not dirty only because of it. `before` is read from
   * the store just before the write. When the draft changed again since that
   * write, its dirty state holds those other edits, so only the value goes back.
   */
  restoreConfigValue: (
    path: ConfigPath,
    value: unknown,
    before: ConfigDraftMark,
  ) => void;
  getAgentToolOverrides: (
    agentId: string,
    toolName: string,
  ) => ToolOverrides | null;
  /** Merge a patch into a listed tool's overrides; unlisted tools are left alone. */
  updateAgentToolOverrides: (
    agentId: string,
    toolName: string,
    overrides: ToolOverrides,
  ) => void;
  getDefaultToolOverrides: (toolName: string) => ToolOverrides | null;
  updateDefaultToolOverrides: (
    toolName: string,
    overrides: ToolOverrides,
  ) => void;
}

function clearedLoadedConfigState(
  diagnostics: ConfigDiagnostic[],
  agentPoliciesRequestId: number,
  draftVersion: number,
  recoveryConfigSource: string | null = null,
  committedGeneration: number | null = null,
) {
  return {
    committedGeneration,
    loadedConfig: null,
    config: null,
    recoveryConfigSource,
    recoveryConfigSourceOriginal: recoveryConfigSource,
    draftVersion,
    agents: [],
    teams: [],
    rooms: [],
    agentPoliciesByAgent: {},
    agentPoliciesStale: false,
    agentPoliciesRequestId,
    selectedAgentId: null,
    selectedTeamId: null,
    selectedRoomId: null,
    isDirty: false,
    dirtyRoots: [],
    isLoading: false,
    diagnostics,
    syncStatus: "error",
    privateWorkerScopeBackups: {},
  } satisfies Partial<ConfigState>;
}

// Records a failed save in the store and reports it to the caller.
function failSave(
  set: (partial: Partial<ConfigState>) => void,
  diagnostics: ConfigDiagnostic[],
  message: string,
  extra: Partial<ConfigState> = {},
): SaveConfigResult {
  set({ diagnostics, isLoading: false, syncStatus: "error", ...extra });
  return { status: "error", message, diagnostics };
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  // Initial state
  committedGeneration: null,
  loadedConfig: null,
  config: null,
  recoveryConfigSource: null,
  recoveryConfigSourceOriginal: null,
  draftVersion: 0,
  agents: [],
  teams: [],
  rooms: [],
  agentPoliciesByAgent: {},
  agentPoliciesStale: false,
  agentPoliciesRequestId: 0,
  loadConfigRequestId: 0,
  saveConfigRequestId: 0,
  selectedAgentId: null,
  selectedTeamId: null,
  selectedRoomId: null,
  isDirty: false,
  dirtyRoots: [],
  isLoading: false,
  diagnostics: [],
  syncStatus: "disconnected",
  configUsesIncludes: false,
  configJournalPendingRestart: false,
  privateWorkerScopeBackups: {},

  // Load configuration from backend
  loadConfig: async () => {
    const currentState = get();
    const loadConfigRequestId = currentState.loadConfigRequestId + 1;
    const draftVersionAtStart = currentState.draftVersion;
    const draftEdited = (state: ConfigState) =>
      state.isDirty || state.draftVersion != draftVersionAtStart;
    set({
      isLoading: true,
      diagnostics: currentState.isDirty
        ? retainedDraftDiagnostics(currentState.diagnostics)
        : currentState.diagnostics.filter(isConfigConflictDiagnostic),
      loadConfigRequestId,
    });
    try {
      const {
        config: loadedConfig,
        generation,
        usesIncludes,
        pendingRestart,
      } = await configService.loadConfig();
      const normalizedConfig: Config = {
        ...loadedConfig,
        rooms: normalizedRoomConfigs(loadedConfig.rooms),
        knowledge_bases: loadedConfig.knowledge_bases || {},
      };
      const { agents, teams, rooms } =
        deriveConfigCollections(normalizedConfig);
      let agentPoliciesByAgent: AgentPoliciesByAgent = {};
      let diagnostics: ConfigDiagnostic[] = [];
      let agentPoliciesStale = false;
      try {
        agentPoliciesByAgent = await configService.getAgentPolicies(
          normalizedConfig,
          agents,
        );
      } catch {
        diagnostics = globalDiagnostics(AGENT_POLICIES_ERROR_MESSAGE, false);
        agentPoliciesStale = true;
      }
      const latestState = get();
      if (latestState.loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      // An edited structured draft stays; an edited recovery draft is replaced.
      if (
        draftEdited(latestState) &&
        latestState.config != null &&
        latestState.recoveryConfigSource == null
      ) {
        set({
          committedGeneration: generation,
          loadedConfig: normalizedConfig,
          isLoading: false,
          diagnostics: retainedDraftDiagnostics(latestState.diagnostics),
          configUsesIncludes: usesIncludes,
          configJournalPendingRestart: pendingRestart,
        });
        return;
      }
      const nextAgentPoliciesRequestId = latestState.agentPoliciesRequestId + 1;
      const nextDraft = nextDraftVersion(latestState.draftVersion);

      set({
        committedGeneration: generation,
        loadedConfig: normalizedConfig,
        config: normalizedConfig,
        recoveryConfigSource: null,
        recoveryConfigSourceOriginal: null,
        draftVersion: nextDraft,
        agents,
        teams,
        rooms,
        agentPoliciesByAgent,
        agentPoliciesStale,
        agentPoliciesRequestId: nextAgentPoliciesRequestId,
        isLoading: false,
        syncStatus: "synced",
        isDirty: false,
        dirtyRoots: [],
        diagnostics,
        configUsesIncludes: usesIncludes,
        configJournalPendingRestart: pendingRestart,
        privateWorkerScopeBackups: {},
      });
    } catch (error) {
      if (get().loadConfigRequestId != loadConfigRequestId) {
        return;
      }
      const nextAgentPoliciesRequestId = get().agentPoliciesRequestId + 1;
      if (error instanceof configService.ConfigValidationError) {
        let recoveryConfigSource: string | null = null;
        let recoveryGeneration: number | null = null;
        let recoveryConfigSourceError: unknown = null;
        try {
          const recovery = await configService.loadRawConfigSource();
          recoveryConfigSource = recovery.source;
          recoveryGeneration = recovery.generation;
        } catch (recoveryError) {
          recoveryConfigSourceError = recoveryError;
        }
        const latestState = get();
        if (latestState.loadConfigRequestId != loadConfigRequestId) {
          return;
        }
        if (recoveryConfigSourceError != null) {
          const recoveryDiagnostics = [
            ...latestState.diagnostics.filter(isConfigConflictDiagnostic),
            ...globalDiagnostics(
              recoveryConfigSourceError instanceof Error
                ? recoveryConfigSourceError.message
                : "Failed to load raw configuration",
              true,
            ),
          ];
          if (draftEdited(latestState)) {
            set({
              isLoading: false,
              syncStatus: "error",
              diagnostics: recoveryDiagnostics,
            });
            return;
          }
          const nextDraft = nextDraftVersion(get().draftVersion);
          set(
            clearedLoadedConfigState(
              recoveryDiagnostics,
              nextAgentPoliciesRequestId,
              nextDraft,
            ),
          );
          return;
        }
        if (draftEdited(latestState)) {
          set({
            isLoading: false,
            syncStatus: "error",
            diagnostics: [
              ...latestState.diagnostics.filter(isConfigConflictDiagnostic),
              ...validationDiagnostics(error.issues, {
                blocking:
                  latestState.recoveryConfigSource != null &&
                  latestState.config == null,
              }),
            ],
          });
          return;
        }
        const nextDraft = nextDraftVersion(get().draftVersion);
        set(
          clearedLoadedConfigState(
            validationDiagnostics(error.issues, { blocking: true }),
            nextAgentPoliciesRequestId,
            nextDraft,
            recoveryConfigSource,
            recoveryGeneration,
          ),
        );
        return;
      }
      set({
        diagnostics: [
          ...get().diagnostics.filter(isConfigConflictDiagnostic),
          ...globalDiagnostics(
            error instanceof Error ? error.message : "Failed to load config",
            true,
          ),
        ],
        isLoading: false,
        syncStatus: "error",
      });
    }
  },

  refreshAgentPolicies: async (agents) => {
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
      const agentPoliciesByAgent = await configService.getAgentPolicies(
        config,
        agents,
      );
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
          ...globalDiagnostics(AGENT_POLICIES_ERROR_MESSAGE, false),
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
      committedGeneration,
      agentPoliciesStale,
      loadedConfig,
      draftVersion,
      diagnostics,
      dirtyRoots,
    } = get();
    const conflict = diagnostics.find(isConfigConflictDiagnostic);
    if (conflict) {
      return { status: "error", message: conflict.message, diagnostics };
    }
    if (!config) {
      return {
        status: "error",
        message: "No configuration draft is available to save.",
        diagnostics,
      };
    }
    if (committedGeneration == null) {
      return failSave(
        set,
        globalDiagnostics(MISSING_GENERATION_MESSAGE, false),
        MISSING_GENERATION_MESSAGE,
      );
    }
    const saveConfigRequestId = get().saveConfigRequestId + 1;

    set({
      isLoading: true,
      diagnostics: retainedDraftDiagnostics(diagnostics),
      syncStatus: "syncing",
      saveConfigRequestId,
    });
    try {
      const dirtyRootSet = new Set(dirtyRoots);
      const baseConfig = loadedConfig ?? config;

      const updatedConfig: Config = {
        ...baseConfig,
        ...Object.fromEntries(
          dirtyRoots
            .filter((root) => !COLLECTION_ROOTS.has(root))
            .map((root) => [root, readConfigRoot(config, root)]),
        ),
        ...(dirtyRootSet.has("agents")
          ? {
              agents: Object.fromEntries(
                agents.map((agent) => [
                  agent.id,
                  authoredFields<AgentConfig>(agent),
                ]),
              ),
            }
          : {}),
        ...(dirtyRootSet.has("teams")
          ? {
              teams: Object.fromEntries(
                teams.map((team) => [
                  team.id,
                  authoredFields<TeamConfig>(team),
                ]),
              ),
            }
          : {}),
        // Draft room names keep the whitespace typed into them.
        ...(dirtyRootSet.has("rooms")
          ? { rooms: normalizedRoomConfigs(config.rooms) }
          : {}),
      };

      const { generation } = await configService.saveConfig(
        updatedConfig,
        committedGeneration,
      );
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: "stale" };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted =
        currentState.draftVersion !== draftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          committedGeneration: generation,
          loadedConfig: updatedConfig,
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        if (currentState.agentPoliciesStale) {
          void get().refreshAgentPolicies(currentState.agents);
        }
        return { status: "stale" };
      }
      const syncedCollections = deriveConfigCollections(updatedConfig);
      set({
        committedGeneration: generation,
        loadedConfig: updatedConfig,
        config: updatedConfig,
        agents: syncedCollections.agents,
        teams: syncedCollections.teams,
        rooms: syncedCollections.rooms,
        isLoading: false,
        syncStatus: "synced",
        isDirty: false,
        dirtyRoots: [],
        diagnostics: [],
        privateWorkerScopeBackups: {},
      });
      if (agentPoliciesStale) {
        void get().refreshAgentPolicies(syncedCollections.agents);
      }
      return { status: "saved" };
    } catch (error) {
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: "stale" };
      }
      const currentState = get();
      if (error instanceof configService.ConfigStaleError) {
        return failSave(
          set,
          configConflictDiagnostics(currentState.diagnostics, false),
          CONFIG_CONFLICT_MESSAGE,
        );
      }
      const draftChangedSinceSaveStarted =
        currentState.draftVersion !== draftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: "stale" };
      }
      if (error instanceof configService.ConfigComposedFromIncludesError) {
        return failSave(
          set,
          globalDiagnostics(error.message, false),
          error.message,
          { configUsesIncludes: true },
        );
      }
      if (error instanceof configService.ConfigValidationError) {
        return failSave(
          set,
          validationDiagnostics(error.issues, { blocking: false }),
          CONFIG_VALIDATION_FAILED_MESSAGE,
        );
      }
      const errorMessage =
        error instanceof Error ? error.message : "Failed to save config";
      return failSave(
        set,
        globalDiagnostics(errorMessage, false),
        errorMessage,
      );
    }
  },

  updateRecoveryConfigSource: (source) => {
    set((state) => {
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
    const {
      recoveryConfigSource,
      diagnostics,
      draftVersion,
      committedGeneration,
    } = get();
    const conflict = diagnostics.find(isConfigConflictDiagnostic);
    if (conflict) {
      return { status: "error", message: conflict.message, diagnostics };
    }
    if (recoveryConfigSource == null) {
      return {
        status: "error",
        message: "No recovery configuration is available to save.",
        diagnostics,
      };
    }
    if (committedGeneration == null) {
      return failSave(
        set,
        globalDiagnostics(MISSING_GENERATION_MESSAGE, true),
        MISSING_GENERATION_MESSAGE,
      );
    }

    const saveConfigRequestId = get().saveConfigRequestId + 1;
    set({
      isLoading: true,
      diagnostics: retainedDraftDiagnostics(diagnostics),
      syncStatus: "syncing",
      saveConfigRequestId,
    });

    try {
      const { generation } = await configService.saveRawConfigSource(
        recoveryConfigSource,
        committedGeneration,
      );
      set((state) => ({
        committedGeneration:
          state.committedGeneration == null
            ? generation
            : Math.max(state.committedGeneration, generation),
      }));
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: "stale" };
      }
      const currentState = get();
      const draftChangedSinceSaveStarted =
        currentState.draftVersion !== draftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: "stale" };
      }

      await get().loadConfig();
      const latestState = get();
      if (
        latestState.config != null &&
        latestState.recoveryConfigSource == null
      ) {
        return { status: "saved" };
      }
      return {
        status: "error",
        message: firstGlobalDiagnosticMessage(
          latestState.diagnostics,
          "Saved replacement configuration, but failed to reload the structured config.",
        ),
        diagnostics: latestState.diagnostics,
      };
    } catch (error) {
      if (get().saveConfigRequestId != saveConfigRequestId) {
        return { status: "stale" };
      }
      const currentState = get();
      if (error instanceof configService.ConfigStaleError) {
        return failSave(
          set,
          configConflictDiagnostics(currentState.diagnostics, true),
          CONFIG_CONFLICT_MESSAGE,
        );
      }
      const draftChangedSinceSaveStarted =
        currentState.draftVersion !== draftVersion;
      if (draftChangedSinceSaveStarted) {
        set({
          isLoading: false,
          syncStatus: draftSyncStatus(currentState),
        });
        return { status: "stale" };
      }
      if (error instanceof configService.ConfigValidationError) {
        return failSave(
          set,
          validationDiagnostics(error.issues, { blocking: true }),
          CONFIG_VALIDATION_FAILED_MESSAGE,
        );
      }
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Failed to save raw configuration";
      return failSave(set, globalDiagnostics(errorMessage, true), errorMessage);
    }
  },

  // Select an agent for editing
  selectAgent: (agentId) => {
    set({ selectedAgentId: agentId });
  },

  // Update an existing agent
  updateAgent: (agentId, updates) => {
    let nextAgents: Agent[] = [];
    let shouldRefreshAgentPolicies = false;
    set((state) => {
      const currentAgent = state.agents.find((agent) => agent.id === agentId);
      if (!currentAgent) {
        return state;
      }

      const edit = editedAgentDraft(state, currentAgent, updates);
      nextAgents = edit.patch.agents;
      shouldRefreshAgentPolicies = edit.policyChanged;
      return edit.patch;
    });
    if (shouldRefreshAgentPolicies && get().config != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  setAgentPrivateEnabled: (agentId, enabled) => {
    let nextAgents: Agent[] = [];
    let shouldRefreshAgentPolicies = false;
    set((state) => {
      const currentAgent = state.agents.find((agent) => agent.id === agentId);
      if (!currentAgent) {
        return state;
      }

      const nextBackups = { ...state.privateWorkerScopeBackups };
      let privateUpdates: Partial<Agent>;
      if (enabled) {
        if (!(agentId in nextBackups)) {
          nextBackups[agentId] = currentAgent.worker_scope ?? null;
        }
        privateUpdates = { private: getDefaultPrivateConfig(currentAgent) };
      } else {
        const restoredWorkerScope = nextBackups[agentId];
        delete nextBackups[agentId];
        privateUpdates =
          restoredWorkerScope != null
            ? { private: undefined, worker_scope: restoredWorkerScope }
            : { private: undefined };
      }

      const edit = editedAgentDraft(state, currentAgent, privateUpdates);
      nextAgents = edit.patch.agents;
      shouldRefreshAgentPolicies = edit.policyChanged;
      return { ...edit.patch, privateWorkerScopeBackups: nextBackups };
    });
    if (shouldRefreshAgentPolicies && get().config != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  // Create a new agent
  createAgent: (agentData) => {
    const id = agentData.display_name.toLowerCase().replace(/\s+/g, "_");
    const newAgent: Agent = withFieldDefaults(
      { id, ...agentData },
      agentFieldDefaults(),
    );
    set((state) => ({
      agents: [...state.agents, newAgent],
      rooms: roomsFromDraft(
        state.config,
        state.rooms,
        [...state.agents, newAgent],
        state.teams,
      ),
      selectedAgentId: id,
      ...markDraftDirty(state, [["agents"]]),
    }));
    if (get().config != null) {
      void get().refreshAgentPolicies(get().agents);
    }
  },

  // Delete an agent
  deleteAgent: (agentId) => {
    const state = get();
    const deletedAgent = state.agents.find((agent) => agent.id === agentId);
    const nextAgents = state.agents
      .filter((agent) => agent.id !== agentId)
      .map((agent) => {
        if (!agent.delegate_to?.includes(agentId)) {
          return agent;
        }
        return withEdits(agent, {
          delegate_to: agent.delegate_to.filter((id) => id !== agentId),
        });
      });
    const nextAgentPoliciesByAgent = Object.fromEntries(
      Object.entries(state.agentPoliciesByAgent).filter(
        ([id]) => id !== agentId,
      ),
    );
    const nextTeams = removeMissingTeamMembers(state.teams, nextAgents);
    const nextRooms = roomsFromDraft(
      state.config,
      state.rooms,
      nextAgents,
      nextTeams,
    );
    const { config, touchedPaths } = configWithoutEntityKeys(
      state,
      agentId,
      nextRooms,
    );
    const { [agentId]: _removedBackup, ...remainingBackups } =
      state.privateWorkerScopeBackups;
    set({
      agents: nextAgents,
      teams: nextTeams,
      config,
      rooms: nextRooms,
      agentPoliciesByAgent: nextAgentPoliciesByAgent,
      privateWorkerScopeBackups: remainingBackups,
      selectedAgentId:
        state.selectedAgentId === agentId ? null : state.selectedAgentId,
      ...markDraftDirty(state, [["agents"], ["teams"], ...touchedPaths]),
    });
    if (get().config != null && deletedAgent != null) {
      void get().refreshAgentPolicies(nextAgents);
    }
  },

  // Select a team for editing
  selectTeam: (teamId) => {
    set({ selectedTeamId: teamId });
  },

  // Update an existing team
  updateTeam: (teamId, updates) => {
    set((state) => {
      const currentTeam = state.teams.find((team) => team.id === teamId);
      if (!currentTeam) {
        return state;
      }

      const normalizedUpdates = normalizeTeamUpdates(currentTeam, updates);
      const touchedPaths = Object.keys(normalizedUpdates).map(
        (key) => ["teams", teamId, key] as ConfigDiagnosticPath,
      );
      const nextTeams = state.teams.map((team) =>
        team.id === teamId ? withEdits(team, normalizedUpdates) : team,
      );
      return {
        teams: nextTeams,
        rooms: roomsFromDraft(
          state.config,
          state.rooms,
          state.agents,
          nextTeams,
        ),
        ...markDraftDirty(state, touchedPaths),
      };
    });
  },

  // Create a new team
  createTeam: (teamData) => {
    const id = teamData.display_name.toLowerCase().replace(/\s+/g, "_");
    const newTeam: Team = {
      id,
      ...teamData,
    };
    set((state) => ({
      teams: [...state.teams, newTeam],
      rooms: roomsFromDraft(state.config, state.rooms, state.agents, [
        ...state.teams,
        newTeam,
      ]),
      selectedTeamId: id,
      ...markDraftDirty(state, [["teams"]]),
    }));
  },

  // Delete a team
  deleteTeam: (teamId) => {
    set((state) => {
      const nextTeams = state.teams.filter((team) => team.id !== teamId);
      const nextRooms = roomsFromDraft(
        state.config,
        state.rooms,
        state.agents,
        nextTeams,
      );
      const { config, touchedPaths } = configWithoutEntityKeys(
        state,
        teamId,
        nextRooms,
      );
      return {
        teams: nextTeams,
        config,
        rooms: nextRooms,
        selectedTeamId:
          state.selectedTeamId === teamId ? null : state.selectedTeamId,
        ...markDraftDirty(state, [["teams"], ...touchedPaths]),
      };
    });
  },

  // Select a room for editing
  selectRoom: (roomId) => {
    set({ selectedRoomId: roomId });
  },

  // Update an existing room
  updateRoom: (roomId, updates) => {
    set((state) => {
      const existingRoom = state.rooms.find((room) => room.id === roomId);
      if (!existingRoom || roomUpdateIsNoop(existingRoom, updates)) {
        return {};
      }

      const updatedRooms = state.rooms.map((room) =>
        room.id === roomId ? { ...room, ...updates } : room,
      );

      let updatedConfig = state.config;
      const modelUpdateProvided = hasUpdateKey(updates, "model");
      if (modelUpdateProvided && updatedConfig) {
        updatedConfig = configWithRoomModel(
          updatedConfig,
          roomId,
          updates.model,
        );
      }

      let roomMetadataTouchedPaths = (["display_name", "description"] as const)
        .filter((key) => hasUpdateKey(updates, key))
        .map((key) => ["rooms", roomId, key] as ConfigDiagnosticPath);
      if (roomMetadataTouchedPaths.length > 0 && updatedConfig) {
        const configWithMetadata = updateDraftRoomMetadata(
          updatedConfig,
          roomId,
          updates,
        );
        if (configWithMetadata === updatedConfig) {
          roomMetadataTouchedPaths = [];
        }
        updatedConfig = configWithMetadata;
      }

      const roomModelTouchedPaths = modelUpdateProvided
        ? ([["room_models", roomId]] as ConfigDiagnosticPath[])
        : [];

      // If agents changed, update the agents' rooms arrays
      let updatedAgents = state.agents;
      if (updates.agents) {
        const oldAgents = existingRoom.agents;
        const newAgents = updates.agents;

        // Remove room from agents no longer in the room
        const removedAgents = oldAgents.filter((id) => !newAgents.includes(id));
        // Add room to new agents
        const addedAgents = newAgents.filter((id) => !oldAgents.includes(id));

        updatedAgents = state.agents.map((agent) => {
          if (removedAgents.includes(agent.id)) {
            return withEdits(agent, {
              rooms: agent.rooms.filter((r) => r !== roomId),
            });
          }
          if (addedAgents.includes(agent.id) && !agent.rooms.includes(roomId)) {
            return withEdits(agent, { rooms: [...agent.rooms, roomId] });
          }
          return agent;
        });
      }

      const touchedPaths: ConfigDiagnosticPath[] = [
        ...(updates.agents ? [["agents"]] : []),
        ...roomMetadataTouchedPaths,
        ...roomModelTouchedPaths,
      ];
      return {
        config: updatedConfig,
        agents: updatedAgents,
        rooms: roomsFromDraft(
          updatedConfig,
          updatedRooms,
          updatedAgents,
          state.teams,
        ),
        ...(touchedPaths.length > 0 ? markDraftDirty(state, touchedPaths) : {}),
      };
    });
  },

  // Create a new room
  createRoom: (roomData) => {
    const displayName =
      normalizedRoomDisplayName(roomData.display_name) ?? "New Room";
    const id = displayName.toLowerCase().replace(/\s+/g, "_");
    const newRoom: Room = {
      id,
      ...roomData,
      display_name: displayName,
    };

    set((state) => {
      // Add room to selected agents
      const updatedAgents = state.agents.map((agent) => {
        if (roomData.agents.includes(agent.id) && !agent.rooms.includes(id)) {
          return withEdits(agent, { rooms: [...agent.rooms, id] });
        }
        return agent;
      });
      let updatedConfig = state.config
        ? updateDraftRoomMetadata(state.config, id, newRoom)
        : state.config;
      const touchedPaths: ConfigDiagnosticPath[] = [["rooms"]];
      if (roomData.model !== undefined && updatedConfig) {
        updatedConfig = configWithRoomModel(updatedConfig, id, roomData.model);
        touchedPaths.push(["room_models", id]);
      }
      if (roomData.agents.length > 0) {
        touchedPaths.push(["agents"]);
      }

      return {
        config: updatedConfig,
        rooms: roomsFromDraft(
          updatedConfig,
          [...state.rooms, newRoom],
          updatedAgents,
          state.teams,
        ),
        agents: updatedAgents,
        selectedRoomId: id,
        ...markDraftDirty(state, touchedPaths),
      };
    });
  },

  // Delete a room
  deleteRoom: (roomId) => {
    set((state) => {
      const agentsReferenceRoom = state.agents.some((agent) =>
        agent.rooms.includes(roomId),
      );
      const teamsReferenceRoom = state.teams.some((team) =>
        team.rooms.includes(roomId),
      );

      // Remove room from the agents and teams that list it
      const updatedAgents = agentsReferenceRoom
        ? state.agents.map((agent) =>
            agent.rooms.includes(roomId)
              ? withEdits(agent, {
                  rooms: agent.rooms.filter((r) => r !== roomId),
                })
              : agent,
          )
        : state.agents;
      const updatedTeams = teamsReferenceRoom
        ? state.teams.map((team) =>
            team.rooms.includes(roomId)
              ? withEdits(team, {
                  rooms: team.rooms.filter((r) => r !== roomId),
                })
              : team,
          )
        : state.teams;

      // Remove from room_models if it exists
      let updatedConfig = state.config;
      const touchedPaths: ConfigDiagnosticPath[] = [];
      if (agentsReferenceRoom) {
        touchedPaths.push(["agents"]);
      }
      if (teamsReferenceRoom) {
        touchedPaths.push(["teams"]);
      }
      if (state.config?.rooms?.[roomId]) {
        updatedConfig = deleteDraftRoomMetadata(state.config, roomId);
        touchedPaths.push(["rooms"]);
      }
      if (updatedConfig?.room_models?.[roomId]) {
        updatedConfig = configWithRoomModel(updatedConfig, roomId, undefined);
        touchedPaths.push(["room_models", roomId]);
      }

      return {
        agents: updatedAgents,
        teams: updatedTeams,
        config: updatedConfig,
        rooms: roomsFromDraft(
          updatedConfig,
          state.rooms.filter((room) => room.id !== roomId),
          updatedAgents,
          updatedTeams,
        ),
        selectedRoomId:
          state.selectedRoomId === roomId ? null : state.selectedRoomId,
        ...markDraftDirty(state, touchedPaths),
      };
    });
  },

  // Update memory configuration
  updateMemoryConfig: (memoryConfig) => {
    set((state) => {
      if (!state.config) return state;
      const nextConfig = { ...state.config, memory: memoryConfig };
      return {
        config: nextConfig,
        ...markDraftDirty(state, [["memory"]]),
      };
    });
  },

  // Update one knowledge base configuration
  updateKnowledgeBase: (baseName, baseConfig) => {
    set((state) => {
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
      return {
        config: nextConfig,
        ...markDraftDirty(state, [["knowledge_bases", baseName]]),
      };
    });
  },

  // Delete a knowledge base and unassign it from agents
  deleteKnowledgeBase: (baseName) => {
    set((state) => {
      if (!state.config) return state;

      const knowledgeBases = { ...(state.config.knowledge_bases || {}) };
      delete knowledgeBases[baseName];

      const agents = state.agents.map((agent) =>
        agent.knowledge_bases?.includes(baseName)
          ? withEdits(agent, {
              knowledge_bases: agent.knowledge_bases.filter(
                (base) => base !== baseName,
              ),
            })
          : agent,
      );
      const nextConfig = {
        ...state.config,
        knowledge_bases: knowledgeBases,
      };

      return {
        config: nextConfig,
        agents,
        ...markDraftDirty(state, [["knowledge_bases", baseName], ["agents"]]),
      };
    });
  },

  // Delete a model configuration
  deleteModel: (modelId) => {
    set((state) => {
      if (!state.config) return state;
      const { [modelId]: _, ...remainingModels } = state.config.models;
      const nextConfig = {
        ...state.config,
        models: remainingModels,
      };
      return {
        config: nextConfig,
        ...markDraftDirty(state, [["models"]]),
      };
    });
  },

  updateConfigValue: (path, value) => {
    set((state) => {
      if (!state.config) return state;
      const nextConfig = configWithValue(state, path, value);
      return {
        config: nextConfig,
        rooms: deriveRooms(nextConfig, state.agents, state.teams),
        ...markDraftDirty(state, [[...path]]),
      };
    });
  },

  restoreConfigValue: (path, value, before) => {
    set((state) => {
      if (!state.config) return state;
      const nextConfig = configWithValue(state, path, value);
      const diagnostics = retainedDraftDiagnostics(state.diagnostics, [
        [...path],
      ]);
      // The write moved the draft version on by one. Any other version means
      // the draft changed after it, and those edits stay dirty.
      const writeIsLatest =
        state.draftVersion === nextDraftVersion(before.draftVersion);
      const isDirty = writeIsLatest ? before.isDirty : state.isDirty;
      return {
        config: nextConfig,
        rooms: deriveRooms(nextConfig, state.agents, state.teams),
        isDirty,
        dirtyRoots: writeIsLatest ? before.dirtyRoots : state.dirtyRoots,
        diagnostics,
        draftVersion: nextDraftVersion(state.draftVersion),
        // A conflict keeps its error status; otherwise the draft is as in sync as it was.
        syncStatus: diagnostics.some(isConfigConflictDiagnostic)
          ? state.syncStatus
          : draftSyncStatus({
              loadedConfig: state.loadedConfig,
              isDirty,
            }),
      };
    });
  },

  getAgentToolOverrides: (agentId, toolName) =>
    getToolOverrides(
      toolName,
      get().agents.find((agent) => agent.id === agentId)?.tools,
    ),

  updateAgentToolOverrides: (agentId, toolName, overrides) => {
    const tools = get().agents.find((agent) => agent.id === agentId)?.tools;
    if (tools?.some((entry) => toolEntryName(entry) === toolName)) {
      get().updateAgent(agentId, {
        tools: setToolOverridesInEntries(toolName, overrides, tools),
      });
    }
  },

  getDefaultToolOverrides: (toolName) =>
    getToolOverrides(toolName, get().config?.defaults?.tools),

  updateDefaultToolOverrides: (toolName, overrides) => {
    const tools = get().config?.defaults?.tools;
    if (tools?.some((entry) => toolEntryName(entry) === toolName)) {
      get().updateConfigValue(
        ["defaults", "tools"],
        setToolOverridesInEntries(toolName, overrides, tools),
      );
    }
  },
}));
