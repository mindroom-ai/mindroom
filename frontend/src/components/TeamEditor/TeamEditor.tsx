import { useMemo } from "react";
import { useConfigStore } from "@/store/configStore";
import { useSwipeBack } from "@/hooks/useSwipeBack";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Users } from "lucide-react";
import {
  CheckboxListField,
  EditorPanel,
  EditorPanelEmptyState,
  FieldGroup,
  HistoryContextSection,
} from "@/components/shared";
import { Team } from "@/types/config";
import { useScopedConfigValidation } from "@/hooks/useScopedConfigValidation";
import { SchemaSection } from "@/components/SchemaForm";

const AGENT_POLICY_UNAVAILABLE_REASON =
  "Agent policy preview is unavailable. Save or refresh to validate team eligibility.";

/** TeamConfig keys this editor renders by hand; More settings shows the rest. */
const TEAM_EDITOR_FIELDS = [
  "display_name",
  "role",
  "agents",
  "rooms",
  "model",
  "mode",
  "compaction",
  "num_history_runs",
  "num_history_messages",
  "max_tool_calls_from_history",
] as const;

export function TeamEditor() {
  const {
    teams,
    agents,
    rooms,
    selectedTeamId,
    updateTeam,
    deleteTeam,
    saveConfig,
    agentPoliciesByAgent,
    config,
    isDirty,
    isLoading,
    selectTeam,
  } = useConfigStore();

  const selectedTeam = teams.find((t) => t.id === selectedTeamId);
  const validationPrefix = useMemo<Array<string | number> | null>(
    () => (selectedTeamId == null ? null : ["teams", selectedTeamId]),
    [selectedTeamId],
  );
  const validationErrorForPath = useScopedConfigValidation(validationPrefix);
  const teamRootError = validationErrorForPath([], true);
  const displayNameError = validationErrorForPath(["display_name"], true);
  const roleError = validationErrorForPath(["role"], true);
  const modeError = validationErrorForPath(["mode"], true);
  const modelError = validationErrorForPath(["model"], true);
  const membersError = validationErrorForPath(["agents"]);
  const roomsError = validationErrorForPath(["rooms"]);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectTeam(null),
    enabled: !!selectedTeamId && window.innerWidth < 1024,
  });

  if (!selectedTeam) {
    return (
      <EditorPanelEmptyState icon={Users} message="Select a team to edit" />
    );
  }

  const handleFieldChange = <K extends keyof Team>(field: K, value: Team[K]) =>
    updateTeam(selectedTeam.id, { [field]: value });

  const handleDelete = () => {
    if (confirm("Are you sure you want to delete this team?")) {
      deleteTeam(selectedTeam.id);
    }
  };

  return (
    <EditorPanel
      icon={Users}
      title="Team Details"
      isDirty={isDirty}
      onSave={saveConfig}
      onDelete={handleDelete}
      disableSave={isLoading}
      onBack={() => selectTeam(null)}
    >
      {teamRootError && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {teamRootError}
        </div>
      )}

      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the team"
        htmlFor="display_name"
        error={displayNameError}
      >
        <Input
          id="display_name"
          value={selectedTeam.display_name}
          placeholder="Team display name"
          onChange={(e) => handleFieldChange("display_name", e.target.value)}
        />
      </FieldGroup>

      {/* Role */}
      <FieldGroup
        label="Team Purpose"
        helperText="Description of the team's purpose and what it does"
        htmlFor="role"
        error={roleError}
      >
        <Textarea
          id="role"
          value={selectedTeam.role}
          placeholder="What this team does..."
          rows={2}
          onChange={(e) => handleFieldChange("role", e.target.value)}
        />
      </FieldGroup>

      {/* Collaboration Mode */}
      <FieldGroup
        label="Collaboration Mode"
        helperText="Coordinate delegates through a leader; collaborate sends the task to all members."
        htmlFor="mode"
        error={modeError}
      >
        <Select
          value={selectedTeam.mode}
          onValueChange={(value) =>
            handleFieldChange("mode", value as Team["mode"])
          }
        >
          <SelectTrigger id="mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="coordinate">
              Coordinate (Leader delegates and synthesizes)
            </SelectItem>
            <SelectItem value="collaborate">
              Collaborate (All members work on the task)
            </SelectItem>
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Team Model (Optional)"
        helperText="Override model for all agents in this team"
        htmlFor="model"
        error={modelError}
      >
        <Select
          value={selectedTeam.model || "default_model"}
          onValueChange={(value) =>
            handleFieldChange(
              "model",
              value === "default_model" ? undefined : value,
            )
          }
        >
          <SelectTrigger id="model">
            <SelectValue placeholder="Use default model" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default_model">Use default model</SelectItem>
            {config &&
              Object.keys(config.models).map((modelId) => (
                <SelectItem key={modelId} value={modelId}>
                  {modelId}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
      </FieldGroup>

      <HistoryContextSection
        values={selectedTeam}
        onChange={(field, value) =>
          updateTeam(selectedTeam.id, { [field]: value })
        }
        resetKey={selectedTeamId}
        path={["teams", selectedTeam.id]}
        defaults={config?.defaults}
        historyRunsHelperText="Number of prior team-scoped runs to include as replay."
        historyMessagesHelperText="Max replay messages from team-scoped history."
        maxToolCallsHelperText="Max tool call messages replayed from team history."
        autoCompactionHelperText="Automatically compact older team-scoped history before the next run when the request crosses the compaction threshold on a route without native compaction, or when raw replay exceeds the hard context budget."
        compactionModelPlaceholder={
          config?.defaults?.compaction?.model ?? "Default: team run model"
        }
      />

      {/* Team Members (Agents) */}
      <FieldGroup
        label="Team Members"
        helperText="Select agents that compose this team"
        error={membersError}
      >
        <div className="space-y-2">
          {agents.map((agent) => {
            const isChecked = selectedTeam.agents.includes(agent.id);
            const agentPolicy = agentPoliciesByAgent[agent.id];
            const eligibilityReason =
              agentPolicy?.team_eligibility_reason ??
              (agentPolicy == null ? AGENT_POLICY_UNAVAILABLE_REASON : null);
            const isSelectable = eligibilityReason == null;
            return (
              <div
                key={agent.id}
                className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-secondary transition-all duration-200"
              >
                <Checkbox
                  id={`agent-${agent.id}`}
                  checked={isChecked}
                  disabled={!isSelectable && !isChecked}
                  onCheckedChange={(checked) =>
                    handleFieldChange(
                      "agents",
                      checked
                        ? [...selectedTeam.agents, agent.id]
                        : selectedTeam.agents.filter((a) => a !== agent.id),
                    )
                  }
                  className="h-5 w-5 sm:h-4 sm:w-4"
                />
                <label
                  htmlFor={`agent-${agent.id}`}
                  className="flex-1 cursor-pointer select-none"
                >
                  <div className="font-medium">{agent.display_name}</div>
                  <div className="text-sm text-gray-500 dark:text-gray-400">
                    {agent.role}
                  </div>
                  {eligibilityReason != null && (
                    <div className="text-xs text-amber-600 dark:text-amber-400">
                      {eligibilityReason}
                    </div>
                  )}
                </label>
              </div>
            );
          })}
        </div>
      </FieldGroup>

      {/* Rooms */}
      <FieldGroup
        label="Team Rooms"
        helperText="Select rooms where this team can operate"
        error={roomsError}
      >
        <CheckboxListField
          items={rooms.map((room) => ({
            value: room.id,
            label: room.display_name,
            description: room.description,
          }))}
          selected={selectedTeam.rooms}
          onChange={(next) => handleFieldChange("rooms", next)}
          idPrefix="room"
          emptyMessage="No rooms available. Create rooms in the Rooms tab."
        />
      </FieldGroup>

      <SchemaSection
        title="More settings"
        definition="TeamConfig"
        value={selectedTeam}
        path={["teams", selectedTeam.id]}
        exclude={TEAM_EDITOR_FIELDS}
        onFieldChange={(key, next) =>
          updateTeam(selectedTeam.id, { [key]: next } as Partial<Team>)
        }
      />
    </EditorPanel>
  );
}
