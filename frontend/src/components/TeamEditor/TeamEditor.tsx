import { useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { useSwipeBack } from '@/hooks/useSwipeBack';
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
import { useForm, Controller } from 'react-hook-form';
import { Team } from '@/types/config';

export function TeamEditor() {
  const {
    teams,
    agents,
    rooms,
    selectedTeamId,
    updateTeam,
    deleteTeam,
    saveConfig,
    config,
    isDirty,
    selectTeam,
  } = useConfigStore();

  const selectedTeam = teams.find(t => t.id === selectedTeamId);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectTeam(null),
    enabled: !!selectedTeamId && window.innerWidth < 1024,
  });

  const { control, reset } = useForm<Team>({
    defaultValues: selectedTeam || {
      id: '',
      display_name: '',
      role: '',
      agents: [],
      rooms: [],
      mode: 'coordinate',
    },
  });

  // Reset form when selected team changes
  useEffect(() => {
    if (selectedTeam) {
      reset(selectedTeam);
    }
  }, [selectedTeam, reset]);

  // Create a debounced update function
  const handleFieldChange = useCallback(
    (fieldName: keyof Team, value: any) => {
      if (selectedTeamId) {
        updateTeam(selectedTeamId, { [fieldName]: value });
      }
    },
    [selectedTeamId, updateTeam]
  );

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
      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the team"
        htmlFor="display_name"
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

      {/* Team Members (Agents) */}
      <FieldGroup label="Team Members" helperText="Select agents that compose this team">
        <div className="space-y-2">
          {agents.map(agent => (
            <Controller
              key={agent.id}
              name="agents"
              control={control}
              render={({ field }) => {
                const isChecked = field.value.includes(agent.id);
                return (
                  <div className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200">
                    <Checkbox
                      id={`agent-${agent.id}`}
                      checked={isChecked}
                      onCheckedChange={checked => {
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
                    </label>
                  </div>
                );
              }}
            />
          ))}
        </div>
      </FieldGroup>

      {/* Rooms */}
      <FieldGroup label="Team Rooms" helperText="Select rooms where this team can operate">
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
