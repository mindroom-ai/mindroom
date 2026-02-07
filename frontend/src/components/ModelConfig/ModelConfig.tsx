import { useState, useMemo, useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { EditorPanel } from '@/components/shared/EditorPanel';
import { FieldGroup } from '@/components/shared/FieldGroup';
import { Save, Trash2, Settings, Plus, Key, X, Copy, Pencil } from 'lucide-react';
import { toast } from '@/components/ui/toaster';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { ProviderLogo } from './ProviderLogos';
import { getProviderInfo, getProviderList } from '@/lib/providers';

interface ModelFormData {
  provider: string;
  id: string;
  host?: string;
  configId?: string;
  extra_kwargs?: string;
  base_url?: string;
  api_key?: string;
}

interface KeyStatus {
  hasKey: boolean;
  source: string | null;
  maskedKey: string | null;
}

const EMPTY_FORM: ModelFormData = { provider: 'ollama', id: '', configId: '' };

async function fetchKeyStatus(service: string): Promise<KeyStatus> {
  try {
    const res = await fetch(`/api/credentials/${service}/api-key?key_name=api_key`);
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

function getKeyStatusDisplay(
  modelId: string,
  provider: string,
  modelKeys: Record<string, KeyStatus>,
  providerKeys: Record<string, KeyStatus>
) {
  if (provider === 'ollama') return null;

  const modelKey = modelKeys[modelId];
  if (modelKey?.hasKey) {
    return {
      label: 'Custom key',
      maskedKey: modelKey.maskedKey,
      className: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
    };
  }

  const providerKey = providerKeys[provider];
  if (providerKey?.hasKey) {
    return {
      label: providerKey.source === 'env' ? 'From env' : 'Provider key',
      maskedKey: providerKey.maskedKey,
      className: '',
    };
  }

  return {
    label: 'No API key',
    maskedKey: null,
    className: 'border-destructive/50 text-destructive',
  };
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, saveConfig } = useConfigStore();
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [isAddingModel, setIsAddingModel] = useState(false);
  const [showKeyInput, setShowKeyInput] = useState(false);
  const [modelForm, setModelForm] = useState<ModelFormData>(EMPTY_FORM);

  const [providerKeys, setProviderKeys] = useState<Record<string, KeyStatus>>({});
  const [modelKeys, setModelKeys] = useState<Record<string, KeyStatus>>({});

  const dialogOpen = editingModel !== null || isAddingModel;

  const fetchAllKeyStatuses = useCallback(async () => {
    if (!config) return;

    const models = Object.entries(config.models);
    const uniqueProviders = [...new Set(models.map(([_, m]) => m.provider))].filter(
      p => p !== 'ollama'
    );

    const [providerEntries, modelEntries] = await Promise.all([
      Promise.all(
        uniqueProviders.map(async provider => {
          const service = provider === 'gemini' ? 'google' : provider;
          return [provider, await fetchKeyStatus(service)] as const;
        })
      ),
      Promise.all(
        models
          .filter(([_, m]) => m.provider !== 'ollama')
          .map(async ([modelId]) => {
            return [modelId, await fetchKeyStatus(`model:${modelId}`)] as const;
          })
      ),
    ]);

    setProviderKeys(Object.fromEntries(providerEntries));
    setModelKeys(Object.fromEntries(modelEntries));
  }, [config]);

  useEffect(() => {
    fetchAllKeyStatuses();
  }, [fetchAllKeyStatuses]);

  const sortedModels = useMemo(() => {
    if (!config) return [];
    return Object.entries(config.models).sort(([a], [b]) => a.localeCompare(b));
  }, [config?.models]);

  if (!config) return null;

  const resetForm = () => {
    setEditingModel(null);
    setIsAddingModel(false);
    setShowKeyInput(false);
    setModelForm(EMPTY_FORM);
  };

  const openAddDialog = () => {
    setIsAddingModel(true);
    setEditingModel(null);
    setShowKeyInput(false);
    setModelForm({ ...EMPTY_FORM, provider: 'openrouter' });
  };

  const openEditDialog = (
    modelId: string,
    modelConfig: { provider: string; id: string; host?: string; extra_kwargs?: Record<string, any> }
  ) => {
    setEditingModel(modelId);
    setIsAddingModel(false);
    setShowKeyInput(false);
    const { base_url, ...restKwargs } = modelConfig.extra_kwargs || {};
    setModelForm({
      provider: modelConfig.provider,
      id: modelConfig.id,
      host: modelConfig.host,
      base_url: base_url as string | undefined,
      extra_kwargs: Object.keys(restKwargs).length > 0 ? JSON.stringify(restKwargs, null, 2) : '',
    });
  };

  const handleSaveModel = async () => {
    let parsedExtraKwargs = undefined;
    if (modelForm.extra_kwargs) {
      try {
        parsedExtraKwargs = JSON.parse(modelForm.extra_kwargs);
      } catch {
        toast({
          title: 'Invalid JSON',
          description: 'The Advanced Settings must be valid JSON',
          variant: 'destructive',
        });
        return;
      }
    }

    if (modelForm.base_url) {
      parsedExtraKwargs = { ...(parsedExtraKwargs || {}), base_url: modelForm.base_url };
    }

    const targetModelId = isAddingModel ? modelForm.configId! : editingModel!;

    if (modelForm.api_key) {
      try {
        const res = await fetch(`/api/credentials/model:${targetModelId}/api-key`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            service: `model:${targetModelId}`,
            api_key: modelForm.api_key,
            key_name: 'api_key',
          }),
        });
        if (!res.ok) throw new Error();
      } catch {
        toast({ title: 'Error', description: 'Failed to save API key', variant: 'destructive' });
        return;
      }
    }

    if (isAddingModel) {
      if (!modelForm.configId || !modelForm.id) {
        toast({
          title: 'Error',
          description: 'Please provide both a configuration name and model ID',
          variant: 'destructive',
        });
        return;
      }
      if (config.models[modelForm.configId]) {
        toast({
          title: 'Error',
          description: 'A model with this configuration name already exists',
          variant: 'destructive',
        });
        return;
      }
    }

    const modelData = {
      provider: modelForm.provider as any,
      id: modelForm.id,
      ...(modelForm.host && { host: modelForm.host }),
      ...(parsedExtraKwargs && { extra_kwargs: parsedExtraKwargs }),
    };

    updateModel(targetModelId, modelData);
    const action = isAddingModel ? 'Added' : 'Updated';
    resetForm();
    toast({
      title: `Model ${action}`,
      description: `Model ${targetModelId} has been ${action.toLowerCase()}`,
    });
    await fetchAllKeyStatuses();
  };

  const handleClearModelKey = async (modelId: string) => {
    const res = await fetch(`/api/credentials/model:${modelId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Failed to clear API key');
    await fetchAllKeyStatuses();
    toast({ title: 'API Key Cleared', description: `Custom API key removed for ${modelId}` });
  };

  const handleCopyKeyFrom = async (targetModelId: string, sourceModelId: string) => {
    try {
      const res = await fetch(
        `/api/credentials/model:${targetModelId}/copy-from/model:${sourceModelId}`,
        { method: 'POST' }
      );
      if (!res.ok) throw new Error();
      await fetchAllKeyStatuses();
      toast({
        title: 'API Key Copied',
        description: `Key from ${sourceModelId} applied to ${targetModelId}`,
      });
    } catch {
      toast({ title: 'Error', description: 'Failed to copy API key', variant: 'destructive' });
    }
  };

  const handleDeleteModel = (modelId: string) => {
    if (modelId === 'default') {
      toast({
        title: 'Cannot Delete',
        description: 'The default model cannot be deleted',
        variant: 'destructive',
      });
      return;
    }
    if (confirm(`Delete model "${modelId}"?`)) {
      deleteModel(modelId);
      toast({ title: 'Model Deleted', description: `Model ${modelId} has been removed` });
    }
  };

  const modelsWithKeys = Object.entries(config.models)
    .filter(([id, m]) => m.provider !== 'ollama' && modelKeys[id]?.hasKey)
    .map(([id, m]) => ({ id, provider: m.provider }));

  const currentEditId = isAddingModel ? modelForm.configId : editingModel;
  const copyableModels = modelsWithKeys.filter(m => m.id !== currentEditId);

  const renderApiKeyField = () => {
    if (modelForm.provider === 'ollama') return null;

    const targetId = isAddingModel ? modelForm.configId : editingModel;
    const modelKey = targetId ? modelKeys[targetId] : undefined;
    const hasCustomKey = modelKey?.hasKey;

    return (
      <FieldGroup label="API Key" htmlFor="dialog-api-key">
        {hasCustomKey && !showKeyInput ? (
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="bg-green-500/10 text-green-600 dark:text-green-400">
              <Key className="h-3 w-3 mr-1" />
              {modelKey.maskedKey}
            </Badge>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowKeyInput(true)}
              className="h-7 text-xs"
            >
              Change
            </Button>
            {targetId && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => handleClearModelKey(targetId)}
                className="h-7 text-xs text-destructive hover:text-destructive"
              >
                <X className="h-3 w-3 mr-1" />
                Clear
              </Button>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex gap-1">
              <Input
                id="dialog-api-key"
                type="password"
                value={modelForm.api_key || ''}
                onChange={e => setModelForm({ ...modelForm, api_key: e.target.value })}
                placeholder="Leave blank to use provider-level key"
                className="h-9 text-sm"
              />
              {hasCustomKey && showKeyInput && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => setShowKeyInput(false)}
                  className="h-9 w-9 shrink-0"
                  title="Cancel"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              )}
            </div>
            {copyableModels.length > 0 && (
              <Select
                onValueChange={sourceId => targetId && handleCopyKeyFrom(targetId, sourceId)}
                value=""
              >
                <SelectTrigger className="h-8 text-xs text-muted-foreground">
                  <div className="flex items-center gap-1.5">
                    <Copy className="h-3 w-3" />
                    <span>Use key from another model...</span>
                  </div>
                </SelectTrigger>
                <SelectContent>
                  {copyableModels.map(m => (
                    <SelectItem key={m.id} value={m.id}>
                      <div className="flex items-center gap-2">
                        <ProviderLogo provider={m.provider} className="h-3.5 w-3.5" />
                        <span>{m.id}</span>
                        {modelKeys[m.id]?.maskedKey && (
                          <span className="text-muted-foreground text-xs">
                            ({modelKeys[m.id].maskedKey})
                          </span>
                        )}
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        )}
      </FieldGroup>
    );
  };

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
          <Button onClick={openAddDialog} variant="outline" size="sm">
            <Plus className="h-4 w-4 mr-1.5" />
            Add Model
          </Button>
        </div>

        {/* Models Table */}
        <div className="border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="text-left font-medium px-4 py-2.5">Name</th>
                <th className="text-left font-medium px-4 py-2.5">Provider</th>
                <th className="text-left font-medium px-4 py-2.5">Model ID</th>
                <th className="text-left font-medium px-4 py-2.5">API Key</th>
                <th className="text-right font-medium px-4 py-2.5 w-20">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sortedModels.map(([modelId, modelConfig]) => {
                const providerInfo = getProviderInfo(modelConfig.provider);
                const keyStatus = getKeyStatusDisplay(
                  modelId,
                  modelConfig.provider,
                  modelKeys,
                  providerKeys
                );
                return (
                  <tr
                    key={modelId}
                    className="border-b last:border-b-0 hover:bg-muted/30 transition-colors"
                  >
                    <td className="px-4 py-2.5 font-medium">{modelId}</td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <ProviderLogo provider={modelConfig.provider} className="h-4 w-4" />
                        <span>{providerInfo.name}</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <code className="text-xs bg-muted px-1.5 py-0.5 rounded">
                        {modelConfig.id}
                      </code>
                    </td>
                    <td className="px-4 py-2.5">
                      {keyStatus && (
                        <Badge variant="outline" className={cn('text-xs', keyStatus.className)}>
                          {keyStatus.label}
                          {keyStatus.maskedKey && (
                            <span className="ml-1 opacity-70">{keyStatus.maskedKey}</span>
                          )}
                        </Badge>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => openEditDialog(modelId, modelConfig)}
                          className="h-7 w-7"
                          title="Edit"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        {modelId !== 'default' && (
                          <Button
                            size="icon"
                            variant="ghost"
                            onClick={() => handleDeleteModel(modelId)}
                            className="h-7 w-7 hover:bg-destructive/10"
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5 text-destructive" />
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Save All Changes */}
        <Button onClick={() => saveConfig()} variant="default" className="w-full">
          <Save className="h-4 w-4 mr-2" />
          Save All Changes
        </Button>
      </div>

      {/* Add/Edit Dialog */}
      <Dialog
        open={dialogOpen}
        onOpenChange={open => {
          if (!open) resetForm();
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{isAddingModel ? 'Add New Model' : `Edit: ${editingModel}`}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            {isAddingModel && (
              <FieldGroup label="Configuration Name" required htmlFor="dialog-config-name">
                <Input
                  id="dialog-config-name"
                  value={modelForm.configId}
                  onChange={e => setModelForm({ ...modelForm, configId: e.target.value })}
                  placeholder="e.g., my-gpt4, claude-sonnet"
                  className="h-9"
                />
              </FieldGroup>
            )}

            <FieldGroup label="Provider" required htmlFor="dialog-provider">
              <Select
                value={modelForm.provider}
                onValueChange={value => setModelForm({ ...modelForm, provider: value })}
              >
                <SelectTrigger id="dialog-provider" className="h-9">
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
            </FieldGroup>

            <FieldGroup label="Model ID" required htmlFor="dialog-model-id">
              <Input
                id="dialog-model-id"
                value={modelForm.id}
                onChange={e => setModelForm({ ...modelForm, id: e.target.value })}
                placeholder="e.g., gpt-4, claude-sonnet-4-latest"
                className="h-9"
              />
            </FieldGroup>

            {modelForm.provider === 'ollama' && (
              <FieldGroup label="Host" htmlFor="dialog-host">
                <Input
                  id="dialog-host"
                  value={modelForm.host || ''}
                  onChange={e => setModelForm({ ...modelForm, host: e.target.value })}
                  placeholder="http://localhost:11434"
                  className="h-9"
                />
              </FieldGroup>
            )}

            {modelForm.provider === 'openai' && (
              <FieldGroup label="Base URL" htmlFor="dialog-base-url">
                <Input
                  id="dialog-base-url"
                  value={modelForm.base_url || ''}
                  onChange={e => setModelForm({ ...modelForm, base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                  className="h-9"
                />
              </FieldGroup>
            )}

            {renderApiKeyField()}

            <FieldGroup label="Advanced Settings (JSON)" htmlFor="dialog-extra-kwargs">
              <Textarea
                id="dialog-extra-kwargs"
                value={modelForm.extra_kwargs || ''}
                onChange={e => setModelForm({ ...modelForm, extra_kwargs: e.target.value })}
                placeholder='{ "temperature": 0.7 }'
                className="font-mono text-xs min-h-[60px]"
              />
            </FieldGroup>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={resetForm}>
              Cancel
            </Button>
            <Button
              onClick={handleSaveModel}
              disabled={isAddingModel ? !modelForm.configId || !modelForm.id : !modelForm.id}
            >
              {isAddingModel ? 'Add' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </EditorPanel>
  );
}
