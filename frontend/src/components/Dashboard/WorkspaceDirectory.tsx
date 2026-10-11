import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Download, Search, X } from "lucide-react";

import { EntityAvatar } from "@/components/shared/EntityAvatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useConfigStore } from "@/store/configStore";
import {
  resolveEffectiveDefaultTools,
  type Agent,
  type Config,
  type Room,
  type Team,
} from "@/types/config";

type EntityKind = "agent" | "room" | "team";
type TypeFilter = "all" | "agents" | "rooms" | "teams";

interface Selection {
  kind: EntityKind;
  id: string;
}

interface DirectoryItem {
  kind: EntityKind;
  id: string;
  name: string;
  description: string;
  metadata: string;
  searchText: string;
}

const KIND_LABELS: Record<EntityKind, string> = {
  agent: "Agent",
  room: "Room",
  team: "Team",
};

const TYPE_FILTERS: Record<EntityKind, TypeFilter> = {
  agent: "agents",
  room: "rooms",
  team: "teams",
};

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function naturalCompare(left: string, right: string): number {
  return left.localeCompare(right, undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

function displayNames(
  ids: string[],
  items: Array<{ id: string; display_name: string }>,
): string[] {
  return ids.map(
    (id) => items.find((item) => item.id === id)?.display_name ?? id,
  );
}

function resolveAgentTools(agent: Agent, config: Config | null): string[] {
  const inheritedTools =
    agent.include_default_tools === false
      ? []
      : resolveEffectiveDefaultTools(config?.defaults);
  return [...new Set([...agent.tools, ...inheritedTools])];
}

function resolveModelMetadata(
  model: string | undefined,
  config: Config | null,
) {
  const alias = model ?? "default";
  const modelConfig = config?.models[alias];
  return {
    alias,
    label: modelConfig
      ? `${alias} · ${modelConfig.provider}/${modelConfig.id}`
      : alias,
    searchText: [
      alias,
      modelConfig?.provider,
      modelConfig?.id,
      modelConfig?.display_name,
    ]
      .filter((value): value is string => Boolean(value))
      .join(" "),
  };
}

function WorkspaceDirectorySkeleton() {
  return (
    <div className="space-y-3" aria-label="Loading workspace overview">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-2">
          <Skeleton className="h-7 w-44" />
          <Skeleton className="h-4 w-64" />
        </div>
        <Skeleton className="h-9 w-32" />
      </div>
      <div className="workspace-surface overflow-hidden rounded-xl border border-border bg-card">
        <div className="flex gap-2 border-b border-border p-3">
          <Skeleton className="h-9 flex-1" />
          <Skeleton className="h-9 w-28" />
        </div>
        {Array.from({ length: 8 }).map((_, index) => (
          <div
            key={index}
            className="flex items-center gap-3 border-b border-border px-4 py-3 last:border-b-0"
          >
            <Skeleton className="h-8 w-8 rounded-lg" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-3 w-3/4" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3 border-t border-border py-2.5 first:border-t-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 break-words text-sm text-foreground">
        {children}
      </dd>
    </div>
  );
}

function AgentDetails({
  agent,
  rooms,
  teams,
  tools,
  modelLabel,
}: {
  agent: Agent;
  rooms: Room[];
  teams: Team[];
  tools: string[];
  modelLabel: (model: string | undefined) => string;
}) {
  const agentTeams = teams.filter((team) => team.agents.includes(agent.id));
  return (
    <dl>
      <DetailRow label="Role">{agent.role || "No role set"}</DetailRow>
      <DetailRow label="Model">{modelLabel(agent.model)}</DetailRow>
      <DetailRow label="Tools">
        {tools.length > 0 ? tools.join(", ") : "No tools"}
      </DetailRow>
      <DetailRow label="Skills">
        {agent.skills.length > 0 ? agent.skills.join(", ") : "No skills"}
      </DetailRow>
      <DetailRow label="Rooms">
        {agent.rooms.length > 0
          ? displayNames(agent.rooms, rooms).join(", ")
          : "No rooms"}
      </DetailRow>
      <DetailRow label="Teams">
        {agentTeams.length > 0
          ? agentTeams.map((team) => team.display_name).join(", ")
          : "No teams"}
      </DetailRow>
    </dl>
  );
}

function RoomDetails({
  room,
  agents,
  teams,
  modelLabel,
}: {
  room: Room;
  agents: Agent[];
  teams: Team[];
  modelLabel: (model: string | undefined) => string;
}) {
  const roomTeams = teams.filter((team) => team.rooms.includes(room.id));
  return (
    <dl>
      <DetailRow label="ID">{room.id}</DetailRow>
      <DetailRow label="Description">
        {room.description || "No description"}
      </DetailRow>
      <DetailRow label="Model">
        {room.model ? modelLabel(room.model) : "Uses agent/team models"}
      </DetailRow>
      <DetailRow label="Agents">
        {room.agents.length > 0
          ? displayNames(room.agents, agents).join(", ")
          : "No agents"}
      </DetailRow>
      <DetailRow label="Teams">
        {roomTeams.length > 0
          ? roomTeams.map((team) => team.display_name).join(", ")
          : "No teams"}
      </DetailRow>
    </dl>
  );
}

function TeamDetails({
  team,
  agents,
  rooms,
  modelLabel,
}: {
  team: Team;
  agents: Agent[];
  rooms: Room[];
  modelLabel: (model: string | undefined) => string;
}) {
  return (
    <dl>
      <DetailRow label="Role">{team.role || "No role set"}</DetailRow>
      <DetailRow label="Mode">{team.mode}</DetailRow>
      <DetailRow label="Model">
        {team.model === null ? "No model set" : modelLabel(team.model)}
      </DetailRow>
      <DetailRow label="Members">
        {team.agents.length > 0
          ? displayNames(team.agents, agents).join(", ")
          : "No members"}
      </DetailRow>
      <DetailRow label="Rooms">
        {team.rooms.length > 0
          ? displayNames(team.rooms, rooms).join(", ")
          : "No rooms"}
      </DetailRow>
    </dl>
  );
}

export function WorkspaceDirectory() {
  const navigate = useNavigate();
  const { agents, rooms, teams, config, selectAgent, selectRoom, selectTeam } =
    useConfigStore();
  const [searchTerm, setSearchTerm] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [selection, setSelection] = useState<Selection | null>(null);
  const detailsRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const closeFocusKeyRef = useRef<string | null>(null);

  const modelLabel = useCallback(
    (model: string | undefined) => resolveModelMetadata(model, config).label,
    [config],
  );

  const directoryItems = useMemo<DirectoryItem[]>(() => {
    const agentItems = agents.map<DirectoryItem>((agent) => {
      const roomNames = displayNames(agent.rooms, rooms);
      const agentTeams = teams.filter((team) => team.agents.includes(agent.id));
      const tools = resolveAgentTools(agent, config);
      const model = resolveModelMetadata(agent.model, config);
      return {
        kind: "agent",
        id: agent.id,
        name: agent.display_name,
        description: agent.role || "No role set",
        metadata: [
          `model ${model.alias}`,
          tools.length > 0 ? `tools ${tools.join(", ")}` : "no tools",
          agent.rooms.length > 0 ? `rooms ${roomNames.join(", ")}` : "no rooms",
        ].join(" · "),
        searchText: [
          agent.id,
          agent.display_name,
          agent.role,
          model.searchText,
          ...tools,
          ...agent.skills,
          ...agent.rooms,
          ...roomNames,
          ...agentTeams.flatMap((team) => [team.id, team.display_name]),
        ]
          .join(" ")
          .toLocaleLowerCase(),
      };
    });

    const roomItems = rooms.map<DirectoryItem>((room) => {
      const agentNames = displayNames(room.agents, agents);
      const roomTeams = teams.filter((team) => team.rooms.includes(room.id));
      const model = room.model
        ? resolveModelMetadata(room.model, config)
        : null;
      return {
        kind: "room",
        id: room.id,
        name: room.display_name,
        description: room.description || "No description",
        metadata: [
          model ? `model ${model.alias}` : "no room override",
          room.agents.length > 0
            ? `agents ${agentNames.join(", ")}`
            : "no agents",
          roomTeams.length > 0
            ? `teams ${roomTeams.map((team) => team.display_name).join(", ")}`
            : "no teams",
        ].join(" · "),
        searchText: [
          room.id,
          room.display_name,
          room.description,
          model?.searchText,
          ...room.agents,
          ...agentNames,
          ...roomTeams.flatMap((team) => [team.id, team.display_name]),
        ]
          .filter(Boolean)
          .join(" ")
          .toLocaleLowerCase(),
      };
    });

    const teamItems = teams.map<DirectoryItem>((team) => {
      const agentNames = displayNames(team.agents, agents);
      const roomNames = displayNames(team.rooms, rooms);
      const model =
        team.model === null ? null : resolveModelMetadata(team.model, config);
      return {
        kind: "team",
        id: team.id,
        name: team.display_name,
        description: team.role || "No role set",
        metadata: [
          team.mode,
          model ? `model ${model.alias}` : "No model set",
          team.agents.length > 0
            ? `members ${agentNames.join(", ")}`
            : "no members",
          team.rooms.length > 0 ? `rooms ${roomNames.join(", ")}` : "no rooms",
        ].join(" · "),
        searchText: [
          team.id,
          team.display_name,
          team.role,
          team.mode,
          model?.searchText,
          ...team.agents,
          ...agentNames,
          ...team.rooms,
          ...roomNames,
        ]
          .filter(Boolean)
          .join(" ")
          .toLocaleLowerCase(),
      };
    });

    return [...agentItems, ...roomItems, ...teamItems].sort((left, right) =>
      naturalCompare(left.name, right.name),
    );
  }, [agents, config, rooms, teams]);

  const query = searchTerm.trim().toLocaleLowerCase();
  const filteredItems = directoryItems.filter(
    (item) =>
      (typeFilter === "all" || TYPE_FILTERS[item.kind] === typeFilter) &&
      (!query || item.searchText.includes(query)),
  );

  const selectedAgent =
    selection?.kind === "agent"
      ? (agents.find((agent) => agent.id === selection.id) ?? null)
      : null;
  const selectedRoom =
    selection?.kind === "room"
      ? (rooms.find((room) => room.id === selection.id) ?? null)
      : null;
  const selectedTeam =
    selection?.kind === "team"
      ? (teams.find((team) => team.id === selection.id) ?? null)
      : null;
  const selectedEntity = selectedAgent ?? selectedRoom ?? selectedTeam;

  useEffect(() => {
    if (selection && !selectedEntity) setSelection(null);
  }, [selection, selectedEntity]);

  useEffect(() => {
    const focusKey = closeFocusKeyRef.current;
    if (selection || !focusKey) return;

    closeFocusKeyRef.current = null;
    (rowRefs.current.get(focusKey) ?? searchRef.current)?.focus();
  }, [selection]);

  useEffect(() => {
    if (
      !selection ||
      !selectedEntity ||
      typeof window.matchMedia !== "function" ||
      !window.matchMedia("(max-width: 1023px)").matches
    ) {
      return;
    }
    detailsRef.current?.focus({ preventScroll: true });
    detailsRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
  }, [selection, selectedEntity]);

  const exportConfiguration = useCallback(() => {
    if (!config) return;
    const exportData = {
      timestamp: new Date().toISOString(),
      stats: {
        totalAgents: agents.length,
        totalRooms: rooms.length,
        totalTeams: teams.length,
        modelsInUse: Object.keys(config.models).length,
        voiceEnabled: config.voice?.enabled ?? false,
      },
      agents: agents.map((agent) => ({
        ...agent,
        teamMemberships: teams
          .filter((team) => team.agents.includes(agent.id))
          .map((team) => team.display_name),
      })),
      rooms: rooms.map((room) => ({
        ...room,
        teamsInRoom: teams
          .filter((team) => team.rooms.includes(room.id))
          .map((team) => team.display_name),
      })),
      teams,
      modelConfigurations: config.models,
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `mindroom-config-${new Date().toISOString().split("T")[0]}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }, [agents, config, rooms, teams]);

  const openEditor = () => {
    if (!selection || !selectedEntity) return;
    if (selection.kind === "agent") {
      selectAgent(selection.id);
      selectRoom(null);
      selectTeam(null);
      navigate("/agents");
      return;
    }
    if (selection.kind === "room") {
      selectAgent(null);
      selectRoom(selection.id);
      selectTeam(null);
      navigate("/rooms");
      return;
    }
    selectAgent(null);
    selectRoom(null);
    selectTeam(selection.id);
    navigate("/teams");
  };

  const closeDetails = () => {
    if (!selection) return;
    closeFocusKeyRef.current = `${selection.kind}:${selection.id}`;
    setSelection(null);
  };

  if (!config) return <WorkspaceDirectorySkeleton />;

  const noConfiguredItems = directoryItems.length === 0;

  return (
    <div className="space-y-3 text-foreground">
      <header className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Workspace overview
          </h2>
          <div className="mt-1 flex flex-wrap items-center gap-x-1 text-sm text-muted-foreground">
            <button
              type="button"
              className="rounded px-1 py-0.5 hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setTypeFilter("agents")}
              aria-label={`Show ${countLabel(agents.length, "agent")}`}
            >
              {countLabel(agents.length, "agent")}
            </button>
            <span aria-hidden="true">·</span>
            <button
              type="button"
              className="rounded px-1 py-0.5 hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setTypeFilter("rooms")}
              aria-label={`Show ${countLabel(rooms.length, "room")}`}
            >
              {countLabel(rooms.length, "room")}
            </button>
            <span aria-hidden="true">·</span>
            <button
              type="button"
              className="rounded px-1 py-0.5 hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setTypeFilter("teams")}
              aria-label={`Show ${countLabel(teams.length, "team")}`}
            >
              {countLabel(teams.length, "team")}
            </button>
            <span aria-hidden="true">·</span>
            <span className="px-1">
              {countLabel(Object.keys(config.models).length, "model")}
            </span>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={exportConfiguration}>
          <Download className="mr-1.5 h-4 w-4" aria-hidden="true" />
          Export summary
        </Button>
      </header>

      <section className="workspace-surface overflow-hidden rounded-xl border border-border bg-card">
        <div className="flex flex-col gap-2 border-b border-border p-3 sm:flex-row">
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">Search workspace</span>
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              ref={searchRef}
              type="search"
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="Search names, models, tools, rooms…"
              className="h-9 bg-background/70 pl-9"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="sr-only">Filter by type</span>
            <select
              aria-label="Filter by type"
              value={typeFilter}
              onChange={(event) =>
                setTypeFilter(event.target.value as TypeFilter)
              }
              className="h-9 min-w-32 rounded-md border border-input bg-background/70 px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="all">All types</option>
              <option value="agents">Agents</option>
              <option value="rooms">Rooms</option>
              <option value="teams">Teams</option>
            </select>
          </label>
          <span className="self-center whitespace-nowrap px-1 text-xs text-muted-foreground">
            {filteredItems.length === directoryItems.length
              ? countLabel(directoryItems.length, "result")
              : `${filteredItems.length} of ${countLabel(directoryItems.length, "result")}`}
          </span>
        </div>

        <div
          className={`grid ${
            selectedEntity && selection
              ? "lg:grid-cols-[minmax(0,1fr)_20rem]"
              : "grid-cols-1"
          }`}
        >
          {selectedEntity && selection ? (
            <aside
              ref={detailsRef}
              tabIndex={-1}
              className="order-1 border-b border-border outline-none lg:order-2 lg:border-b-0 lg:border-l"
            >
              <div className="p-4">
                <div className="mb-3 flex items-start justify-between gap-3">
                  <EntityAvatar
                    kind={selection.kind}
                    id={selection.id}
                    name={selectedEntity.display_name}
                  />
                  <div className="min-w-0">
                    <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      {KIND_LABELS[selection.kind]} details
                    </p>
                    <h3 className="break-words text-lg font-semibold">
                      {selectedEntity.display_name}
                    </h3>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 shrink-0"
                    onClick={closeDetails}
                    aria-label="Close details"
                  >
                    <X className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </div>

                {selectedAgent ? (
                  <AgentDetails
                    agent={selectedAgent}
                    rooms={rooms}
                    teams={teams}
                    tools={resolveAgentTools(selectedAgent, config)}
                    modelLabel={modelLabel}
                  />
                ) : null}
                {selectedRoom ? (
                  <RoomDetails
                    room={selectedRoom}
                    agents={agents}
                    teams={teams}
                    modelLabel={modelLabel}
                  />
                ) : null}
                {selectedTeam ? (
                  <TeamDetails
                    team={selectedTeam}
                    agents={agents}
                    rooms={rooms}
                    modelLabel={modelLabel}
                  />
                ) : null}

                <Button className="mt-4 w-full" size="sm" onClick={openEditor}>
                  Open {selection.kind} editor
                </Button>
              </div>
            </aside>
          ) : null}

          <div className="order-2 min-w-0 lg:order-1">
            {filteredItems.length > 0 ? (
              <div aria-label="Workspace directory">
                {filteredItems.map((item) => {
                  const isSelected =
                    selection?.kind === item.kind && selection.id === item.id;
                  return (
                    <button
                      key={`${item.kind}:${item.id}`}
                      ref={(node) => {
                        const key = `${item.kind}:${item.id}`;
                        if (node) rowRefs.current.set(key, node);
                        else rowRefs.current.delete(key);
                      }}
                      type="button"
                      aria-label={`${item.name}, ${KIND_LABELS[item.kind]}`}
                      aria-pressed={isSelected}
                      onClick={() =>
                        setSelection({ kind: item.kind, id: item.id })
                      }
                      className={`flex w-full items-center gap-3 border-b border-border px-4 py-2 text-left transition-colors last:border-b-0 hover:bg-muted/60 focus-visible:relative focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${
                        isSelected ? "bg-primary/8" : ""
                      }`}
                    >
                      <EntityAvatar
                        kind={item.kind}
                        id={item.id}
                        name={item.name}
                      />
                      <span className="grid min-w-0 flex-1 gap-x-5 xl:grid-cols-[minmax(12rem,0.8fr)_minmax(18rem,1.2fr)] xl:items-center">
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-baseline gap-x-2 leading-tight">
                            <span className="text-sm font-medium text-foreground">
                              {item.name}
                            </span>
                            <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                              {KIND_LABELS[item.kind]}
                            </span>
                          </span>
                          <span className="mt-0.5 block text-sm leading-tight text-muted-foreground">
                            {item.description}
                          </span>
                        </span>
                        <span className="mt-1 block break-words text-xs leading-snug text-muted-foreground xl:mt-0 xl:text-right">
                          {item.metadata}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="flex min-h-56 flex-col items-start justify-center px-5 py-8">
                <p className="font-medium text-foreground">
                  {noConfiguredItems
                    ? "No workspace items are configured yet."
                    : "No workspace items match your search and type filter."}
                </p>
                {!noConfiguredItems ? (
                  <>
                    <p className="mt-1 text-sm text-muted-foreground">
                      Try another term or show every item type.
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-3"
                      onClick={() => {
                        setSearchTerm("");
                        setTypeFilter("all");
                      }}
                    >
                      Reset filters
                    </Button>
                  </>
                ) : null}
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
