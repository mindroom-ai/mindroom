import { useState, useMemo } from 'react';
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
import { Eye, EyeOff, TestTube, Save, Trash2, Settings, Sparkles, Code } from 'lucide-react';
import { toast } from '@/components/ui/toaster';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { ApiKeyConfig } from '@/components/ApiKeyConfig';
import { FilterSelector } from '@/components/shared/FilterSelector';
import { ProviderLogo } from './ProviderLogos';
import { getProviderInfo, getProviderList } from '@/lib/providers';

interface ModelFormData {
  provider: string;
  id: string;
  host?: string;
  configId?: string; // The key in the config.models object
  extra_kwargs?: string; // JSON string for editing
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, setAPIKey, testModel, saveConfig, apiKeys } =
    useConfigStore();
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});
  const [testingModel, setTestingModel] = useState<string | null>(null);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [isAddingModel, setIsAddingModel] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState<string>('all');
  const [modelForm, setModelForm] = useState<ModelFormData>({
    provider: 'ollama',
    id: '',
    configId: '',
  });

  // Get unique providers and filter models - must be before any conditional returns
  const providers = useMemo(() => {
    if (!config) return ['all'];
    const providerSet = new Set(Object.values(config.models).map(m => m.provider));
    return ['all', ...Array.from(providerSet)];
  }, [config?.models]);

  const filteredModels = useMemo(() => {
    if (!config) return [];
    if (selectedProvider === 'all') {
      return Object.entries(config.models);
    }
    return Object.entries(config.models).filter(
      ([_, model]) => model.provider === selectedProvider
    );
  }, [config?.models, selectedProvider]);

  if (!config) return null;

  const handleTestModel = async (modelId: string) => {
    setTestingModel(modelId);
    try {
      const success = await testModel(modelId);
      toast({
        title: success ? 'Connection Successful' : 'Connection Failed',
        description: success
          ? `Model ${modelId} is working correctly`
          : `Failed to connect to model ${modelId}`,
        variant: success ? 'default' : 'destructive',
      });
    } catch (error) {
      toast({
        title: 'Test Failed',
        description: 'An error occurred while testing the model',
        variant: 'destructive',
      });
    } finally {
      setTestingModel(null);
    }
  };

  const handleSaveModel = () => {
    // Parse extra_kwargs if provided
    let parsedExtraKwargs = undefined;
    if (modelForm.extra_kwargs) {
      try {
        parsedExtraKwargs = JSON.parse(modelForm.extra_kwargs);
      } catch (e) {
        toast({
          title: 'Invalid JSON',
          description: 'The Advanced Settings must be valid JSON',
          variant: 'destructive',
        });
        return;
      }
    }

    if (isAddingModel) {
      // Creating a new model
      if (!modelForm.configId || !modelForm.id) {
        toast({
          title: 'Error',
          description: 'Please provide both a configuration name and model ID',
          variant: 'destructive',
        });
        return;
      }

      // Check if configId already exists
      if (config.models[modelForm.configId]) {
        toast({
          title: 'Error',
          description: 'A model with this configuration name already exists',
          variant: 'destructive',
        });
        return;
      }

      updateModel(modelForm.configId, {
        provider: modelForm.provider as any,
        id: modelForm.id,
        ...(modelForm.host && { host: modelForm.host }),
        ...(parsedExtraKwargs && { extra_kwargs: parsedExtraKwargs }),
      });

      setIsAddingModel(false);
      setModelForm({ provider: 'ollama', id: '', configId: '' });
      toast({
        title: 'Model Added',
        description: `Model ${modelForm.configId} has been created`,
      });
    } else if (editingModel && modelForm.id) {
      // Updating existing model
      updateModel(editingModel, {
        provider: modelForm.provider as any,
        id: modelForm.id,
        ...(modelForm.host && { host: modelForm.host }),
        ...(parsedExtraKwargs && { extra_kwargs: parsedExtraKwargs }),
      });
      setEditingModel(null);
      setModelForm({ provider: 'ollama', id: '', configId: '' });
      toast({
        title: 'Model Updated',
        description: `Model ${editingModel} has been updated`,
      });
    }
  };

  const handleAddModel = () => {
    setIsAddingModel(true);
    setEditingModel(null);
    setModelForm({
      provider: 'openrouter',
      id: '',
      configId: '',
    });
  };

  const handleCancelEdit = () => {
    setEditingModel(null);
    setIsAddingModel(false);
    setModelForm({ provider: 'ollama', id: '', configId: '' });
  };

  const handleDeleteModel = (modelId: string) => {
    // Don't allow deleting default model
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
      toast({
        title: 'Model Deleted',
        description: `Model ${modelId} has been removed`,
      });
    }
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
          {/* Add New Model Button */}
          {!isAddingModel && (
            <Button
              onClick={handleAddModel}
              className="glass-card hover:glass px-4 py-2 transition-all duration-200 hover:scale-105 shadow-lg hover:shadow-xl"
              variant="outline"
            >
              <Sparkles className="h-4 w-4 mr-2 text-primary" />
              <span className="font-medium">Add New Model</span>
            </Button>
          )}

          {/* Provider Filter Selector */}
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

              <FieldGroup
                label="Provider"
                helperText="The AI provider for this model"
                required
                htmlFor="provider"
              >
                <Select
                  value={modelForm.provider}
                  onValueChange={value => setModelForm({ ...modelForm, provider: value })}
                >
                  <SelectTrigger id="provider">
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
                helperText="The actual model identifier used by the provider"
                required
                htmlFor="model-id"
              >
                <Input
                  id="model-id"
                  value={modelForm.id}
                  onChange={e => setModelForm({ ...modelForm, id: e.target.value })}
                  placeholder="e.g., gpt-4, claude-3-opus, meta-llama/llama-3-70b"
                />
              </FieldGroup>

              {modelForm.provider === 'ollama' && (
                <FieldGroup
                  label="Host"
                  helperText="The URL where your Ollama server is running"
                  htmlFor="host"
                >
                  <Input
                    id="host"
                    value={modelForm.host || ''}
                    onChange={e => setModelForm({ ...modelForm, host: e.target.value })}
                    placeholder="http://localhost:11434"
                  />
                </FieldGroup>
              )}

              {/* Advanced Settings (extra_kwargs) */}
              <FieldGroup
                label="Advanced Settings (JSON)"
                helperText={
                  modelForm.provider === 'openrouter'
                    ? 'Provider routing, custom parameters, etc. Example: {"request_params": {"provider": {"order": ["Cerebras"]}}}'
                    : 'Provider-specific parameters like temperature, max_tokens, etc.'
                }
                htmlFor="extra-kwargs"
              >
                <Textarea
                  id="extra-kwargs"
                  value={modelForm.extra_kwargs || ''}
                  onChange={e => setModelForm({ ...modelForm, extra_kwargs: e.target.value })}
                  placeholder={
                    modelForm.provider === 'openrouter'
                      ? '{\n  "request_params": {\n    "provider": {\n      "order": ["Cerebras"]\n    }\n  }\n}'
                      : '{\n  "temperature": 0.7,\n  "max_tokens": 4096\n}'
                  }
                  className="font-mono text-sm min-h-[120px]"
                />
              </FieldGroup>

              <div className="flex gap-3 pt-4">
                <Button
                  onClick={handleSaveModel}
                  disabled={!modelForm.configId || !modelForm.id}
                  className="hover-lift"
                >
                  Add Model
                </Button>
                <Button variant="outline" onClick={handleCancelEdit} className="hover-lift">
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
                        <Badge
                          variant="outline"
                          className={cn('mt-1.5 text-xs', providerInfo.color)}
                        >
                          {providerInfo.name}
                        </Badge>
                      </div>
                      <div className="flex gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => handleTestModel(modelId)}
                          disabled={testingModel === modelId}
                          className="h-8 w-8 hover:bg-primary/10"
                          title="Test Connection"
                        >
                          <TestTube className="h-4 w-4" />
                        </Button>
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
                              onClick={handleCancelEdit}
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
                              onClick={() => {
                                setEditingModel(modelId);
                                setModelForm({
                                  provider: modelConfig.provider,
                                  id: modelConfig.id,
                                  host: modelConfig.host,
                                  extra_kwargs: modelConfig.extra_kwargs
                                    ? JSON.stringify(modelConfig.extra_kwargs, null, 2)
                                    : '',
                                });
                              }}
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
                      <>
                        <FieldGroup
                          label="Provider"
                          helperText=""
                          required
                          htmlFor={`provider-${modelId}`}
                        >
                          <Select
                            value={modelForm.provider}
                            onValueChange={value => setModelForm({ ...modelForm, provider: value })}
                          >
                            <SelectTrigger id={`provider-${modelId}`} className="h-9">
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
                          helperText=""
                          required
                          htmlFor={`model-id-${modelId}`}
                        >
                          <Input
                            id={`model-id-${modelId}`}
                            value={modelForm.id}
                            onChange={e => setModelForm({ ...modelForm, id: e.target.value })}
                            placeholder="e.g., gpt-4, claude-3, llama2"
                            className="h-9"
                          />
                        </FieldGroup>

                        {modelForm.provider === 'ollama' && (
                          <FieldGroup label="Host" helperText="" htmlFor={`host-${modelId}`}>
                            <Input
                              id={`host-${modelId}`}
                              value={modelForm.host || ''}
                              onChange={e => setModelForm({ ...modelForm, host: e.target.value })}
                              placeholder="http://localhost:11434"
                              className="h-9"
                            />
                          </FieldGroup>
                        )}

                        <FieldGroup
                          label="Advanced Settings (JSON)"
                          helperText=""
                          htmlFor={`extra-kwargs-${modelId}`}
                        >
                          <Textarea
                            id={`extra-kwargs-${modelId}`}
                            value={modelForm.extra_kwargs || ''}
                            onChange={e =>
                              setModelForm({ ...modelForm, extra_kwargs: e.target.value })
                            }
                            placeholder="{ }"
                            className="font-mono text-xs min-h-[80px]"
                          />
                        </FieldGroup>
                      </>
                    ) : (
                      <>
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
                          {modelConfig.extra_kwargs && (
                            <div className="flex items-start gap-2">
                              <span className="text-muted-foreground">
                                <Code className="h-3 w-3 inline mr-1" />
                                Advanced:
                              </span>
                              <code className="text-xs bg-muted px-1.5 py-0.5 rounded block max-w-[150px] truncate">
                                {JSON.stringify(modelConfig.extra_kwargs)}
                              </code>
                            </div>
                          )}
                        </div>

                        {/* API Key Management */}
                        {modelConfig.provider !== 'ollama' && (
                          <div className="space-y-1">
                            <label className="text-xs text-muted-foreground">API Key</label>
                            <div className="flex gap-1">
                              <Input
                                id={`api-key-${modelId}`}
                                type={showKeys[modelConfig.provider] ? 'text' : 'password'}
                                value={apiKeys[modelConfig.provider]?.key || ''}
                                onChange={e => setAPIKey(modelConfig.provider, e.target.value)}
                                placeholder="Enter API key..."
                                className="h-9 text-sm"
                              />
                              <Button
                                size="icon"
                                variant="ghost"
                                onClick={() =>
                                  setShowKeys({
                                    ...showKeys,
                                    [modelConfig.provider]: !showKeys[modelConfig.provider],
                                  })
                                }
                                className="h-9 w-9 shrink-0"
                              >
                                {showKeys[modelConfig.provider] ? (
                                  <EyeOff className="h-3.5 w-3.5" />
                                ) : (
                                  <Eye className="h-3.5 w-3.5" />
                                )}
                              </Button>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}

        {/* API Key Configuration Section */}
        <div className="space-y-4 pt-6 border-t border-border">
          <h3 className="text-lg font-semibold mb-4">Provider API Keys</h3>
          <div className="grid gap-4 md:grid-cols-2">
            {getProviderList()
              .filter(provider => provider.requiresApiKey)
              .map(provider => (
                <ApiKeyConfig
                  key={provider.id}
                  service={provider.id === 'gemini' ? 'google' : provider.id}
                  displayName={provider.name}
                  description={provider.description || `Configure your ${provider.name} API key`}
                />
              ))}
          </div>
        </div>

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
