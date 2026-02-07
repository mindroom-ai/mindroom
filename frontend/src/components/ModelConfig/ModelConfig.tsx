import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  type Column,
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { useConfigStore } from '@/store/configStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { EditorPanel } from '@/components/shared/EditorPanel';
import { ArrowUpDown, Copy, Pencil, Plus, Save, Settings, Trash2, X } from 'lucide-react';
import { toast } from '@/components/ui/toaster';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { ProviderLogo } from './ProviderLogos';
import { getProviderInfo, getProviderList } from '@/lib/providers';
import type { ProviderType } from '@/types/config';

interface RowDraft {
  modelName: string;
  provider: string;
  modelId: string;
  apiKey: string;
  selectedKeySourceModel: string;
  clearCustomKey: boolean;
}

interface KeyStatus {
  hasKey: boolean;
  source: string | null;
  maskedKey: string | null;
}

interface KeyDisplayInfo {
  hasKey: boolean;
  label: string;
  maskedKey: string | null;
  keyId: string | null;
  sourceLabel: string;
}

interface ModelRowData {
  modelName: string;
  provider: string;
  providerName: string;
  modelId: string;
  modelConfig: {
    provider: string;
    id: string;
    host?: string;
    extra_kwargs?: Record<string, unknown>;
  };
  keyDisplay: KeyDisplayInfo | null;
}

const EMPTY_DRAFT: RowDraft = {
  modelName: '',
  provider: 'openrouter',
  modelId: '',
  apiKey: '',
  selectedKeySourceModel: '',
  clearCustomKey: false,
};

const NONE_OPTION_VALUE = '__none__';

const KEY_BADGE_COLORS = [
  'bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/25',
  'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/25',
  'bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/25',
  'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/25',
  'bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 border-cyan-500/25',
  'bg-lime-500/10 text-lime-700 dark:text-lime-300 border-lime-500/25',
  'bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/25',
  'bg-teal-500/10 text-teal-700 dark:text-teal-300 border-teal-500/25',
  'bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/25',
];

const NO_KEY_BADGE_COLOR = 'bg-red-500/10 text-red-700 dark:text-red-300 border-red-500/25';

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) % 2147483647;
  }
  return Math.abs(hash);
}

function getKeyBadgeColorClass(keyId: string | null): string {
  if (!keyId) {
    return NO_KEY_BADGE_COLOR;
  }
  return KEY_BADGE_COLORS[hashString(keyId) % KEY_BADGE_COLORS.length];
}

function providerToService(provider: string): string {
  return provider === 'gemini' ? 'google' : provider;
}

function sourceToLabel(source: string | null, hasKey: boolean): string {
  if (!hasKey) {
    return 'Not set';
  }
  if (source === 'env') {
    return '.env';
  }
  if (source === 'ui') {
    return 'UI';
  }
  if (source) {
    return source;
  }
  return 'Stored';
}

async function fetchKeyStatus(service: string): Promise<KeyStatus> {
  try {
    const res = await fetch(`/api/credentials/${service}/api-key?key_name=api_key`);
    if (!res.ok) {
      return { hasKey: false, source: null, maskedKey: null };
    }

    const data = await res.json();
    return {
      hasKey: data.has_key,
      source: data.source || null,
      maskedKey: data.masked_key || null,
    };
  } catch {
    return { hasKey: false, source: null, maskedKey: null };
  }
}

async function fetchApiKeyValue(service: string): Promise<string | null> {
  try {
    const res = await fetch(
      `/api/credentials/${service}/api-key?key_name=api_key&include_value=true`
    );
    if (!res.ok) {
      return null;
    }
    const data = await res.json();
    return data.api_key || null;
  } catch {
    return null;
  }
}

async function copyTextToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return true;
  }

  if (typeof document === 'undefined') {
    return false;
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  return copied;
}

