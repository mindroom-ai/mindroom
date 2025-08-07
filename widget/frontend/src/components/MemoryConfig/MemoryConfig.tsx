import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Brain, Save } from 'lucide-react';
import { useState, useEffect } from 'react';

const EMBEDDER_PROVIDERS = [
  { value: 'ollama', label: 'Ollama (Local)' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'huggingface', label: 'HuggingFace' },
  { value: 'sentence-transformers', label: 'Sentence Transformers' },
];

const OLLAMA_MODELS = ['nomic-embed-text', 'all-minilm', 'mxbai-embed-large'];

const OPENAI_MODELS = [
  'text-embedding-ada-002',
  'text-embedding-3-small',
  'text-embedding-3-large',
];

export function MemoryConfig() {
  const { config, updateMemoryConfig, saveConfig, isDirty } = useConfigStore();
  const [localConfig, setLocalConfig] = useState({
    provider: 'ollama',
    model: 'nomic-embed-text',
    host: 'http://localhost:11434',
  });

  useEffect(() => {
    if (config?.memory?.embedder) {
      setLocalConfig({
        provider: config.memory.embedder.provider,
        model: config.memory.embedder.config.model,
        host: config.memory.embedder.config.host || 'http://localhost:11434',
      });
    }
  }, [config]);

  const handleProviderChange = (provider: string) => {
    let defaultModel = '';
    switch (provider) {
      case 'ollama':
        defaultModel = 'nomic-embed-text';
        break;
      case 'openai':
        defaultModel = 'text-embedding-ada-002';
        break;
      case 'huggingface':
        defaultModel = 'sentence-transformers/all-MiniLM-L6-v2';
        break;
      case 'sentence-transformers':
        defaultModel = 'all-MiniLM-L6-v2';
        break;
    }

    const updated = { ...localConfig, provider, model: defaultModel };
    setLocalConfig(updated);
    updateMemoryConfig(updated);
  };

  const handleModelChange = (model: string) => {
    const updated = { ...localConfig, model };
    setLocalConfig(updated);
    updateMemoryConfig(updated);
  };

  const handleHostChange = (host: string) => {
    const updated = { ...localConfig, host };
    setLocalConfig(updated);
    updateMemoryConfig(updated);
  };

  const handleSave = async () => {
    await saveConfig();
  };

  const getAvailableModels = () => {
    switch (localConfig.provider) {
      case 'ollama':
        return OLLAMA_MODELS;
      case 'openai':
        return OPENAI_MODELS;
      case 'huggingface':
        return [
          'sentence-transformers/all-MiniLM-L6-v2',
          'sentence-transformers/all-mpnet-base-v2',
        ];
      case 'sentence-transformers':
        return ['all-MiniLM-L6-v2', 'all-mpnet-base-v2', 'multi-qa-MiniLM-L6-cos-v1'];
      default:
        return [];
    }
  };

  return (
    <Card className="h-full flex flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Brain className="h-5 w-5" />
            Memory Configuration
          </CardTitle>
          <Button size="sm" onClick={handleSave} disabled={!isDirty}>
            <Save className="h-4 w-4 mr-1" />
            Save
          </Button>
        </div>
        <p className="text-sm text-gray-600 mt-2">
          Configure the embedder for agent memory storage and retrieval.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Provider Selection */}
        <div>
          <Label htmlFor="provider">Embedder Provider</Label>
          <Select value={localConfig.provider} onValueChange={handleProviderChange}>
            <SelectTrigger id="provider">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {EMBEDDER_PROVIDERS.map(provider => (
                <SelectItem key={provider.value} value={provider.value}>
                  {provider.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-gray-500 mt-1">
            {localConfig.provider === 'ollama' && 'Local embeddings using Ollama'}
            {localConfig.provider === 'openai' && 'Cloud embeddings using OpenAI API'}
            {localConfig.provider === 'huggingface' && 'Cloud embeddings using HuggingFace API'}
            {localConfig.provider === 'sentence-transformers' &&
              'Local embeddings using sentence-transformers'}
          </p>
        </div>

        {/* Model Selection */}
        <div>
          <Label htmlFor="model">Embedding Model</Label>
          <Select value={localConfig.model} onValueChange={handleModelChange}>
            <SelectTrigger id="model">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {getAvailableModels().map(model => (
                <SelectItem key={model} value={model}>
                  {model}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-gray-500 mt-1">
            The model used to generate embeddings for memory storage
          </p>
        </div>

        {/* Host Configuration (for Ollama) */}
        {localConfig.provider === 'ollama' && (
          <div>
            <Label htmlFor="host">Ollama Host URL</Label>
            <Input
              id="host"
              type="url"
              value={localConfig.host}
              onChange={e => handleHostChange(e.target.value)}
              placeholder="http://localhost:11434"
            />
            <p className="text-xs text-gray-500 mt-1">
              The URL where your Ollama server is running
            </p>
          </div>
        )}

        {/* API Key Notice */}
        {(localConfig.provider === 'openai' || localConfig.provider === 'huggingface') && (
          <div className="p-3 bg-yellow-50 border border-yellow-200 rounded-lg">
            <p className="text-sm text-yellow-800">
              <strong>Note:</strong> You'll need to set the {localConfig.provider.toUpperCase()}
              _API_KEY environment variable for this provider to work.
            </p>
          </div>
        )}

        {/* Current Configuration Display */}
        <div className="mt-6 p-4 bg-gray-50 rounded-lg">
          <h3 className="text-sm font-medium mb-2">Current Configuration</h3>
          <div className="space-y-1 text-sm">
            <div>
              <span className="text-gray-600">Provider:</span>{' '}
              <span className="font-mono">{localConfig.provider}</span>
            </div>
            <div>
              <span className="text-gray-600">Model:</span>{' '}
              <span className="font-mono">{localConfig.model}</span>
            </div>
            {localConfig.provider === 'ollama' && (
              <div>
                <span className="text-gray-600">Host:</span>{' '}
                <span className="font-mono">{localConfig.host}</span>
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
