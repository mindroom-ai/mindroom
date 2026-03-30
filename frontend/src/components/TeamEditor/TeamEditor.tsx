import { useEffect, useCallback, useMemo, useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { useSwipeBack } from '@/hooks/useSwipeBack';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Users } from 'lucide-react';
import { EditorPanel, EditorPanelEmptyState, FieldGroup } from '@/components/shared';
import { useForm, useWatch, Controller } from 'react-hook-form';
import { Team } from '@/types/config';
import { useScopedConfigValidation } from '@/hooks/useScopedConfigValidation';

const AGENT_POLICY_UNAVAILABLE_REASON =
  'Agent policy preview is unavailable. Save or refresh to validate team eligibility.';

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
    selectTeam,
  } = useConfigStore();

  const selectedTeam = teams.find(t => t.id === selectedTeamId);
  const validationPrefix = useMemo<Array<string | number> | null>(
    () => (selectedTeamId == null ? null : ['teams', selectedTeamId]),
    [selectedTeamId]
  );
  const validationErrorForPath = useScopedConfigValidation(validationPrefix);
  const teamRootError = validationErrorForPath([], true);
  const displayNameError = validationErrorForPath(['display_name'], true);
  const roleError = validationErrorForPath(['role'], true);
  const modeError = validationErrorForPath(['mode'], true);
  const modelError = validationErrorForPath(['model'], true);
  const numHistoryRunsError = validationErrorForPath(['num_history_runs'], true);
  const numHistoryMessagesError = validationErrorForPath(['num_history_messages'], true);
  const maxToolCallsFromHistoryError = validationErrorForPath(
    ['max_tool_calls_from_history'],
    true
  );
  const compactionError = validationErrorForPath(['compaction'], true);
  const membersError = validationErrorForPath(['agents']);
  const roomsError = validationErrorForPath(['rooms']);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectTeam(null),
    enabled: !!selectedTeamId && window.innerWidth < 1024,
  });

  const { control, reset, setValue, getValues } = useForm<Team>({
    defaultValues: selectedTeam || {
      id: '',
      display_name: '',
      role: '',
      agents: [],
      rooms: [],
      mode: 'coordinate',
      compaction: undefined,
    },
  });
  const numHistoryRuns = useWatch({ name: 'num_history_runs', control });
  const numHistoryMessages = useWatch({ name: 'num_history_messages', control });
  const compactionConfig = useWatch({ name: 'compaction', control });
  const [compactionThresholdPercentInput, setCompactionThresholdPercentInput] = useState('');

  // Reset form when selected team changes
  useEffect(() => {
    if (selectedTeam) {
      reset({
        ...selectedTeam,
        compaction: selectedTeam.compaction ?? undefined,
      });
    }
  }, [selectedTeam, reset]);

  // Let the store normalize against current state so sequential UI updates do not
  // reuse stale render-time team data.
  const handleFieldChange = useCallback(
    <K extends keyof Team>(fieldName: K, value: Team[K]) => {
      if (selectedTeamId) {
        updateTeam(selectedTeamId, { [fieldName]: value });
      }
    },
    [selectedTeamId, updateTeam]
  );

  const updateCompaction = useCallback(
    (nextCompaction: Team['compaction']) => {
      setValue('compaction', nextCompaction);
      handleFieldChange('compaction', nextCompaction);
    },
    [handleFieldChange, setValue]
  );

  const mutateCompaction = useCallback(
    (mutator: (current: Team['compaction']) => Team['compaction']) => {
      updateCompaction(mutator(getValues('compaction')));
    },
    [getValues, updateCompaction]
  );

  const parseOptionalInt = (raw: string): number | null => {
    const trimmed = raw.trim();
    if (trimmed === '') {
      return null;
    }
    return Number.parseInt(trimmed, 10);
  };

  const parseOptionalUnitFloat = (raw: string): number | null => {
    const trimmed = raw.trim();
    if (trimmed === '') {
      return null;
    }
    const value = Number.parseFloat(trimmed);
    if (!Number.isFinite(value) || value <= 0 || value >= 1) {
      return null;
    }
    return value;
  };

  useEffect(() => {
    setCompactionThresholdPercentInput(
      compactionConfig?.threshold_percent != null ? String(compactionConfig.threshold_percent) : ''
    );
  }, [compactionConfig?.threshold_percent, selectedTeamId]);

  const defaultCompaction = config?.defaults.compaction ?? null;
  const effectiveCompactionEnabled =
    compactionConfig != null
      ? compactionConfig.enabled ?? true
      : defaultCompaction?.enabled ?? false;

  const handleDelete = () => {
    if (selectedTeamId && confirm('Are you sure you want to delete this team?')) {
      deleteTeam(selectedTeamId);
    }
  };

  const handleSave = async () => {
    await saveConfig();
  };

  if (!selectedTeam) {
    return <EditorPanelEmptyState icon={Users} message="Select a team to edit" />;
  }

  return (
    <EditorPanel
      icon={Users}
      title="Team Details"
      isDirty={isDirty}
      onSave={handleSave}
      onDelete={handleDelete}
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
        <Controller
          name="display_name"
          control={control}
          render={({ field }) => (
            <Input
              {...field}
              id="display_name"
              placeholder="Team display name"
              onChange={e => {
                field.onChange(e);
                handleFieldChange('display_name', e.target.value);
              }}
            />
          )}
        />
      </FieldGroup>

      {/* Role */}
      <FieldGroup
        label="Team Purpose"
        helperText="Description of the team's purpose and what it does"
        htmlFor="role"
        error={roleError}
      >
        <Controller
          name="role"
          control={control}
          render={({ field }) => (
            <Textarea
              {...field}
              id="role"
              placeholder="What this team does..."
              rows={2}
              onChange={e => {
                field.onChange(e);
                handleFieldChange('role', e.target.value);
              }}
            />
          )}
        />
      </FieldGroup>

      {/* Collaboration Mode */}
      <FieldGroup
        label="Collaboration Mode"
        helperText="How agents work together: sequential (coordinate) or parallel (collaborate)"
        htmlFor="mode"
        error={modeError}
      >
        <Controller
          name="mode"
          control={control}
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={value => {
                field.onChange(value);
                handleFieldChange('mode', value as 'coordinate' | 'collaborate');
              }}
            >
              <SelectTrigger id="mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="coordinate">
                  Coordinate (Sequential - agents work one after another)
                </SelectItem>
                <SelectItem value="collaborate">
                  Collaborate (Parallel - agents work simultaneously)
                </SelectItem>
              </SelectContent>
            </Select>
          )}
        />
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Team Model (Optional)"
        helperText="Override model for all agents in this team"
        htmlFor="model"
        error={modelError}
      >
        <Controller
          name="model"
          control={control}
          render={({ field }) => (
            <Select
              value={field.value || 'default_model'}
              onValueChange={value => {
                const newValue = value === 'default_model' ? undefined : value;
                field.onChange(newValue);
                handleFieldChange('model', newValue);
              }}
            >
              <SelectTrigger id="model">
                <SelectValue placeholder="Use default model" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default_model">Use default model</SelectItem>
                {config &&
                  Object.keys(config.models).map(modelId => (
                    <SelectItem key={modelId} value={modelId}>
                      {modelId}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          )}
        />
      </FieldGroup>

      <div className="border-t border-gray-200 dark:border-gray-700 pt-4 mt-2">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-4">
          History & Context
        </h3>

        <div className="space-y-4">
          <FieldGroup
            label="History Runs"
            helperText={`Number of prior team-scoped runs to include as replay. Leave empty to use default${
              config?.defaults.num_history_runs != null
                ? ` (${config.defaults.num_history_runs})`
                : ' (all)'
            }.`}
            htmlFor="num_history_runs"
            error={numHistoryRunsError}
          >
            <Controller
              name="num_history_runs"
              control={control}
              render={({ field }) => (
                <Input
                  id="num_history_runs"
                  type="number"
                  min={0}
                  value={field.value ?? ''}
                  placeholder={
                    config?.defaults.num_history_runs != null
                      ? `Default: ${config.defaults.num_history_runs}`
                      : 'Default: all'
                  }
                  disabled={numHistoryMessages != null}
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value);
                    field.onChange(value);
                    handleFieldChange('num_history_runs', value);
                  }}
                />
              )}
            />
            {numHistoryMessages != null && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                Disabled because History Messages is set.
              </p>
            )}
          </FieldGroup>

          <FieldGroup
            label="History Messages"
            helperText={`Max replay messages from team-scoped history. Leave empty to use default${
              config?.defaults.num_history_messages != null
                ? ` (${config.defaults.num_history_messages})`
                : ' (all)'
            }.`}
            htmlFor="num_history_messages"
            error={numHistoryMessagesError}
          >
            <Controller
              name="num_history_messages"
              control={control}
              render={({ field }) => (
                <Input
                  id="num_history_messages"
                  type="number"
                  min={0}
                  value={field.value ?? ''}
                  placeholder={
                    config?.defaults.num_history_messages != null
                      ? `Default: ${config.defaults.num_history_messages}`
                      : 'Default: all'
                  }
                  disabled={numHistoryRuns != null}
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value);
                    field.onChange(value);
                    handleFieldChange('num_history_messages', value);
                  }}
                />
              )}
            />
            {numHistoryRuns != null && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                Disabled because History Runs is set.
              </p>
            )}
          </FieldGroup>

          <FieldGroup
            label="Max Tool Calls from History"
            helperText={`Max tool call messages replayed from team history. Leave empty to use default${
              config?.defaults.max_tool_calls_from_history != null
                ? ` (${config.defaults.max_tool_calls_from_history})`
                : ' (no limit)'
            }.`}
            htmlFor="max_tool_calls_from_history"
            error={maxToolCallsFromHistoryError}
          >
            <Controller
              name="max_tool_calls_from_history"
              control={control}
              render={({ field }) => (
                <Input
                  id="max_tool_calls_from_history"
                  type="number"
                  min={0}
                  value={field.value ?? ''}
                  placeholder={
                    config?.defaults.max_tool_calls_from_history != null
                      ? `Default: ${config.defaults.max_tool_calls_from_history}`
                      : 'Default: no limit'
                  }
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value);
                    field.onChange(value);
                    handleFieldChange('max_tool_calls_from_history', value);
                  }}
                />
              )}
            />
          </FieldGroup>

          <FieldGroup
            label="Auto-Compaction"
            helperText="Automatically compact older team-scoped history before the next run when the context budget gets tight."
            htmlFor="compaction_enabled"
            error={compactionError}
          >
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <Checkbox
                  id="compaction_enabled"
                  checked={effectiveCompactionEnabled}
                  onCheckedChange={checked => {
                    mutateCompaction(current => ({
                      ...(current ?? {}),
                      enabled: checked === true,
                    }));
                  }}
                />
                <label
                  htmlFor="compaction_enabled"
                  className="text-sm font-medium cursor-pointer select-none"
                >
                  Enable auto-compaction
                </label>
                {compactionConfig != null && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => updateCompaction(undefined)}
                  >
                    Use inherited settings
                  </Button>
                )}
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <FieldGroup
                  label="Threshold Tokens"
                  helperText="Absolute token threshold that triggers compaction."
                  htmlFor="compaction_threshold_tokens"
                >
                  <Input
                    id="compaction_threshold_tokens"
                    type="number"
                    min={1}
                    value={compactionConfig?.threshold_tokens ?? ''}
                    placeholder={
                      defaultCompaction?.threshold_tokens != null
                        ? `Default: ${defaultCompaction.threshold_tokens}`
                        : 'Default: derived from context window'
                    }
                    onChange={e => {
                      const value = parseOptionalInt(e.target.value);
                      mutateCompaction(current => ({
                        ...(current ?? {}),
                        threshold_tokens: value ?? undefined,
                      }));
                    }}
                  />
                </FieldGroup>

                <FieldGroup
                  label="Threshold Percent"
                  helperText="Fraction of the context window that triggers compaction, greater than 0 and less than 1."
                  htmlFor="compaction_threshold_percent"
                >
                  <Input
                    id="compaction_threshold_percent"
                    type="number"
                    min={0.01}
                    max={0.99}
                    step="0.01"
                    value={compactionThresholdPercentInput}
                    placeholder={
                      defaultCompaction?.threshold_percent != null
                        ? `Default: ${defaultCompaction.threshold_percent}`
                        : 'Default: 0.8'
                    }
                    onChange={e => {
                      const raw = e.target.value;
                      setCompactionThresholdPercentInput(raw);
                      const value = parseOptionalUnitFloat(raw);
                      if (raw.trim() !== '' && value == null) {
                        return;
                      }
                      mutateCompaction(current => ({
                        ...(current ?? {}),
                        threshold_percent: value ?? undefined,
                      }));
                    }}
                    onBlur={() => {
                      const value = parseOptionalUnitFloat(compactionThresholdPercentInput);
                      if (compactionThresholdPercentInput.trim() !== '' && value == null) {
                        setCompactionThresholdPercentInput(
                          compactionConfig?.threshold_percent != null
                            ? String(compactionConfig.threshold_percent)
                            : ''
                        );
                      }
                    }}
                  />
                </FieldGroup>

                <FieldGroup
                  label="Reserve Tokens"
                  helperText="Headroom reserved for the current prompt, tools, and model output."
                  htmlFor="compaction_reserve_tokens"
                >
                  <Input
                    id="compaction_reserve_tokens"
                    type="number"
                    min={0}
                    value={compactionConfig?.reserve_tokens ?? ''}
                    placeholder={`Default: ${defaultCompaction?.reserve_tokens ?? 16384}`}
                    onChange={e => {
                      const value = parseOptionalInt(e.target.value);
                      mutateCompaction(current => ({
                        ...(current ?? {}),
                        reserve_tokens: value ?? undefined,
                      }));
                    }}
                  />
                </FieldGroup>

                <FieldGroup
                  label="Compaction Model"
                  helperText="Optional model config name used only for summary generation during compaction."
                  htmlFor="compaction_model"
                >
                  <Input
                    id="compaction_model"
                    value={compactionConfig?.model ?? ''}
                    placeholder={defaultCompaction?.model ?? 'Default: team run model'}
                    onChange={e => {
                      const value = e.target.value.trim();
                      mutateCompaction(current => ({
                        ...(current ?? {}),
                        model: value === '' ? undefined : value,
                      }));
                    }}
                  />
                </FieldGroup>

                <FieldGroup
                  label="Notify Room"
                  helperText={`Post a room notice after compaction completes (default: ${
                    defaultCompaction?.notify ? 'on' : 'off'
                  }).`}
                  htmlFor="compaction_notify"
                >
                  <div className="flex items-center gap-2">
                    <Checkbox
                      id="compaction_notify"
                      checked={compactionConfig?.notify ?? defaultCompaction?.notify ?? false}
                      onCheckedChange={checked => {
                        mutateCompaction(current => ({
                          ...(current ?? {}),
                          notify: checked === true,
                        }));
                      }}
                    />
                    <label
                      htmlFor="compaction_notify"
                      className="text-sm font-medium cursor-pointer select-none"
                    >
                      Send compaction notices
                    </label>
                  </div>
                </FieldGroup>
              </div>
            </div>
          </FieldGroup>
        </div>
      </div>

      {/* Team Members (Agents) */}
      <FieldGroup
        label="Team Members"
        helperText="Select agents that compose this team"
        error={membersError}
      >
        <div className="space-y-2">
          {agents.map(agent => (
            <Controller
              key={agent.id}
              name="agents"
              control={control}
              render={({ field }) => {
                const isChecked = field.value.includes(agent.id);
                const agentPolicy = agentPoliciesByAgent[agent.id];
                const eligibilityReason =
                  agentPolicy?.team_eligibility_reason ??
                  (agentPolicy == null ? AGENT_POLICY_UNAVAILABLE_REASON : null);
                const isSelectable = agentPolicy != null && eligibilityReason == null;
                return (
                  <div className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200">
                    <Checkbox
                      id={`agent-${agent.id}`}
                      checked={isChecked}
                      disabled={!isSelectable && !isChecked}
                      onCheckedChange={checked => {
                        if (!isSelectable && checked === true) {
                          return;
                        }
                        const newAgents = checked
                          ? [...field.value, agent.id]
                          : field.value.filter(a => a !== agent.id);
                        field.onChange(newAgents);
                        handleFieldChange('agents', newAgents);
                      }}
                      className="h-5 w-5 sm:h-4 sm:w-4"
                    />
                    <label
                      htmlFor={`agent-${agent.id}`}
                      className="flex-1 cursor-pointer select-none"
                    >
                      <div className="font-medium">{agent.display_name}</div>
                      <div className="text-sm text-gray-500 dark:text-gray-400">{agent.role}</div>
                      {eligibilityReason != null && (
                        <div className="text-xs text-amber-600 dark:text-amber-400">
                          {eligibilityReason}
                        </div>
                      )}
                    </label>
                  </div>
                );
              }}
            />
          ))}
        </div>
      </FieldGroup>

      {/* Rooms */}
      <FieldGroup
        label="Team Rooms"
        helperText="Select rooms where this team can operate"
        error={roomsError}
      >
        <Controller
          name="rooms"
          control={control}
          render={({ field }) => (
            <div className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2">
              {rooms.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-2">
                  No rooms available. Create rooms in the Rooms tab.
                </p>
              ) : (
                rooms.map(room => {
                  const isChecked = field.value.includes(room.id);
                  return (
                    <div
                      key={room.id}
                      className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200"
                    >
                      <Checkbox
                        id={`room-${room.id}`}
                        checked={isChecked}
                        onCheckedChange={checked => {
                          const newRooms = checked
                            ? [...field.value, room.id]
                            : field.value.filter(r => r !== room.id);
                          field.onChange(newRooms);
                          handleFieldChange('rooms', newRooms);
                        }}
                        className="h-5 w-5 sm:h-4 sm:w-4"
                      />
                      <label
                        htmlFor={`room-${room.id}`}
                        className="flex-1 cursor-pointer select-none"
                      >
                        <div className="font-medium text-sm">{room.display_name}</div>
                        {room.description && (
                          <div className="text-xs text-gray-500 dark:text-gray-400">
                            {room.description}
                          </div>
                        )}
                      </label>
                    </div>
                  );
                })
              )}
            </div>
          )}
        />
      </FieldGroup>
    </EditorPanel>
  );
}
