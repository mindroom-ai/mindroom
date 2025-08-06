import React, { useEffect } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Save, Trash2, Plus, X, FileCode } from 'lucide-react';
import { AVAILABLE_TOOLS } from '@/types/config';
import { useForm, Controller } from 'react-hook-form';
import { Agent } from '@/types/config';

export function AgentEditor() {
  const {
    agents,
    selectedAgentId,
    updateAgent,
    deleteAgent,
    saveConfig,
    config,
    isDirty
  } = useConfigStore();

  const selectedAgent = agents.find(a => a.id === selectedAgentId);

  const { control, reset, watch, setValue, getValues } = useForm<Agent>({
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

  // Watch form changes and update store
  useEffect(() => {
    const subscription = watch((value) => {
      if (selectedAgentId && value) {
        updateAgent(selectedAgentId, value as Partial<Agent>);
      }
    });
    return () => subscription.unsubscribe();
  }, [watch, selectedAgentId, updateAgent]);

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
    setValue('instructions', [...current, '']);
  };

  const handleRemoveInstruction = (index: number) => {
    const current = getValues('instructions');
    setValue('instructions', current.filter((_, i) => i !== index));
  };

  const handleAddRoom = () => {
    const current = getValues('rooms');
    setValue('rooms', [...current, 'new_room']);
  };

  const handleRemoveRoom = (index: number) => {
    const current = getValues('rooms');
    setValue('rooms', current.filter((_, i) => i !== index));
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
    <Card className="h-full overflow-hidden flex flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle>Agent Details</CardTitle>
          <div className="flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              onClick={handleDelete}
            >
              <Trash2 className="h-4 w-4 mr-1" />
              Delete
            </Button>
            <Button
              variant="default"
              size="sm"
              onClick={handleSave}
              disabled={!isDirty}
            >
              <Save className="h-4 w-4 mr-1" />
              Save
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto">
        <div className="space-y-4">
          {/* Display Name */}
          <div>
            <Label htmlFor="display_name">Display Name</Label>
            <Controller
              name="display_name"
              control={control}
              render={({ field }) => (
                <Input {...field} id="display_name" placeholder="Agent display name" />
              )}
            />
          </div>

          {/* Role */}
          <div>
            <Label htmlFor="role">Role Description</Label>
            <Controller
              name="role"
              control={control}
              render={({ field }) => (
                <Textarea
                  {...field}
                  id="role"
                  placeholder="What this agent does..."
                  rows={2}
                />
              )}
            />
          </div>

          {/* Model Selection */}
          <div>
            <Label htmlFor="model">Model</Label>
            <Controller
              name="model"
              control={control}
              render={({ field }) => (
                <Select value={field.value || 'default'} onValueChange={field.onChange}>
                  <SelectTrigger id="model">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {config && Object.keys(config.models).map((modelId) => (
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
            <div className="grid grid-cols-2 gap-2 mt-2">
              {AVAILABLE_TOOLS.map((tool) => (
                <Controller
                  key={tool}
                  name="tools"
                  control={control}
                  render={({ field }) => {
                    const isChecked = field.value.includes(tool);
                    return (
                      <div className="flex items-center space-x-2">
                        <Checkbox
                          id={tool}
                          checked={isChecked}
                          onCheckedChange={(checked) => {
                            if (checked) {
                              field.onChange([...field.value, tool]);
                            } else {
                              field.onChange(field.value.filter(t => t !== tool));
                            }
                          }}
                        />
                        <label
                          htmlFor={tool}
                          className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70"
                        >
                          {tool}
                        </label>
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
              <Label>Instructions</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={handleAddInstruction}
              >
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
                        onChange={(e) => {
                          const updated = [...field.value];
                          updated[index] = e.target.value;
                          field.onChange(updated);
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
            <div className="flex items-center justify-between mb-2">
              <Label>Rooms</Label>
              <Button
                variant="outline"
                size="sm"
                onClick={handleAddRoom}
              >
                <Plus className="h-3 w-3 mr-1" />
                Add
              </Button>
            </div>
            <Controller
              name="rooms"
              control={control}
              render={({ field }) => (
                <div className="space-y-2">
                  {field.value.map((room, index) => (
                    <div key={index} className="flex gap-2">
                      <Input
                        value={room}
                        onChange={(e) => {
                          const updated = [...field.value];
                          updated[index] = e.target.value;
                          field.onChange(updated);
                        }}
                        placeholder="Room name..."
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleRemoveRoom(index)}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            />
          </div>

          {/* History Runs */}
          <div>
            <Label htmlFor="num_history_runs">History Runs</Label>
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
                  onChange={(e) => field.onChange(parseInt(e.target.value) || 5)}
                />
              )}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
