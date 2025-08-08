import { useEffect, useCallback, useState } from 'react';
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
import { Save, Trash2, Plus, X, FileCode, Settings } from 'lucide-react';
import { AVAILABLE_TOOLS } from '@/types/config';
import { useForm, Controller } from 'react-hook-form';
import { Agent } from '@/types/config';
import { ToolConfigDialog } from '@/components/ToolConfig/ToolConfigDialog';
import { TOOL_SCHEMAS, toolNeedsConfiguration } from '@/types/toolConfig';
import { Badge } from '@/components/ui/badge';

export function AgentEditor() {
  const { agents, rooms, selectedAgentId, updateAgent, deleteAgent, saveConfig, config, isDirty } =
    useConfigStore();

  const [configDialogTool, setConfigDialogTool] = useState<string | null>(null);
  const selectedAgent = agents.find(a => a.id === selectedAgentId);

  const { control, reset, setValue, getValues } = useForm<Agent>({
    defaultValues: selectedAgent || {
      id: '',
      display_name: '',
      role: '',
      tools: [],
      instructions: [],
      rooms: [],
      num_history_runs: 5,
    },
  });

  // Reset form when selected agent changes
  useEffect(() => {
    if (selectedAgent) {
      reset(selectedAgent);
    }
  }, [selectedAgent, reset]);

  // Create a debounced update function
  const handleFieldChange = useCallback(
    (fieldName: keyof Agent, value: any) => {
      if (selectedAgentId) {
        updateAgent(selectedAgentId, { [fieldName]: value });
      }
    },
    [selectedAgentId, updateAgent]
  );

  const handleDelete = () => {
    if (selectedAgentId && confirm('Are you sure you want to delete this agent?')) {
      deleteAgent(selectedAgentId);
    }
  };

  const handleSave = async () => {
    await saveConfig();
  };

  const handleAddInstruction = () => {
    const current = getValues('instructions');
    const updated = [...current, ''];
    setValue('instructions', updated);
    handleFieldChange('instructions', updated);
  };

  const handleRemoveInstruction = (index: number) => {
    const current = getValues('instructions');
    const updated = current.filter((_, i) => i !== index);
    setValue('instructions', updated);
    handleFieldChange('instructions', updated);
  };

  if (!selectedAgent) {
    return (
      <Card className="h-full flex items-center justify-center">
        <div className="text-gray-500 text-center">
          <FileCode className="h-12 w-12 mx-auto mb-2 text-gray-300" />
          <p>Select an agent to edit</p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="h-full flex flex-col overflow-hidden">
      <CardHeader className="pb-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <CardTitle>Agent Details</CardTitle>
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
            <p className="text-xs text-muted-foreground mb-1">Human-readable name for the agent</p>
            <Controller
              name="display_name"
              control={control}
              render={({ field }) => (
                <Input
                  {...field}
                  id="display_name"
                  placeholder="Agent display name"
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
            <Label htmlFor="role">Role Description</Label>
            <p className="text-xs text-muted-foreground mb-1">
              Description of the agent's purpose and capabilities
            </p>
            <Controller
              name="role"
              control={control}
              render={({ field }) => (
                <Textarea
                  {...field}
                  id="role"
                  placeholder="What this agent does..."
                  rows={2}
                  onChange={e => {
                    field.onChange(e);
                    handleFieldChange('role', e.target.value);
                  }}
                />
              )}
            />
          </div>

          {/* Model Selection */}
          <div>
            <Label htmlFor="model">Model</Label>
            <p className="text-xs text-muted-foreground mb-1">
              AI model to use (defaults to 'default' model if not specified)
            </p>
            <Controller
              name="model"
              control={control}
              render={({ field }) => (
                <Select
                  value={field.value || 'default'}
                  onValueChange={value => {
                    field.onChange(value);
                    handleFieldChange('model', value);
                  }}
                >
                  <SelectTrigger id="model">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
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

          {/* Tools */}
          <div>
            <Label>Tools</Label>
            <p className="text-xs text-muted-foreground mb-1">Available tools this agent can use</p>
            <div className="space-y-2 mt-2">
              {AVAILABLE_TOOLS.map(tool => (
                <Controller
                  key={tool}
                  name="tools"
                  control={control}
                  render={({ field }) => {
                    const isChecked = field.value.includes(tool);
                    const hasSchema = !!TOOL_SCHEMAS[tool];
                    const needsConfig = toolNeedsConfiguration(tool);
                    const isConfigured =
                      config?.tools?.[tool] && Object.keys(config.tools[tool]).length > 0;

                    return (
                      <div className="flex items-center justify-between p-2 rounded-lg hover:bg-gray-50 transition-colors">
                        <div className="flex items-center space-x-2">
                          <Checkbox
                            id={tool}
                            checked={isChecked}
                            onCheckedChange={checked => {
                              const newTools = checked
                                ? [...field.value, tool]
                                : field.value.filter(t => t !== tool);
                              field.onChange(newTools);
                              handleFieldChange('tools', newTools);
                            }}
                          />
                          <label
                            htmlFor={tool}
                            className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer"
                          >
                            {TOOL_SCHEMAS[tool]?.name || tool}
                          </label>
                          {isChecked && needsConfig && !isConfigured && (
                            <Badge variant="destructive" className="text-xs">
                              Needs Config
                            </Badge>
                          )}
                          {isChecked && isConfigured && (
                            <Badge
                              variant="default"
                              className="text-xs bg-green-100 text-green-800"
                            >
                              Configured
                            </Badge>
                          )}
                        </div>
                        {isChecked && hasSchema && (needsConfig || isConfigured) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setConfigDialogTool(tool)}
                            className="h-7 px-2"
                          >
                            <Settings className="h-3 w-3 mr-1" />
                            Configure
                          </Button>
                        )}
                      </div>
                    );
                  }}
                />
              ))}
            </div>
          </div>

          {/* Instructions */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <div>
                <Label>Instructions</Label>
                <p className="text-xs text-muted-foreground">Custom instructions for this agent</p>
              </div>
              <Button variant="outline" size="sm" onClick={handleAddInstruction}>
                <Plus className="h-3 w-3 mr-1" />
                Add
              </Button>
            </div>
            <Controller
              name="instructions"
              control={control}
              render={({ field }) => (
                <div className="space-y-2">
                  {field.value.map((instruction, index) => (
                    <div key={index} className="flex gap-2">
                      <Input
                        value={instruction}
                        onChange={e => {
                          const updated = [...field.value];
                          updated[index] = e.target.value;
                          field.onChange(updated);
                          handleFieldChange('instructions', updated);
                        }}
                        placeholder="Instruction..."
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleRemoveInstruction(index)}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            />
          </div>

          {/* Rooms */}
          <div>
            <Label>Agent Rooms</Label>
            <p className="text-xs text-muted-foreground mb-2">
              Select rooms where this agent can operate
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

          {/* History Runs */}
          <div>
            <Label htmlFor="num_history_runs">History Runs</Label>
            <p className="text-xs text-muted-foreground mb-1">
              Number of previous conversation turns to include as context
            </p>
            <Controller
              name="num_history_runs"
              control={control}
              render={({ field }) => (
                <Input
                  {...field}
                  id="num_history_runs"
                  type="number"
                  min={1}
                  max={20}
                  onChange={e => {
                    const value = parseInt(e.target.value) || 5;
                    field.onChange(value);
                    handleFieldChange('num_history_runs', value);
                  }}
                />
              )}
            />
          </div>
        </div>
      </CardContent>

      {/* Tool Configuration Dialog */}
      {configDialogTool && (
        <ToolConfigDialog
          toolId={configDialogTool}
          open={!!configDialogTool}
          onOpenChange={open => {
            if (!open) setConfigDialogTool(null);
          }}
        />
      )}
    </Card>
  );
}
