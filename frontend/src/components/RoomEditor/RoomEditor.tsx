import { useEffect, useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { useSwipeBack } from '@/hooks/useSwipeBack';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Home, Bot } from 'lucide-react';
import { EditorPanel, EditorPanelEmptyState, FieldGroup } from '@/components/shared';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export function RoomEditor() {
  const {
    rooms,
    agents,
    config,
    selectedRoomId,
    updateRoom,
    deleteRoom,
    saveConfig,
    isDirty,
    selectRoom,
  } = useConfigStore();

  const selectedRoom = rooms.find(r => r.id === selectedRoomId);
  const [localRoom, setLocalRoom] = useState(selectedRoom);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectRoom(null),
    enabled: !!selectedRoomId && window.innerWidth < 1024,
  });

  useEffect(() => {
    setLocalRoom(selectedRoom);
  }, [selectedRoom]);

  if (!selectedRoom || !localRoom) {
    return <EditorPanelEmptyState icon={Home} message="Select a room to edit" />;
  }

  const handleFieldChange = (field: string, value: any) => {
    setLocalRoom({ ...localRoom, [field]: value });
    updateRoom(selectedRoom.id, { [field]: value });
  };

  const handleAgentToggle = (agentId: string, checked: boolean) => {
    const newAgents = checked
      ? [...localRoom.agents, agentId]
      : localRoom.agents.filter(id => id !== agentId);

    setLocalRoom({ ...localRoom, agents: newAgents });
    updateRoom(selectedRoom.id, { agents: newAgents });
  };

  const handleDelete = () => {
    if (confirm('Are you sure you want to delete this room?')) {
      deleteRoom(selectedRoom.id);
    }
  };

  const modelOptions = Object.keys(config?.models || {});

  return (
    <EditorPanel
      icon={Home}
      title="Room Details"
      isDirty={isDirty}
      onSave={saveConfig}
      onDelete={handleDelete}
      onBack={() => selectRoom(null)}
    >
      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the room"
        htmlFor="display-name"
      >
        <Input
          id="display-name"
          value={localRoom.display_name}
          onChange={e => handleFieldChange('display_name', e.target.value)}
          placeholder="Room name"
        />
      </FieldGroup>

      {/* Description */}
      <FieldGroup
        label="Description"
        helperText="Describe this room's purpose"
        htmlFor="description"
      >
        <Textarea
          id="description"
          value={localRoom.description || ''}
          onChange={e => handleFieldChange('description', e.target.value)}
          placeholder="Describe this room's purpose..."
          rows={3}
        />
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Room Model (Optional)"
        helperText="Override the default model for agents and teams in this room"
        htmlFor="room-model"
      >
        <Select
          value={localRoom.model || 'default_model'}
          onValueChange={value => {
            const newValue = value === 'default_model' ? undefined : value;
            handleFieldChange('model', newValue);
          }}
        >
          <SelectTrigger id="room-model">
            <SelectValue placeholder="Select a model" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default_model">Use default model</SelectItem>
            {modelOptions.map(modelId => (
              <SelectItem key={modelId} value={modelId}>
                {modelId}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Agents in Room */}
      <FieldGroup
        label="Agents in Room"
        helperText="Select agents that should have access to this room. Their room list will be updated automatically."
      >
        <div className="space-y-2 max-h-[300px] overflow-y-auto border rounded-lg p-2">
          {agents.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">No agents available</p>
          ) : (
            agents.map(agent => (
              <div
                key={agent.id}
                className="flex items-center space-x-3 p-3 sm:p-2 rounded-lg hover:bg-muted/50"
              >
                <Checkbox
                  id={`agent-${agent.id}`}
                  checked={localRoom.agents.includes(agent.id)}
                  onCheckedChange={checked => handleAgentToggle(agent.id, checked as boolean)}
                  className="h-5 w-5 sm:h-4 sm:w-4"
                />
                <label htmlFor={`agent-${agent.id}`} className="flex-1 cursor-pointer select-none">
                  <div className="flex items-center gap-2">
                    <Bot className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <div className="font-medium text-sm">{agent.display_name}</div>
                      <div className="text-xs text-muted-foreground">{agent.role}</div>
                    </div>
                  </div>
                </label>
              </div>
            ))
          )}
        </div>
      </FieldGroup>
    </EditorPanel>
  );
}
