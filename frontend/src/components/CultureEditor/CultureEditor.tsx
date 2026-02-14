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
import { Sparkles } from 'lucide-react';
import { EditorPanel, EditorPanelEmptyState, FieldGroup } from '@/components/shared';
import { useForm, Controller } from 'react-hook-form';
import { Culture } from '@/types/config';
import { useToast } from '@/components/ui/use-toast';

export function CultureEditor() {
  const { toast } = useToast();
  const {
    cultures,
    agents,
    selectedCultureId,
    updateCulture,
    deleteCulture,
    saveConfig,
    isDirty,
    selectCulture,
  } = useConfigStore();

  const selectedCulture = cultures.find(culture => culture.id === selectedCultureId);

  useSwipeBack({
    onSwipeBack: () => selectCulture(null),
    enabled: !!selectedCultureId && window.innerWidth < 1024,
  });

  const { control, reset } = useForm<Culture>({
    defaultValues: selectedCulture || {
      id: '',
      description: '',
      mode: 'automatic',
      agents: [],
    },
  });

  useEffect(() => {
    if (selectedCulture) {
      reset(selectedCulture);
    }
  }, [selectedCulture, reset]);

  const handleFieldChange = useCallback(
    (fieldName: keyof Culture, value: any) => {
      if (selectedCultureId) {
        updateCulture(selectedCultureId, { [fieldName]: value });
      }
    },
    [selectedCultureId, updateCulture]
  );

  const handleDelete = () => {
    if (selectedCultureId && confirm('Are you sure you want to delete this culture?')) {
      deleteCulture(selectedCultureId);
    }
  };

  const handleSave = async () => {
    await saveConfig();
  };

  if (!selectedCulture) {
    return <EditorPanelEmptyState icon={Sparkles} message="Select a culture to edit" />;
  }

  return (
    <EditorPanel
      icon={Sparkles}
      title="Culture Details"
      isDirty={isDirty}
      onSave={handleSave}
      onDelete={handleDelete}
      onBack={() => selectCulture(null)}
    >
      <FieldGroup
        label="Culture ID"
        helperText="Unique identifier for this culture"
        htmlFor="culture_id"
      >
        <Input id="culture_id" value={selectedCulture.id} disabled readOnly className="bg-muted" />
      </FieldGroup>

      <FieldGroup
        label="Description"
        helperText="Shared principles and best practices for this culture"
        htmlFor="description"
      >
        <Controller
          name="description"
          control={control}
          render={({ field }) => (
            <Textarea
              {...field}
              id="description"
              placeholder="Shared engineering principles, standards, and best practices..."
              rows={3}
              onChange={e => {
                field.onChange(e);
                handleFieldChange('description', e.target.value);
              }}
            />
          )}
        />
      </FieldGroup>

      <FieldGroup
        label="Mode"
        helperText="Automatic updates after runs, agentic updates via tool, or read-only manual mode"
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
                handleFieldChange('mode', value as Culture['mode']);
              }}
            >
              <SelectTrigger id="mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="automatic">Automatic (capture after every run)</SelectItem>
                <SelectItem value="agentic">Agentic (explicit tool-driven updates)</SelectItem>
                <SelectItem value="manual">Manual (read-only culture context)</SelectItem>
              </SelectContent>
            </Select>
          )}
        />
      </FieldGroup>

      <FieldGroup
        label="Assigned Agents"
        helperText="Select agents that share this culture (an agent can belong to only one culture)"
      >
        <Controller
          name="agents"
          control={control}
          render={({ field }) => (
            <div className="space-y-2 max-h-56 overflow-y-auto border rounded-lg p-2">
              {agents.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-2">
                  No agents available. Create agents in the Agents tab.
                </p>
              ) : (
                agents.map(agent => {
                  const isChecked = field.value.includes(agent.id);
                  const assignedCulture = cultures.find(culture =>
                    culture.agents.includes(agent.id)
                  );
                  const assignedCultureLabel = !assignedCulture
                    ? 'Currently in: none'
                    : assignedCulture.id === selectedCulture.id
                      ? 'Currently in: this culture'
                      : `Currently in: ${assignedCulture.id}`;
                  return (
                    <div
                      key={agent.id}
                      className="flex items-center space-x-3 sm:space-x-2 p-3 sm:p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/5 transition-all duration-200"
                    >
                      <Checkbox
                        id={`culture-agent-${agent.id}`}
                        checked={isChecked}
                        onCheckedChange={checked => {
                          const shouldAssign = checked === true;
                          const previousCulture = cultures.find(
                            culture =>
                              culture.id !== selectedCulture.id && culture.agents.includes(agent.id)
                          );
                          const newAgents = shouldAssign
                            ? [...field.value, agent.id]
                            : field.value.filter(id => id !== agent.id);

                          if (shouldAssign && !isChecked && previousCulture) {
                            toast({
                              title: 'Agent moved to culture',
                              description: `${agent.display_name} moved from ${previousCulture.id} to ${selectedCulture.id}.`,
                            });
                          }

                          field.onChange(newAgents);
                          handleFieldChange('agents', newAgents);
                        }}
                        className="h-5 w-5 sm:h-4 sm:w-4"
                      />
                      <label
                        htmlFor={`culture-agent-${agent.id}`}
                        className="flex-1 cursor-pointer select-none"
                      >
                        <div className="font-medium text-sm">{agent.display_name}</div>
                        <div className="text-xs text-gray-500 dark:text-gray-400">{agent.role}</div>
                        <div className="text-xs text-gray-500 dark:text-gray-400">
                          {assignedCultureLabel}
                        </div>
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
