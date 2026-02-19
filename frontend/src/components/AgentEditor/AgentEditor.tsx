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
import {
  EditorPanel,
  EditorPanelEmptyState,
  FieldGroup,
  CheckboxListField,
  CheckboxListItem,
} from '@/components/shared';
import { useForm, useWatch, Controller } from 'react-hook-form';
import { Agent } from '@/types/config';
import { ToolConfigDialog } from '@/components/ToolConfig/ToolConfigDialog';
import { TOOL_SCHEMAS } from '@/types/toolConfig';
import { Badge } from '@/components/ui/badge';
import { useTools } from '@/hooks/useTools';
import { useSkills } from '@/hooks/useSkills';

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
  const defaultLearning = config?.defaults.learning ?? true;
  const defaultLearningMode = config?.defaults.learning_mode ?? 'always';
  const defaultShowToolCalls = config?.defaults.show_tool_calls ?? true;
  const knowledgeBaseNames = useMemo(
    () => Object.keys(config?.knowledge_bases || {}).sort(),
    [config?.knowledge_bases]
  );

  // Fetch tools and skills from backend
  const { tools: backendTools, loading: toolsLoading } = useTools();
  const { skills: availableSkills, loading: skillsLoading } = useSkills();

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
      // Exclude everything else (requires_config)
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
      skills: [],
      instructions: [],
      rooms: [],
      knowledge_bases: [],
      learning: defaultLearning,
      learning_mode: defaultLearningMode,
    },
  });
  const learningEnabled = useWatch({ name: 'learning', control });
  const effectiveLearningEnabled = learningEnabled ?? defaultLearning;

  // Prepare checkbox items for skills (includes orphaned selected skills)
  const skillItems: CheckboxListItem[] = useMemo(() => {
    const selected = selectedAgent?.skills ?? [];
    const availableByName = new Map(availableSkills.map(s => [s.name, s]));
    const allNames = [
      ...availableSkills.map(s => s.name),
      ...selected.filter(name => !availableByName.has(name)),
    ];
    return allNames.map(name => ({
      value: name,
      label: name,
      description:
        availableByName.get(name)?.description || 'Skill not available; uncheck to remove',
    }));
  }, [availableSkills, selectedAgent?.skills]);

  // Prepare checkbox items for rooms
  const roomItems: CheckboxListItem[] = useMemo(
    () =>
      rooms.map(room => ({
        value: room.id,
        label: room.display_name,
        description: room.description,
      })),
    [rooms]
  );
  const knowledgeBaseItems: CheckboxListItem[] = useMemo(
    () =>
      knowledgeBaseNames.map(baseName => ({
        value: baseName,
        label: baseName,
      })),
    [knowledgeBaseNames]
  );

  // Reset form when selected agent changes
  useEffect(() => {
    if (selectedAgent) {
      reset({
        ...selectedAgent,
        knowledge_bases: selectedAgent.knowledge_bases ?? [],
        learning: selectedAgent.learning ?? defaultLearning,
        learning_mode: selectedAgent.learning_mode ?? defaultLearningMode,
      });
    }
  }, [defaultLearning, defaultLearningMode, selectedAgent, reset]);

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

      {/* Knowledge Bases */}
      <FieldGroup
        label="Knowledge Bases"
        helperText="Assign one or more knowledge bases for this agent to search"
      >
        <CheckboxListField
          name="knowledge_bases"
          control={control}
          items={knowledgeBaseItems}
          fieldName="knowledge_bases"
          onFieldChange={handleFieldChange}
          idPrefix="knowledge-base"
          emptyMessage="No knowledge bases available. Add one in the Knowledge tab."
          className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2"
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
              No tools are available. Please configure tools in the Tools tab first.
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

      {/* Skills */}
      <FieldGroup label="Skills" helperText="Select skills this agent can invoke">
        {skillsLoading ? (
          <div className="text-sm text-muted-foreground text-center py-2">Loading skills...</div>
        ) : (
          <CheckboxListField
            name="skills"
            control={control}
            items={skillItems}
            fieldName="skills"
            onFieldChange={handleFieldChange}
            idPrefix="skill"
            emptyMessage="No skills available. Create skills in the Skills tab first."
          />
        )}
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
        <CheckboxListField
          name="rooms"
          control={control}
          items={roomItems}
          fieldName="rooms"
          onFieldChange={handleFieldChange}
          idPrefix="room"
          emptyMessage="No rooms available. Create rooms in the Rooms tab."
          className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2"
        />
      </FieldGroup>

      {/* Learning */}
      <FieldGroup
        label="Learning"
        helperText="Enable Agno Learning so this agent can learn from conversations"
        htmlFor="learning"
      >
        <Controller
          name="learning"
          control={control}
          render={({ field }) => (
            <div className="flex items-center gap-2">
              <Checkbox
                id="learning"
                checked={field.value ?? defaultLearning}
                onCheckedChange={checked => {
                  const value = checked === true;
                  field.onChange(value);
                  handleFieldChange('learning', value);
                }}
              />
              <label htmlFor="learning" className="text-sm font-medium cursor-pointer select-none">
                Enable learning
              </label>
            </div>
          )}
        />
      </FieldGroup>

      <FieldGroup
        label="Learning Mode"
        helperText="Always: automatic extraction. Agentic: agent decides via tools."
        htmlFor="learning_mode"
      >
        <Controller
          name="learning_mode"
          control={control}
          render={({ field }) => (
            <Select
              value={field.value ?? defaultLearningMode}
              onValueChange={value => {
                field.onChange(value);
                handleFieldChange('learning_mode', value);
              }}
              disabled={effectiveLearningEnabled === false}
            >
              <SelectTrigger id="learning_mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="always">Always (automatic)</SelectItem>
                <SelectItem value="agentic">Agentic (tool-driven)</SelectItem>
              </SelectContent>
            </Select>
          )}
        />
      </FieldGroup>

      {/* Show Tool Calls */}
      <FieldGroup
        label="Show Tool Calls"
        helperText="Display tool call details inline in agent responses"
        htmlFor="show_tool_calls"
      >
        <Controller
          name="show_tool_calls"
          control={control}
          render={({ field }) => (
            <div className="flex items-center gap-2">
              <Checkbox
                id="show_tool_calls"
                checked={field.value ?? defaultShowToolCalls}
                onCheckedChange={checked => {
                  const value = checked === true;
                  field.onChange(value);
                  handleFieldChange('show_tool_calls', value);
                }}
              />
              <label
                htmlFor="show_tool_calls"
                className="text-sm font-medium cursor-pointer select-none"
              >
                Show tool calls inline
              </label>
            </div>
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
