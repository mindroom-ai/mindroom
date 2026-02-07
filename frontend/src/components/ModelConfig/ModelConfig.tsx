import { useState, useMemo, useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
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
import { EditorPanel } from '@/components/shared/EditorPanel';
import { FieldGroup } from '@/components/shared/FieldGroup';
import { Save, Trash2, Settings, Sparkles, Code, Globe, Key, X, Copy } from 'lucide-react';
import { toast } from '@/components/ui/toaster';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { FilterSelector } from '@/components/shared/FilterSelector';
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
      variant: 'default' as const,
      maskedKey: modelKey.maskedKey,
      className: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
    };
  }

  const providerKey = providerKeys[provider];
  if (providerKey?.hasKey) {
    return {
      label: providerKey.source === 'env' ? 'From environment' : 'Provider key',
      variant: 'secondary' as const,
      maskedKey: providerKey.maskedKey,
      className: '',
    };
  }

  return { label: 'No API key', variant: 'destructive' as const, maskedKey: null, className: '' };
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, saveConfig } = useConfigStore();
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [isAddingModel, setIsAddingModel] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState<string>('all');
  const [showKeyInput, setShowKeyInput] = useState(false);
  const [modelForm, setModelForm] = useState<ModelFormData>(EMPTY_FORM);

  const [providerKeys, setProviderKeys] = useState<Record<string, KeyStatus>>({});
  const [modelKeys, setModelKeys] = useState<Record<string, KeyStatus>>({});

  const providers = useMemo(() => {
    if (!config) return ['all'];
    const providerSet = new Set(Object.values(config.models).map(m => m.provider));
    return ['all', ...Array.from(providerSet)];
  }, [config?.models]);

  const filteredModels = useMemo(() => {
    if (!config) return [];
    if (selectedProvider === 'all') return Object.entries(config.models);
    return Object.entries(config.models).filter(
      ([_, model]) => model.provider === selectedProvider
    );
  }, [config?.models, selectedProvider]);

  const fetchAllKeyStatuses = useCallback(async () => {
    if (!config) return;

    const models = Object.entries(config.models);
    const uniqueProviders = [...new Set(models.map(([_, m]) => m.provider))].filter(
      p => p !== 'ollama'
    );

    // Fetch provider and model key statuses in parallel
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

  if (!config) return null;

  const resetForm = () => {
    setEditingModel(null);
    setIsAddingModel(false);
    setShowKeyInput(false);
    setModelForm(EMPTY_FORM);
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

  const handleDeleteModel = (modelId: string) => {
    if (modelId === 'default') {
      toast({
        title: 'Cannot Delete',
        description: 'The default model cannot be deleted',
        variant: 'destructive',
      });
      return;
    }
    if (confirm(`Are you sure you want to delete the model "${modelId}"?`)) {
      deleteModel(modelId);
      toast({ title: 'Model Deleted', description: `Model ${modelId} has been removed` });
    }
  };

  const startEditing = (
    modelId: string,
    modelConfig: { provider: string; id: string; host?: string; extra_kwargs?: Record<string, any> }
  ) => {
    setEditingModel(modelId);
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

  // Shared form fields rendered in both Add and Edit modes
  const renderFormFields = (idPrefix: string, showHelperText: boolean) => (
    <>
      <FieldGroup
        label="Provider"
        helperText={showHelperText ? 'The AI provider for this model' : ''}
        required
        htmlFor={`provider-${idPrefix}`}
      >
        <Select
          value={modelForm.provider}
          onValueChange={value => setModelForm({ ...modelForm, provider: value })}
        >
          <SelectTrigger id={`provider-${idPrefix}`} className="h-9">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {getProviderList().map(provider => (
              <SelectItem key={provider.id} value={provider.id}>
                <div className="flex items-center gap-2">
                  <span aria-hidden="true">
                    <ProviderLogo provider={provider.id} className="h-4 w-4" />
                  </span>
                  <span>{provider.name}</span>
                </div>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </FieldGroup>

      <FieldGroup
        label="Model ID"
        helperText={showHelperText ? 'The actual model identifier used by the provider' : ''}
        required
        htmlFor={`model-id-${idPrefix}`}
      >
        <Input
          id={`model-id-${idPrefix}`}
          value={modelForm.id}
          onChange={e => setModelForm({ ...modelForm, id: e.target.value })}
          placeholder="e.g., gpt-4, claude-3-opus, meta-llama/llama-3-70b"
          className="h-9"
        />
      </FieldGroup>

      {modelForm.provider === 'ollama' && (
        <FieldGroup
          label="Host"
          helperText={showHelperText ? 'The URL where your Ollama server is running' : ''}
          htmlFor={`host-${idPrefix}`}
        >
          <Input
            id={`host-${idPrefix}`}
            value={modelForm.host || ''}
            onChange={e => setModelForm({ ...modelForm, host: e.target.value })}
            placeholder="http://localhost:11434"
            className="h-9"
          />
        </FieldGroup>
      )}

      {modelForm.provider === 'openai' && (
        <FieldGroup
          label="Base URL"
          helperText={
            showHelperText
              ? 'Custom base URL for OpenAI-compatible APIs (e.g., local inference servers)'
              : ''
          }
          htmlFor={`base-url-${idPrefix}`}
        >
          <Input
            id={`base-url-${idPrefix}`}
            value={modelForm.base_url || ''}
            onChange={e => setModelForm({ ...modelForm, base_url: e.target.value })}
            placeholder="https://api.openai.com/v1"
            className="h-9"
          />
        </FieldGroup>
      )}

      {renderApiKeyField(idPrefix)}

      <FieldGroup
        label="Advanced Settings (JSON)"
        helperText={
          showHelperText
            ? modelForm.provider === 'openrouter'
              ? 'Provider routing, custom parameters, etc.'
              : 'Provider-specific parameters like temperature, max_tokens, etc.'
            : ''
        }
        htmlFor={`extra-kwargs-${idPrefix}`}
      >
        <Textarea
          id={`extra-kwargs-${idPrefix}`}
          value={modelForm.extra_kwargs || ''}
          onChange={e => setModelForm({ ...modelForm, extra_kwargs: e.target.value })}
          placeholder={
            modelForm.provider === 'openrouter'
              ? '{\n  "request_params": {\n    "provider": {\n      "order": ["Cerebras"]\n    }\n  }\n}'
              : '{\n  "temperature": 0.7,\n  "max_tokens": 4096\n}'
          }
          className="font-mono text-xs min-h-[80px]"
        />
      </FieldGroup>
    </>
  );

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

  const getModelsWithKeys = (currentModelId: string) => {
    if (!config) return [];
    return Object.entries(config.models)
      .filter(
        ([id, m]) => id !== currentModelId && m.provider !== 'ollama' && modelKeys[id]?.hasKey
      )
      .map(([id, m]) => ({ id, provider: m.provider }));
  };

  const renderApiKeyField = (idPrefix: string) => {
    if (modelForm.provider === 'ollama') return null;

    const modelKey = modelKeys[idPrefix];
    const hasCustomKey = modelKey?.hasKey;
    const modelsWithKeys = getModelsWithKeys(idPrefix);

    return (
      <FieldGroup
        label="API Key"
        helperText={
          hasCustomKey
            ? 'This model has a custom API key set'
            : 'Leave blank to use the provider-level key'
        }
        htmlFor={`api-key-${idPrefix}`}
      >
        {hasCustomKey && !showKeyInput ? (
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="bg-green-500/10 text-green-600 dark:text-green-400">
              <Key className="h-3 w-3 mr-1" />
              Custom key: {modelKey.maskedKey}
            </Badge>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowKeyInput(true)}
              className="h-7 text-xs"
            >
              Change
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => handleClearModelKey(idPrefix)}
              className="h-7 text-xs text-destructive hover:text-destructive"
            >
              <X className="h-3 w-3 mr-1" />
              Clear
            </Button>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex gap-1">
              <Input
                id={`api-key-${idPrefix}`}
                type="password"
                value={modelForm.api_key || ''}
                onChange={e => setModelForm({ ...modelForm, api_key: e.target.value })}
                placeholder="Enter API key to override provider key..."
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
            {modelsWithKeys.length > 0 && (
              <Select onValueChange={sourceId => handleCopyKeyFrom(idPrefix, sourceId)} value="">
                <SelectTrigger className="h-8 text-xs text-muted-foreground">
                  <div className="flex items-center gap-1.5">
                    <Copy className="h-3 w-3" />
                    <span>Use key from another model...</span>
                  </div>
                </SelectTrigger>
                <SelectContent>
                  {modelsWithKeys.map(m => (
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

  const renderExtraKwargsDisplay = (extraKwargs: Record<string, any>) => {
    const { base_url, ...rest } = extraKwargs;
    return (
      <>
        {base_url && (
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">
              <Globe className="h-3 w-3 inline mr-1" />
              Base URL:
            </span>
            <code className="text-xs bg-muted px-1.5 py-0.5 rounded truncate max-w-[150px]">
              {base_url as string}
            </code>
          </div>
        )}
        {Object.keys(rest).length > 0 && (
          <div className="flex items-start gap-2">
            <span className="text-muted-foreground">
              <Code className="h-3 w-3 inline mr-1" />
              Advanced:
            </span>
            <code className="text-xs bg-muted px-1.5 py-0.5 rounded block max-w-[150px] truncate">
              {JSON.stringify(rest)}
            </code>
          </div>
        )}
      </>
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
      <div className="space-y-6">
        {/* Header with Add Button and Provider Filter */}
        <div className="flex flex-col sm:flex-row gap-4 items-start sm:items-center justify-between">
          {!isAddingModel && (
            <Button
              onClick={() => {
                setIsAddingModel(true);
                setEditingModel(null);
                setShowKeyInput(false);
                setModelForm({ ...EMPTY_FORM, provider: 'openrouter' });
              }}
              className="glass-card hover:glass px-4 py-2 transition-all duration-200 hover:scale-105 shadow-lg hover:shadow-xl"
              variant="outline"
            >
              <Sparkles className="h-4 w-4 mr-2 text-primary" />
              <span className="font-medium">Add New Model</span>
            </Button>
          )}

          {!isAddingModel && providers.length > 1 && (
            <FilterSelector
              options={providers.map(provider => {
                const providerInfo = provider === 'all' ? null : getProviderInfo(provider);
                const count =
                  provider === 'all'
                    ? undefined
                    : Object.values(config.models).filter(m => m.provider === provider).length;
                return {
                  value: provider,
                  label: provider === 'all' ? 'All' : providerInfo?.name || provider,
                  count,
                  showIcon: provider === 'all',
                  icon: provider !== 'all' ? providerInfo?.icon('h-4 w-4') : undefined,
                };
              })}
              value={selectedProvider}
              onChange={value => setSelectedProvider(value as string)}
              className="w-full sm:w-auto"
              showFilterIcon={false}
            />
          )}
        </div>

        {/* New Model Form */}
        {isAddingModel && (
          <Card className="shadow-md">
            <CardHeader className="pb-4">
              <CardTitle className="text-lg font-semibold">Add New Model</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <FieldGroup
                label="Configuration Name"
                helperText="A unique name to identify this model configuration"
                required
                htmlFor="config-name"
              >
                <Input
                  id="config-name"
                  value={modelForm.configId}
                  onChange={e => setModelForm({ ...modelForm, configId: e.target.value })}
                  placeholder="e.g., openrouter-gpt4, anthropic-claude3"
                />
              </FieldGroup>

              {renderFormFields('new', true)}

              <div className="flex gap-3 pt-4">
                <Button
                  onClick={handleSaveModel}
                  disabled={!modelForm.configId || !modelForm.id}
                  className="hover-lift"
                >
                  Add Model
                </Button>
                <Button variant="outline" onClick={resetForm} className="hover-lift">
                  Cancel
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Existing Models Grid */}
        {filteredModels.length === 0 ? (
          <Card className="glass-subtle p-8 text-center">
            <div className="text-muted-foreground">
              <Sparkles className="h-12 w-12 mx-auto mb-3 opacity-30" />
              <p className="text-sm">No models found for the selected provider.</p>
              <p className="text-xs mt-1">Try selecting a different provider or add a new model.</p>
            </div>
          </Card>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {filteredModels.map(([modelId, modelConfig]) => {
              const providerInfo = getProviderInfo(modelConfig.provider);
              const keyStatus = getKeyStatusDisplay(
                modelId,
                modelConfig.provider,
                modelKeys,
                providerKeys
              );
              return (
                <Card
                  key={modelId}
                  className={cn(
                    'glass-card hover:glass transition-all duration-200 hover:scale-[1.02]',
                    'relative overflow-hidden'
                  )}
                >
                  <CardHeader className="pb-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <ProviderLogo
                            provider={modelConfig.provider}
                            className="h-5 w-5 opacity-70"
                          />
                          <CardTitle className="text-base font-semibold truncate">
                            {modelId}
                          </CardTitle>
                        </div>
                        <div className="flex items-center gap-1.5 mt-1.5">
                          <Badge variant="outline" className={cn('text-xs', providerInfo.color)}>
                            {providerInfo.name}
                          </Badge>
                          {keyStatus && (
                            <Badge
                              variant={keyStatus.variant}
                              className={cn('text-xs', keyStatus.className)}
                            >
                              <Key className="h-2.5 w-2.5 mr-1" />
                              {keyStatus.label}
                            </Badge>
                          )}
                        </div>
                      </div>
                      <div className="flex gap-1">
                        {editingModel === modelId ? (
                          <>
                            <Button
                              size="icon"
                              variant="ghost"
                              onClick={handleSaveModel}
                              className="h-8 w-8 hover:bg-green-500/10"
                              title="Save"
                              aria-label="Save"
                            >
                              <Save className="h-4 w-4 text-green-600 dark:text-green-400" />
                            </Button>
                            <Button
                              size="icon"
                              variant="ghost"
                              onClick={resetForm}
                              className="h-8 w-8 hover:bg-gray-500/10"
                              title="Cancel"
                              aria-label="Cancel"
                            >
                              <span className="text-sm">✕</span>
                            </Button>
                          </>
                        ) : (
                          <>
                            <Button
                              size="icon"
                              variant="ghost"
                              onClick={() => startEditing(modelId, modelConfig)}
                              className="h-8 w-8 hover:bg-primary/10"
                              title="Edit"
                              aria-label="Edit"
                            >
                              <Settings className="h-4 w-4" />
                            </Button>
                            {modelId !== 'default' && (
                              <Button
                                size="icon"
                                variant="ghost"
                                onClick={() => handleDeleteModel(modelId)}
                                className="h-8 w-8 hover:bg-destructive/10"
                                title="Delete"
                              >
                                <Trash2 className="h-4 w-4 text-destructive" />
                              </Button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3 pt-2">
                    {editingModel === modelId ? (
                      renderFormFields(modelId, false)
                    ) : (
                      <div className="space-y-1.5 text-sm">
                        <div className="flex items-center gap-2">
                          <span className="text-muted-foreground">Model:</span>
                          <code className="text-xs bg-muted px-1.5 py-0.5 rounded">
                            {modelConfig.id}
                          </code>
                        </div>
                        {modelConfig.host && (
                          <div className="flex items-center gap-2">
                            <span className="text-muted-foreground">Host:</span>
                            <code className="text-xs bg-muted px-1.5 py-0.5 rounded truncate max-w-[150px]">
                              {modelConfig.host}
                            </code>
                          </div>
                        )}
                        {modelConfig.extra_kwargs &&
                          renderExtraKwargsDisplay(modelConfig.extra_kwargs)}
                        {keyStatus?.maskedKey && (
                          <div className="flex items-center gap-2">
                            <span className="text-muted-foreground">
                              <Key className="h-3 w-3 inline mr-1" />
                              Key:
                            </span>
                            <code className="text-xs bg-muted px-1.5 py-0.5 rounded font-mono">
                              {keyStatus.maskedKey}
                            </code>
                          </div>
                        )}
                      </div>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}

        {/* Save All Changes Button */}
        <div className="pt-6 border-t border-border">
          <Button onClick={() => saveConfig()} variant="default" className="w-full hover-lift">
            <Save className="h-4 w-4 mr-2" />
            Save All Changes
          </Button>
        </div>
      </div>
    </EditorPanel>
  );
}
