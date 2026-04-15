import { type Dispatch, type ReactNode, type SetStateAction, useMemo, useState } from 'react';
import {
  type Column,
  type ColumnDef,
  type SortingState,
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
import { showSaveFailureToastIfNeeded } from '@/components/shared';
import { ArrowUpDown, Pencil, Plus, Save, Settings, Trash2 } from 'lucide-react';
import { toast } from '@/components/ui/toaster';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { ProviderLogo } from './ProviderLogos';
import { getProviderInfo, getProviderList } from '@/lib/providers';
import { defaultConnectionIdForPurpose, type ProviderType } from '@/types/config';

interface RowDraft {
  modelName: string;
  provider: string;
  modelId: string;
  baseUrl: string;
  contextWindow: string;
  connection: string;
}

interface ModelRowData {
  modelName: string;
  provider: string;
  providerName: string;
  modelId: string;
  openAIBaseUrl: string | null;
  contextWindow: number | null;
  connection: string | null;
  effectiveConnection: string | null;
}

const EMPTY_DRAFT: RowDraft = {
  modelName: '',
  provider: 'openrouter',
  modelId: '',
  baseUrl: '',
  contextWindow: '',
  connection: '',
};

const DEFAULT_OPENAI_BASE_URL = 'https://api.openai.com/v1';

function getOpenAIBaseUrl(modelConfig: { extra_kwargs?: Record<string, unknown> }): string | null {
  const extraKwargs = modelConfig.extra_kwargs;
  if (!extraKwargs) {
    return null;
  }
  const baseUrl = extraKwargs.base_url;
  if (typeof baseUrl !== 'string') {
    return null;
  }
  const trimmed = baseUrl.trim();
  return trimmed || null;
}

function isValidHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

function parseOptionalPositiveInteger(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (!/^\d+$/.test(trimmed)) {
    return Number.NaN;
  }
  const parsed = Number.parseInt(trimmed, 10);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    return Number.NaN;
  }
  return parsed;
}

