import { useEffect, useMemo, useState } from 'react';
import { Plus, X } from 'lucide-react';

import { FieldGroup } from '@/components/shared';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { useConfigStore } from '@/store/configStore';
import type { ToolFieldSchema } from '@/hooks/useTools';

interface ToolConfigPanelProps {
  agentId: string;
  toolName: string | null;
  toolDisplayName?: string;
  fields?: ToolFieldSchema[] | null;
}

type DraftValues = Record<string, string | string[]>;

function coerceDraftValue(field: ToolFieldSchema, value: unknown): string | string[] {
  if (field.type === 'string[]') {
    if (Array.isArray(value)) {
      return value.filter((entry): entry is string => typeof entry === 'string');
    }
    if (typeof value === 'string' && value.trim().length > 0) {
      return [value.trim()];
    }
    return [];
  }

  return typeof value === 'string' ? value : '';
}

function normalizePersistedValue(field: ToolFieldSchema, value: string | string[]): unknown | null {
  if (field.type === 'string[]') {
    const values = (Array.isArray(value) ? value : [])
      .map(entry => entry.trim())
      .filter(entry => entry.length > 0);
    return values.length > 0 ? values : null;
  }

  const trimmed = typeof value === 'string' ? value.trim() : '';
  return trimmed.length > 0 ? trimmed : null;
}

function hasActiveOverrides(
  fields: ToolFieldSchema[] | null | undefined,
  draftValues: DraftValues
): boolean {
  if (!fields || fields.length === 0) {
    return false;
  }
  return fields.some(
    field => normalizePersistedValue(field, draftValues[field.name] ?? '') != null
  );
}

interface KVEntry {
  key: string;
  value: string;
}