function getKeyStatusDisplay(
  modelName: string,
  provider: string,
  modelKeys: Record<string, KeyStatus>,
  providerKeys: Record<string, KeyStatus>
): KeyDisplayInfo | null {
  if (provider === 'ollama') {
    return null;
  }

  const modelKey = modelKeys[modelName];
  if (modelKey?.hasKey) {
    return {
      hasKey: true,
      label: 'Custom key',
      maskedKey: modelKey.maskedKey,
      keyId: modelKey.maskedKey ? `key:${modelKey.maskedKey}` : `model:${modelName}`,
      sourceLabel: sourceToLabel(modelKey.source, true),
    };
  }

  const providerKey = providerKeys[provider];
  if (providerKey?.hasKey) {
    return {
      hasKey: true,
      label: 'Provider key',
      maskedKey: providerKey.maskedKey,
      keyId: providerKey.maskedKey ? `key:${providerKey.maskedKey}` : `provider:${provider}`,
      sourceLabel: sourceToLabel(providerKey.source, true),
    };
  }

  return {
    hasKey: false,
    label: 'No API key',
    maskedKey: null,
    keyId: null,
    sourceLabel: sourceToLabel(null, false),
  };
}

function SortableHeader({
  label,
  column,
}: {
  label: string;
  column: Column<ModelRowData, unknown>;
}) {
  return (
    <Button
      variant="ghost"
      className="h-auto px-0 py-0 font-medium hover:bg-transparent"
      onClick={() => column.toggleSorting(column.getIsSorted() === 'asc')}
    >
      <span>{label}</span>
      <ArrowUpDown className="ml-1.5 h-3.5 w-3.5 opacity-60" />
    </Button>
  );
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, saveConfig } = useConfigStore();

  const [providerKeys, setProviderKeys] = useState<Record<string, KeyStatus>>({});
  const [modelKeys, setModelKeys] = useState<Record<string, KeyStatus>>({});
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'provider', desc: false },
    { id: 'modelName', desc: false },
  ]);

  const [editingRowId, setEditingRowId] = useState<string | null>(null);
  const [rowDraft, setRowDraft] = useState<RowDraft | null>(null);
  const [isSavingRow, setIsSavingRow] = useState(false);

  const [isAddingRow, setIsAddingRow] = useState(false);
  const [newRowDraft, setNewRowDraft] = useState<RowDraft>(EMPTY_DRAFT);
  const [isSavingNewRow, setIsSavingNewRow] = useState(false);

  const models = useMemo(() => config?.models ?? {}, [config?.models]);

  const fetchAllKeyStatuses = useCallback(async () => {
    if (!config) {
      return;
    }

    const modelEntries = Object.entries(config.models);
    const uniqueProviders = [...new Set(modelEntries.map(([_, model]) => model.provider))].filter(
      provider => provider !== 'ollama'
    );

    const [providerResults, modelResults] = await Promise.all([
      Promise.all(
        uniqueProviders.map(async provider => {
          return [provider, await fetchKeyStatus(providerToService(provider))] as const;
        })
      ),
      Promise.all(
        modelEntries
          .filter(([_, model]) => model.provider !== 'ollama')
          .map(async ([modelName]) => {
            return [modelName, await fetchKeyStatus(`model:${modelName}`)] as const;
          })
      ),
    ]);

    setProviderKeys(Object.fromEntries(providerResults));
    setModelKeys(Object.fromEntries(modelResults));
  }, [config]);

  useEffect(() => {
    fetchAllKeyStatuses();
  }, [fetchAllKeyStatuses]);

  const saveModelApiKey = async (modelName: string, apiKey: string): Promise<boolean> => {
    try {
      const res = await fetch(`/api/credentials/model:${modelName}/api-key`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service: `model:${modelName}`,
          api_key: apiKey,
          key_name: 'api_key',
        }),
      });

      if (!res.ok) {
        throw new Error('Failed to save API key');
      }

      return true;
    } catch {
      toast({ title: 'Error', description: 'Failed to save API key', variant: 'destructive' });
      return false;
    }
  };

  const copyModelApiKey = async (
    targetModelName: string,
    sourceModelName: string
  ): Promise<boolean> => {
    try {
      const res = await fetch(
        `/api/credentials/model:${targetModelName}/copy-from/model:${sourceModelName}`,
        { method: 'POST' }
      );

      if (!res.ok) {
        throw new Error('Failed to copy API key');
      }

      return true;
    } catch {
      toast({ title: 'Error', description: 'Failed to copy API key', variant: 'destructive' });
      return false;
    }
  };

  const deleteModelApiKey = async (modelName: string): Promise<boolean> => {
    try {
      const res = await fetch(`/api/credentials/model:${modelName}`, { method: 'DELETE' });
      if (!res.ok) {
        throw new Error('Failed to clear API key');
      }

      return true;
    } catch {
      toast({ title: 'Error', description: 'Failed to clear API key', variant: 'destructive' });
      return false;
    }
  };

  const getProviderScopedKeyModels = (provider: string, excludedModelNames: string[] = []) => {
    return Object.entries(models)
      .filter(
        ([modelName, modelConfig]) =>
          !excludedModelNames.includes(modelName) &&
          modelConfig.provider === provider &&
          modelConfig.provider !== 'ollama' &&
          modelKeys[modelName]?.hasKey
      )
      .map(([modelName]) => ({ modelName, maskedKey: modelKeys[modelName]?.maskedKey || null }));
  };

  const copyApiKeyForRow = async (modelName: string, provider: string) => {
    const service = modelKeys[modelName]?.hasKey
      ? `model:${modelName}`
      : providerToService(provider);

    const apiKey = await fetchApiKeyValue(service);
    if (!apiKey) {
      toast({
        title: 'Error',
        description: 'API key is not available for copying',
        variant: 'destructive',
      });
      return;
    }

    const copied = await copyTextToClipboard(apiKey);
    if (!copied) {
      toast({
        title: 'Error',
        description: 'Failed to copy API key',
        variant: 'destructive',
      });
      return;
    }

    toast({ title: 'Copied', description: `API key copied from ${service}` });
  };

  const startEditingRow = (row: ModelRowData) => {
    setEditingRowId(row.modelName);
    setRowDraft({
      modelName: row.modelName,
      provider: row.provider,
      modelId: row.modelId,
      apiKey: '',
      selectedKeySourceModel: '',
      clearCustomKey: false,
    });
  };

  const cancelEditingRow = () => {
    setEditingRowId(null);
    setRowDraft(null);
  };

  const startAddingRow = () => {
    if (editingRowId) {
      toast({
        title: 'Finish current edit first',
        description: 'Save or cancel the active row before adding a new model.',
      });
      return;
    }

    setIsAddingRow(true);
    setNewRowDraft({ ...EMPTY_DRAFT });
  };

  const cancelAddingRow = () => {
    setIsAddingRow(false);
    setNewRowDraft({ ...EMPTY_DRAFT });
  };

  const handleSaveRow = async () => {
    if (!editingRowId || !rowDraft) {
      return;
    }

    const originalModelName = editingRowId;
    const targetModelName = rowDraft.modelName.trim();
    const targetModelId = rowDraft.modelId.trim();
    const originalModelConfig = models[originalModelName];

    if (!originalModelConfig) {
      return;
    }

    if (!targetModelName || !targetModelId) {
      toast({
        title: 'Error',
        description: 'Model name and model ID are required',
        variant: 'destructive',
      });
      return;
    }

    if (originalModelName === 'default' && targetModelName !== 'default') {
      toast({
        title: 'Error',
        description: 'The default model name cannot be changed',
        variant: 'destructive',
      });
      return;
    }

    if (targetModelName !== originalModelName && models[targetModelName]) {
      toast({
        title: 'Error',
        description: 'A model with this name already exists',
        variant: 'destructive',
      });
      return;
    }

    setIsSavingRow(true);

    const renamed = targetModelName !== originalModelName;
    const hadCustomKey = Boolean(modelKeys[originalModelName]?.hasKey);
    const hasManualApiKey = Boolean(rowDraft.apiKey.trim());
    const hasKeyReuseSource = Boolean(rowDraft.selectedKeySourceModel);

    let keyOperationOk = true;

    if (rowDraft.provider !== 'ollama') {
      if (hasKeyReuseSource) {
        keyOperationOk = await copyModelApiKey(targetModelName, rowDraft.selectedKeySourceModel);
      } else if (hasManualApiKey) {
        keyOperationOk = await saveModelApiKey(targetModelName, rowDraft.apiKey.trim());
      } else if (rowDraft.clearCustomKey && hadCustomKey) {
        keyOperationOk = await deleteModelApiKey(originalModelName);
      } else if (renamed && hadCustomKey) {
        keyOperationOk = await copyModelApiKey(targetModelName, originalModelName);
      }
    } else if (hadCustomKey) {
      keyOperationOk = await deleteModelApiKey(originalModelName);
    }

    if (keyOperationOk && renamed && hadCustomKey && (hasKeyReuseSource || hasManualApiKey)) {
      keyOperationOk = await deleteModelApiKey(originalModelName);
    }

    if (!keyOperationOk) {
      setIsSavingRow(false);
      return;
    }

    const nextModelConfig = {
      ...originalModelConfig,
      provider: rowDraft.provider as ProviderType,
      id: targetModelId,
    };

    if (rowDraft.provider !== 'ollama') {
      delete nextModelConfig.host;
    }

    updateModel(targetModelName, nextModelConfig);
    if (renamed) {
      deleteModel(originalModelName);
    }

    await fetchAllKeyStatuses();
    setIsSavingRow(false);
    cancelEditingRow();

    toast({
      title: 'Model Updated',
      description: renamed
        ? `Model ${originalModelName} renamed to ${targetModelName}`
        : `Model ${targetModelName} updated`,
    });
  };

  const handleSaveNewRow = async () => {
    const modelName = newRowDraft.modelName.trim();
    const modelId = newRowDraft.modelId.trim();

    if (!modelName || !modelId) {
      toast({
        title: 'Error',
        description: 'Model name and model ID are required',
        variant: 'destructive',
      });
      return;
    }

    if (models[modelName]) {
      toast({
        title: 'Error',
        description: 'A model with this name already exists',
        variant: 'destructive',
      });
      return;
    }

    setIsSavingNewRow(true);

    let keyOperationOk = true;
    if (newRowDraft.provider !== 'ollama') {
      if (newRowDraft.selectedKeySourceModel) {
        keyOperationOk = await copyModelApiKey(modelName, newRowDraft.selectedKeySourceModel);
      } else if (newRowDraft.apiKey.trim()) {
        keyOperationOk = await saveModelApiKey(modelName, newRowDraft.apiKey.trim());
      }
    }

    if (!keyOperationOk) {
      setIsSavingNewRow(false);
      return;
    }

    updateModel(modelName, {
      provider: newRowDraft.provider as ProviderType,
      id: modelId,
    });

    await fetchAllKeyStatuses();
    setIsSavingNewRow(false);
    cancelAddingRow();

    toast({ title: 'Model Added', description: `Model ${modelName} has been added` });
  };

  const handleDeleteModel = (modelName: string) => {
    if (modelName === 'default') {
      toast({
        title: 'Cannot Delete',
        description: 'The default model cannot be deleted',
        variant: 'destructive',
      });
      return;
    }

    if (confirm(`Delete model "${modelName}"?`)) {
      deleteModel(modelName);
      toast({ title: 'Model Deleted', description: `Model ${modelName} has been removed` });
    }
  };

  const rows = useMemo<ModelRowData[]>(() => {
    return Object.entries(models).map(([modelName, modelConfig]) => {
      const keyDisplay = getKeyStatusDisplay(
        modelName,
        modelConfig.provider,
        modelKeys,
        providerKeys
      );

      return {
        modelName,
        provider: modelConfig.provider,
        providerName: getProviderInfo(modelConfig.provider).name,
        modelId: modelConfig.id,
        modelConfig,
        keyDisplay,
      };
    });
  }, [models, modelKeys, providerKeys]);

  const renderReadonlyKeyStatus = (
    keyDisplay: KeyDisplayInfo | null,
    modelName?: string,
    provider?: string
  ) => {
    if (!keyDisplay) {
      return <span className="text-sm text-muted-foreground">N/A</span>;
    }

    return (
      <div className="space-y-1">
        <div className="flex items-center gap-1.5">
          <Badge
            variant="outline"
            className={cn(
              'text-xs',
              getKeyBadgeColorClass(keyDisplay.hasKey ? keyDisplay.keyId : null)
            )}
          >
            {keyDisplay.label}
            {keyDisplay.maskedKey && (
              <span className="ml-1 font-mono opacity-80">{keyDisplay.maskedKey}</span>
            )}
          </Badge>
          {keyDisplay.hasKey && modelName && provider && (
            <Button
              size="icon"
              variant="ghost"
              className="h-6 w-6"
              title="Copy API key"
              onClick={event => {
                event.stopPropagation();
                void copyApiKeyForRow(modelName, provider);
              }}
            >
              <Copy className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">Source: {keyDisplay.sourceLabel}</p>
      </div>
    );
  };

  const renderEditableApiCell = (
    draft: RowDraft,
    setDraft: Dispatch<SetStateAction<RowDraft>>,
    currentModelName?: string
  ) => {
    if (draft.provider === 'ollama') {
      return <span className="text-xs text-muted-foreground">No key needed for Ollama</span>;
    }

    const providerKeyOptions = getProviderScopedKeyModels(
      draft.provider,
      currentModelName ? [currentModelName] : []
    );

    const hasCustomKey = Boolean(currentModelName && modelKeys[currentModelName]?.hasKey);
    const currentStatus = currentModelName
      ? getKeyStatusDisplay(currentModelName, draft.provider, modelKeys, providerKeys)
      : null;

    return (
      <div className="space-y-2" onClick={event => event.stopPropagation()}>
        {currentStatus && currentModelName && (
          <div className="space-y-1">
            {renderReadonlyKeyStatus(currentStatus, currentModelName, draft.provider)}
            {hasCustomKey && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                onClick={() => {
                  setDraft(current => ({
                    ...current,
                    clearCustomKey: true,
                    apiKey: '',
                    selectedKeySourceModel: '',
                  }));
                }}
              >
                <X className="mr-1 h-3 w-3" />
                Clear custom key
              </Button>
            )}
          </div>
        )}

        <Input
          type="password"
          value={draft.apiKey}
          onChange={event => {
            const apiKey = event.target.value;
            setDraft(current => ({
              ...current,
              apiKey,
              selectedKeySourceModel: '',
              clearCustomKey: false,
            }));
          }}
          placeholder="Paste new API key"
          className="h-8 text-xs"
        />

        {providerKeyOptions.length > 0 && (
          <Select
            value={draft.selectedKeySourceModel || NONE_OPTION_VALUE}
            onValueChange={modelName => {
              if (modelName === NONE_OPTION_VALUE) {
                setDraft(current => ({ ...current, selectedKeySourceModel: '' }));
                return;
              }

              setDraft(current => ({
                ...current,
                selectedKeySourceModel: modelName,
                apiKey: '',
                clearCustomKey: false,
              }));
            }}
          >
            <SelectTrigger className="h-8 text-xs">
              <div className="flex items-center gap-1.5 truncate">
                <Copy className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">
                  {draft.selectedKeySourceModel
                    ? `Reuse key from ${draft.selectedKeySourceModel}`
                    : 'Reuse from same provider'}
                </span>
              </div>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NONE_OPTION_VALUE}>Do not reuse key</SelectItem>
              {providerKeyOptions.map(option => (
                <SelectItem key={option.modelName} value={option.modelName}>
                  <div className="flex items-center gap-2">
                    <span>{option.modelName}</span>
                    {option.maskedKey && (
                      <span className="text-xs text-muted-foreground">({option.maskedKey})</span>
                    )}
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
    );
  };

  const columns: ColumnDef<ModelRowData>[] = [
    {
      accessorKey: 'modelName',
      header: ({ column }) => <SortableHeader label="Name" column={column} />,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName && rowDraft;
        if (!isEditing) {
          return <span className="font-medium">{row.original.modelName}</span>;
        }

        return (
          <Input
            value={rowDraft.modelName}
            onChange={event => {
              const modelName = event.target.value;
              setRowDraft(current => (current ? { ...current, modelName } : current));
            }}
            onClick={event => event.stopPropagation()}
            disabled={row.original.modelName === 'default'}
            className="h-8 text-xs"
          />
        );
      },
    },
    {
      id: 'provider',
      accessorFn: row => row.providerName,
      header: ({ column }) => <SortableHeader label="Provider" column={column} />,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName && rowDraft;
        if (!isEditing) {
          return (
            <div className="flex items-center gap-1.5">
              <ProviderLogo provider={row.original.provider} className="h-4 w-4" />
              <span>{row.original.providerName}</span>
            </div>
          );
        }

        return (
          <div onClick={event => event.stopPropagation()}>
            <Select
              value={rowDraft.provider}
              onValueChange={provider => {
                setRowDraft(current =>
                  current
                    ? {
                        ...current,
                        provider,
                        apiKey: '',
                        selectedKeySourceModel: '',
                        clearCustomKey: provider === 'ollama' ? true : current.clearCustomKey,
                      }
                    : current
                );
              }}
            >
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {getProviderList().map(provider => (
                  <SelectItem key={provider.id} value={provider.id}>
                    <div className="flex items-center gap-2">
                      <ProviderLogo provider={provider.id} className="h-4 w-4" />
                      <span>{provider.name}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        );
      },
    },
    {
      accessorKey: 'modelId',
      header: ({ column }) => <SortableHeader label="Model ID" column={column} />,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName && rowDraft;
        if (!isEditing) {
          return (
            <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{row.original.modelId}</code>
          );
        }

        return (
          <Input
            value={rowDraft.modelId}
            onChange={event => {
              const modelId = event.target.value;
              setRowDraft(current => (current ? { ...current, modelId } : current));
            }}
            onClick={event => event.stopPropagation()}
            className="h-8 text-xs"
          />
        );
      },
    },
    {
      id: 'apiKey',
      accessorFn: row => row.keyDisplay?.keyId || '',
      header: ({ column }) => <SortableHeader label="API Key" column={column} />,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName && rowDraft;
        if (!isEditing) {
          return renderReadonlyKeyStatus(
            row.original.keyDisplay,
            row.original.modelName,
            row.original.provider
          );
        }

        return renderEditableApiCell(
          rowDraft,
          setRowDraft as Dispatch<SetStateAction<RowDraft>>,
          row.original.modelName
        );
      },
    },
    {
      id: 'actions',
      header: () => <span className="font-medium">Actions</span>,
      enableSorting: false,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName;

        if (isEditing) {
          return (
            <div className="flex justify-end gap-1" onClick={event => event.stopPropagation()}>
              <Button
                size="sm"
                onClick={handleSaveRow}
                disabled={isSavingRow}
                className="h-7 px-2 text-xs"
              >
                Save
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={cancelEditingRow}
                disabled={isSavingRow}
                className="h-7 px-2 text-xs"
              >
                Cancel
              </Button>
            </div>
          );
        }

        return (
          <div className="flex justify-end gap-1" onClick={event => event.stopPropagation()}>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => startEditingRow(row.original)}
              className="h-7 w-7"
              title="Edit"
            >
              <Pencil className="h-3.5 w-3.5" />
            </Button>
            {row.original.modelName !== 'default' && (
              <Button
                size="icon"
                variant="ghost"
                onClick={() => handleDeleteModel(row.original.modelName)}
                className="h-7 w-7 hover:bg-destructive/10"
                title="Delete"
              >
                <Trash2 className="h-3.5 w-3.5 text-destructive" />
              </Button>
            )}
          </div>
        );
      },
    },
  ];

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  if (!config) {
    return null;
  }

  return (
    <EditorPanel
      icon={Settings}
      title="Model Configuration"
      isDirty={false}
      onSave={() => saveConfig()}
      onDelete={() => {}}
      showActions={false}
      className="h-full"
    >
      <div className="space-y-4">
        <div className="flex justify-end">
          <Button onClick={startAddingRow} variant="outline" size="sm" disabled={isAddingRow}>
            <Plus className="mr-1.5 h-4 w-4" />
            Add Model
          </Button>
        </div>

        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-sm">
            <thead>
              {table.getHeaderGroups().map(headerGroup => (
                <tr key={headerGroup.id} className="border-b bg-muted/50">
                  {headerGroup.headers.map(header => (
                    <th
                      key={header.id}
                      className={cn(
                        'px-4 py-2.5 text-left align-middle',
                        header.column.id === 'actions' ? 'w-40 text-right' : ''
                      )}
                    >
                      {header.isPlaceholder
                        ? null
                        : flexRender(header.column.columnDef.header, header.getContext())}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>

            <tbody>
              {isAddingRow && (
                <tr className="border-b bg-muted/20">
                  <td className="px-4 py-2.5 align-middle">
                    <Input
                      value={newRowDraft.modelName}
                      onChange={event => {
                        const modelName = event.target.value;
                        setNewRowDraft(current => ({ ...current, modelName }));
                      }}
                      className="h-8 text-xs"
                      placeholder="model name"
                    />
                  </td>
                  <td className="px-4 py-2.5 align-middle">
                    <Select
                      value={newRowDraft.provider}
                      onValueChange={provider => {
                        setNewRowDraft(current => ({
                          ...current,
                          provider,
                          apiKey: '',
                          selectedKeySourceModel: '',
                        }));
                      }}
                    >
                      <SelectTrigger className="h-8 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {getProviderList().map(provider => (
                          <SelectItem key={provider.id} value={provider.id}>
                            <div className="flex items-center gap-2">
                              <ProviderLogo provider={provider.id} className="h-4 w-4" />
                              <span>{provider.name}</span>
                            </div>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="px-4 py-2.5 align-middle">
                    <Input
                      value={newRowDraft.modelId}
                      onChange={event => {
                        const modelId = event.target.value;
                        setNewRowDraft(current => ({ ...current, modelId }));
                      }}
                      className="h-8 text-xs"
                      placeholder="provider model id"
                    />
                  </td>
                  <td className="px-4 py-2.5 align-middle">
                    {renderEditableApiCell(newRowDraft, setNewRowDraft)}
                  </td>
                  <td className="px-4 py-2.5 align-middle text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        onClick={handleSaveNewRow}
                        disabled={isSavingNewRow}
                        className="h-7 px-2 text-xs"
                      >
                        Add
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={cancelAddingRow}
                        disabled={isSavingNewRow}
                        className="h-7 px-2 text-xs"
                      >
                        Cancel
                      </Button>
                    </div>
                  </td>
                </tr>
              )}

              {table.getRowModel().rows.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-center text-sm text-muted-foreground">
                    No models configured.
                  </td>
                </tr>
              ) : (
                table.getRowModel().rows.map(row => {
                  const isEditing = editingRowId === row.original.modelName;
                  return (
                    <tr
                      key={row.id}
                      onClick={() => {
                        if (isAddingRow) {
                          toast({
                            title: 'Finish adding first',
                            description:
                              'Save or cancel the new model row before editing another row.',
                          });
                          return;
                        }

                        if (editingRowId && editingRowId !== row.original.modelName) {
                          toast({
                            title: 'Finish current edit first',
                            description:
                              'Save or cancel the active row before editing another one.',
                          });
                          return;
                        }

                        if (!editingRowId) {
                          startEditingRow(row.original);
                        }
                      }}
                      className={cn(
                        'border-b transition-colors last:border-b-0',
                        isEditing ? 'bg-muted/20' : 'cursor-pointer hover:bg-muted/30'
                      )}
                    >
                      {row.getVisibleCells().map(cell => (
                        <td
                          key={cell.id}
                          className={cn(
                            'px-4 py-2.5 align-middle',
                            cell.column.id === 'actions' ? 'text-right' : ''
                          )}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <Button onClick={() => saveConfig()} variant="default" className="w-full">
          <Save className="mr-2 h-4 w-4" />
          Save All Changes
        </Button>
      </div>
    </EditorPanel>
  );
}
