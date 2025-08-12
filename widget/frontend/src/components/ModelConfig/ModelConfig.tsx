import { useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
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
import { FieldGroup } from '@/components/shared/FieldGroup';
import { Eye, EyeOff, TestTube, Save, Plus, Trash2, Settings } from 'lucide-react';
import { toast } from '@/components/ui/toaster';

interface ModelFormData {
  provider: string;
  id: string;
  host?: string;
  configId?: string; // The key in the config.models object
}

export function ModelConfig() {
  const { config, updateModel, deleteModel, setAPIKey, testModel, saveConfig, apiKeys } =
    useConfigStore();
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});
  const [testingModel, setTestingModel] = useState<string | null>(null);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [isAddingModel, setIsAddingModel] = useState(false);
  const [modelForm, setModelForm] = useState<ModelFormData>({
    provider: 'ollama',
    id: '',
    configId: '',
  });

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
        {/* Add New Model Button */}
        {!isAddingModel && (
          <Button
            onClick={handleAddModel}
            className="w-full bg-gradient-to-r from-blue-600 to-purple-600 hover:from-blue-700 hover:to-purple-700 text-white shadow-md transition-all hover:shadow-lg hover-lift"
          >
            <Plus className="h-4 w-4 mr-2" />
            Add New Model
          </Button>
        )}

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
                    <SelectItem value="openai">OpenAI</SelectItem>
                    <SelectItem value="anthropic">Anthropic</SelectItem>
                    <SelectItem value="ollama">Ollama</SelectItem>
                    <SelectItem value="openrouter">OpenRouter</SelectItem>
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

        {/* Existing Models */}
        {Object.entries(config.models).map(([modelId, modelConfig]) => (
          <Card key={modelId} className="shadow-card hover-lift-card">
            <CardHeader className="pb-4">
              <CardTitle className="text-lg font-semibold flex items-center justify-between">
                {modelId}
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleTestModel(modelId)}
                    disabled={testingModel === modelId}
                    className="hover-lift"
                  >
                    <TestTube className="h-4 w-4 mr-1" />
                    {testingModel === modelId ? 'Testing...' : 'Test'}
                  </Button>
                  {editingModel === modelId ? (
                    <>
                      <Button size="sm" onClick={handleSaveModel} className="hover-lift">
                        Save
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={handleCancelEdit}
                        className="hover-lift"
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setEditingModel(modelId);
                          setModelForm({
                            provider: modelConfig.provider,
                            id: modelConfig.id,
                            host: modelConfig.host,
                          });
                        }}
                        className="hover-lift"
                      >
                        Edit
                      </Button>
                      {modelId !== 'default' && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => handleDeleteModel(modelId)}
                          className="hover-lift text-destructive hover:bg-destructive/10"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {editingModel === modelId ? (
                <>
                  <FieldGroup
                    label="Provider"
                    helperText="The AI provider for this model"
                    required
                    htmlFor={`provider-${modelId}`}
                  >
                    <Select
                      value={modelForm.provider}
                      onValueChange={value => setModelForm({ ...modelForm, provider: value })}
                    >
                      <SelectTrigger id={`provider-${modelId}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="openai">OpenAI</SelectItem>
                        <SelectItem value="anthropic">Anthropic</SelectItem>
                        <SelectItem value="ollama">Ollama</SelectItem>
                        <SelectItem value="openrouter">OpenRouter</SelectItem>
                      </SelectContent>
                    </Select>
                  </FieldGroup>

                  <FieldGroup
                    label="Model ID"
                    helperText="The actual model identifier used by the provider"
                    required
                    htmlFor={`model-id-${modelId}`}
                  >
                    <Input
                      id={`model-id-${modelId}`}
                      value={modelForm.id}
                      onChange={e => setModelForm({ ...modelForm, id: e.target.value })}
                      placeholder="e.g., gpt-4, claude-3, llama2"
                    />
                  </FieldGroup>

                  {modelForm.provider === 'ollama' && (
                    <FieldGroup
                      label="Host"
                      helperText="The URL where your Ollama server is running"
                      htmlFor={`host-${modelId}`}
                    >
                      <Input
                        id={`host-${modelId}`}
                        value={modelForm.host || ''}
                        onChange={e => setModelForm({ ...modelForm, host: e.target.value })}
                        placeholder="http://localhost:11434"
                      />
                    </FieldGroup>
                  )}
                </>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div>
                      <span className="text-gray-500 dark:text-gray-400">Provider:</span>{' '}
                      {modelConfig.provider}
                    </div>
                    <div>
                      <span className="text-gray-500 dark:text-gray-400">Model:</span>{' '}
                      {modelConfig.id}
                    </div>
                    {modelConfig.host && (
                      <div className="col-span-2">
                        <span className="text-gray-500 dark:text-gray-400">Host:</span>{' '}
                        {modelConfig.host}
                      </div>
                    )}
                  </div>

                  {/* API Key Management */}
                  {modelConfig.provider !== 'ollama' && (
                    <FieldGroup
                      label="API Key"
                      helperText={`Enter your ${modelConfig.provider.toUpperCase()} API key`}
                      required
                      htmlFor={`api-key-${modelId}`}
                      actions={
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            setShowKeys({
                              ...showKeys,
                              [modelConfig.provider]: !showKeys[modelConfig.provider],
                            })
                          }
                        >
                          {showKeys[modelConfig.provider] ? (
                            <EyeOff className="h-4 w-4" />
                          ) : (
                            <Eye className="h-4 w-4" />
                          )}
                        </Button>
                      }
                    >
                      <Input
                        id={`api-key-${modelId}`}
                        type={showKeys[modelConfig.provider] ? 'text' : 'password'}
                        value={apiKeys[modelConfig.provider]?.key || ''}
                        onChange={e => setAPIKey(modelConfig.provider, e.target.value)}
                        placeholder="Enter API key..."
                      />
                    </FieldGroup>
                  )}
                </>
              )}
            </CardContent>
          </Card>
        ))}

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
