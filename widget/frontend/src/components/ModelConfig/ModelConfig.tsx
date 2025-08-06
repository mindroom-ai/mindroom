import React, { useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Eye, EyeOff, TestTube, Save, Plus } from 'lucide-react';
import { toast } from '@/components/ui/toaster';

interface ModelFormData {
  provider: string;
  id: string;
  host?: string;
}

export function ModelConfig() {
  const { config, updateModel, setAPIKey, testModel, saveConfig, apiKeys } = useConfigStore();
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});
  const [testingModel, setTestingModel] = useState<string | null>(null);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [modelForm, setModelForm] = useState<ModelFormData>({
    provider: 'ollama',
    id: '',
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
    if (editingModel && modelForm.id) {
      updateModel(editingModel, {
        provider: modelForm.provider as any,
        id: modelForm.id,
        ...(modelForm.host && { host: modelForm.host }),
      });
      setEditingModel(null);
      setModelForm({ provider: 'ollama', id: '' });
      toast({
        title: 'Model Updated',
        description: `Model ${editingModel} has been updated`,
      });
    }
  };

  const handleAddModel = () => {
    const newModelId = `custom_${Date.now()}`;
    updateModel(newModelId, {
      provider: 'ollama' as any,
      id: 'new-model',
    });
    setEditingModel(newModelId);
    setModelForm({
      provider: 'ollama',
      id: 'new-model',
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-2xl font-semibold">Model Configuration</h2>
        <Button onClick={() => saveConfig()} variant="default">
          <Save className="h-4 w-4 mr-2" />
          Save All Changes
        </Button>
      </div>

      <div className="grid gap-4">
        {Object.entries(config.models).map(([modelId, modelConfig]) => (
          <Card key={modelId}>
            <CardHeader>
              <CardTitle className="text-lg flex items-center justify-between">
                {modelId}
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleTestModel(modelId)}
                    disabled={testingModel === modelId}
                  >
                    <TestTube className="h-4 w-4 mr-1" />
                    {testingModel === modelId ? 'Testing...' : 'Test'}
                  </Button>
                  {editingModel === modelId ? (
                    <>
                      <Button size="sm" onClick={handleSaveModel}>
                        Save
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setEditingModel(null);
                          setModelForm({ provider: 'ollama', id: '' });
                        }}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
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
                    >
                      Edit
                    </Button>
                  )}
                </div>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {editingModel === modelId ? (
                <>
                  <div>
                    <Label>Provider</Label>
                    <Select
                      value={modelForm.provider}
                      onValueChange={(value) => setModelForm({ ...modelForm, provider: value })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="openai">OpenAI</SelectItem>
                        <SelectItem value="anthropic">Anthropic</SelectItem>
                        <SelectItem value="ollama">Ollama</SelectItem>
                        <SelectItem value="openrouter">OpenRouter</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Model ID</Label>
                    <Input
                      value={modelForm.id}
                      onChange={(e) => setModelForm({ ...modelForm, id: e.target.value })}
                      placeholder="e.g., gpt-4, claude-3, llama2"
                    />
                  </div>
                  {modelForm.provider === 'ollama' && (
                    <div>
                      <Label>Host (optional)</Label>
                      <Input
                        value={modelForm.host || ''}
                        onChange={(e) => setModelForm({ ...modelForm, host: e.target.value })}
                        placeholder="http://localhost:11434"
                      />
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div>
                      <span className="text-gray-500">Provider:</span> {modelConfig.provider}
                    </div>
                    <div>
                      <span className="text-gray-500">Model:</span> {modelConfig.id}
                    </div>
                    {modelConfig.host && (
                      <div className="col-span-2">
                        <span className="text-gray-500">Host:</span> {modelConfig.host}
                      </div>
                    )}
                  </div>

                  {/* API Key Management */}
                  {modelConfig.provider !== 'ollama' && (
                    <div>
                      <Label>API Key</Label>
                      <div className="flex gap-2">
                        <Input
                          type={showKeys[modelConfig.provider] ? 'text' : 'password'}
                          value={apiKeys[modelConfig.provider]?.key || ''}
                          onChange={(e) => setAPIKey(modelConfig.provider, e.target.value)}
                          placeholder="Enter API key..."
                        />
                        <Button
                          size="icon"
                          variant="outline"
                          onClick={() => setShowKeys({
                            ...showKeys,
                            [modelConfig.provider]: !showKeys[modelConfig.provider]
                          })}
                        >
                          {showKeys[modelConfig.provider] ? (
                            <EyeOff className="h-4 w-4" />
                          ) : (
                            <Eye className="h-4 w-4" />
                          )}
                        </Button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      <Button onClick={handleAddModel} variant="outline" className="w-full">
        <Plus className="h-4 w-4 mr-2" />
        Add New Model
      </Button>
    </div>
  );
}