function GenericOverrideEditor({
  agentId,
  toolName,
  title,
}: {
  agentId: string;
  toolName: string;
  title: string;
}) {
  const { getAgentToolOverrides, updateAgentToolOverrides } = useConfigStore();
  const currentOverrides = getAgentToolOverrides(agentId, toolName);
  const overrideSignature = JSON.stringify(currentOverrides ?? null);
  const [entries, setEntries] = useState<KVEntry[]>([]);

  useEffect(() => {
    if (!currentOverrides || Object.keys(currentOverrides).length === 0) {
      setEntries([]);
      return;
    }
    setEntries(
      Object.entries(currentOverrides).map(([key, val]) => ({
        key,
        value: typeof val === 'string' ? val : JSON.stringify(val),
      }))
    );
  }, [overrideSignature]);

  const commitEntries = (nextEntries: KVEntry[]) => {
    const overrides: Record<string, unknown> = {};
    for (const entry of nextEntries) {
      const k = entry.key.trim();
      const v = entry.value.trim();
      if (k.length > 0 && v.length > 0) {
        overrides[k] = v;
      }
    }
    updateAgentToolOverrides(
      agentId,
      toolName,
      Object.keys(overrides).length > 0 ? overrides : null
    );
  };

  const updateEntry = (index: number, patch: Partial<KVEntry>) => {
    const next = entries.map((e, i) => (i === index ? { ...e, ...patch } : e));
    setEntries(next);
    commitEntries(next);
  };

  const removeEntry = (index: number) => {
    const next = entries.filter((_, i) => i !== index);
    setEntries(next);
    commitEntries(next);
  };

  const addEntry = () => {
    setEntries([...entries, { key: '', value: '' }]);
  };

  const isCustomized = entries.some(e => e.key.trim().length > 0 && e.value.trim().length > 0);

  return (
    <div className="rounded-lg border bg-background/80 p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">{title} settings</div>
          <div className="text-xs text-muted-foreground">
            Add custom key-value overrides for this tool on this agent.
          </div>
        </div>
        {isCustomized && <Badge variant="secondary">Customized</Badge>}
      </div>

      <div className="space-y-2">
        {entries.length === 0 && (
          <div className="text-xs text-muted-foreground">
            No overrides configured. Add a key-value pair to customize this tool.
          </div>
        )}
        {entries.map((entry, index) => (
          <div key={index} className="flex items-center gap-2">
            <Input
              value={entry.key}
              placeholder="Key"
              onChange={e => updateEntry(index, { key: e.target.value })}
              className="flex-1"
            />
            <Input
              value={entry.value}
              placeholder="Value"
              onChange={e => updateEntry(index, { value: e.target.value })}
              className="flex-1"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Remove override ${index + 1}`}
              onClick={() => removeEntry(index)}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        ))}
        <Button type="button" variant="outline" size="sm" onClick={addEntry}>
          <Plus className="mr-2 h-4 w-4" />
          Add override
        </Button>
      </div>
    </div>
  );
}

export function ToolConfigPanel({
  agentId,
  toolName,
  toolDisplayName,
  fields,
}: ToolConfigPanelProps) {
  const { getAgentToolOverrides, updateAgentToolOverrides } = useConfigStore();
  const currentOverrides = toolName ? getAgentToolOverrides(agentId, toolName) : null;
  const overrideSignature = JSON.stringify(currentOverrides ?? null);
  const [draftValues, setDraftValues] = useState<DraftValues>({});
  const hasTypedFields = (fields?.length ?? 0) > 0;

  useEffect(() => {
    if (!toolName || !hasTypedFields) {
      setDraftValues({});
      return;
    }

    setDraftValues(
      Object.fromEntries(
        fields!.map(field => [field.name, coerceDraftValue(field, currentOverrides?.[field.name])])
      )
    );
  }, [toolName, fields, overrideSignature, hasTypedFields]);

  const title = toolDisplayName ?? toolName ?? 'Tool settings';
  const isCustomized = useMemo(
    () => hasActiveOverrides(fields, draftValues),
    [draftValues, fields]
  );

  if (!toolName) {
    return (
      <div className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
        Select a checked tool to edit per-agent settings.
      </div>
    );
  }

  if (!hasTypedFields) {
    return <GenericOverrideEditor agentId={agentId} toolName={toolName} title={title} />;
  }

  const commitDraft = (nextDraftValues: DraftValues) => {
    if (!toolName) {
      return;
    }

    const overrideUpdates = Object.fromEntries(
      fields!.map(field => [
        field.name,
        normalizePersistedValue(field, nextDraftValues[field.name] ?? ''),
      ])
    );
    updateAgentToolOverrides(agentId, toolName, overrideUpdates);
  };

  const updateDraftValue = (fieldName: string, value: string | string[]) => {
    const nextDraftValues = {
      ...draftValues,
      [fieldName]: value,
    };
    setDraftValues(nextDraftValues);
    commitDraft(nextDraftValues);
  };

  return (
    <div className="rounded-lg border bg-background/80 p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">{title} settings</div>
          <div className="text-xs text-muted-foreground">
            These values override the tool only for this agent.
          </div>
        </div>
        {isCustomized && <Badge variant="secondary">Customized</Badge>}
      </div>

      <div className="space-y-4">
        {fields!.map(field => {
          const value = draftValues[field.name] ?? (field.type === 'string[]' ? [] : '');

          if (field.type === 'string[]') {
            const items = Array.isArray(value) ? value : [];

            return (
              <FieldGroup
                key={field.name}
                label={field.label}
                helperText={field.description ?? 'Add one value per row.'}
              >
                <div className="space-y-2">
                  {items.length === 0 && (
                    <div className="text-xs text-muted-foreground">
                      No values configured. Add one to create an override.
                    </div>
                  )}
                  {items.map((item, index) => (
                    <div key={`${field.name}-${index}`} className="flex items-center gap-2">
                      <Input
                        value={item}
                        placeholder={field.placeholder ?? field.label}
                        onChange={event => {
                          const nextItems = [...items];
                          nextItems[index] = event.target.value;
                          updateDraftValue(field.name, nextItems);
                        }}
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove ${field.label} value ${index + 1}`}
                        onClick={() => {
                          updateDraftValue(
                            field.name,
                            items.filter((_, itemIndex) => itemIndex !== index)
                          );
                        }}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      updateDraftValue(field.name, [...items, '']);
                    }}
                  >
                    <Plus className="mr-2 h-4 w-4" />
                    Add value
                  </Button>
                </div>
              </FieldGroup>
            );
          }

          return (
            <FieldGroup
              key={field.name}
              label={field.label}
              helperText={field.description ?? 'Leave blank to inherit the default tool behavior.'}
              htmlFor={`tool-setting-${field.name}`}
            >
              <Input
                id={`tool-setting-${field.name}`}
                value={typeof value === 'string' ? value : ''}
                placeholder={field.placeholder ?? field.label}
                onChange={event => updateDraftValue(field.name, event.target.value)}
              />
            </FieldGroup>
          );
        })}
      </div>
    </div>
  );
}