function normalizeConnection(value: string): string | null {
  const trimmed = value.trim();
  return trimmed || null;
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

function renderTableValue<TContext>(
  renderer: ((context: TContext) => ReactNode) | ReactNode,
  context: TContext
): ReactNode {
  if (typeof renderer === 'function') {
    return renderer(context);
  }
  return renderer;
}

function renderOpenAIEndpointEditor(draft: RowDraft, setDraft: Dispatch<SetStateAction<RowDraft>>) {
  if (draft.provider !== 'openai') {
    return null;
  }

  return (
    <div className="space-y-2" onClick={event => event.stopPropagation()}>
      <Input
        value={draft.baseUrl}
        onChange={event => {
          const baseUrl = event.target.value;
          setDraft(current => ({ ...current, baseUrl }));
        }}
        onClick={event => event.stopPropagation()}
        placeholder={DEFAULT_OPENAI_BASE_URL}
        className="h-8 text-xs"
      />
      <p className="text-xs text-muted-foreground">
        Leave empty to use{' '}
        <code className="rounded bg-muted px-1 py-0.5 text-[10px]">{DEFAULT_OPENAI_BASE_URL}</code>.
      </p>
    </div>
  );
}

function renderContextWindowEditor(draft: RowDraft, setDraft: Dispatch<SetStateAction<RowDraft>>) {
  return (
    <div className="space-y-2" onClick={event => event.stopPropagation()}>
      <Input
        value={draft.contextWindow}
        onChange={event => {
          const contextWindow = event.target.value;
          setDraft(current => ({ ...current, contextWindow }));
        }}
        onClick={event => event.stopPropagation()}
        placeholder="optional context window"
        inputMode="numeric"
        className="h-8 text-xs"
      />
      <p className="text-xs text-muted-foreground">
        Required for compaction-aware budgeting when the provider does not report it elsewhere.
      </p>
    </div>
  );
}

function renderConnectionSummary(row: Pick<ModelRowData, 'connection' | 'effectiveConnection'>) {
  if (row.connection) {
    return (
      <div className="space-y-1">
        <Badge variant="outline" className="text-xs">
          Explicit connection
        </Badge>
        <p className="font-mono text-xs text-muted-foreground">{row.connection}</p>
      </div>
    );
  }

  if (row.effectiveConnection) {
    return (
      <div className="space-y-1">
        <Badge variant="outline" className="text-xs">
          Configured default
        </Badge>
        <p className="font-mono text-xs text-muted-foreground">{row.effectiveConnection}</p>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <Badge variant="outline" className="text-xs">
        No default connection
      </Badge>
      <p className="font-mono text-xs text-muted-foreground">Set an explicit connection id.</p>
    </div>
  );
}

function renderConnectionEditor(
  draft: RowDraft,
  setDraft: Dispatch<SetStateAction<RowDraft>>,
  defaultConnection: string | null
) {
  return (
    <div className="space-y-2" onClick={event => event.stopPropagation()}>
      <Input
        value={draft.connection}
        onChange={event => {
          const connection = event.target.value;
          setDraft(current => ({ ...current, connection }));
        }}
        onClick={event => event.stopPropagation()}
        placeholder={defaultConnection ?? 'explicit connection id'}
        className="h-8 text-xs"
      />
      {defaultConnection ? (
        <p className="text-xs text-muted-foreground">
          Leave empty to use{' '}
          <code className="rounded bg-muted px-1 py-0.5 text-[10px]">{defaultConnection}</code>.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          No default connection is configured for this provider. Set an explicit connection id.
        </p>
      )}
    </div>
  );
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, saveConfig, isLoading } = useConfigStore();

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

  const startEditingRow = (row: ModelRowData) => {
    setEditingRowId(row.modelName);
    setRowDraft({
      modelName: row.modelName,
      provider: row.provider,
      modelId: row.modelId,
      baseUrl: row.openAIBaseUrl || '',
      contextWindow: row.contextWindow != null ? String(row.contextWindow) : '',
      connection: row.connection || '',
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

  const validateOpenAIBaseUrl = (
    draft: Pick<RowDraft, 'provider' | 'baseUrl'>
  ): { valid: true; value: string | null } | { valid: false } => {
    if (draft.provider !== 'openai') {
      return { valid: true, value: null };
    }

    const normalizedBaseUrl = draft.baseUrl.trim();
    if (!normalizedBaseUrl) {
      return { valid: true, value: null };
    }

    if (!isValidHttpUrl(normalizedBaseUrl)) {
      toast({
        title: 'Error',
        description: 'Base URL must be a valid http(s) URL',
        variant: 'destructive',
      });
      return { valid: false };
    }

    return { valid: true, value: normalizedBaseUrl };
  };

  const validateContextWindow = (
    rawContextWindow: string
  ): { valid: true; value: number | null } | { valid: false } => {
    const contextWindow = parseOptionalPositiveInteger(rawContextWindow);
    if (contextWindow !== null && Number.isNaN(contextWindow)) {
      toast({
        title: 'Error',
        description: 'Context window must be a positive integer',
        variant: 'destructive',
      });
      return { valid: false };
    }
    return { valid: true, value: contextWindow };
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

    const baseUrlValidation = validateOpenAIBaseUrl(rowDraft);
    if (!baseUrlValidation.valid) {
      return;
    }
    const normalizedBaseUrl = baseUrlValidation.value;
    const contextWindowValidation = validateContextWindow(rowDraft.contextWindow);
    if (!contextWindowValidation.valid) {
      return;
    }
    const normalizedContextWindow = contextWindowValidation.value;
    const normalizedConnection = normalizeConnection(rowDraft.connection);

    setIsSavingRow(true);

    const nextModelConfig = {
      ...originalModelConfig,
      provider: rowDraft.provider as ProviderType,
      id: targetModelId,
    };

    const nextExtraKwargs = { ...(originalModelConfig.extra_kwargs ?? {}) };
    if (rowDraft.provider === 'openai') {
      if (normalizedBaseUrl) {
        nextExtraKwargs.base_url = normalizedBaseUrl;
      } else {
        delete nextExtraKwargs.base_url;
      }
    } else {
      delete nextExtraKwargs.base_url;
    }

    if (Object.keys(nextExtraKwargs).length > 0) {
      nextModelConfig.extra_kwargs = nextExtraKwargs;
    } else {
      delete nextModelConfig.extra_kwargs;
    }

    if (normalizedContextWindow != null) {
      nextModelConfig.context_window = normalizedContextWindow;
    } else {
      delete nextModelConfig.context_window;
    }

    if (normalizedConnection) {
      nextModelConfig.connection = normalizedConnection;
    } else {
      delete nextModelConfig.connection;
    }

    if (rowDraft.provider !== 'ollama') {
      delete nextModelConfig.host;
    }

    const renamed = targetModelName !== originalModelName;
    updateModel(targetModelName, nextModelConfig);
    if (renamed) {
      deleteModel(originalModelName);
    }

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

    const baseUrlValidation = validateOpenAIBaseUrl(newRowDraft);
    if (!baseUrlValidation.valid) {
      return;
    }
    const normalizedBaseUrl = baseUrlValidation.value;
    const contextWindowValidation = validateContextWindow(newRowDraft.contextWindow);
    if (!contextWindowValidation.valid) {
      return;
    }
    const normalizedContextWindow = contextWindowValidation.value;
    const normalizedConnection = normalizeConnection(newRowDraft.connection);

    setIsSavingNewRow(true);

    const nextModelConfig: {
      provider: ProviderType;
      id: string;
      connection?: string;
      context_window?: number;
      extra_kwargs?: Record<string, unknown>;
    } = {
      provider: newRowDraft.provider as ProviderType,
      id: modelId,
    };

    if (normalizedConnection) {
      nextModelConfig.connection = normalizedConnection;
    }
    if (newRowDraft.provider === 'openai' && normalizedBaseUrl) {
      nextModelConfig.extra_kwargs = { base_url: normalizedBaseUrl };
    }
    if (normalizedContextWindow != null) {
      nextModelConfig.context_window = normalizedContextWindow;
    }

    updateModel(modelName, nextModelConfig);

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

  const handleSaveAllChanges = async () => {
    const result = await saveConfig();
    showSaveFailureToastIfNeeded(result);
  };

  const rows = useMemo<ModelRowData[]>(() => {
    return Object.entries(models).map(([modelName, modelConfig]) => {
      const openAIBaseUrl =
        modelConfig.provider === 'openai' ? getOpenAIBaseUrl(modelConfig) : null;
      const configuredDefaultConnection = defaultConnectionIdForPurpose(
        modelConfig.provider,
        'chat_model',
        config?.connections
      );

      return {
        modelName,
        provider: modelConfig.provider,
        providerName: getProviderInfo(modelConfig.provider).name,
        modelId: modelConfig.id,
        openAIBaseUrl,
        contextWindow: modelConfig.context_window ?? null,
        connection: normalizeConnection(modelConfig.connection ?? ''),
        effectiveConnection:
          normalizeConnection(modelConfig.connection ?? '') ?? configuredDefaultConnection,
      };
    });
  }, [config?.connections, models]);

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
                        baseUrl: provider === 'openai' ? current.baseUrl : '',
                        connection: '',
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
            <div className="space-y-1">
              <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{row.original.modelId}</code>
              {row.original.provider === 'openai' && row.original.openAIBaseUrl && (
                <p className="text-xs text-muted-foreground">
                  Endpoint:{' '}
                  <code className="rounded bg-muted px-1 py-0.5 text-[10px]">
                    {row.original.openAIBaseUrl}
                  </code>
                </p>
              )}
              {row.original.contextWindow != null && (
                <p className="text-xs text-muted-foreground">
                  Context window:{' '}
                  <code className="rounded bg-muted px-1 py-0.5 text-[10px]">
                    {row.original.contextWindow.toLocaleString()}
                  </code>
                </p>
              )}
            </div>
          );
        }

        return (
          <div className="space-y-2">
            <Input
              value={rowDraft.modelId}
              onChange={event => {
                const modelId = event.target.value;
                setRowDraft(current => (current ? { ...current, modelId } : current));
              }}
              onClick={event => event.stopPropagation()}
              className="h-8 text-xs"
            />
            {renderOpenAIEndpointEditor(
              rowDraft,
              setRowDraft as Dispatch<SetStateAction<RowDraft>>
            )}
            {renderContextWindowEditor(rowDraft, setRowDraft as Dispatch<SetStateAction<RowDraft>>)}
          </div>
        );
      },
    },
    {
      id: 'connection',
      accessorFn: row => row.effectiveConnection,
      header: ({ column }) => <SortableHeader label="Connection" column={column} />,
      cell: ({ row }) => {
        const isEditing = editingRowId === row.original.modelName && rowDraft;
        if (!isEditing) {
          return renderConnectionSummary(row.original);
        }

        return renderConnectionEditor(
          rowDraft,
          setRowDraft as Dispatch<SetStateAction<RowDraft>>,
          defaultConnectionIdForPurpose(rowDraft.provider, 'chat_model', config?.connections)
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
                onClick={() => void handleSaveRow()}
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
      onSave={handleSaveAllChanges}
      onDelete={() => {}}
      showActions={false}
      disableSave={isLoading}
      className="h-full"
    >
      <div className="space-y-4">
        <div className="flex justify-end">
          <Button onClick={startAddingRow} variant="outline" size="sm" disabled={isAddingRow}>
            <Plus className="mr-1.5 h-4 w-4" />
            Add Model
          </Button>
        </div>

        <div className="rounded-lg border">
          <div className="overflow-x-auto" data-testid="models-table-scroll-container">
            <table className="w-full min-w-[880px] text-sm">
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
                          : renderTableValue(header.column.columnDef.header, header.getContext())}
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
                            baseUrl: provider === 'openai' ? current.baseUrl : '',
                            connection: '',
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
                      <div className="space-y-2">
                        <Input
                          value={newRowDraft.modelId}
                          onChange={event => {
                            const modelId = event.target.value;
                            setNewRowDraft(current => ({ ...current, modelId }));
                          }}
                          className="h-8 text-xs"
                          placeholder="provider model id"
                        />
                        {renderOpenAIEndpointEditor(newRowDraft, setNewRowDraft)}
                        {renderContextWindowEditor(newRowDraft, setNewRowDraft)}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 align-middle">
                      {renderConnectionEditor(
                        newRowDraft,
                        setNewRowDraft,
                        defaultConnectionIdForPurpose(
                          newRowDraft.provider,
                          'chat_model',
                          config?.connections
                        )
                      )}
                    </td>
                    <td className="px-4 py-2.5 align-middle text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="sm"
                          onClick={() => void handleSaveNewRow()}
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
                            {renderTableValue(cell.column.columnDef.cell, cell.getContext())}
                          </td>
                        ))}
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

        <Button
          onClick={() => void handleSaveAllChanges()}
          variant="default"
          className="w-full"
          disabled={isLoading}
        >
          <Save className="mr-2 h-4 w-4" />
          Save All Changes
        </Button>
      </div>
    </EditorPanel>
  );
}
