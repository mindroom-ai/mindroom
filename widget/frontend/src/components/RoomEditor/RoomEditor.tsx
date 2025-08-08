import { useEffect, useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Settings2, Trash2, Save, Users, Bot } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';

export function RoomEditor() {
  const { rooms, agents, config, selectedRoomId, updateRoom, deleteRoom, saveConfig, isDirty } =
    useConfigStore();

  const selectedRoom = rooms.find(r => r.id === selectedRoomId);
  const [localRoom, setLocalRoom] = useState(selectedRoom);

  useEffect(() => {
    setLocalRoom(selectedRoom);
  }, [selectedRoom]);

  if (!selectedRoom || !localRoom) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <Settings2 className="h-12 w-12 mx-auto mb-3 text-muted-foreground opacity-20" />
          <p className="text-muted-foreground">Select a room to edit</p>
        </div>
      </div>
    );
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
    <div className="h-full flex flex-col gap-4 p-4 overflow-y-auto">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Settings2 className="h-5 w-5" />
              Room Details
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleDelete}
                className="text-destructive"
              >
                <Trash2 className="h-4 w-4 mr-1" />
                Delete
              </Button>
              <Button variant="default" size="sm" onClick={saveConfig} disabled={!isDirty}>
                <Save className="h-4 w-4 mr-1" />
                Save
              </Button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label htmlFor="display-name">Display Name</Label>
            <Input
              id="display-name"
              value={localRoom.display_name}
              onChange={e => handleFieldChange('display_name', e.target.value)}
              placeholder="Room name"
            />
          </div>

          <div>
            <Label htmlFor="description">Description</Label>
            <Textarea
              id="description"
              value={localRoom.description || ''}
              onChange={e => handleFieldChange('description', e.target.value)}
              placeholder="Describe this room's purpose..."
              rows={3}
            />
          </div>

          <div>
            <Label htmlFor="room-model">Room Model (Optional)</Label>
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
            <p className="text-xs text-muted-foreground mt-1">
              Override the default model for agents and teams in this room
            </p>
          </div>
        </CardContent>
      </Card>

      <Card className="flex-1">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Agents in Room
          </CardTitle>
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-[300px]">
            <div className="space-y-2">
              {agents.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">
                  No agents available
                </p>
              ) : (
                agents.map(agent => (
                  <div
                    key={agent.id}
                    className="flex items-center space-x-3 p-2 rounded-lg hover:bg-muted/50"
                  >
                    <Checkbox
                      id={`agent-${agent.id}`}
                      checked={localRoom.agents.includes(agent.id)}
                      onCheckedChange={checked => handleAgentToggle(agent.id, checked as boolean)}
                    />
                    <label htmlFor={`agent-${agent.id}`} className="flex-1 cursor-pointer">
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
          </ScrollArea>
          <p className="text-xs text-muted-foreground mt-4">
            Select agents that should have access to this room. Their room list will be updated
            automatically.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
