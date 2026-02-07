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
  { value: 'openai', label: 'OpenAI' },
  { value: 'ollama', label: 'Ollama' },
];

const DEFAULT_MODELS: Record<string, string> = {
  openai: 'text-embedding-3-small',
  ollama: 'nomic-embed-text',
};

const DEFAULT_HOSTS: Record<string, string> = {
  openai: '',
  ollama: 'http://localhost:11434',
};

const MODEL_PLACEHOLDERS: Record<string, string> = {
  openai: 'e.g. text-embedding-3-small',
  ollama: 'e.g. nomic-embed-text',
};

export function MemoryConfig() {
  const { config, updateMemoryConfig, saveConfig, isDirty } = useConfigStore();
  const [localConfig, setLocalConfig] = useState({
    provider: 'openai',
    model: 'text-embedding-3-small',
    host: '',
  });

  useEffect(() => {
    if (config?.memory?.embedder) {
      setLocalConfig({
        provider: config.memory.embedder.provider,
        model: config.memory.embedder.config.model,
        host:
          config.memory.embedder.config.host ||
          DEFAULT_HOSTS[config.memory.embedder.provider] ||
          '',
      });
    }
  }, [config]);

  const handleProviderChange = (provider: string) => {
    const updated = {
      ...localConfig,
      provider,
      model: DEFAULT_MODELS[provider] || '',
      host: DEFAULT_HOSTS[provider] || '',
    };
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
                  ? 'OpenAI or any OpenAI-compatible API (set Base URL below)'
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
            <Input
              id="model"
              type="text"
              value={localConfig.model}
              onChange={e => handleModelChange(e.target.value)}
              placeholder={MODEL_PLACEHOLDERS[localConfig.provider] || 'Model name'}
              className="transition-colors hover:border-ring focus:border-ring"
            />
          </FieldGroup>

          {/* Host / Base URL */}
          <FieldGroup
            label={localConfig.provider === 'ollama' ? 'Ollama Host URL' : 'Base URL'}
            helperText={
              localConfig.provider === 'ollama'
                ? 'The URL where your Ollama server is running'
                : 'Leave empty for official OpenAI API, or set for OpenAI-compatible servers'
            }
            required={localConfig.provider === 'ollama'}
            htmlFor="host"
          >
            <Input
              id="host"
              type="url"
              value={localConfig.host}
              onChange={e => handleHostChange(e.target.value)}
              placeholder={
                localConfig.provider === 'ollama'
                  ? 'http://localhost:11434'
                  : 'https://api.openai.com/v1'
              }
              className="transition-colors hover:border-ring focus:border-ring"
            />
          </FieldGroup>
        </div>

        {/* API Key Notice */}
        {localConfig.provider === 'openai' && !localConfig.host && (
          <div className="p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800/30 rounded-lg shadow-sm">
            <p className="text-sm text-yellow-800 dark:text-yellow-300">
              <strong>Note:</strong> You'll need to set the OPENAI_API_KEY environment variable for
              this provider to work.
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
            {localConfig.host && (
              <div className="flex justify-between">
                <span className="text-muted-foreground">
                  {localConfig.provider === 'ollama' ? 'Host:' : 'Base URL:'}
                </span>
                <span className="font-mono text-foreground">{localConfig.host}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </EditorPanel>
  );
}
