import { useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Save, Trash2, Users } from 'lucide-react';
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
  } = useConfigStore();

  const selectedTeam = teams.find(t => t.id === selectedTeamId);

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
    return (
      <Card className="h-full flex items-center justify-center">
        <div className="text-gray-500 text-center">
          <Users className="h-12 w-12 mx-auto mb-2 text-gray-300" />
          <p>Select a team to edit</p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="h-full flex flex-col overflow-hidden">
      <CardHeader className="pb-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <CardTitle>Team Details</CardTitle>
          <div className="flex gap-2">
            <Button variant="destructive" size="sm" onClick={handleDelete}>
              <Trash2 className="h-4 w-4 mr-1" />
              Delete
            </Button>
            <Button variant="default" size="sm" onClick={handleSave} disabled={!isDirty}>
              <Save className="h-4 w-4 mr-1" />
              Save
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto min-h-0">
        <div className="space-y-4">
          {/* Display Name */}
          <div>
            <Label htmlFor="display_name">Display Name</Label>
            <p className="text-xs text-muted-foreground mb-1">Human-readable name for the team</p>
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
          </div>

          {/* Role */}
          <div>
            <Label htmlFor="role">Team Purpose</Label>
            <p className="text-xs text-muted-foreground mb-1">
              Description of the team's purpose and what it does
            </p>
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
          </div>

          {/* Collaboration Mode */}
          <div>
            <Label htmlFor="mode">Collaboration Mode</Label>
            <p className="text-xs text-muted-foreground mb-1">
              How agents work together: sequential (coordinate) or parallel (collaborate)
            </p>
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
          </div>

          {/* Model Selection */}
          <div>
            <Label htmlFor="model">Team Model (Optional)</Label>
            <p className="text-xs text-muted-foreground mb-1">
              Override model for all agents in this team
            </p>
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
          </div>

          {/* Team Members (Agents) */}
          <div>
            <Label>Team Members</Label>
            <p className="text-xs text-muted-foreground mb-1">
              Select agents that compose this team
            </p>
            <div className="space-y-2 mt-2">
              {agents.map(agent => (
                <Controller
                  key={agent.id}
                  name="agents"
                  control={control}
                  render={({ field }) => {
                    const isChecked = field.value.includes(agent.id);
                    return (
                      <div className="flex items-center space-x-2 p-2 rounded-lg hover:bg-gray-50">
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
                        />
                        <label htmlFor={`agent-${agent.id}`} className="flex-1 cursor-pointer">
                          <div className="font-medium">{agent.display_name}</div>
                          <div className="text-sm text-gray-500">{agent.role}</div>
                        </label>
                      </div>
                    );
                  }}
                />
              ))}
            </div>
          </div>

          {/* Rooms */}
          <div>
            <Label>Team Rooms</Label>
            <p className="text-xs text-muted-foreground mb-2">
              Select rooms where this team can operate
            </p>
            <Controller
              name="rooms"
              control={control}
              render={({ field }) => (
                <div className="space-y-2 mt-2 max-h-48 overflow-y-auto border rounded-lg p-2">
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
                          className="flex items-center space-x-2 p-2 rounded-lg hover:bg-gray-50"
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
                          />
                          <label htmlFor={`room-${room.id}`} className="flex-1 cursor-pointer">
                            <div className="font-medium text-sm">{room.display_name}</div>
                            {room.description && (
                              <div className="text-xs text-gray-500">{room.description}</div>
                            )}
                          </label>
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
