import { useConfigStore } from '@/store/configStore';
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
import { Brain } from 'lucide-react';
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
    <EditorPanel
      icon={Brain}
      title="Memory Configuration"
      isDirty={isDirty}
      onSave={handleSave}
      onDelete={() => {}}
      showActions={true}
      disableDelete={true}
      className="h-full"
    >
      <div className="space-y-6">
        {/* Description Section */}
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            Configure the embedder for agent memory storage and retrieval.
          </p>
        </div>

        {/* Configuration Fields */}
        <div className="space-y-4">
          <FieldGroup
            label="Embedder Provider"
            helperText={
              localConfig.provider === 'ollama'
                ? 'Local embeddings using Ollama'
                : localConfig.provider === 'openai'
                  ? 'Cloud embeddings using OpenAI API'
                  : localConfig.provider === 'huggingface'
                    ? 'Cloud embeddings using HuggingFace API'
                    : localConfig.provider === 'sentence-transformers'
                      ? 'Local embeddings using sentence-transformers'
                      : 'Choose your embedding provider'
            }
            required
            htmlFor="provider"
          >
            <Select value={localConfig.provider} onValueChange={handleProviderChange}>
              <SelectTrigger id="provider" className="transition-colors hover:border-ring">
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
          </FieldGroup>

          <FieldGroup
            label="Embedding Model"
            helperText="The model used to generate embeddings for memory storage"
            required
            htmlFor="model"
          >
            <Select value={localConfig.model} onValueChange={handleModelChange}>
              <SelectTrigger id="model" className="transition-colors hover:border-ring">
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
          </FieldGroup>

          {/* Host Configuration (for Ollama) */}
          {localConfig.provider === 'ollama' && (
            <FieldGroup
              label="Ollama Host URL"
              helperText="The URL where your Ollama server is running"
              required
              htmlFor="host"
            >
              <Input
                id="host"
                type="url"
                value={localConfig.host}
                onChange={e => handleHostChange(e.target.value)}
                placeholder="http://localhost:11434"
                className="transition-colors hover:border-ring focus:border-ring"
              />
            </FieldGroup>
          )}
        </div>

        {/* API Key Notice */}
        {(localConfig.provider === 'openai' || localConfig.provider === 'huggingface') && (
          <div className="p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800/30 rounded-lg shadow-sm">
            <p className="text-sm text-yellow-800 dark:text-yellow-300">
              <strong>Note:</strong> You'll need to set the {localConfig.provider.toUpperCase()}
              _API_KEY environment variable for this provider to work.
            </p>
          </div>
        )}

        {/* Current Configuration Display */}
        <div className="p-4 bg-muted/50 rounded-lg shadow-sm border border-border">
          <h3 className="text-sm font-medium mb-3">Current Configuration</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Provider:</span>
              <span className="font-mono text-foreground">{localConfig.provider}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Model:</span>
              <span className="font-mono text-foreground">{localConfig.model}</span>
            </div>
            {localConfig.provider === 'ollama' && (
              <div className="flex justify-between">
                <span className="text-muted-foreground">Host:</span>
                <span className="font-mono text-foreground">{localConfig.host}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </EditorPanel>
  );
}
