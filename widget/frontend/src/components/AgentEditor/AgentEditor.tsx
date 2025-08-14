import { useEffect, useCallback, useState, useMemo } from 'react';
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
import { Plus, X, Bot, Settings } from 'lucide-react';
import { EditorPanel, EditorPanelEmptyState, FieldGroup } from '@/components/shared';
import { useForm, Controller } from 'react-hook-form';
import { Agent } from '@/types/config';
import { ToolConfigDialog } from '@/components/ToolConfig/ToolConfigDialog';
import { TOOL_SCHEMAS } from '@/types/toolConfig';
import { Badge } from '@/components/ui/badge';
import { useTools } from '@/hooks/useTools';

export function AgentEditor() {
  const {
    agents,
    rooms,
    selectedAgentId,
    updateAgent,
    deleteAgent,
    saveConfig,
    config,
    isDirty,
    selectAgent,
  } = useConfigStore();

  const [configDialogTool, setConfigDialogTool] = useState<string | null>(null);
  const selectedAgent = agents.find(a => a.id === selectedAgentId);

  // Fetch tools from backend
  const { tools: backendTools, loading: toolsLoading } = useTools();

  // Split tools into configured and unconfigured (but usable) categories
  const { configuredTools, unconfiguredTools } = useMemo(() => {
    const configured: typeof backendTools = [];
    const unconfigured: typeof backendTools = [];

    backendTools.forEach(tool => {
      // Tools that don't require configuration are "unconfigured but usable"
      if (tool.setup_type === 'none') {
        unconfigured.push(tool);
      }
      // Tools that are configured
      else if (tool.status === 'available') {
        configured.push(tool);
      }
      // Exclude everything else (requires_config, coming_soon)
    });

    return {
      configuredTools: configured.sort((a, b) => a.display_name.localeCompare(b.display_name)),
      unconfiguredTools: unconfigured.sort((a, b) => a.display_name.localeCompare(b.display_name)),
    };
  }, [backendTools]);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectAgent(null),
    enabled: !!selectedAgentId && window.innerWidth < 1024, // Only on mobile when agent is selected
  });

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
    return <EditorPanelEmptyState icon={Bot} message="Select an agent to edit" />;
  }

  return (
    <EditorPanel
      icon={Bot}
      title="Agent Details"
      isDirty={isDirty}
      onSave={handleSave}
      onDelete={handleDelete}
      onBack={() => selectAgent(null)}
    >
      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the agent"
        htmlFor="display_name"
      >
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
      </FieldGroup>

      {/* Role */}
      <FieldGroup
        label="Role Description"
        helperText="Description of the agent's purpose and capabilities"
        htmlFor="role"
      >
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
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Model"
        helperText="AI model to use (defaults to 'default' model if not specified)"
        htmlFor="model"
      >
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
      </FieldGroup>

      {/* Tools */}
      <FieldGroup label="Tools" helperText="Select tools this agent can use">
        <div className="space-y-4">
          {toolsLoading ? (
            <div className="text-sm text-muted-foreground text-center py-4">
              Loading available tools...
            </div>
          ) : configuredTools.length === 0 && unconfiguredTools.length === 0 ? (
            <div className="text-sm text-muted-foreground text-center py-4">
              No tools are available. Please configure tools in the Integrations tab first.
            </div>
          ) : (
            <>
              {/* Configured Tools Section */}
              {configuredTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Configured Tools
                    </h4>
                    <Badge
                      variant="default"
                      className="text-xs bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                    >
                      {configuredTools.length}
                    </Badge>
                  </div>
                  <div className="pl-2 space-y-1">
                    {configuredTools.map(tool => (
                      <Controller
                        key={tool.name}
                        name="tools"
                        control={control}
                        render={({ field }) => {
                          const isChecked = field.value.includes(tool.name);
                          const hasSchema = !!TOOL_SCHEMAS[tool.name];
                          const needsConfig =
                            tool.setup_type !== 'none' &&
                            tool.config_fields &&
                            tool.config_fields.length > 0;

                          return (
                            <div className="flex items-center justify-between p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-colors">
                              <div className="flex items-center space-x-3 sm:space-x-2">
                                <Checkbox
                                  id={`configured-${tool.name}`}
                                  checked={isChecked}
                                  onCheckedChange={checked => {
                                    const newTools = checked
                                      ? [...field.value, tool.name]
                                      : field.value.filter(t => t !== tool.name);
                                    field.onChange(newTools);
                                    handleFieldChange('tools', newTools);
                                  }}
                                  className="h-5 w-5 sm:h-4 sm:w-4"
                                />
                                <label
                                  htmlFor={`configured-${tool.name}`}
                                  className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer select-none"
                                >
                                  {tool.display_name}
                                </label>
                              </div>
                              {isChecked && hasSchema && needsConfig && (
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => setConfigDialogTool(tool.name)}
                                  className="h-8 px-2"
                                >
                                  <Settings className="h-4 w-4 sm:mr-1" />
                                  <span className="hidden sm:inline">Settings</span>
                                </Button>
                              )}
                            </div>
                          );
                        }}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Divider if both sections have content */}
              {configuredTools.length > 0 && unconfiguredTools.length > 0 && (
                <div className="border-t border-gray-200 dark:border-gray-700" />
              )}

              {/* Unconfigured but Usable Tools Section */}
              {unconfiguredTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Default Tools
                    </h4>
                    <Badge variant="secondary" className="text-xs">
                      {unconfiguredTools.length}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      (work without configuration)
                    </span>
                  </div>
                  <div className="pl-2 space-y-1">
                    {unconfiguredTools.map(tool => (
                      <Controller
                        key={tool.name}
                        name="tools"
                        control={control}
                        render={({ field }) => {
                          const isChecked = field.value.includes(tool.name);

                          return (
                            <div className="flex items-center justify-between p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-colors">
                              <div className="flex items-center space-x-3 sm:space-x-2">
                                <Checkbox
                                  id={`default-${tool.name}`}
                                  checked={isChecked}
                                  onCheckedChange={checked => {
                                    const newTools = checked
                                      ? [...field.value, tool.name]
                                      : field.value.filter(t => t !== tool.name);
                                    field.onChange(newTools);
                                    handleFieldChange('tools', newTools);
                                  }}
                                  className="h-5 w-5 sm:h-4 sm:w-4"
                                />
                                <label
                                  htmlFor={`default-${tool.name}`}
                                  className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer select-none"
                                >
                                  {tool.display_name}
                                </label>
                              </div>
                            </div>
                          );
                        }}
                      />
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </FieldGroup>

      {/* Instructions */}
      <FieldGroup
        label="Instructions"
        helperText="Custom instructions for this agent"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={handleAddInstruction}
            data-testid="add-instruction-button"
            className="h-9 px-3"
          >
            <Plus className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Add</span>
          </Button>
        }
      >
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
                    className="min-h-[40px]"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => handleRemoveInstruction(index)}
                    className="h-10 w-10 flex-shrink-0"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        />
      </FieldGroup>

      {/* Rooms */}
      <FieldGroup label="Agent Rooms" helperText="Select rooms where this agent can operate">
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
                      className="flex items-center space-x-2 p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200"
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

      {/* History Runs */}
      <FieldGroup
        label="History Runs"
        helperText="Number of previous conversation turns to include as context"
        htmlFor="num_history_runs"
      >
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
      </FieldGroup>

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
    </EditorPanel>
  );
}
